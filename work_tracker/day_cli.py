"""CLI planning actions through private RPC, without MCP or direct datastore access."""

import argparse
import json
import sys
import uuid


def configure(commands):
    parser = commands.add_parser('day', help='Read My Day, manage daily goals, and keep persistent awareness items')
    parser.add_argument('--date', dest='day_date', help='Read a local calendar day as YYYY-MM-DD; past days are read-only')
    parser.add_argument('--json', action='store_true')
    operations = parser.add_subparsers(dest='day_command')
    targets = operations.add_parser('targets', help='Search existing Tasks and workstreams without querying GitHub')
    targets.add_argument('--search', default='')
    targets.add_argument('--kind', choices=('task','workstream'))
    targets.add_argument('--limit', type=int, default=50)
    targets.add_argument('--offset', type=int, default=0)
    add = operations.add_parser('add', help='Quick-add a Task or link a known Task, workstream, or tracked PR')
    add.add_argument('text', nargs='?', help='Text for a new Task or an optional daily label for an existing target')
    add.add_argument('--notes',help='Optional multiline notes for a new daily goal')
    add.add_argument('--awareness', action='store_true', help='Keep watching this item until it is explicitly dismissed')
    add.add_argument('--in-project','--project',dest='task_project',help='Project for a new Task; blank uses Unclassified')
    add.add_argument('--in-workstream',dest='task_workstream',help='Workstream for a new Task; requires --in-project')
    group = add.add_mutually_exclusive_group()
    group.add_argument('--task')
    group.add_argument('--workstream')
    group.add_argument('--pr')
    mutations = [add]
    for action, help_text in (('done','Complete only today\'s goal'),('reopen','Mark today\'s goal open'),
                              ('remove','Remove a goal from today without deleting its Task'),
                              ('dismiss','Dismiss a persistent awareness item'),
                              ('carry','Explicitly copy an unfinished past goal to today'),
                              ('edit','Change the day label or explicitly assign the Task'),
                              ('classify','Assign the underlying Task to a project/workstream')):
        operation = operations.add_parser(action, help=help_text)
        operation.add_argument('item_id')
        operation.add_argument('--version', type=int, help='Expected item version; otherwise use the currently displayed version')
        if action=='edit':
            operation.add_argument('text',nargs='?')
            operation.add_argument('--notes',help='Replace this daily goal\'s notes; empty text clears them')
        if action in ('edit','classify'):
            operation.add_argument('--in-project','--project',dest='task_project')
            operation.add_argument('--in-workstream','--workstream',dest='task_workstream')
        mutations.append(operation)
    for operation in mutations:
        operation.add_argument('--key', help='Reuse this key with the identical payload after an uncertain response')
    for operation in [targets,*mutations]:
        operation.add_argument('--json', action='store_true', default=argparse.SUPPRESS)


def run(root, args, rpc):
    action = args.day_command
    if action=='targets':
        return rpc(root,'day_targets',{'search':args.search,'kind':args.kind,'limit':args.limit,'offset':args.offset})
    view = rpc(root,'my_day',{'day':args.day_date} if args.day_date else {})
    if action is None:
        if args.json:
            return view
        def safe(value): return ''.join(char if char.isprintable() else ' ' for char in str(value))
        print(f"My Day {view['day']} | {view['open_count']} open / {view['done_count']} done")
        for label, items in (('Goals',view['goals']),('Awareness',view['awareness']),('Carry-forward candidates',view['carry_candidates'])):
            print('\n'+label)
            for item in items:
                state = '[x]' if item['done'] else '[ ]'
                target = item['target']
                print(safe(f"{state} {item['label']} | {target['project_name']} / {target['workstream_name']} | "
                           f"id={item['id']} version={item['version']} source-day={item['day']}"))
                if item.get('notes'):
                    print('    Notes:')
                    for line in item['notes'].split('\n'): print('    '+safe(line))
        return None
    key = args.key or uuid.uuid4().hex
    payload = {'day':view['today'] if action=='carry' else view['day']}
    if action in ('add','edit') and args.notes is not None: payload['notes']=args.notes
    if action=='add':
        payload['kind'] = 'awareness' if args.awareness else 'goal'
        payload['target_kind'] = 'pr' if args.pr else ('workstream' if args.workstream else ('task' if args.task else 'new'))
        if args.pr: payload['url'] = args.pr
        elif args.task or args.workstream: payload['target_id'] = args.task or args.workstream
        if args.text is not None: payload['text'] = args.text
        if args.task_project is not None or args.task_workstream is not None:
            if payload['target_kind']!='new':
                raise ValueError('Existing links inherit classification. Use day classify after adding the Task.')
            if args.task_project is not None: payload['project']=args.task_project
            if args.task_workstream is not None: payload['workstream']=args.task_workstream
    else:
        item = next((item for item in [*view['goals'],*view['awareness'],*view['carry_candidates']] if item['id']==args.item_id),None)
        if args.version is None and item is None:
            raise ValueError('Item is not in this view. Select its history date or provide its exact --version.')
        payload.update(item_id=args.item_id,expected_version=args.version if args.version is not None else item['version'])
        if action in ('done','reopen'):
            payload['done'] = action=='done'
            action = 'set_done'
        elif action in ('edit','classify'):
            if action=='edit' and args.text is not None: payload['text']=args.text
            assigning=args.task_project is not None or args.task_workstream is not None
            if action=='classify' and not assigning:
                raise ValueError('Provide --project or --workstream for classification')
            if assigning:
                if item is None or item['target_kind']!='task':
                    raise ValueError('Select a visible Task item before changing its classification')
                target=item['target']
                project=args.task_project if args.task_project is not None else target['project_name']
                workstream=args.task_workstream if args.task_workstream is not None else (
                    'Unclassified' if (project.strip() or 'Unclassified')!=target['project_name'] else target['workstream_name'])
                payload.update(project=project,workstream=workstream,expected_workstream_id=target['workstream_id'])
            action='edit'
    request = {'key':key,'action':action,'payload':payload,'source':{'client':'cli'}}
    print(f"My Day request key: {key}; day: {payload['day']}",file=sys.stderr)
    try:
        return rpc(root,'day_intent',request)
    except (ValueError,RuntimeError,OSError) as exc:
        raise ValueError(f'{exc} Retain key {key} and day {payload["day"]} for an identical retry.') from exc
