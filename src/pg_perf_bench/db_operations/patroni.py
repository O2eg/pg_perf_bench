"""Transport-independent Patroni detection and PostgreSQL restart."""

from __future__ import annotations

import json
import shlex
from datetime import datetime
from pathlib import Path

from pg_perf_bench.errors import ConfigurationError

MIN_REMOTE_PYTHON = (3, 10)
PYTHON_REQUIREMENT = (
    'Patroni detection and control require Python 3.10 or newer on the database host'
)


def _checked_script(source):
    # Check before parsing the helper: older interpreters cannot even import it.
    return (
        'import json, sys\n'
        f'if sys.version_info < {MIN_REMOTE_PYTHON!r}:\n'
        f'    print(json.dumps({{"error": {PYTHON_REQUIREMENT!r}}}))\n'
        'else:\n'
        f'    exec({source!r})\n'
    )


class PatroniController:
    def __init__(self, conn, data_path, member):
        self.conn = conn
        self.data_path = data_path
        self.member = member

    @staticmethod
    async def _command(conn, data_path, action, process_id=None):
        timeout = float(getattr(conn, 'command_timeout', 300.0))
        root = Path(__file__).parent
        probe = _checked_script((root / 'patroni_probe.py').read_text())
        member = _checked_script((root / 'patroni_member.py').read_text())
        arguments = ['-c', probe, action, str(data_path), member, str(timeout)]
        if process_id is not None:
            arguments.append(str(process_id))
        # Bare PostgreSQL images need not install Python. If Patroni uses a venv
        # outside PATH, its running Python executable can run the stdlib probe.
        version_check = shlex.quote(
            f'import sys; sys.exit(sys.version_info < {MIN_REMOTE_PYTHON!r})'
        )
        command = (
            'patroni_python=$(command -v python3 || true)\n'
            'patroni_python_found=$patroni_python\n'
            'if [ -n "$patroni_python" ] && ! "$patroni_python" -c '
            + version_check
            + ' >/dev/null 2>&1; then patroni_python=; fi\n'
            'if [ -z "$patroni_python" ]; then\n'
            '  for patroni_exe in /proc/[0-9]*/exe; do\n'
            '    patroni_candidate=$(readlink "$patroni_exe" 2>/dev/null) || continue\n'
            '    case "$patroni_candidate" in */python*)\n'
            '      [ "$patroni_candidate" = "$patroni_python_found" ] && continue\n'
            '      patroni_python_found=$patroni_candidate\n'
            '      if "$patroni_candidate" -c '
            + version_check
            + ' >/dev/null 2>&1; then patroni_python=$patroni_candidate; break; fi;;\n'
            '    esac\n'
            '  done\n'
            'fi\n'
            'if [ -n "$patroni_python" ]; then\n'
            '  exec "$patroni_python" ' + shlex.join(arguments) + '\n'
            'fi\n'
            'if [ -n "$patroni_python_found" ]; then\n'
            '  echo ' + shlex.quote(json.dumps({'error': PYTHON_REQUIREMENT})) + '\n'
            '  exit 0\n'
            'fi\n'
            'if [ -f '
            + shlex.quote(str(data_path).rstrip('/') + '/patroni.dynamic.json')
            + ' ] || grep -q "overwritten by Patroni" '
            + shlex.quote(str(data_path).rstrip('/') + '/postgresql.conf')
            + ' 2>/dev/null; then\n'
            '  echo \'{"error":"Patroni detected but its Python interpreter is inaccessible"}\'\n'
            'else\n'
            '  echo null\n'
            'fi'
        )
        response = json.loads(await conn.run_command(command, check=True, timeout=timeout))
        if response is not None and not isinstance(response, dict):
            raise RuntimeError('Invalid Patroni discovery response')
        if response and response.get('error'):
            raise RuntimeError(response['error'])
        required = {'name', 'scope', 'data_directory', 'postmaster_start_time'}
        if action == 'detect':
            required.add('process_id')
        if response is not None and not required.issubset(response):
            raise RuntimeError('Incomplete Patroni discovery response')
        return response

    @classmethod
    async def detect(cls, conn, data_path):
        member = await cls._command(conn, data_path, 'detect')
        return cls(conn, data_path, member) if member else None

    @staticmethod
    def validate_options(workload_conf):
        if workload_conf.get('pg_custom_config'):
            raise ConfigurationError(
                '--pg-custom-config cannot overwrite Patroni-managed postgresql.conf; '
                'apply PostgreSQL settings through Patroni before benchmarking'
            )
        if workload_conf.get('drop_os_caches'):
            raise ConfigurationError(
                '--drop-os-caches requires PostgreSQL to remain stopped and is not '
                'supported with Patroni; run without this option'
            )

    async def verify_database(self, db_tasks):
        async with db_tasks._connect('postgres') as db:
            start_time = await db.fetchval('SELECT pg_postmaster_start_time()')
            in_recovery = await db.fetchval('SELECT pg_is_in_recovery()')
        member_time = datetime.fromisoformat(self.member['postmaster_start_time'])
        if in_recovery or start_time != member_time:
            raise ConfigurationError(
                'The SQL connection does not reach the detected Patroni primary; '
                'use a direct connection to the member on the selected database host'
            )
        return start_time

    async def restart(self, db_tasks, logger):
        previous_start = await self.verify_database(db_tasks)
        logger.info(
            'Restarting PostgreSQL through Patroni: cluster=%s member=%s.',
            self.member['scope'],
            self.member['name'],
        )
        await self._command(self.conn, self.data_path, 'restart', self.member['process_id'])
        await db_tasks.check_db_access()
        self.member = await self._command(self.conn, self.data_path, 'detect')
        if not self.member:
            raise RuntimeError('Patroni disappeared after PostgreSQL restart')
        current_start = await self.verify_database(db_tasks)
        if current_start <= previous_start:
            raise RuntimeError('Patroni did not restart the selected PostgreSQL instance')
