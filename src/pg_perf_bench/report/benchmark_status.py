"""Execution status and timing, separate from comparable performance points."""

import json


def build_execution_section(runs, *, failed_run=None, error=None, requested_seconds=None):
    rows = []
    for run in [*runs, *([failed_run] if failed_run else [])]:
        workload = run.get('workload') or {}
        metrics = run.get('metrics') or {}
        sampler = run.get('system_metrics') or {}
        iteration = run.get('iteration') or {}
        rows.append(
            [
                iteration.get('index'),
                iteration.get('value'),
                f'cleanup_{run["status"]}'
                if run.get('failure_phase') == 'cleanup'
                else run.get('status', 'completed'),
                run.get('init_policy', 'each-iteration'),
                run.get('initialization_performed', True),
                metrics.get('duration_seconds', requested_seconds),
                workload.get('elapsed_seconds'),
                sampler.get('duration_seconds'),
                sampler.get('stop_reason'),
            ]
        )
    reports = {
        'timing': {
            'header': 'Iteration status and duration',
            'description': 'Requested duration is the pgbench scheduling window. Process time '
            'also includes connections and completion of in-flight scripts. OS sampling finishes '
            'its last interval before storage/diagnostic queries; it can extend past process exit. '
            'An explicit sampling duration limit can end OS collection earlier.',
            'item_type': 'table',
            'state': 'expanded',
            'theader': [
                'Iteration',
                'Load value',
                'Status',
                'Initialization policy',
                'Initialized this iteration',
                'Requested (s)',
                'Process elapsed (s)',
                'OS collection elapsed (s)',
                'OS collection stop reason',
            ],
            'data': rows,
        },
    }
    if error is not None:
        reports['failure'] = {
            'header': 'Benchmark did not complete',
            'state': 'expanded',
            'description': 'Only completed iterations are included in TPS comparisons. '
            'The failed iteration is diagnostic evidence, even if its stdout contains TPS '
            'or reports zero failed transactions. A cleanup failure after a completed '
            'measurement is shown separately and does not discard that measurement.',
            'item_type': 'plain_text',
            'collection_status': 'error',
            'reason': error,
            'data': error,
        }
        reports['failed_command'] = {
            'header': 'Failed iteration: raw command output',
            'state': 'collapsed',
            'item_type': 'plain_text',
            'data': json.dumps(
                (failed_run or {}).get('workload')
                or (failed_run or {}).get('failed_command')
                or {},
                indent=2,
            ),
        }
        if (failed_run or {}).get('cleanup_error'):
            reports['cleanup_error'] = {
                'header': 'Initialization cleanup error',
                'description': 'Failure while restoring initialization settings or releasing '
                'the initialization connection. Any preceding workload error is retained above.',
                'state': 'expanded',
                'item_type': 'plain_text',
                'collection_status': 'error',
                'data': json.dumps(failed_run['cleanup_error'], indent=2),
            }
    return {
        'header': 'Benchmark execution',
        'state': 'expanded',
        'description': 'Completed results and execution diagnostics. '
        'With once/skip, later iterations '
        'reuse data and cache state; writes from earlier iterations persist. Skip does not '
        'verify generator identity or scale and does not run VACUUM ANALYZE.',
        'reports': reports,
    }
