"""Derived, read-only session actions. Stored references never supply shell code."""

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shlex
import shutil
import socket
import subprocess
import tempfile
import threading
import time

import work_activity as activity
from .herdr_sessions import eligible


FIELDS = ('tool', 'path', 'sessionId', 'machine')


def references(record):
    """Include legacy OpenCode provenance without changing either stored contract."""
    values = list(record.get('agent-sessions') or [])
    old = record.get('source-session')
    if isinstance(old, dict):
        legacy = {'tool': 'opencode', 'path': old.get('directory'),
                  'sessionId': old.get('id'), 'machine': old.get('machine')}
        same_legacy = any(isinstance(value, dict) and all(value.get(key) == legacy[key]
                          for key in ('tool', 'path', 'sessionId')) and
                          (not legacy['machine'] or value.get('machine') == legacy['machine']) for value in values)
        if not same_legacy:
            values.append(legacy)
    result = []
    for value in values:
        reference = {key: value.get(key) for key in FIELDS} if isinstance(value, dict) else {}
        if reference not in result:
            result.append(reference)
    return result


def inspect_session(argv, cwd):
    """Read native JSON through argument arrays. Do not resume or create a session."""
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        completed = subprocess.run(argv, cwd=cwd, stdout=output, stderr=errors, timeout=6, check=False)
        if completed.returncode:
            raise ValueError('The native session lookup failed. No conversation was opened.')
        if output.tell() > 16 * 1024 * 1024:
            raise ValueError('The native session response exceeds the inspection limit.')
        output.seek(0)
        return json.load(output)


class SessionActions:
    def __init__(self, machine=None, which=None, is_directory=None, inspect=None):
        self.machine = machine or socket.gethostname()
        self.which = which or shutil.which
        self.is_directory = is_directory or (lambda path: Path(path).is_dir())
        self.inspect = inspect or inspect_session
        self.checks = {}
        self.lock = threading.RLock()

    def project(self, record, verify=False):
        value = deepcopy(record)
        actions = [self.action(reference, verify) for reference in references(record)]
        value['agent-session-actions'] = actions
        value['tracking-outcomes'] = {
            'pr_registration': 'registered', 'session_references': len(actions),
            'usable_session_actions': sum(action['status'] == 'ready' for action in actions),
            'other_machine_actions': sum(action['status'] == 'other_machine' for action in actions)}
        return value

    def action(self, reference, verify=False):
        result = {'reference': deepcopy(reference), 'status': 'incomplete', 'kind': None,
                  'command': None, 'can_verify': False, 'usable_locally': False,
                  'verified_at': None, 'reason': 'No usable session link. The identity is incomplete.',
                  'can_activate_pane': eligible(reference, self.machine) and bool(self.which('herdr'))}
        if any(not isinstance(reference.get(key), str) or not reference[key].strip() for key in FIELDS):
            return result
        if any(not char.isprintable() for key in FIELDS for char in reference[key]):
            return {**result, 'status': 'unsafe', 'reason': 'No usable session link. Identity contains control characters.'}
        tool, path, sid, machine = (reference[key] for key in FIELDS)
        if not Path(path).is_absolute():
            return {**result, 'status': 'unsafe', 'reason': 'No usable session link. The workspace path is not absolute.'}
        tool = tool.casefold()
        if tool not in ('fx', 'opencode'):
            return {**result, 'status': 'unsupported', 'reason': 'No usable session link. This agent tool has no resume adapter.'}
        pattern = r'[A-Za-z0-9_-]{1,200}' if tool == 'fx' else r'ses_[A-Za-z0-9]+'
        if not re.fullmatch(pattern, sid) or tool == 'fx' and sid == 'last':
            return {**result, 'status': 'unsafe', 'reason': 'No usable session link. The session ID is not valid for this adapter.'}
        if tool == 'fx':
            resume = ['fx', 'session', 'resume', '--id', sid] if sid.startswith('-') else ['fx', '--resume', sid]
        else:
            resume = ['opencode', '--session', sid]
        result.update(kind='copy_resume_command', command=f'cd -- {shlex.quote(path)} && {shlex.join(resume)}')
        normalize = lambda value: value.casefold().rstrip('.')
        if normalize(machine) != normalize(self.machine):
            return {**result, 'status': 'other_machine',
                    'reason': f'Run the command on {machine}, not on this machine. The remote session was not checked.'}
        try:
            exists = self.is_directory(path)
        except (OSError, ValueError):
            exists = False
        if not exists:
            return {**result, 'status': 'missing_workspace', 'reason': 'No usable session link. The recorded workspace does not exist here.'}
        executable = self.which(tool)
        if not executable:
            return {**result, 'status': 'tool_unavailable', 'reason': 'No usable session link. The agent executable is not on the application PATH.'}
        result.update(status='unverified', can_verify=True,
                      reason='Resume command available. Check the saved session before reporting a usable link.')
        identity = hashlib.sha256(json.dumps(reference, sort_keys=True).encode()).hexdigest()
        with self.lock:
            if not verify:
                checked = self.checks.get(identity)
                if checked and time.monotonic() - checked[0] < 300:
                    result.update(checked[1])
                return result
            argv = ([executable, 'session', '--id', sid, '--json'] if tool == 'fx' else
                    [executable, 'api', 'session.get', '--param', f'sessionID={sid}'])
            try:
                data = self.inspect(argv, path)
                info = data if tool == 'fx' else data.get('data')
                if not isinstance(info, dict) or info.get('id') != sid:
                    raise ValueError('The native lookup did not return the recorded session ID.')
                if tool == 'opencode':
                    directory = (info.get('location') or {}).get('directory')
                    if not isinstance(directory, str) or Path(directory).resolve() != Path(path).resolve():
                        raise ValueError('The native session workspace does not match the recorded workspace.')
                check = {'status': 'ready', 'usable_locally': True,
                         'verified_at': activity.iso(datetime.now(timezone.utc)),
                         'reason': 'Saved session found. Copy the command and run it in your terminal. No conversation was opened.'}
            except (ValueError, OSError, subprocess.SubprocessError, TypeError, AttributeError):
                check = {'status': 'unavailable', 'usable_locally': False, 'verified_at': None,
                         'reason': 'No usable session link. Native inspection could not confirm this session and workspace.'}
            self.checks[identity] = (time.monotonic(), check)
            result.update(check)
            return result
