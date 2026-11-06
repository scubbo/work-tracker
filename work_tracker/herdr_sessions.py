"""Find an existing local pane, never start an agent or execute stored shell text."""

import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import tempfile
import time


FIELDS = ('tool', 'path', 'sessionId', 'machine')


class InspectionUnavailable(Exception):
    """A required inspection tool is missing, not evidence of an absent session."""


def eligible(reference, machine):
    if not isinstance(reference, dict) or any(
        not isinstance(reference.get(key), str) or not reference[key].strip() or
        not all(char.isprintable() for char in reference[key]) for key in FIELDS
    ):
        return False
    return (Path(reference['path']).is_absolute() and
            bool(re.fullmatch(r'[A-Za-z0-9_-]{1,200}', reference['sessionId'])) and
            reference['sessionId'] != 'last' and
            bool(re.fullmatch(r'[A-Za-z0-9_-]{1,64}', reference['tool'])) and
            reference['machine'].casefold().rstrip('.') == machine.casefold().rstrip('.'))


def read_output(argv, timeout, env=None, allow_empty=False):
    # Regular files avoid pipe truncation and keep terminal output out of the UI.
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        done = subprocess.run(argv, stdout=output, stderr=errors, timeout=timeout,
                              env=env, check=False)
        if output.tell() > 16 * 1024 * 1024:
            raise ValueError('Native response exceeds the inspection limit.')
        output.seek(0)
        data = output.read().decode('utf-8')
        if done.returncode and not (allow_empty and done.returncode == 1 and not data):
            raise ValueError('Native pane inspection failed.')
        return data


class HerdrSessions:
    def __init__(self, machine=None, which=None, run=None, home=None):
        self.machine = machine or socket.gethostname()
        self.which = which or shutil.which
        self.run = run or read_output
        self.home = Path(home) if home else Path.home()

    def _json(self, executable, args, end):
        # Never inherit a caller's pane or remote socket. Route each request explicitly.
        env = {key: value for key, value in os.environ.items() if not key.startswith('HERDR_')}
        remaining = end - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('Pane search timed out.')
        value = json.loads(self.run([executable, *args], timeout=min(2, remaining), env=env))
        if not isinstance(value, dict) or 'error' in value:
            raise ValueError('Invalid native response.')
        return value

    @staticmethod
    def _same_workspace(pane, reference):
        return any(isinstance(cwd, str) and Path(cwd).is_absolute() and
                   Path(cwd).resolve() == Path(reference['path']).resolve()
                   for cwd in (pane.get('foreground_cwd'), pane.get('cwd')))

    def _matches(self, executable, session, pane, reference, end):
        if pane.get('agent') != reference['tool'].casefold() or not self._same_workspace(pane, reference):
            return False
        identity = pane.get('agent_session')
        if identity is not None:
            # An explicitly different native identity overrides every fallback.
            return (isinstance(identity, dict) and identity.get('kind') == 'id' and
                    identity.get('agent') == reference['tool'].casefold() and
                    identity.get('value') == reference['sessionId'])
        if reference['tool'].casefold() != 'fx':
            return False
        lsof = self.which('lsof')
        if not lsof:
            raise InspectionUnavailable('The application cannot find lsof to identify this fx pane. Check its PATH or use the copy-resume command.')
        info = self._json(executable, ['--session', session, 'pane', 'process-info', '--pane', pane['pane_id']], end)
        info = info['result']['process_info']
        if info.get('pane_id') != pane['pane_id']:
            raise ValueError('Native pane identity changed.')
        processes = info['foreground_processes']
        pids = []
        for process in processes:
            argv0 = process.get('argv0')
            # One fx process can host several workspace instances. Its process cwd
            # follows the active instance, not every loaded session. The pane's
            # workspace and the exact open session files provide the identity here.
            if (isinstance(argv0, str) and Path(argv0).name == 'fx' and
                    type(process.get('pid')) is int and process['pid'] > 0):
                pids.append(process['pid'])
        if not pids:
            return False
        remaining = end - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('Pane search timed out.')
        text = self.run([lsof, '-F', 'pfn', '-a', '-p', ','.join(map(str, pids))],
                        timeout=min(2, remaining), allow_empty=True)
        # Fx v2 keeps both files open for a loaded conversation. An index lock,
        # directory, title, argv mention, or log opened by another process is not proof.
        session_path = (self.home / '.fx/sessions/v2' / reference['sessionId']).resolve()
        wanted = {str(session_path / 'lock'), str(session_path / 'log.jsonl')}
        opened, pid = {}, None
        for line in text.splitlines():
            if line.startswith('p'):
                pid = int(line[1:])
            elif line.startswith('n') and pid in pids:
                opened.setdefault(pid, set()).add(line[1:])
        return any(wanted <= paths for paths in opened.values())

    def find(self, reference, end=None):
        """Read-only discovery, also usable for deployment checks without stealing focus."""
        try:
            return self._find(reference, end)
        except InspectionUnavailable as exc:
            return {'status': 'unavailable', 'reason': str(exc)}

    def _find(self, reference, end=None):
        if not eligible(reference, self.machine):
            return {'status': 'unavailable', 'reason': 'This reference cannot activate a pane on this machine.'}
        executable = self.which('herdr')
        if not executable:
            return {'status': 'unavailable', 'reason': 'Herdr is not on the application PATH. Use the copy-resume command.'}
        end = end or time.monotonic() + 7
        sessions = self._json(executable, ['session', 'list', '--json'], end)['sessions']
        if not isinstance(sessions, list):
            raise ValueError('Invalid local session list.')
        matches = []
        for session in sessions:
            if session.get('running') is not True:
                continue
            name = session['name']
            if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', name) or name.startswith('-'):
                raise ValueError('Invalid local herdr session name.')
            snapshot = self._json(executable, ['--session', name, 'api', 'snapshot'], end)['result']['snapshot']
            seen = set()
            for pane in snapshot['agents']:
                pane_id = pane['pane_id']
                if not isinstance(pane_id, str) or not re.fullmatch(r'w[A-Za-z0-9]+:p[A-Za-z0-9]+', pane_id):
                    raise ValueError('Invalid native pane ID.')
                if pane_id not in seen and self._matches(executable, name, pane, reference, end):
                    matches.append({'session': name, 'pane_id': pane_id})
                seen.add(pane_id)
        if len(matches) != 1:
            return {'status': 'ambiguous' if matches else 'not_found',
                    'reason': ('Several panes match this session. No pane was activated.' if matches else
                               'No verified open herdr pane contains this session. Use the copy-resume command.')}
        return {'status': 'found', **matches[0]}

    def activate(self, reference):
        end = time.monotonic() + 7
        try:
            found = self.find(reference, end)
            if found['status'] != 'found':
                return found
            executable = self.which('herdr')
            name, pane_id = found['session'], found['pane_id']
            # Refresh native session metadata immediately before the focus operation.
            snapshot = self._json(executable, ['--session', name, 'api', 'snapshot'], end)['result']['snapshot']
            pane = next((pane for pane in snapshot['agents'] if pane.get('pane_id') == pane_id), None)
            if not pane or not self._matches(executable, name, pane, reference, end):
                return {'status': 'not_found', 'reason': 'The pane no longer contains this session. Use the copy-resume command.'}
            result = self._json(executable, ['--session', name, 'agent', 'focus', pane_id], end)
            receipt = result['result']
            focused = receipt.get('agent')
            if (receipt.get('type') != 'agent_info' or not isinstance(focused, dict) or
                    focused.get('pane_id') != pane_id or focused.get('focused') is not True or
                    focused.get('agent') != reference['tool'].casefold()):
                raise ValueError('Herdr did not confirm pane focus.')
            current = self._json(executable, ['--session', name, 'api', 'snapshot'], end)['result']['snapshot']
            if current.get('focused_pane_id') != pane_id:
                raise ValueError('Herdr did not retain pane focus.')
            return {'status': 'activated', **{key: found[key] for key in ('session', 'pane_id')},
                    'reason': f'Activated herdr pane {pane_id} in session {name}.'}
        except InspectionUnavailable as exc:
            return {'status': 'unavailable', 'reason': str(exc)}
        except (OSError, ValueError, TypeError, KeyError, AttributeError, subprocess.SubprocessError):
            return {'status': 'unavailable', 'reason': 'Could not safely find or activate the herdr pane. Use the copy-resume command.'}
