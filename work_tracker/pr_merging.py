"""Human-confirmed, head-pinned merging. Never a scheduled job or agent intent."""

import re

import work_activity as activity
from .pr_closing import PRCloser


METHODS = {'merge': 'Merge commit', 'squash': 'Squash merge', 'rebase': 'Rebase merge'}


class MergeConditionError(ValueError):
    """A safe, application-authored reason that prevents a merge."""


class PRMerger:
    def __init__(self, storage, run, mutation_lock):
        self.storage, self.run, self.mutation_lock = storage, run, mutation_lock
        self.reader = PRCloser(storage, run, mutation_lock)

    def _registered(self, url):
        activity.pr_url(url)
        value = next((row for row in self.storage.list_prs(state=None) if row['url'] == url), None)
        if value is None:
            raise ValueError('Only an ingested PR can be merged')
        return value

    @staticmethod
    def _head(remote):
        sha = (remote.get('head') or {}).get('sha')
        if not isinstance(sha, str) or not re.fullmatch(r'(?:[0-9a-f]{40}|[0-9a-f]{64})', sha):
            raise MergeConditionError('GitHub did not return an exact head commit')
        return sha

    def _methods(self, url):
        repo, _ = self.reader.endpoint(url)
        value = self.run(['gh', 'api', '--method', 'GET', repo])
        if not isinstance(value, dict) or value.get('full_name', '').casefold() != repo.removeprefix('repos/').casefold():
            raise ValueError('GitHub returned a different repository')
        return [method for method, field in (('merge', 'allow_merge_commit'), ('squash', 'allow_squash_merge'),
                                             ('rebase', 'allow_rebase_merge')) if value.get(field) is True]

    @staticmethod
    def _open(remote):
        if remote['state'] != 'open':
            raise MergeConditionError('This PR is already merged or closed')
        if remote.get('draft') is not False:
            raise MergeConditionError('A draft PR or an unknown draft state cannot be merged')
        if remote.get('mergeable') is False:
            raise MergeConditionError('This PR has merge conflicts. Resolve them on GitHub first.')
        if remote.get('mergeable') is not True or remote.get('mergeable_state') != 'clean':
            raise MergeConditionError('GitHub does not report this PR ready for immediate merge. Open the PR to review checks, approvals, or queue requirements.')

    def preview(self, params):
        if set(params) != {'url'}:
            raise ValueError('Merge preview accepts one PR URL')
        self._registered(params['url'])
        try:
            remote = self.reader.read(params['url'])
            self._open(remote)
            methods = self._methods(params['url'])
            if not methods:
                raise ValueError('The repository did not offer an allowed merge method')
            return {'url': params['url'], 'head_sha': self._head(remote), 'methods': methods,
                    'title': remote.get('title') or params['url']}
        except activity.CollectionError as exc:
            raise ValueError('Could not load GitHub merge conditions. Check authentication and permissions.') from exc

    def _observe_merged(self, url, remote=None):
        with self.mutation_lock:
            current = self._registered(url)
            github = {**current['github'], 'state': 'merged'}
            if remote and remote.get('merged_at'):
                github.update(mergedAt=remote['merged_at'], closedAt=remote.get('closed_at'),
                              updatedAt=remote.get('updated_at'))
            self.storage.observe(url, github, current['review_state'], current['review_reason'])

    def merge(self, params):
        if set(params) != {'key', 'url', 'head_sha', 'method', 'confirmed'} or params.get('confirmed') is not True:
            raise ValueError('Merging requires explicit confirmation, a PR URL, head SHA, method, and request key')
        value, created = self.storage.begin_merge(params['key'], params['url'], params['head_sha'], params['method'])
        if not created:
            return self.reader.receipt(value)
        key, url = params['key'], params['url']
        phase, writes_started = 'preflight', False
        try:
            remote = self.reader.read(url)
            if remote.get('merged_at'):
                self._observe_merged(url, remote)
                return self.reader.receipt(self.storage.finish_merge(
                    key, 'already_merged', 'PR is already merged. No merge request was sent.', phase))
            self._open(remote)
            if self._head(remote) != params['head_sha']:
                raise MergeConditionError('The head commit changed. Reopen the dialog to review the new commit before merging.')
            if params['method'] not in self._methods(url):
                raise MergeConditionError('The repository no longer allows this merge method. Reopen the dialog.')
            repo, number = self.reader.endpoint(url)
            phase = 'merging'
            self.storage.finish_merge(key, 'running', 'Sending the confirmed merge request to GitHub', phase)
            writes_started = True
            result = self.run(['gh', 'api', '--method', 'PUT', f'{repo}/pulls/{number}/merge',
                               '--raw-field', 'sha='+params['head_sha'], '--raw-field', 'merge_method='+params['method']])
            if not isinstance(result, dict) or result.get('merged') is not True:
                if isinstance(result, dict) and result.get('merged') is False:
                    return self.reader.receipt(self.storage.finish_merge(key, 'failed',
                        'GitHub did not merge the PR. Check required checks, approvals, and repository rules.', phase))
                raise activity.CollectionError('GitHub did not confirm a merge')
            # A successful PUT is definitive even if the follow-up read is unavailable.
            try:
                remote = self.reader.read(url)
            except Exception:
                remote = None
            self._observe_merged(url, remote)
            return self.reader.receipt(self.storage.finish_merge(key, 'succeeded',
                'PR merged on GitHub. The tracker did not request branch deletion or close the linked Task.', phase))
        except Exception as exc:
            status = 'uncertain' if writes_started else 'failed'
            if writes_started:
                message = 'Merge outcome is uncertain. Check GitHub before another attempt. This request will not repeat any write.'
            elif isinstance(exc, MergeConditionError):
                message = str(exc)
            else:
                message = 'GitHub preflight failed. No merge was sent. Check authentication and permissions.'
            return self.reader.receipt(self.storage.finish_merge(key, status, message, phase))

    def result(self, params):
        if set(params) - {'key', 'check_github'} or type(params.get('check_github', False)) is not bool:
            raise ValueError('Invalid merge-result lookup')
        value = self.storage.merge_request(params.get('key'))
        if params.get('check_github') and value['status'] == 'uncertain':
            try:
                remote = self.reader.read(value['url'])
                if remote.get('merged_at'):
                    self._observe_merged(value['url'], remote)
                    if self._head(remote) == value['head_sha']:
                        value = self.storage.finish_merge(value['key'], 'succeeded',
                            'GitHub confirms that the PR with the confirmed head commit is merged.', value['phase'])
            except Exception:
                pass
        return self.reader.receipt(value)
