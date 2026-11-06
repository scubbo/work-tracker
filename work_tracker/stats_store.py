"""Statistics inside the application's existing SQLite store, never a client DB."""

from datetime import datetime, timedelta, timezone
import json

import work_activity as activity
from .daily_stats import OVERALL, day_window, iso_day, metrics, project_label, timestamp


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


class DailyStatsStore:
    def __init__(self, storage):
        self.storage = storage

    def capture(self, now):
        window = day_window(now)
        now = now.astimezone(timezone.utc)
        captured = activity.iso(now)
        with self.storage._transaction() as db:
            days = {row['day']: json.loads(row['metadata']) for row in db.execute('SELECT * FROM stats_days')}
            current = days.get(window['day'])
            if days and (window['day'] < max(days) or current and now < timestamp(current['inventory_captured_at'])):
                return {'ok': True, 'skipped': 'Clock moved before the latest recorded snapshot'}
            if current and not timestamp(current['window_start']) <= now < timestamp(current['window_end']):
                return {'ok': True, 'skipped': 'Timezone changed outside the stored calendar window'}
            projects = [{'id': row['id'], 'name': project_label(row['name'])}
                        for row in db.execute('SELECT id,name FROM projects ORDER BY name')]
            members, changed = [], []
            for row in db.execute('SELECT prs.url,prs.github,prs.observed_at,prs.refresh_error,p.id AS project_id,p.name '
                                  'FROM prs JOIN tasks t ON t.id=prs.task_id JOIN workstreams w ON w.id=t.workstream_id '
                                  'JOIN projects p ON p.id=w.project_id'):
                github = json.loads(row['github'])
                state = str(github.get('state') or 'unknown').lower()
                if state == 'closed' and timestamp(github.get('mergedAt')):
                    state = 'merged'
                opened = timestamp(github.get('createdAt'))
                opened = opened if opened and opened <= now else None
                terminal = timestamp(github.get('mergedAt') if state == 'merged' else github.get('closedAt'))
                terminal = terminal if terminal and terminal <= now and state in ('closed', 'merged') else None
                member = {'url': row['url'], 'project_id': row['project_id'], 'state': state,
                          'opened_at': activity.iso(opened) if opened else None,
                          'terminal_at': activity.iso(terminal) if terminal else None,
                          'observed_at': row['observed_at'], 'refresh_error': bool(row['refresh_error'])}
                members.append(member)
                if terminal:
                    old = db.execute('SELECT * FROM stats_closures WHERE url=? AND closed_at=?',
                                     (row['url'], member['terminal_at'])).fetchone()
                    # Keep observed events through reopening. Promote a close to a
                    # merge only when the same event later gets a merge timestamp.
                    kind = 'merged' if state == 'merged' or old and old['kind'] == 'merged' else 'closed'
                    opened_at = member['opened_at'] or (old['opened_at'] if old else None)
                    if old is None or old['kind'] != kind or old['opened_at'] != opened_at:
                        db.execute('INSERT INTO stats_closures VALUES(?,?,?,?,?) ON CONFLICT(url,closed_at) DO UPDATE SET '
                                   'kind=excluded.kind,opened_at=excluded.opened_at,observed_at=excluded.observed_at',
                                   (row['url'], member['terminal_at'], kind, opened_at, captured))
                        changed.append(terminal)
            meta = days.get(window['day'])
            if meta is None:
                meta = {**window, 'first_captured_at': captured,
                        'first_day_partial': not days and now > timestamp(window['window_start'])}
            if not timestamp(meta['window_start']) <= now < timestamp(meta['window_end']):
                return {'ok': True, 'skipped': 'Timezone changed outside the stored calendar window'}
            meta.update(inventory_captured_at=captured, revised_at=captured, projects=projects, finalized=False)
            db.execute('INSERT INTO stats_days VALUES(?,?) ON CONFLICT(day) DO UPDATE SET metadata=excluded.metadata',
                       (meta['day'], encoded(meta)))
            db.execute('DELETE FROM stats_members WHERE day=?', (meta['day'],))
            db.executemany('INSERT INTO stats_members VALUES(?,?,?)',
                           [(meta['day'], member['url'], encoded(member)) for member in members])
            days[meta['day']] = meta
            events = [dict(row) for row in db.execute('SELECT * FROM stats_closures')]
            revised = 0
            for day, previous in days.items():
                if day > meta['day']:
                    continue
                start, end = timestamp(previous['window_start']), timestamp(previous['window_end'])
                if day != meta['day'] and previous.get('finalized') and not any(start <= value < end for value in changed):
                    continue
                previous.update(finalized=day != meta['day'], revised_at=captured)
                historical = members if day == meta['day'] else [json.loads(row[0]) for row in db.execute(
                    'SELECT facts FROM stats_members WHERE day=?', (day,))]
                values = metrics(previous, historical, events, now if day == meta['day'] else end)
                for value in values:
                    key = value['project_id'] or OVERALL
                    db.execute('INSERT INTO stats_metrics VALUES(?,?,?) ON CONFLICT(day,project_id) DO UPDATE SET payload=excluded.payload',
                               (day, key, encoded(value)))
                db.execute('UPDATE stats_days SET metadata=? WHERE day=?', (encoded(previous), day))
                revised += 1
        return {'ok': True, 'day': meta['day'], 'projects': len(projects), 'revised_days': revised}

    def history(self, params, now):
        if set(params) - {'since', 'until', 'days', 'project', 'limit', 'offset'}:
            raise ValueError('Unknown statistics filter')
        days = params.get('days', 30)
        limit, offset = params.get('limit', 1000), params.get('offset', 0)
        if type(days) is not int or not 1 <= days <= 3660:
            raise ValueError('Statistics days must be 1 to 3660')
        if type(limit) is not int or not 1 <= limit <= 5000 or type(offset) is not int or offset < 0:
            raise ValueError('Statistics limit must be 1 to 5000; offset must be nonnegative')
        end = iso_day(params['until']) if params.get('until') is not None else now.astimezone().date()
        start = iso_day(params['since']) if params.get('since') is not None else end-timedelta(days=days-1)
        if start > end or (end-start).days > 3659:
            raise ValueError('Statistics date range must span 1 to 3660 days')
        project = params.get('project')
        if project is not None and (not isinstance(project, str) or not project):
            raise ValueError('Statistics project must be a project name, ID, or *')
        with self.storage._connection(read=True) as db:
            saved = [json.loads(row[0]) for row in db.execute('SELECT payload FROM stats_metrics WHERE day BETWEEN ? AND ? '
                                                              "ORDER BY day DESC,CASE WHEN project_id='overall' THEN 0 ELSE 1 END,project_id", (start.isoformat(), end.isoformat()))]
            first = db.execute('SELECT min(day) FROM stats_days').fetchone()[0]
            job = db.execute("SELECT value FROM jobs WHERE name='daily_stats'").fetchone()
            job = json.loads(job[0]) if job else {}
            collection = {key: job.get(key) for key in ('status', 'last_error', 'last_finished', 'next_run')}
            recorded = {row[0] for row in db.execute('SELECT day FROM stats_days WHERE day BETWEEN ? AND ?',
                                                   (start.isoformat(), end.isoformat()))}
            projects = [{'id': row['id'], 'name': project_label(row['name'])}
                        for row in db.execute('SELECT id,name FROM projects ORDER BY name')]
        values = [value for value in saved if (project == '*' or project is None and value['project_id'] is None or
                   project is not None and project in (value['project_id'], value['project']))]
        missing = [(start+timedelta(days=index)).isoformat() for index in range((end-start).days+1)
                   if (start+timedelta(days=index)).isoformat() not in recorded]
        return {'since': start.isoformat(), 'until': end.isoformat(), 'first_recorded_day': first,
                'projects': projects, 'missing_days': missing, 'collection': collection, 'total': len(values),
                'items': values[offset:offset+limit], 'limit': limit, 'offset': offset,
                'closure_cohort': 'Closed or merged on that local calendar day', 'percentile': 'nearest-rank p90',
                'history_note': 'Inventory and project membership are last observed snapshots. Missing days are not reconstructed.'}
