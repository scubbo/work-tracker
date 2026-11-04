"""Explicit human-confirmed GitHub closing. Never an agent intent or scheduled retry."""

import work_activity as activity


class PRCloser:
    def __init__(self, storage, run, mutation_lock):
        self.storage, self.run, self.mutation_lock = storage, run, mutation_lock

    @staticmethod
    def receipt(value):
        return {key: value[key] for key in ('key', 'url', 'status', 'message', 'phase')}

    @staticmethod
    def endpoint(url):
        activity.pr_url(url)
        owner, repo, _, number = url.split('github.com/', 1)[1].split('/')
        return f'repos/{owner}/{repo}', number

    def read(self, url):
        repo, number = self.endpoint(url)
        value = self.run(['gh', 'api', '--method', 'GET', f'{repo}/pulls/{number}'])
        return self.validate(value, url)

    @staticmethod
    def validate(value, url):
        if (not isinstance(value, dict) or value.get('html_url', '').rstrip('/').casefold() != url.casefold()
                or value.get('state') not in ('open', 'closed')):
            raise activity.CollectionError('GitHub returned an invalid PR close response')
        return value

    def observe_closed(self, url, remote):
        with self.mutation_lock:
            current = next(p for p in self.storage.list_prs(state=None) if p['url'] == url)
            github = {**current['github'], 'state': 'merged' if remote.get('merged_at') else 'closed',
                      'closedAt': remote.get('closed_at'), 'mergedAt': remote.get('merged_at'),
                      'updatedAt': remote.get('updated_at')}
            self.storage.observe(url, github, current['review_state'], current['review_reason'])

    def close(self, params):
        if set(params) != {'key', 'url', 'comment', 'confirmed'} or params.get('confirmed') is not True:
            raise ValueError('Closing requires an explicit confirmation, PR URL, comment, and request key')
        value, created = self.storage.begin_close(params['key'], params['url'], params['comment'])
        if not created:
            return self.receipt(value)
        key, url, comment = params['key'], params['url'], params['comment']
        writes_started = False
        phase = 'preflight'
        try:
            remote = self.read(url)
            if remote['state'] == 'closed':
                self.observe_closed(url, remote)
                return self.receipt(self.storage.finish_close(
                    key, 'already_closed', 'PR is already merged or closed. No comment was posted.', phase))
            repo, number = self.endpoint(url)
            if comment.strip():
                phase = 'commenting'
                self.storage.finish_close(key, 'running', 'Posting the close comment on GitHub', phase)
                writes_started = True
                self.run(['gh', 'api', '--method', 'POST', f'{repo}/issues/{number}/comments', '--raw-field', 'body='+comment])
            phase = 'closing'
            self.storage.finish_close(key, 'running', 'Closing the PR on GitHub', phase)
            writes_started = True
            remote = self.validate(self.run(['gh', 'api', '--method', 'PATCH', f'{repo}/pulls/{number}',
                                             '--raw-field', 'state=closed']), url)
            if remote['state'] != 'closed':
                raise activity.CollectionError('GitHub did not confirm closure')
            self.observe_closed(url, remote)
            return self.receipt(self.storage.finish_close(
                key, 'succeeded', 'PR closed on GitHub. Branches and the linked Task were not changed.', phase))
        except Exception:
            status = 'uncertain' if writes_started else 'failed'
            message = ('GitHub outcome is uncertain. Check the PR before another close. This request will not repeat any write.'
                       if writes_started else 'GitHub preflight failed. No comment or close was sent. Check authentication and permissions.')
            return self.receipt(self.storage.finish_close(key, status, message, phase))

    def result(self, params):
        if set(params) - {'key', 'check_github'} or type(params.get('check_github', False)) is not bool:
            raise ValueError('Invalid close-result lookup')
        value = self.storage.close_request(params.get('key'))
        if params.get('check_github') and value['status'] == 'uncertain':
            try:
                remote = self.read(value['url'])
                if remote['state'] == 'closed':
                    self.observe_closed(value['url'], remote)
                    value = self.storage.finish_close(value['key'], 'succeeded',
                        'GitHub confirms the PR is closed. Check its comments if the earlier response was lost.', value['phase'])
            except Exception:
                pass
        return self.receipt(value)
