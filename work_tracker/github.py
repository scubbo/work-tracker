"""Read-only GitHub discovery and conservative human-review classification."""

from urllib.parse import urlencode

import work_activity as activity


DETAIL_FIELDS = "url,title,body,author,state,isDraft,mergeable,mergeStateStatus,reviewDecision,statusCheckRollup,createdAt,updatedAt,closedAt,mergedAt"


def review_status(detail, reviews, automated_reviewers=()):
    ignored = {name.casefold() for name in automated_reviewers}
    opinions = {}
    for review in sorted(reviews, key=lambda item: (item.get("submitted_at") or "", item.get("id", 0))):
        user = review.get("user") or {}
        login = user.get("login", "")
        if user.get("type") != "User" or login.casefold() in ignored or login.endswith("[bot]"):
            continue
        state = review.get("state")
        if state in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
            opinions[login.casefold()] = state
    if "CHANGES_REQUESTED" in opinions.values():
        return "Changes requested", "A human reviewer requested changes"
    if "APPROVED" not in opinions.values():
        return "Awaiting review", "No current human approval; automated feedback does not count"
    if detail.get("isDraft"):
        return "Blocked", "PR is a draft"
    if detail.get("reviewDecision") not in (None, "", "APPROVED"):
        return "Blocked", "GitHub review requirements are not satisfied (possibly CODEOWNERS)"
    if detail.get("mergeable") != "MERGEABLE" or detail.get("mergeStateStatus") != "CLEAN":
        return "Blocked", f"GitHub has not confirmed clean merge readiness: {detail.get('mergeStateStatus') or 'unknown'}"
    for check in detail.get("statusCheckRollup") or []:
        if "conclusion" in check:
            good = check.get("status") == "COMPLETED" and check["conclusion"] in ("SUCCESS", "NEUTRAL", "SKIPPED")
        else:
            good = check.get("state") == "SUCCESS"
        if not good:
            return "Blocked", "A check is failing, pending, or unknown"
    return "Ready to Merge", "Human approval and GitHub merge readiness confirmed"


def open_inventory(run=activity.run_json):
    """Read an account-wide count/link, never enroll or fetch those PR details."""
    login = run(["gh", "api", "user"])["login"]
    query = f"is:pr is:open author:{login}"
    result = run(["gh", "api", "--method", "GET", "search/issues", "-f", f"q={query}", "-f", "per_page=1"])
    if result.get("incomplete_results") or type(result.get("total_count")) is not int:
        raise activity.CollectionError("GitHub returned an incomplete open-PR count")
    return {"author": login, "total_open": result["total_count"],
            "github_url": "https://github.com/pulls?" + urlencode({"q": query})}


def fetch_pr(url, run=activity.run_json):
    url = activity.pr_url(url)
    detail = run(["gh", "pr", "view", url, "--json", DETAIL_FIELDS])
    if detail.get("url", "").rstrip("/") != url:
        raise activity.CollectionError("GitHub returned a different PR")
    owner, repo, _, number = url.split("github.com/", 1)[1].split("/")
    pages = run(["gh", "api", "--paginate", "--slurp", f"repos/{owner}/{repo}/pulls/{number}/reviews?per_page=100"])
    if not isinstance(pages, list) or any(not isinstance(page, list) for page in pages):
        raise activity.CollectionError("GitHub review pagination returned an unexpected response")
    reviews = [item for page in pages for item in page]
    github = dict(detail)
    github["state"] = detail["state"].lower()
    github["author"] = (detail.get("author") or {}).get("login")
    return github, reviews


def fetch_batch(urls, run=activity.run_json, review_urls=None):
    """Read at most 20 already-enrolled PRs in one non-mutating GraphQL query."""
    if not urls or len(urls) > 20:
        raise ValueError("A PR batch must contain 1 to 20 tracked URLs")
    definitions, selections, variables = [], [], []
    base = ("url title body state isDraft mergeable mergeStateStatus reviewDecision "
            "createdAt updatedAt closedAt mergedAt author{login} "
            "commits(last:1){nodes{commit{statusCheckRollup{state}}}}")
    opinions = (" latestOpinionatedReviews(first:100,writersOnly:false){"
                "nodes{id state submittedAt author{__typename login}} pageInfo{hasNextPage}}")
    for index, url in enumerate(urls):
        activity.pr_url(url)
        owner, repo, _, number = url.split('github.com/', 1)[1].split('/')
        definitions.extend((f'$o{index}:String!', f'$r{index}:String!', f'$n{index}:Int!'))
        fields = base + (opinions if review_urls is None or url in review_urls else '')
        selection = f'p{index}:repository(owner:$o{index},name:$r{index})' + '{pullRequest(number:$n' + str(index) + '){' + fields + '}}'
        selections.append(selection)
        variables.extend(('-f', f'o{index}={owner}', '-f', f'r{index}={repo}', '-F', f'n{index}={number}'))
    query = 'query WorkTrackerPRs(' + ','.join(definitions) + '){' + ' '.join(selections) + '}'
    response = run(['gh', 'api', 'graphql', '-f', 'query=' + query, *variables])
    if response.get('errors') or not isinstance(response.get('data'), dict):
        raise activity.CollectionError('GitHub returned an incomplete PR batch')
    result = {}
    for index, url in enumerate(urls):
        repository = response['data'].get(f'p{index}') or {}
        detail = repository.get('pullRequest')
        if not detail or detail.get('url', '').rstrip('/').casefold() != url.casefold():
            result[url] = None
            continue
        connection = detail.get('latestOpinionatedReviews')
        if detail['state'] == 'OPEN' and (connection is None or connection['pageInfo']['hasNextPage']):
            try:
                result[url] = fetch_pr(url, run)
            except (activity.CollectionError, ValueError, KeyError, TypeError):
                result[url] = None
            continue
        commits = (detail.get('commits') or {}).get('nodes') or []
        if not commits:
            result[url] = None
            continue
        rollup = commits[-1]['commit'].get('statusCheckRollup')
        github = {key: value for key, value in detail.items() if key not in ('commits', 'latestOpinionatedReviews')}
        github['state'] = detail['state'].lower()
        github['author'] = (detail.get('author') or {}).get('login')
        github['statusCheckRollup'] = [{'state': rollup['state']}] if rollup else []
        reviews = []
        for review in (connection or {}).get('nodes', []):
            actor = review.get('author') or {}
            reviews.append({'id': review['id'], 'state': review['state'], 'submitted_at': review.get('submittedAt'),
                            'user': {'type': actor.get('__typename'), 'login': actor.get('login', '')}})
        result[url] = github, reviews
    return result
