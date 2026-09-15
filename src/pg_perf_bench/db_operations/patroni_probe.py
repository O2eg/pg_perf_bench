"""Read-only discovery on the database host; also executable as a remote script.

Only the standard library is required for discovery. The member helper runs
with the active Patroni process's Python, environment and working directory.
Configuration and credentials never leave that host.
"""

from __future__ import annotations

import json
import os
import pwd
import shlex
import shutil
import subprocess
import sys
from pathlib import Path


def patroni_command(argv):
    """Recognize the Patroni entry point, excluding patronictl and shell wrappers."""
    if not argv:
        return None
    if Path(argv[0]).name == 'patroni':
        return argv[0], argv[1:]
    if Path(argv[0]).name.startswith('python'):
        args = argv[1:]
        while args and args[0] in {'-u', '-B', '-E', '-s', '-S', '-I', '-O', '-OO'}:
            args = args[1:]
        if args and Path(args[0]).name == 'patroni':
            return argv[0], args[1:]
        if args[:2] == ['-m', 'patroni']:
            return argv[0], args[2:]
    return None


def process_context(process, executable):
    environment = dict(
        item.split('=', 1)
        for item in process.joinpath('environ').read_text().split('\0')
        if '=' in item
    )
    cwd = os.readlink(process / 'cwd')
    if Path(executable).name == 'patroni':
        script = Path(executable)
        if not script.is_absolute():
            script = Path(
                shutil.which(executable, path=environment.get('PATH')) or str(Path(cwd) / script)
            )
        shebang = shlex.split(script.read_text().splitlines()[0].removeprefix('#!'))
        if Path(shebang[0]).name == 'env':
            executable = shutil.which(shebang[1], path=environment.get('PATH'))
        else:
            executable = shebang[0]
    if not executable:
        raise RuntimeError('Cannot find the active Patroni Python interpreter')
    return executable, environment, cwd


def invoke_member(process, command, script, action, data_path, timeout):
    executable, args = command
    executable, environment, cwd = process_context(process, executable)
    # Patroni accepts an optional positional YAML file or configuration directory.
    # Other modes (--validate-config, --generate-config, etc.) do not manage PG.
    if len(args) > 1 or (args and args[0].startswith('-')):
        return None
    result = subprocess.run(
        [executable, '-c', script, action, data_path, args[0] if args else '', str(timeout)],
        env=environment,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode:
        # Library diagnostics can contain YAML values or authentication details.
        raise RuntimeError(
            'Cannot inspect/control Patroni process '
            + process.name
            + '; check access to its configuration, Python environment and REST API'
        )
    response = json.loads(result.stdout)
    if response and response.get('error'):
        raise RuntimeError(response['error'])
    return response


def discover(data_path, script, timeout, proc=Path('/proc')):
    matches = []
    errors = []
    for process in proc.iterdir():
        if not process.name.isdigit():
            continue
        try:
            argv = process.joinpath('cmdline').read_bytes().decode().rstrip('\0').split('\0')
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
        command = patroni_command(argv)
        if command is None:
            continue
        try:
            member = invoke_member(process, command, script, 'inspect', data_path, timeout)
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
            errors.append(str(exc))
            continue
        if member:
            member['process_id'] = int(process.name)
            matches.append(member)
    if len(matches) == 1:
        return matches[0]
    if matches:
        raise RuntimeError('Multiple Patroni processes manage the selected data directory')
    if errors:
        raise RuntimeError(errors[0])
    data = Path(data_path)
    config = data / 'postgresql.conf'
    if (data / 'patroni.dynamic.json').exists() or (
        config.exists() and 'overwritten by Patroni' in config.read_text()
    ):
        raise RuntimeError(
            'Patroni data directory detected, but its running process could not be identified; '
            'check process/configuration access on the selected database host'
        )
    return None


def main():
    action, data_path, script, timeout = sys.argv[1:5]
    timeout = float(timeout)
    # Container root usually lacks CAP_SYS_PTRACE and cannot read another UID's
    # /proc/*/environ. Use the PGDATA owner, as PostgreSQL and Patroni do.
    if os.geteuid() == 0:
        owner = pwd.getpwuid(Path(data_path).stat().st_uid)
        if owner.pw_uid != 0:
            os.initgroups(owner.pw_name, owner.pw_gid)
            os.setgid(owner.pw_gid)
            os.setuid(owner.pw_uid)
    if action == 'detect':
        result = discover(data_path, script, timeout)
    else:
        process = Path('/proc') / str(int(sys.argv[5]))
        argv = process.joinpath('cmdline').read_bytes().decode().rstrip('\0').split('\0')
        command = patroni_command(argv)
        if command is None:
            raise RuntimeError('The detected Patroni process is no longer running')
        result = invoke_member(process, command, script, action, data_path, timeout)
        if result is None:
            raise RuntimeError('The detected Patroni process no longer manages this data directory')
    print(json.dumps(result))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # No traceback: configuration values and process environments stay private.
        print(json.dumps({'error': str(exc)}))
