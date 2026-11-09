"""Evidence-bound advice, never execution. Cloud analysis is explicit and bounded."""

from copy import deepcopy
from datetime import date,datetime,timezone
import hashlib
import json
import re
import threading
from urllib.parse import urlsplit,parse_qs

import work_activity as activity
from .gateway import Gateway


def stamp(value):
    try:
        parsed=activity.parse_time(value)
        return parsed if parsed.tzinfo else None
    except (TypeError,AttributeError,ValueError):return None


def clipped(value,limit=500):
    return ''.join(char if char.isprintable() or char in '\n\t' else '?' for char in str(value or ''))[:limit]


def filters(params):
    if set(params)-{'project','workstream','stale_days'}:raise ValueError('Unknown follow-up filter')
    days=params.get('stale_days',7)
    if type(days) is not int or not 1<=days<=365:raise ValueError('Stale threshold must be 1 to 365 days')
    for field in ('project','workstream'):
        if params.get(field) is not None and (not isinstance(params[field],str) or len(params[field])>200):raise ValueError('Invalid follow-up scope')
    return {'project':params.get('project') or None,'workstream':params.get('workstream') or None,'stale_days':days}


def build_report(snapshot,now,scope):
    tasks={item['id']:item for item in snapshot['tasks']}
    candidates=[];snoozed=0;unknown=0
    projects=sorted({item['project'] for item in tasks.values()})
    workstreams=sorted({item['workstream'] for item in tasks.values() if not scope['project'] or item['project']==scope['project']})
    open_pr_tasks={item['task_id'] for item in snapshot['prs'] if item['github'].get('state','open').lower()=='open' or item['pending_action']}
    all_pr_tasks={item['task_id'] for item in snapshot['prs']}
    def include(task):return (not scope['project'] or task['project']==scope['project']) and (not scope['workstream'] or task['workstream']==scope['workstream'])
    def make(task,pr=None):
        nonlocal snoozed,unknown
        guidance=task['guidance']
        if guidance['revisit_after'] and guidance['revisit_after']>snapshot['today']:
            snoozed+=1;return
        identity=pr['url'] if pr else task['id']
        identifier=('pr-' if pr else 'task-')+hashlib.sha256(identity.encode()).hexdigest()[:16]
        deps=[tasks[identifier] for identifier in guidance['dependencies'] if identifier in tasks and not tasks[identifier]['closed']]
        today_goal=any(item['day']==snapshot['today'] and not item['done'] for item in task['goals'])
        signals=[];evidence=[]
        if pr:
            github=pr['github'];observed=stamp(pr.get('observed_at'))
            fresh=bool(observed and 0<=(now-observed).total_seconds()<=900 and not pr.get('refresh_error'))
            if pr['pending_action']:
                action='Check the pending or uncertain GitHub operation before another action';kind='check_outcome'
            elif deps:
                action='Resolve or clarify dependency: '+', '.join(clipped(item['title'],150) for item in deps);kind='dependency'
            elif guidance['next_action']:
                action=guidance['next_action'];kind='manual'
            elif not fresh:
                action='Refresh this PR before deciding what to do next';kind='refresh'
            elif github.get('isDraft') is True:
                action='Clarify the remaining work on this draft PR';kind='draft'
            elif pr['review_state']=='Ready to Merge' and github.get('mergeStateStatus')=='CLEAN' and github.get('mergeable') in ('MERGEABLE',True) and github.get('isDraft') is False:
                action='Open merge confirmation for this PR';kind='merge'
            elif pr['review_state']=='Changes requested':
                action='Address the human review feedback';kind='changes'
            elif guidance['waiting_on']:
                action='Follow up with '+guidance['waiting_on']+' about this PR';kind='follow_up'
            elif pr['review_state']=='Awaiting review':
                action='Check the review discussion and choose the next review follow-up';kind='review'
            else:
                action='Inspect the blocking check or review requirement';kind='blocked'
            evidence.append('Cached GitHub state: '+pr['review_state']+'. '+clipped(pr['reason']))
            age_at=stamp(pr.get('state_changed_at')) or stamp(pr.get('state_first_seen_at'))
            if age_at and 0<=(now-age_at).total_seconds():
                age_days=int((now-age_at).total_seconds()/86400)
                if age_days>=scope['stale_days']:
                    signals.append(f'No observed review-state change for at least {age_days} days; this does not prove human inactivity')
            else:unknown+=1
            opened=stamp(github.get('createdAt'))
            if opened and opened<=now:
                open_days=int((now-opened).total_seconds()/86400)
                if open_days>=max(30,scope['stale_days']):
                    signals.append(f'Opened {open_days} days ago; age alone is not proof of neglect')
            if not fresh:evidence.append('The GitHub observation is missing, stale, or failed. Refresh before acting on readiness.')
            title=github.get('title') or task['title']
        else:
            if deps:action='Resolve or clarify dependency: '+', '.join(clipped(item['title'],150) for item in deps);kind='dependency'
            elif task['closed']:
                action='Plan follow-on work: '+guidance['after_task'];kind='follow_on'
            elif guidance['next_action']:action=guidance['next_action'];kind='manual'
            elif guidance['after_task'] and task['id'] in all_pr_tasks:
                action='Confirm whether this Task is complete, then plan follow-on work: '+guidance['after_task'];kind='clarify_completion'
            elif guidance['waiting_on']:action='Discuss the next step with '+guidance['waiting_on'];kind='follow_up'
            else:action='Clarify the next concrete step for this Task';kind='clarify_task'
            evidence.append('Task completion is separate from PR merge and daily-goal completion.')
            touches=[stamp(item['updated_at']) for item in task['goals']]+[stamp(guidance.get('updated_at'))]
            touches=[item for item in touches if item and item<=now]
            if touches:
                age_days=int((now-max(touches)).total_seconds()/86400)
                if age_days>=scope['stale_days']:signals.append(f'No recorded planning or follow-up edit for {age_days} days; work outside the tracker is unknown')
            else:unknown+=1
            title=task['title']
        if guidance['waiting_on']:evidence.append('Explicit waiting-on metadata: '+clipped(guidance['waiting_on']))
        if guidance['next_action']:evidence.append('Explicit next-action metadata: '+clipped(guidance['next_action']))
        candidates.append({'id':identifier,'task_id':task['id'],'url':pr['url'] if pr else None,'title':clipped(title,300),
          'project':task['project'],'workstream':task['workstream'],'action':clipped(action,2000),'kind':kind,'today':today_goal,
          'signals':signals,'evidence':evidence,'observed_at':pr.get('observed_at') if pr else None,
          'context_available':{'task':True,'github':pr is not None,'slack':bool(pr and pr['threads'])}})
    for pr in snapshot['prs']:
        task=tasks.get(pr['task_id'])
        if task and include(task) and (pr['github'].get('state','open').lower()=='open' or pr['pending_action']):make(task,pr)
    for task in tasks.values():
        if not include(task) or task['id'] in open_pr_tasks:continue
        explicit=bool(task['guidance'].get('updated_at') or any(not item['done'] for item in task['goals']))
        if (not task['closed'] and (task['id'] not in all_pr_tasks or explicit)) or (task['closed'] and task['guidance']['after_task']):make(task)
    candidates.sort(key=lambda item:(item['project'].casefold(),item['workstream'].casefold(),item['id']))
    mode='recommend' if len(candidates)==1 else ('clarify' if candidates else 'empty')
    next_action={'mode':mode,'candidate_ids':[item['id'] for item in candidates[:3]],
                 'reason':'Only one actionable candidate is in this scope.' if len(candidates)==1 else 'Personal priorities are not defined. Alphabetical display order is not a priority ranking.',
                 'question':'Which project, workstream, or outcome matters most right now?' if len(candidates)>1 else ''}
    fingerprint_data=deepcopy(snapshot)
    for pr in fingerprint_data['prs']:
        for field in ('observed_at','observation_version'):pr.pop(field,None)
    fingerprint=hashlib.sha256(json.dumps({'snapshot':fingerprint_data,'scope':scope,'actions':[(item['id'],item['action']) for item in candidates]},sort_keys=True).encode()).hexdigest()
    return {'today':snapshot['today'],'generated_at':activity.iso(now),'fingerprint':fingerprint,'scope':scope,'projects':projects,'workstreams':workstreams,
            'candidates':candidates,'stale':[item for item in candidates if item['signals']],'next':next_action,'snoozed_count':snoozed,
            'unknown_age_count':unknown,'ordering':'Alphabetical, not a learned priority policy','stale_rule':'Observed review-state/planning changes, not proof of human inactivity'}


class Followup:
    def __init__(self,storage,root,clock,run=None,gateway=None):
        self.storage=storage;self.root=root;self.clock=clock
        self.run=run or (lambda args:activity.run_json(args,timeout=12))
        self.gateway=gateway or Gateway(root)
        self.lock=threading.RLock()
        storage.recover_followup(clock())

    def read(self,params):
        scope=filters(params);report=build_report(self.storage.followup_snapshot(self.clock()),self.clock(),scope)
        history=self.storage.followup_runs({'limit':10})
        report['gateway']=self.gateway.status();report['analysis']=history[0] if history else None
        cached=next((item for item in history if item['status']=='succeeded' and item['result'].get('fingerprint')==report['fingerprint']),None)
        if cached and cached['result'].get('usage',{}).get('model_calls',0)>0:
            report['next']=cached['result']['next'];report['ai_report_key']=cached['key']
        elif history:report['ai_warning']='No matching AI report for the current facts/scope. Local advice is shown.'
        return report

    def context(self,candidate,source,snapshot):
        task=next(item for item in snapshot['tasks'] if item['id']==candidate['task_id'])
        if source=='task':
            return {'guidance':task['guidance'],'recent_goals':task['goals'][:2],
                    'local_pr_descriptions':[clipped(item['metadata'].get('description'),700) for item in snapshot['prs'] if item['task_id']==task['id']][:2]}
        pr=next((item for item in snapshot['prs'] if item['url']==candidate['url']),None)
        if not pr:raise ValueError('This candidate has no PR context')
        if source=='github':
            url=activity.pr_url(pr['url']);owner,repo,_,number=url.split('github.com/',1)[1].split('/')
            query='query FollowupContext($owner:String!,$repo:String!,$number:Int!){repository(owner:$owner,name:$repo){pullRequest(number:$number){url state isDraft mergeStateStatus body comments(last:10){nodes{author{login __typename} createdAt body}} reviews(last:10){nodes{author{login __typename} submittedAt state body}}}}}'
            response=self.run(['gh','api','graphql','-f','query='+query,'-f','owner='+owner,'-f','repo='+repo,'-F','number='+number])
            detail=((response.get('data') or {}).get('repository') or {}).get('pullRequest') or {}
            if response.get('errors') or detail.get('url')!=url:raise ValueError('GitHub context unavailable or mismatched')
            return {'coverage':'Latest 10 issue comments and latest 10 reviews; inline threads are not included',
              'state':detail.get('state'),'isDraft':detail.get('isDraft'),'mergeStateStatus':detail.get('mergeStateStatus'),
              'body':clipped(detail.get('body'),800),
              'comments':[{'user':(item.get('author') or {}).get('login'),'type':(item.get('author') or {}).get('__typename'),'at':item.get('createdAt'),'body':clipped(item.get('body'),350)} for item in (detail.get('comments') or {}).get('nodes',[])],
              'reviews':[{'user':(item.get('author') or {}).get('login'),'state':item.get('state'),'at':item.get('submittedAt'),'body':clipped(item.get('body'),350)} for item in (detail.get('reviews') or {}).get('nodes',[])]}
        if source!='slack' or not pr['threads']:raise ValueError('No explicitly linked Slack thread is available')
        url=activity.slack_url(pr['threads'][0]['url'])
        parts=urlsplit(url);channel,message=parts.path.split('/')[2:4]
        ts=parse_qs(parts.query).get('thread_ts',[None])[0]
        if ts is None:ts=message[1:-6]+'.'+message[-6:]
        if not re.fullmatch(r'\d{1,20}\.\d{1,6}',ts):raise ValueError('Unsupported Slack timestamp')
        response=self.run(['slack-cli','messages','thread',channel,ts,'--limit','10','--json'])
        if not response.get('success'):raise ValueError('Linked Slack thread unavailable')
        data=response.get('data') or {}
        if not any(item.get('ts')==ts for item in data.get('messages',[])):
            raise ValueError('The linked thread parent was not returned; do not infer its contents')
        return {'coverage':'At most 10 messages from the first explicitly linked thread only','messages':[
            {'user':item.get('userDisplayName') or item.get('user'),'at':item.get('ts'),'text':clipped(item.get('text'),400)} for item in data.get('messages',[])[:10]]}

    def analyze(self,params):
        key=params['key'];scope=filters({name:value for name,value in params.items() if name!='key'})
        self.storage.save_followup_run(key,'running',{'scope':scope},self.clock())
        usage={'model_calls':0,'context_reads':0,'input_bytes':0,'total_tokens':0};context=[];seen=set()
        try:
            snapshot=self.storage.followup_snapshot(self.clock());report=build_report(snapshot,self.clock(),scope)
            if not report['candidates']:
                result={**report,'usage':usage,'next':report['next']}
            else:
                shortlist=[];compact=[]
                for item in report['candidates'][:24]:
                    summary={name:clipped(item[name],180) for name in ('id','title','project','workstream','action','kind')}
                    summary.update(today=item['today'],signals=[clipped(text,120) for text in item['signals'][:2]],
                                   evidence=[clipped(text,120) for text in item['evidence'][:3]],context_available=item['context_available'])
                    if len(json.dumps([*compact,summary],ensure_ascii=False).encode())>16000:break
                    shortlist.append(item);compact.append(summary)
                frame={'priority_policy':'Unknown; ask for clarification when candidates compete','coverage':{'shown':len(shortlist),'total':len(report['candidates'])},
                  'candidates':compact,
                  'context':context,'remaining_reads':3}
                model=self.gateway.model();credential=self.gateway.key()
                for turn in range(3):
                    usage['model_calls']+=1
                    decision,counts=self.gateway.decide(frame,model=model,credential=credential)
                    for field in ('input_bytes','total_tokens'):usage[field]+=counts.get(field,0)
                    if decision['mode']!='context':break
                    requests=decision['context_requests']
                    if not requests or turn==2:break
                    before_reads=usage['context_reads']
                    for item in requests:
                        identity=(item['candidate_id'],item['source'])
                        if identity in seen or usage['context_reads']>=3:continue
                        seen.add(identity);usage['context_reads']+=1
                        candidate=next(value for value in shortlist if value['id']==item['candidate_id'])
                        try:
                            content=clipped(json.dumps(self.context(candidate,item['source'],snapshot),ensure_ascii=False),3000)
                        except Exception:content='Context unavailable. Do not infer the missing content.'
                        context.append({'candidate_id':identity[0],'source':identity[1],'excerpt':content})
                    frame['remaining_reads']=3-usage['context_reads']
                    if usage['context_reads']==before_reads:break
                if decision['mode']=='context':
                    decision={'mode':'clarify','candidate_ids':decision['candidate_ids'],'reason':'The bounded context budget did not resolve the choice.','question':'What outcome or missing context will help choose between these candidates?'}
                if len(report['candidates'])>len(shortlist) and decision['mode']=='recommend':
                    decision.update(mode='clarify',question='Only part of the backlog was analyzed. Which project or workstream should we focus on?')
                result={'fingerprint':report['fingerprint'],'scope':scope,'candidates':compact,'model':model,'next':{name:decision[name] for name in ('mode','candidate_ids','reason','question')},
                        'usage':usage,'context_sources':[{'candidate_id':item['candidate_id'],'source':item['source']} for item in context],
                        'interpretation':'AI rationale is an interpretation. Candidate actions and eligibility come from tracker facts.',
                        'coverage':frame['coverage']}
            self.storage.save_followup_run(key,'succeeded',result,self.clock())
            return {'ok':True,'key':key,'usage':usage}
        except Exception as exc:
            # Never persist a provider/CLI error body, credential, or prompt in a job error.
            warning='Analysis unavailable ('+type(exc).__name__+'). Local advice remains available. Configure the key or request a new analysis explicitly.'
            self.storage.save_followup_run(key,'failed',{'warning':warning,'usage':usage,'scope':scope},self.clock())
            return {'ok':False,'key':key,'error':warning}
