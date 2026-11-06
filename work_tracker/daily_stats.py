"""Daily-cohort metrics from observed facts, with explicit missing-data counts."""

from datetime import date, datetime, time, timedelta, timezone
import math

import work_activity as activity


OVERALL = 'overall'


def timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


def day_window(now):
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError('Statistics need an aware observation time')
    local = now.astimezone()
    day = local.date()
    start = datetime.combine(day, time.min).astimezone(timezone.utc)
    end = datetime.combine(day+timedelta(days=1), time.min).astimezone(timezone.utc)
    return {'day': day.isoformat(), 'window_start': activity.iso(start), 'window_end': activity.iso(end),
            'timezone': f'{local.tzname()}:{local.utcoffset()}'}


def iso_day(value):
    if not isinstance(value, str):
        raise ValueError('Dates must use YYYY-MM-DD')
    try:
        result = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError('Dates must use YYYY-MM-DD') from exc
    if result.isoformat() != value:
        raise ValueError('Dates must use YYYY-MM-DD')
    return result


def duration_text(seconds):
    if seconds is None:
        return '—'
    if seconds >= 86400:
        return f'{seconds/86400:.1f}d'
    if seconds >= 3600:
        return f'{seconds/3600:.1f}h'
    return f'{seconds/60:.1f}m'


def project_label(name):
    return 'Unallocated' if name == 'Unclassified' else name


def metrics(meta, members, events, as_of):
    """One PR can have several observed closure events after reopening."""
    start, end = timestamp(meta['window_start']), timestamp(meta['window_end'])
    cutoff = min(as_of, end)
    groups = {OVERALL: {'project_id': None, 'project': 'Overall'}}
    groups.update({project['id']: {'project_id': project['id'], 'project': project['name']} for project in meta['projects']})
    for group in groups.values():
        group.update(day=meta['day'], total_tracked=0, open_count=0, closed_count=0, merged_count=0,
                     unknown_state_count=0, missing_open_date_count=0, missing_close_date_count=0,
                     stale_pr_count=0, closure_sample_count=0, unknown_closure_time_count=0,
                     oldest_open_age_seconds=None, oldest_open_url=None, oldest_open_complete=True,
                     mean_closure_seconds=None, p90_closure_seconds=None, _durations=[])
    by_url = {member['url']: member for member in members}
    terminal = {}
    for event in events:
        closed = timestamp(event['closed_at'])
        if closed and closed <= cutoff and closed < end:
            terminal[event['url']] = max(terminal.get(event['url'], closed), closed)
    for member in members:
        targets = (groups[OVERALL], groups[member['project_id']])
        opened = timestamp(member.get('opened_at'))
        opened = opened if opened and opened <= cutoff else None
        observed = timestamp(member.get('observed_at'))
        closed = terminal.get(member['url'])
        state = member['state']
        # A later open observation is evidence of reopening. Do not erase the
        # recorded closure event, or mistake its older timestamp for current state.
        is_open = state == 'open' and not (closed and observed and closed > observed)
        if state in ('closed', 'merged') and timestamp(member.get('terminal_at')):
            is_open = timestamp(member['terminal_at']) > cutoff or timestamp(member['terminal_at']) >= end
        for group in targets:
            group['total_tracked'] += 1
            group['stale_pr_count'] += int(bool(member.get('refresh_error')) or observed is None)
            group['unknown_state_count'] += int(state not in ('open', 'closed', 'merged'))
            group['missing_close_date_count'] += int(state in ('closed', 'merged') and not timestamp(member.get('terminal_at')))
            if is_open:
                group['open_count'] += 1
                if opened is None:
                    group['missing_open_date_count'] += 1
                    group['oldest_open_complete'] = False
                else:
                    age = (cutoff-opened).total_seconds()
                    if group['oldest_open_age_seconds'] is None or age > group['oldest_open_age_seconds']:
                        group['oldest_open_age_seconds'], group['oldest_open_url'] = age, member['url']
    cohort = {}
    for event in events:
        closed = timestamp(event['closed_at'])
        if event['url'] not in by_url or not closed or not start <= closed < end or closed > cutoff:
            continue
        previous = cohort.get(event['url'])
        if previous is None or closed > timestamp(previous['closed_at']):
            cohort[event['url']] = event
    for event in cohort.values():
        closed = timestamp(event['closed_at'])
        member = by_url[event['url']]
        opened = timestamp(event.get('opened_at'))
        duration = (closed-opened).total_seconds() if opened and opened <= closed else None
        for group in (groups[OVERALL], groups[member['project_id']]):
            group['merged_count' if event['kind'] == 'merged' else 'closed_count'] += 1
            if duration is None:
                group['unknown_closure_time_count'] += 1
            else:
                group['_durations'].append(duration)
    for group in groups.values():
        durations = sorted(group.pop('_durations'))
        if group['unknown_state_count']:
            group['oldest_open_complete'] = False
        group['closure_counts_complete'] = not (group['stale_pr_count'] or group['missing_close_date_count'] or group['unknown_state_count'])
        group['closure_sample_count'] = len(durations)
        group['completed_count'] = group['closed_count']+group['merged_count']
        if durations:
            group['mean_closure_seconds'] = sum(durations)/len(durations)
            group['p90_closure_seconds'] = durations[math.ceil(.9*len(durations))-1]
        group.update(as_of=activity.iso(cutoff), inventory_captured_at=meta['inventory_captured_at'],
                     revised_at=meta['revised_at'], provisional=not meta['finalized'],
                     first_day_partial=meta['first_day_partial'], timezone=meta['timezone'],
                     window_start=meta['window_start'], window_end=meta['window_end'])
    return list(groups.values())
