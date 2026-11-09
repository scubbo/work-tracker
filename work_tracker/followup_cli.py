"""Follow-up CLI. All state and cloud work stay behind private application RPC."""

import argparse
import getpass
import json
import sys
import time
import uuid
import warnings


def configure(commands):
    parser=commands.add_parser('follow-up',help='Read advice and stale work; AI analysis is explicit')
    parser.add_argument('--project');parser.add_argument('--workstream')
    parser.add_argument('--stale-days',type=int,default=7)
    parser.add_argument('--json',action='store_true')
    sub=parser.add_subparsers(dest='followup_command')
    analyze=sub.add_parser('analyze',help='Explicitly request bounded AI Gateway analysis')
    analyze.add_argument('--request-key',help='Stable analysis request ID, not the Gateway credential; reuse after an unknown response')
    analyze.add_argument('--wait',action='store_true')
    sub.add_parser('key',help='Set the private Gateway credential with a masked terminal prompt')
    model=sub.add_parser('model',help='Choose an explicit Gateway provider/model');model.add_argument('model')
    history=sub.add_parser('history',help='Read private analysis receipts');history.add_argument('--limit',type=int,default=10)
    task=sub.add_parser('task',help='Read or edit Task follow-up metadata')
    task.add_argument('task_id');task.add_argument('--version',type=int)
    for field in ('next-action','after-task','waiting-on','dependencies','revisit-after'):task.add_argument('--'+field)
    for command in (analyze,model,history,task):command.add_argument('--json',action='store_true',default=argparse.SUPPRESS)


def safe(value):return ''.join(char if char.isprintable() or char=='\n' else f'\\x{ord(char):02x}' for char in str(value))


def run(root,args,rpc):
    action=args.followup_command
    scope={'project':args.project,'workstream':args.workstream,'stale_days':args.stale_days}
    if action=='key':
        if not sys.stdin.isatty():raise ValueError('Run work-tracker follow-up key in a terminal. Do not pass credentials in arguments or chat.')
        with warnings.catch_warnings():
            warnings.simplefilter('error',getpass.GetPassWarning)
            try:key=getpass.getpass('AI Gateway key (masked): ')
            except getpass.GetPassWarning:raise RuntimeError('A masked terminal prompt is unavailable. No key was read.') from None
        return rpc(root,'followup_key',{'key':key})
    if action=='model':return rpc(root,'followup_model',{'model':args.model})
    if action=='history':return rpc(root,'followup_runs',{'limit':args.limit})
    if action=='task':
        params={'task_id':args.task_id}
        for field in ('next_action','after_task','waiting_on','dependencies','revisit_after'):
            value=getattr(args,field)
            if value is None:continue
            if field=='dependencies':value=[item.strip() for item in value.split(',') if item.strip()]
            elif field=='revisit_after':value=value or None
            params[field]=value
        if len(params)>1:
            old=rpc(root,'followup_task',{'task_id':args.task_id})
            params['expected_version']=args.version if args.version is not None else old['version']
        return rpc(root,'followup_task',params)
    if action=='analyze':
        key=args.request_key or uuid.uuid4().hex
        print('Analysis request key: '+safe(key)+'. Reuse it after an unknown response.',file=sys.stderr)
        result=rpc(root,'followup_analyze',{'key':key,**scope})
        if not args.wait:return result
        for _ in range(180):
            result=rpc(root,'followup_runs',{'key':key})
            if result and result['status'] not in ('queued','running'):return result
            time.sleep(1)
        raise ValueError('Analysis still pending. Read follow-up history; do not create another request blindly.')
    report=rpc(root,'followup',scope)
    if args.json:return report
    print('Follow-up | '+report['generated_at'])
    print('What should I do next? '+report['next']['mode'])
    print(safe(report['next']['reason']))
    indexed={item['id']:item for item in report['candidates']}
    for identifier in report['next']['candidate_ids']:
        item=indexed[identifier]
        print(safe(f"  {item['action']} | {item['title']} | {item['project']} / {item['workstream']}"))
        print(safe('  '+(item['url'] or 'Task '+item['task_id'])))
    if report['next']['question']:print(safe('Clarification: '+report['next']['question']))
    print('\nWhat has fallen through the cracks?')
    for item in report['stale']:
        print(safe(f"  {item['title']} | {'; '.join(item['signals'])} | Task {item['task_id']}"))
    print(f"{len(report['stale'])} flagged; {report['unknown_age_count']} unknown ages; {report['snoozed_count']} deferred.")
    print(safe(report['stale_rule']))
    print(safe('Gateway: '+('configured' if report['gateway']['configured'] else 'key missing; run '+report['gateway']['setup_command'])))
    if report.get('ai_warning'):print(safe(report['ai_warning']))
    return None
