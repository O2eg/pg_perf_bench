"""Apply a SQL workload timeout even when a pooler ignores startup PGOPTIONS."""

from __future__ import annotations

import math
import shlex
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from pg_perf_bench.errors import ConfigurationError


def timeout_milliseconds(seconds):
    value = float(seconds)
    if not math.isfinite(value) or not 0 < value <= 2147483.647:
        raise ConfigurationError(
            'statement timeout must be positive and fit PostgreSQL milliseconds'
        )
    return math.ceil(value * 1000)


def script_arguments(command):
    args = shlex.split(command)
    if not args or not Path(args[0]).name.startswith('pgbench'):
        raise ConfigurationError('--statement-timeout-seconds requires a direct pgbench -f command')
    if any(token in {';', '&&', '||', '|', '>', '<', '&'} for token in args):
        raise ConfigurationError('SQL statement timeout does not support compound shell commands')
    scripts = []
    i = 1
    while i < len(args):
        token = args[i]
        i += 1
        if (
            token in ('-b', '--builtin')
            or token.startswith('--builtin=')
            or (token.startswith('-b') and token != '-b')
        ):
            raise ConfigurationError('--statement-timeout-seconds requires SQL files, not builtins')
        prefix = ''
        if token in ('-f', '--file'):
            if i == len(args):
                raise ConfigurationError('pgbench file argument is missing')
            index, value = i, args[i]
            i += 1
        elif token.startswith('--file='):
            index, value, prefix = i - 1, token[len('--file=') :], '--file='
        elif token.startswith('-f') and token != '-f':
            index, value, prefix = i - 1, token[2:], '-f'
        else:
            continue
        path, weight = value, ''
        if '@' in value and value.rsplit('@', 1)[1].isdigit():
            path, suffix = value.rsplit('@', 1)
            weight = '@' + suffix
        if not Path(path).expanduser().is_file():
            raise ConfigurationError(f'Cannot apply SQL timeout: script does not exist: {path}')
        scripts.append((index, Path(path).expanduser(), prefix, weight))
    if not scripts:
        raise ConfigurationError(
            '--statement-timeout-seconds requires at least one pgbench SQL file'
        )
    return args, scripts


@contextmanager
def bounded_workload_command(command, seconds):
    if seconds is None:
        yield command
        return
    milliseconds = timeout_milliseconds(seconds)
    args, scripts = script_arguments(command)
    with TemporaryDirectory(prefix='pg-perf-timeout-') as directory:
        for number, (index, source, prefix, weight) in enumerate(scripts):
            target = Path(directory) / str(number) / source.name
            target.parent.mkdir()
            target.write_text(
                f'SET statement_timeout={milliseconds};\n'
                + source.read_text(encoding='utf-8')
                + '\nRESET statement_timeout;\n',
                encoding='utf-8',
            )
            args[index] = prefix + str(target) + weight
        yield shlex.join(args)
