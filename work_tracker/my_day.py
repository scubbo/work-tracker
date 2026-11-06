"""Local daily planning behind the application's storage boundary."""

from datetime import datetime, timezone
import json
import uuid

import work_activity as activity
from .daily_stats import iso_day


UNCLASSIFIED = 'Unclassified'


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def text(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 2000 or any(not char.isprintable() and char not in '\n\t' for char in value):
        raise ValueError('Enter 1 to 2000 characters without terminal control characters')
    return value.strip()


def today(now):
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError('My Day requires an aware application clock')
    return now.astimezone().date().isoformat()


class MyDayStore:
    def __init__(self, storage):
        self.storage = storage

    @staticmethod
    def _target(db, kind, identifier):
        if kind == 'pr':
            url = activity.pr_url(identifier)
            row = db.execute('SELECT task_id FROM prs WHERE url=?', (url,)).fetchone()
            if row is None:
                raise ValueError('Only a tracked PR can be added to My Day')
            kind, identifier = 'task', row[0]
        if kind == 'task':
            row = db.execute('SELECT t.id,t.title,t.closed,w.id AS workstream_id,w.name AS workstream_name,'
                             'p.id AS project_id,p.name AS project_name FROM tasks t JOIN workstreams w ON w.id=t.workstream_id '
                             'JOIN projects p ON p.id=w.project_id WHERE t.id=?', (identifier,)).fetchone()
        elif kind == 'workstream':
            row = db.execute('SELECT w.id,w.name AS title,w.closed,w.id AS workstream_id,w.name AS workstream_name,'
                             'p.id AS project_id,p.name AS project_name FROM workstreams w '
                             'JOIN projects p ON p.id=w.project_id WHERE w.id=?', (identifier,)).fetchone()
        else:
            raise ValueError('Choose a Task, workstream, or tracked PR')
        if row is None:
            raise ValueError('The selected Task or workstream no longer exists')
        result = {**dict(row), 'kind': kind}
        query = ('SELECT prs.url,prs.github FROM prs WHERE task_id=?' if kind == 'task' else
                 'SELECT prs.url,prs.github FROM prs JOIN tasks t ON t.id=prs.task_id WHERE t.workstream_id=?')
        prs = db.execute(query+' ORDER BY prs.url', (identifier,)).fetchall()
        result['pr_count'] = len(prs)
        result['prs'] = [{'url': value['url'], 'title': json.loads(value['github']).get('title') or value['url'],
                          'state': json.loads(value['github']).get('state', 'unknown')} for value in prs[:50]]
        return result

    def _item(self, db, row):
        value = dict(row)
        value['done'], value['removed'] = bool(value['done']), bool(value['removed'])
        value['target'] = self._target(db, value['target_kind'], value['target_id'])
        return value

    def read(self, params, now):
        if set(params)-{'day'}:
            raise ValueError('My Day accepts a calendar day only')
        current = today(now)
        selected = iso_day(params['day']).isoformat() if params.get('day') is not None else current
        with self.storage._connection(read=True) as db:
            goals = [self._item(db, row) for row in db.execute(
                "SELECT * FROM my_day_items WHERE kind='goal' AND day=? AND removed=0 ORDER BY position,id", (selected,))]
            awareness = [self._item(db, row) for row in db.execute(
                "SELECT * FROM my_day_items WHERE kind='awareness' AND day<=? AND removed=0 "
                'AND (dismissed_day IS NULL OR dismissed_day>?) ORDER BY position,id', (selected,selected))]
            candidates = []
            if selected == current:
                candidates = [self._item(db, row) for row in db.execute(
                    "SELECT g.* FROM my_day_items g WHERE g.kind='goal' AND g.day<? AND g.done=0 AND g.removed=0 "
                    "AND NOT EXISTS(SELECT 1 FROM my_day_items newer WHERE newer.kind='goal' "
                    'AND newer.target_kind=g.target_kind AND newer.target_id=g.target_id '
                    'AND newer.day>g.day AND newer.day<=?) ORDER BY g.day DESC,g.position,g.id LIMIT 500', (current,current))]
        return {'day': selected, 'today': current, 'editable': selected == current,
                'goals': goals, 'awareness': awareness, 'carry_candidates': candidates,
                'open_count': sum(not value['done'] for value in goals), 'done_count': sum(value['done'] for value in goals),
                'note': 'Completion changes only this day item. Awareness persists until dismissed. Carry-forward is explicit.'}

    def targets(self, params):
        if set(params)-{'search','kind','limit','offset'}:
            raise ValueError('Unknown My Day target filter')
        query, kind = params.get('search',''), params.get('kind')
        limit, offset = params.get('limit',50), params.get('offset',0)
        if not isinstance(query,str) or len(query)>200 or kind not in (None,'task','workstream'):
            raise ValueError('Invalid target search or kind')
        if type(limit) is not int or not 1<=limit<=200 or type(offset) is not int or offset<0:
            raise ValueError('Target limit must be 1 to 200; offset must be nonnegative')
        with self.storage._connection(read=True) as db:
            candidates = []
            for target_kind, table in (('task','tasks'),('workstream','workstreams')):
                if kind and target_kind != kind:
                    continue
                for row in db.execute(f'SELECT id FROM {table} WHERE closed=0 ORDER BY id'):
                    target = self._target(db,target_kind,row['id'])
                    search = ' '.join([target['title'],target['project_name'],target['workstream_name'],
                                       *[pr['url'] for pr in target['prs']]])
                    if not query or query.casefold() in search.casefold():
                        candidates.append(target)
            candidates.sort(key=lambda value:(value['title'].casefold(),value['kind'],value['id']))
        return {'total':len(candidates),'items':candidates[offset:offset+limit],'limit':limit,'offset':offset}

    def _add(self, db, day, kind, target_kind, target_id, label, stamp, carried_from=None):
        target = self._target(db,target_kind,target_id)
        if kind == 'goal':
            old = db.execute("SELECT * FROM my_day_items WHERE kind='goal' AND day=? AND target_kind=? AND target_id=?",
                             (day,target['kind'],target['id'])).fetchone()
        else:
            old = db.execute("SELECT * FROM my_day_items WHERE kind='awareness' AND dismissed_day IS NULL AND removed=0 "
                             'AND target_kind=? AND target_id=?', (target['kind'],target['id'])).fetchone()
        if old is not None:
            if old['removed']:
                db.execute('UPDATE my_day_items SET removed=0,version=version+1,updated_at=? WHERE id=?', (stamp,old['id']))
            return old['id'], 'The item is already linked to this plan.'
        count = db.execute('SELECT count(*) FROM my_day_items WHERE kind=? AND removed=0 AND '+
                           ("day=?" if kind=='goal' else 'dismissed_day IS NULL'),
                           (kind,day) if kind=='goal' else (kind,)).fetchone()[0]
        if count>=500:
            raise ValueError('This view already has 500 active items')
        position = db.execute('SELECT coalesce(max(position),-1)+1 FROM my_day_items WHERE kind=?', (kind,)).fetchone()[0]
        identifier = uuid.uuid4().hex
        db.execute('INSERT INTO my_day_items(id,kind,day,target_kind,target_id,label,done,removed,dismissed_day,position,version,created_at,updated_at,carried_from) '
                   'VALUES(?,?,?,?,?,?,0,0,NULL,?,0,?,?,?)',
                   (identifier,kind,day,target['kind'],target['id'],label or target['title'],position,stamp,stamp,carried_from))
        return identifier, 'Added to My Day.'

    def apply(self, params, now):
        if set(params) != {'key','action','payload','source'}:
            raise ValueError('My Day edits require a key, action, payload, and source')
        key, action, payload, source = (params[name] for name in ('key','action','payload','source'))
        if not isinstance(key,str) or not key or len(key)>200 or not isinstance(payload,dict) or not isinstance(source,dict):
            raise ValueError('Invalid My Day request key, payload, or source')
        allowed = {'add':{'day','kind','text','target_kind','target_id','url'},
                   'set_done':{'day','item_id','expected_version','done'},
                   'edit':{'day','item_id','expected_version','text'},
                   'remove':{'day','item_id','expected_version'},
                   'dismiss':{'day','item_id','expected_version'},
                   'carry':{'day','item_id','expected_version'}}
        if not isinstance(action,str) or action not in allowed or set(payload)-allowed[action]:
            raise ValueError('Unsupported My Day action or fields')
        current, stamp = today(now), activity.iso(now.astimezone(timezone.utc))
        with self.storage._transaction() as db:
            old = db.execute('SELECT * FROM my_day_intents WHERE key=?', (key,)).fetchone()
            if old:
                if (old['action'],old['payload'],old['source']) != (action,encoded(payload),encoded(source)):
                    raise ValueError('My Day request key was already used for different content')
                return {**json.loads(old['receipt']), 'duplicate': True}
            if payload.get('day') != current:
                raise ValueError('The calendar day changed or is read-only. Reload My Day before editing.')
            if action == 'add':
                kind, target_kind = payload.get('kind'), payload.get('target_kind','new')
                if kind not in ('goal','awareness'):
                    raise ValueError('Choose a daily goal or persistent awareness item')
                label = text(payload['text']) if payload.get('text') is not None else None
                if target_kind == 'new':
                    if label is None or set(payload) & {'target_id','url'}:
                        raise ValueError('A quick-add item needs text, not an existing target')
                    target_id = uuid.uuid4().hex
                    workstream = self.storage._workstream(db,UNCLASSIFIED,UNCLASSIFIED)
                    db.execute('INSERT INTO tasks(id,workstream_id,title) VALUES(?,?,?)', (target_id,workstream,label))
                    target_kind = 'task'
                else:
                    if target_kind=='pr':
                        if not isinstance(payload.get('url'),str) or 'target_id' in payload:
                            raise ValueError('Select one tracked PR URL')
                        target_id = payload['url']
                    else:
                        if not isinstance(payload.get('target_id'),str) or 'url' in payload:
                            raise ValueError('Select one Task or workstream ID')
                        target_id = payload['target_id']
                identifier, message = self._add(db,current,kind,target_kind,target_id,label,stamp)
            else:
                row = db.execute('SELECT * FROM my_day_items WHERE id=?', (payload.get('item_id'),)).fetchone()
                if row is None or row['removed']:
                    raise ValueError('The day item is no longer available')
                version = payload.get('expected_version')
                if type(version) is not int or version != row['version']:
                    raise ValueError('This item changed. Reload it before editing.')
                if action=='carry':
                    if row['kind']!='goal' or row['day']>=current or row['done']:
                        raise ValueError('Only an unfinished goal from a prior day can be carried forward')
                    identifier,message = self._add(db,current,'goal',row['target_kind'],row['target_id'],row['label'],stamp,row['id'])
                else:
                    if row['kind']=='goal' and row['day']!=current or row['kind']=='awareness' and (row['day']>current or row['dismissed_day']):
                        raise ValueError('This item is historical or dismissed. Reload My Day.')
                    identifier = row['id']
                    if action=='set_done':
                        if row['kind']!='goal' or type(payload.get('done')) is not bool:
                            raise ValueError('Only daily goals have an Open/Done state')
                        db.execute('UPDATE my_day_items SET done=?,version=version+1,updated_at=? WHERE id=?',
                                   (int(payload['done']),stamp,identifier))
                        message = 'Daily goal updated. Its Task and PRs were not changed.'
                    elif action=='edit':
                        label = text(payload.get('text'))
                        db.execute('UPDATE my_day_items SET label=?,version=version+1,updated_at=? WHERE id=?', (label,stamp,identifier))
                        message = 'Day label updated. The underlying Task or workstream was not renamed.'
                    elif action=='remove':
                        if row['kind']!='goal':
                            raise ValueError('Use dismiss for a persistent awareness item')
                        db.execute('UPDATE my_day_items SET removed=1,version=version+1,updated_at=? WHERE id=?', (stamp,identifier))
                        message = 'Removed from today. The underlying Task was not deleted.'
                    elif action=='dismiss':
                        if row['kind']!='awareness':
                            raise ValueError('Only awareness items can be dismissed')
                        db.execute('UPDATE my_day_items SET dismissed_day=?,version=version+1,updated_at=? WHERE id=?', (current,stamp,identifier))
                        message = 'Awareness item dismissed. The underlying Task was not changed.'
            receipt = {'key':key,'item_id':identifier,'day':current,'duplicate':False,'message':message}
            db.execute('INSERT INTO my_day_intents VALUES(?,?,?,?,?,?)', (key,action,encoded(payload),encoded(source),encoded(receipt),stamp))
        return receipt
