#!/usr/bin/env python3
"""Sync this source checkout to the benchmark sidecar without building a package."""

import argparse
import shlex
import subprocess
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', required=True)
    parser.add_argument('--key', type=Path, required=True)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    if args.host.startswith('-') or any(c.isspace() for c in args.host):
        parser.error('host must be a single SSH destination')
    source = Path(__file__).resolve().parents[1]
    assert (source / 'src/pg_perf_bench/cli.py').is_file()
    assert (source / 'pyproject.toml').is_file()
    ssh = ['ssh', '-i', str(args.key), '-o', 'BatchMode=yes']

    def remote(code):
        result = subprocess.run(
            [*ssh, args.host, '~/pgpb-venv/bin/python -'],
            input=code,
            text=True,
            capture_output=True,
            check=True,
        )
        print(result.stdout, end='', flush=True)
        return result.stdout

    if not args.dry_run:
        remote("""from pathlib import Path
import datetime, json, shutil
h=Path.home()
for proc in Path('/proc').glob('[0-9]*/cmdline'):
    try:
        argv=proc.read_bytes().decode(errors='replace').split('\\0')
    except (OSError, ProcessLookupError):
        continue
    if 'benchmark' in argv and any(Path(a).name in ('pg-perf-bench','pg_perf_bench') for a in argv):
        raise SystemExit('A benchmark is running; sync after it completes.')
backup=h/'bench'/('pgpb_source_sync_'+datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
backup.mkdir(parents=True)
source=h/'pg_perf_bench'
if source.exists(): shutil.copytree(source,backup/'source',symlinks=True)
pth=h/'pgpb-venv/lib/python3.12/site-packages/pg_perf_bench_source.pth'
if pth.exists(): shutil.copy2(pth,backup/pth.name)
(backup/'state.json').write_text(json.dumps({'source':str(source),'pth':str(pth),'pth_existed':pth.exists()},indent=2)+'\\n')
print('Backup:',backup)
""")
    cmd = ['rsync', '-a', '--checksum', '--delete-delay', '--itemize-changes']
    for pattern in (
        '.git',
        '__pycache__',
        '*.py[cod]',
        '*.egg-info',
        '.pytest_cache',
        '.ruff_cache',
        '/.venv/',
        '/venv/',
        '/build/',
        '/dist/',
        '/log/',
        '/report/',
        '/db_logs/',
        '/.env',
        '/.env.*',
    ):
        cmd.extend(['--exclude', pattern])
    if args.dry_run:
        cmd.append('--dry-run')
    cmd.extend(['-e', shlex.join(ssh), '--', str(source) + '/', args.host + ':pg_perf_bench/'])
    subprocess.run(cmd, check=True)
    if args.dry_run:
        return
    remote("""from pathlib import Path
import json, subprocess
h=Path.home();source=h/'pg_perf_bench/src'
pth=h/'pgpb-venv/lib/python3.12/site-packages/pg_perf_bench_source.pth'
pth.write_text('import sys; sys.path.insert(0, '+repr(str(source))+')\\n')
code=("import pg_perf_bench; from pathlib import Path; "
      "p=Path(pg_perf_bench.__file__).resolve(); "
      "assert p == Path.home()/'pg_perf_bench/src/pg_perf_bench/__init__.py', p; "
      "print('Active source:',p)")
subprocess.run([str(h/'pgpb-venv/bin/python'),'-c',code],cwd='/tmp',check=True)
subprocess.run([str(h/'pgpb-venv/bin/pg-perf-bench'),'validate'],cwd='/tmp',check=True)
""")
    print('Source synchronized; existing venv dependencies retained. No pip build/install.')


if __name__ == '__main__':
    main()
