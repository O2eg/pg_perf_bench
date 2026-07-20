"""Backward-compatible imports for the unified CLI."""

from __future__ import annotations

import argparse
import sys

from pg_perf_bench.cli import (
    _legacy_argv,
    build_parser,
    execute_namespace,
    get_args_parser,
    main,
    parse_pgbench_options,
)
from pg_perf_bench.log import setup_logger

__all__ = [
    'build_parser',
    'execute_pg_perf_bench',
    'get_args_parser',
    'main',
    'parse_pgbench_options',
]


async def execute_pg_perf_bench(args: argparse.Namespace | None = None):
    """Execute an already parsed namespace for legacy Python callers."""
    if args is None:
        args = get_args_parser().parse_args(_legacy_argv(sys.argv[1:]))
    logger = setup_logger(
        args.log_level,
        args.clear_logs,
        log_dir=getattr(args, 'log_dir', None),
    )
    return await execute_namespace(args, logger)
