"""Validated application-owned preferences, independent of any UI toolkit."""


PR_SORT_FIELDS = ('pr', 'review', 'title', 'state-age', 'opened')
DEFAULT_PR_SORT = {'field': 'pr', 'descending': False}


def validate_pr_sort(value):
    if (not isinstance(value, dict) or set(value) != {'field', 'descending'}
            or value.get('field') not in PR_SORT_FIELDS or type(value.get('descending')) is not bool):
        raise ValueError('PR sort requires a known field and a boolean descending value only')
    return {'field': value['field'], 'descending': value['descending']}
