"""Private follow-up metadata and analysis receipts, inside the application store."""

from datetime import date
import json

import work_activity as activity
from .my_day import today
from .storage import encoded


FIELDS={'next_action','after_task','waiting_on','dependencies','revisit_after'}
DEFAULT={'next_action':'','after_task':'','waiting_on':'','dependencies':[],'revisit_after':None}


def plain(value,limit=2000):
    if not isinstance(value,str) or len(value)>limit or any(not char.isprintable() and char not in '\n\t' for char in value):
        raise ValueError('Follow-up text is invalid or too long')
    return value


class FollowupStore:
    def __init__(self,storage): self.storage=storage

    def snapshot(self,now):
        with self.storage._connection(read=True) as db:
            guidance={row['task_id']:{**json.loads(row['metadata']),'version':row['version'],'updated_at':row['updated_at']}
                      for row in db.execute('SELECT * FROM task_followup')}
            tasks=[]
            for row in db.execute('SELECT t.*,p.name AS project,w.name AS workstream FROM tasks t JOIN workstreams w ON w.id=t.workstream_id JOIN projects p ON p.id=w.project_id ORDER BY t.id'):
                value=dict(row);value['closed']=bool(value['closed'])
                value['guidance']={**DEFAULT,**guidance.get(value['id'],{}),'version':guidance.get(value['id'],{}).get('version',0)}
                value['goals']=[dict(item) for item in db.execute("SELECT day,label,notes,done,updated_at FROM my_day_items WHERE target_kind='task' AND target_id=? AND kind='goal' AND removed=0 ORDER BY day DESC LIMIT 5",(value['id'],))]
                tasks.append(value)
            prs=[]
            for row in db.execute('SELECT * FROM prs ORDER BY url'):
                value=dict(row);value['github']=json.loads(value['github']);value['metadata']=json.loads(value['metadata'])
                value['threads']=[json.loads(item[0]) for item in db.execute("SELECT payload FROM links WHERE owner_kind='pr' AND owner_key=? AND kind='slack' ORDER BY identity",(value['url'],))]
                value['pending_action']=any(db.execute(f"SELECT 1 FROM {table} WHERE url=? AND status IN ('running','uncertain')",(value['url'],)).fetchone() for table in ('pr_close_requests','pr_merge_requests'))
                prs.append(value)
        return {'today':today(now),'tasks':tasks,'prs':prs}

    def guidance(self,params,now):
        if set(params)-({'task_id','expected_version'}|FIELDS) or not isinstance(params.get('task_id'),str):
            raise ValueError('Task follow-up requires a known task_id and supported fields')
        with self.storage._transaction() as db:
            if not db.execute('SELECT 1 FROM tasks WHERE id=?',(params['task_id'],)).fetchone():
                raise ValueError('Task not found')
            old=db.execute('SELECT * FROM task_followup WHERE task_id=?',(params['task_id'],)).fetchone()
            version=old['version'] if old else 0
            value={**DEFAULT,**(json.loads(old['metadata']) if old else {})}
            changing=set(params)&FIELDS
            if changing:
                if type(params.get('expected_version')) is not int:
                    raise ValueError('Task follow-up requires an expected version')
                if params['expected_version']!=version:
                    if all(params[field]==value[field] for field in changing):
                        return {'task_id':params['task_id'],'version':version,**value,'duplicate':True}
                    raise ValueError('Task follow-up changed. Reload before editing.')
                for field in changing:
                    incoming=params[field]
                    if field=='dependencies':
                        if not isinstance(incoming,list) or len(incoming)>50 or any(not isinstance(item,str) for item in incoming):
                            raise ValueError('Dependencies must be at most 50 known Task IDs')
                        incoming=sorted(set(incoming))
                        for item in incoming:
                            if not db.execute('SELECT 1 FROM tasks WHERE id=?',(item,)).fetchone():raise ValueError('Unknown dependency Task')
                    elif field=='revisit_after':
                        if incoming is not None:
                            if not isinstance(incoming,str) or date.fromisoformat(incoming).isoformat()!=incoming:raise ValueError('Use YYYY-MM-DD or null for revisit_after')
                    else: incoming=plain(incoming)
                    value[field]=incoming
                graph={row['task_id']:json.loads(row['metadata']).get('dependencies',[]) for row in db.execute('SELECT * FROM task_followup')}
                graph[params['task_id']]=value['dependencies']
                visited=set()
                def visits(node,path):
                    if node in path:raise ValueError('Dependencies cannot contain a cycle')
                    if node in visited:return
                    for child in graph.get(node,[]):visits(child,path|{node})
                    visited.add(node)
                visits(params['task_id'],set())
                version+=1
                db.execute('INSERT INTO task_followup VALUES(?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET version=excluded.version,metadata=excluded.metadata,updated_at=excluded.updated_at',
                           (params['task_id'],version,encoded(value),activity.iso(now)))
            elif 'expected_version' in params:raise ValueError('No follow-up changes supplied')
        return {'task_id':params['task_id'],'version':version,**value}

    def runs(self,params):
        if set(params)-{'key','limit'}:raise ValueError('Unknown analysis history filter')
        with self.storage._connection(read=True) as db:
            if 'key' in params:
                row=db.execute('SELECT * FROM followup_runs WHERE key=?',(params['key'],)).fetchone()
                return {**dict(row),'result':json.loads(row['result'])} if row else None
            limit=params.get('limit',10)
            if type(limit) is not int or not 1<=limit<=50:raise ValueError('History limit must be 1 to 50')
            return [{**dict(row),'result':json.loads(row['result'])} for row in db.execute('SELECT * FROM followup_runs ORDER BY created_at DESC,rowid DESC LIMIT ?',(limit,))]

    def save_run(self,key,status,result,now,create=False):
        if not isinstance(key,str) or not key or len(key)>128:raise ValueError('Analysis requires a stable key of 1 to 128 characters')
        with self.storage._transaction() as db:
            old=db.execute('SELECT * FROM followup_runs WHERE key=?',(key,)).fetchone()
            if old and create:return False
            if create:db.execute('INSERT INTO followup_runs VALUES(?,?,?,?,?)',(key,status,activity.iso(now),activity.iso(now),encoded(result)))
            elif old:db.execute('UPDATE followup_runs SET status=?,updated_at=?,result=? WHERE key=?',(status,activity.iso(now),encoded(result),key))
            else:raise ValueError('Analysis not found')
        return True

    def recover(self,now):
        with self.storage._transaction() as db:
            for row in db.execute("SELECT * FROM followup_runs WHERE status IN ('queued','running')").fetchall():
                result={**json.loads(row['result']),'warning':'Analysis interrupted. It will not be replayed or charged again automatically.'}
                db.execute("UPDATE followup_runs SET status='interrupted',updated_at=?,result=? WHERE key=?",(activity.iso(now),encoded(result),row['key']))
