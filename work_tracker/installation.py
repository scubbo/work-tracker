"""Deployment helpers. OS supervision never contains a job schedule."""

import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import uuid

from .client import request
import work_activity as activity


def prepare_python(source, home):
    """Install the pinned SDK into an application-owned environment, not the checkout."""
    root = home/".local/lib/work-activity"
    activity.private_directory(root)
    root.chmod(0o700)
    environment = root/"runtime"
    python = environment/"bin/python"
    if not python.exists():
        base = str(Path(getattr(sys, "_base_executable", sys.executable)).resolve())
        subprocess.run([base, "-m", "venv", str(environment)], check=True)
    pins = dict(line.split("==", 1) for line in (source/"requirements.lock").read_text().splitlines()
                if line and not line.startswith("#"))
    audit = (
        "import importlib.metadata as m,json,sys\n"
        "try:\n ok=all(m.version(k)==v for k,v in json.loads(sys.argv[1]).items())\n"
        "except m.PackageNotFoundError:\n ok=False\n"
        "sys.exit(0 if ok else 1)\n")
    ready = subprocess.run([str(python), "-c", audit, json.dumps(pins)], capture_output=True).returncode == 0
    if not ready and shutil.which("uv"):
        # Reuse existing exact wheels without network access or registry/config changes.
        cached = subprocess.run([shutil.which("uv"), "--no-config", "pip", "install", "--offline",
                                 "--python", str(python), "-r", str(source/"requirements.lock")], capture_output=True)
        ready = cached.returncode == 0
    if not ready:
        # Honor the user's configured registry. Never fall back to another online index.
        subprocess.run([str(python), "-m", "pip", "install", "--disable-pip-version-check", "--no-input",
                        "-r", str(source/"requirements.lock")], check=True)
    subprocess.run([str(python), "-c", "from mcp.server import MCPServer"], check=True)
    return str(python)


def check_idle(home, legacy_label):
    legacy = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{legacy_label}"], capture_output=True, text=True)
    if legacy.returncode == 0:
        active = re.search(r"^\s*active count = (\d+)$", legacy.stdout, re.M)
        if not active:
            raise RuntimeError("Cannot determine legacy collection state. Migration was not started.")
        if int(active.group(1)):
            raise RuntimeError("Legacy collection is active. Wait for it to finish before migrating the service.")
    try:
        status = request(home/".local/state/work-activity", "status")
    except RuntimeError:
        return
    if any(job["status"] == "running" for job in status["jobs"]):
        raise RuntimeError("An application job is active; wait for it to finish before reinstalling")


def datastore_free(root):
    path = root/'.lock'
    if not path.exists():
        return True
    with open(path, 'r+') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            return False


def activate(plist, home, label, legacy_label, health=request, sleep=time.sleep):
    domain = f"gui/{os.getuid()}"
    service = f"{domain}/{label}"
    root = home/".local/state/work-activity"
    current = subprocess.run(["launchctl", "print", service], capture_output=True)
    if current.returncode == 0:
        subprocess.run(["launchctl", "bootout", service], check=True)
        for _ in range(150):
            gone = subprocess.run(["launchctl", "print", service], capture_output=True).returncode != 0
            if gone and datastore_free(root):
                sleep(0.2)
                break
            sleep(0.2)
        else:
            raise RuntimeError("Previous application is still stopping. No replacement was started.")
    if not datastore_free(root):
        raise RuntimeError("A manually started application owns the datastore. Stop it before loading the service.")
    subprocess.run(["launchctl", "bootstrap", domain, str(plist)], check=True)
    for _ in range(100):
        try:
            status = health(root, "status")
            if status.get("storage") == "sqlite" and "jobs" in status:
                break
        except (RuntimeError, ValueError):
            pass
        sleep(0.2)
    else:
        raise RuntimeError("New application failed readiness; legacy calendar registration was not removed. Inspect service logs.")
    legacy_service = f"{domain}/{legacy_label}"
    if subprocess.run(["launchctl", "print", legacy_service], capture_output=True).returncode == 0:
        subprocess.run(["launchctl", "bootout", legacy_service], check=True)
    old_plist = home/"Library/LaunchAgents"/f"{legacy_label}.plist"
    if old_plist.exists():
        content = old_plist.read_bytes()
        backup = root/"backups"/f"legacy-scheduler-{uuid.uuid4().hex}.plist"
        activity.atomic_bytes(backup, content)
        if old_plist.read_bytes() != content:
            raise RuntimeError("Legacy plist changed during migration; retained it and its backup")
        old_plist.unlink()
    return status
