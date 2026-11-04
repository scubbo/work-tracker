"""Format elapsed time for observed PR review/readiness state changes."""

from datetime import datetime, timezone

import work_activity as activity


def state_age(record, now=None):
    if record.get('refresh_error'):
        return 'Unknown'
    changed = record.get('state_changed_at')
    anchor = changed or record.get('state_first_seen_at')
    if not anchor:
        return 'Unknown'
    try:
        seconds = int(((now or datetime.now(timezone.utc)) - activity.parse_time(anchor)).total_seconds())
    except (ValueError, TypeError, AttributeError, OverflowError):
        return 'Unknown'
    if seconds < 0:
        return 'Unknown'
    minutes = seconds // 60
    if minutes < 1:
        label = '<1m' if changed else '0m'
    elif minutes < 60:
        label = f'{minutes}m'
    elif minutes < 1440:
        label = f'{minutes // 60}h {minutes % 60}m'
    else:
        label = f'{minutes // 1440}d {minutes % 1440 // 60}h'
    return label if changed else '≥' + label
