import copy
import difflib
import json
import os
from pathlib import Path

from pg_perf_bench.const import get_datetime_report, get_default_report_name
from pg_perf_bench.join_catalog import load_join_task
from pg_perf_bench.log import display_user_configuration
from pg_perf_bench.report.processing import parse_json_in_order


class ReportJoiner:
    """
    A stateless utility class that provides functionality for joining
    multiple JSON reports by comparing and merging their data.
    All methods are static since they do not rely on instance state.
    """

    @staticmethod
    def load_reports(
        logger, input_dir: str, reference_report: str
    ) -> tuple[list[str], list[dict]] | None:
        """
        Loads JSON reports from the input directory. If a reference report is specified,
        it is moved to the beginning of the list.
        """
        if not os.path.isdir(input_dir):
            logger.error(f'Invalid directory: {input_dir}')
            return None

        files = [f for f in sorted(os.listdir(input_dir)) if f.endswith('.json')]
        if not files:
            logger.error(f'No JSON files in {input_dir}')
            return None

        if reference_report and reference_report not in files:
            logger.error(f'Reference report not found: {reference_report}')
            return None
        if reference_report in files:
            idx = files.index(reference_report)
            files[0], files[idx] = files[idx], files[0]

        return ReportJoiner.load_report_paths(
            logger,
            [os.path.join(input_dir, name) for name in files],
            reference_report,
        )

    @staticmethod
    def load_report_paths(
        logger,
        report_paths: list[str],
        reference_report: str | None,
    ) -> tuple[list[str], list[dict]] | None:
        """Load only explicitly selected reports, preserving deterministic order."""
        paths = [Path(value).expanduser().resolve() for value in report_paths]
        if len(paths) != len(set(paths)):
            logger.error('Duplicate report path supplied.')
            return None
        names = [path.name for path in paths]
        if len(names) != len(set(names)):
            logger.error('Every explicitly selected report must have a unique file name.')
            return None
        ordered = sorted(paths, key=lambda path: (path.name, str(path)))
        if reference_report:
            reference = Path(reference_report).expanduser()
            matches = [
                path
                for path in ordered
                if path == reference.resolve() or path.name == reference.name
            ]
            if len(matches) != 1:
                logger.error(f'Reference report not found or ambiguous: {reference_report}')
                return None
            selected = matches[0]
            ordered.remove(selected)
            ordered.insert(0, selected)

        loaded_names = []
        reports = []
        for path in ordered:
            try:
                with path.open(encoding='utf-8') as rf:
                    data = json.load(rf)
                    if isinstance(data, dict):
                        loaded_names.append(path.name)
                        reports.append(data)
                    elif reference_report and path.name == Path(reference_report).name:
                        logger.error(f'Reference report is not a JSON object: {path.name}')
                        return None
                    else:
                        logger.warning(f'Skipping non-object JSON report: {path.name}')
            except (OSError, json.JSONDecodeError) as e:
                logger.warning(f'Cannot load {path.name}: {e}')
                if reference_report and path.name == Path(reference_report).name:
                    logger.error(f'Reference report could not be loaded: {path.name}')
                    return None

        return (loaded_names, reports) if reports else None

    @staticmethod
    def load_compare_items(logger, join_tasks_file: str) -> list[str] | None:
        """
        Loads the list of items to compare from a packaged JOIN scenario.
        """
        try:
            return list(load_join_task(join_tasks_file)['items'])
        except (OSError, ValueError) as e:
            logger.error(str(e))
            return None

    @staticmethod
    def compare_data(logger, ref_data, cmp_data) -> bool:
        """
        Compares two data objects (strings or lists).
        If they differ, prints the diff to debug and returns False.
        """
        if isinstance(ref_data, str) and isinstance(cmp_data, str):
            if ref_data != cmp_data:
                diff = difflib.ndiff(ref_data.splitlines(), cmp_data.splitlines())
                logger.debug('\n'.join(diff))
                return False
            return True

        if isinstance(ref_data, list) and isinstance(cmp_data, list):
            if len(ref_data) != len(cmp_data):
                logger.debug('List length mismatch')
                return False
            for i, (r, c) in enumerate(zip(ref_data, cmp_data, strict=True)):
                if r != c:
                    logger.debug(f'Row mismatch at {i}: {r} != {c}')
                    return False
            return True

        return ref_data == cmp_data

    @staticmethod
    def _value_at_path(report: dict, dotted_path: str):
        value = report
        for component in dotted_path.split('.'):
            if not isinstance(value, dict) or component not in value:
                raise ValueError(f'Comparison item is missing: {dotted_path}')
            value = value[component]
        return value

    @staticmethod
    def compare_reports(
        logger,
        ref_rep: dict,
        cmp_rep: dict,
        compare_items: list[str],
        diff_target: dict | None = None,
        *,
        reference_label: str | None = None,
        comparison_label: str | None = None,
    ) -> bool:
        """
        Compares two reports based on their step order and data.
        Raises ValueError if a mismatch occurs in unlisted items.
        """
        ref_steps, _ = parse_json_in_order(ref_rep)
        cmp_steps, _ = parse_json_in_order(cmp_rep)
        if len(ref_steps) != len(cmp_steps):
            raise ValueError('Different step counts')

        reference_label = reference_label or ref_rep.get('report_name', 'Unnamed')
        comparison_label = comparison_label or cmp_rep.get('report_name', 'Unnamed')

        for item_path in compare_items:
            left = ReportJoiner._value_at_path(ref_rep, item_path)
            right = ReportJoiner._value_at_path(cmp_rep, item_path)
            if not ReportJoiner.compare_data(logger, left, right):
                raise ValueError(
                    f'Required comparison item differs: {item_path}\n'
                    f'reference report - {reference_label}\n'
                    f'comparable report - {comparison_label}'
                )

        for i, (s1, s2) in enumerate(zip(ref_steps, cmp_steps, strict=True)):
            if s1['section'] != s2['section'] or s1['report'] != s2['report']:
                raise ValueError(f'Step mismatch at {i}')
            # Skip result section comparison.
            if s1['section'] == 'result':
                continue

            left = s1['report_obj'].get('data')
            right = s2['report_obj'].get('data')
            if not ReportJoiner.compare_data(logger, left, right):
                ck = f'sections.{s1["section"]}.reports.{s1["report"]}.data'
                if ck in compare_items:
                    raise ValueError(
                        f'Required comparison item differs: "{s1["report"]}"\n'
                        f'reference report - {reference_label}\n'
                        f'comparable report - {comparison_label}'
                    )
                elif diff_target is not None:
                    target = diff_target['sections'][s1['section']]['reports'][s1['report']]
                    if target.get('join_comparison'):
                        target['data'].append([comparison_label, copy.deepcopy(right)])
                    else:
                        target['data'] = [
                            [reference_label, copy.deepcopy(left)],
                            [comparison_label, copy.deepcopy(right)],
                        ]
                        target['join_comparison'] = True
                        old_header = target.get('header', '')
                        target['header'] = f'{old_header} | Diff'
                        if target.get('item_type') != 'table':
                            target['item_type'] = 'table'
                            target['theader'] = ['report', 'value']
        return True

    @staticmethod
    def _result_reports(report: dict) -> dict:
        try:
            result_reports = report['sections']['result']['reports']
            series = result_reports['chart']['data']['series']
            outputs = result_reports['pgbench_outputs']['data']
        except (KeyError, TypeError) as exc:
            raise ValueError('Report has no complete benchmark result structure') from exc
        if not isinstance(result_reports, dict):
            raise ValueError('Result reports must be an object')
        if not isinstance(series, list) or not series:
            raise ValueError('Benchmark chart must contain at least one series')
        if not all(isinstance(item, dict) for item in series):
            raise ValueError('Every benchmark chart series must be an object')
        if not isinstance(outputs, list):
            raise ValueError('pgbench_outputs data must be a list')
        return result_reports

    @staticmethod
    def _maximum_tps(report: dict) -> dict | None:
        maximum = report.get('maximum_tps')
        if isinstance(maximum, dict):
            return copy.deepcopy(maximum)
        candidates = [
            run
            for run in report.get('benchmark_runs', [])
            if isinstance(run, dict)
            and isinstance(run.get('metrics'), dict)
            and isinstance(run['metrics'].get('tps'), (int, float))
            and not isinstance(run['metrics'].get('tps'), bool)
        ]
        if not candidates:
            return None
        best = max(candidates, key=lambda run: float(run['metrics']['tps']))
        return {
            'tps': best['metrics']['tps'],
            'iteration': copy.deepcopy(best.get('iteration')),
            'metrics': copy.deepcopy(best['metrics']),
        }

    @staticmethod
    def add_result(base: dict, inc: dict, *, source_label: str | None = None) -> None:
        """
        Adds performance results from an incremental report into the base report.
        """
        base_result = ReportJoiner._result_reports(base)
        inc_result = ReportJoiner._result_reports(inc)
        source_label = source_label or inc.get('report_name', 'Unnamed')
        chart_series = base_result['chart']['data']['series']
        inc_series = inc_result['chart']['data']['series']
        for incoming_series in inc_series:
            series = copy.deepcopy(incoming_series)
            series['name'] = source_label
            chart_series.append(series)

        base_outputs = base_result['pgbench_outputs']
        inc_outputs = inc_result['pgbench_outputs']
        base_outputs['data'].append([source_label, copy.deepcopy(inc_outputs['data'])])

        base_logs = base_result.get('logs', {})
        inc_logs = inc_result.get('logs', {})

        if base_logs == {} and inc_logs != {}:
            base_result['logs'] = {
                'header': 'database logs',
                'description': 'Local path to the database log archive',
                'item_type': 'link',
                'state': 'collapsed',
                'python_command': '',
                'data': [],
            }
            base_logs = base_result['logs']

        if isinstance(base_logs.get('data'), str):
            base_logs['data'] = [[base.get('report_name', 'Unnamed'), base_logs['data']]]
        if isinstance(base_logs.get('data'), list) and isinstance(inc_logs.get('data'), str):
            base_logs['data'].append([source_label, inc_logs['data']])

    @staticmethod
    def label_chart_groups(report: dict, source_label: str) -> None:
        """Attach a source report label to every vertically rendered chart block."""
        for section in (report.get('sections') or {}).values():
            if not isinstance(section, dict):
                continue
            for item in (section.get('reports') or {}).values():
                if not isinstance(item, dict) or item.get('item_type') != 'chart_group':
                    continue
                for block in item.get('data') or []:
                    if isinstance(block, dict):
                        block['report_name'] = source_label

    @staticmethod
    def add_chart_groups(base: dict, inc: dict, source_label: str) -> None:
        """Stack pg_diag OS charts instead of converting them to diff tables."""
        base_sections = base.get('sections') or {}
        incoming_sections = inc.get('sections') or {}
        for section_name, base_section in base_sections.items():
            if not isinstance(base_section, dict):
                continue
            incoming_reports = (incoming_sections.get(section_name) or {}).get('reports') or {}
            for item_name, base_item in (base_section.get('reports') or {}).items():
                if not isinstance(base_item, dict) or base_item.get('item_type') != 'chart_group':
                    continue
                incoming_item = incoming_reports.get(item_name)
                if (
                    not isinstance(incoming_item, dict)
                    or incoming_item.get('item_type') != 'chart_group'
                ):
                    raise ValueError(
                        f'Incoming report is missing compatible chart group '
                        f'{section_name}.{item_name}'
                    )
        for section_name, incoming_section in incoming_sections.items():
            if not isinstance(incoming_section, dict):
                continue
            incoming_reports = incoming_section.get('reports') or {}
            base_reports = (base_sections.get(section_name) or {}).get('reports') or {}
            for item_name, incoming_item in incoming_reports.items():
                if (
                    not isinstance(incoming_item, dict)
                    or incoming_item.get('item_type') != 'chart_group'
                ):
                    continue
                target = base_reports.get(item_name)
                if not isinstance(target, dict) or target.get('item_type') != 'chart_group':
                    raise ValueError(
                        f'Joined report is missing compatible chart group '
                        f'{section_name}.{item_name}'
                    )
                for raw_block in incoming_item.get('data') or []:
                    block = copy.deepcopy(raw_block)
                    if isinstance(block, dict):
                        block['report_name'] = source_label
                    target.setdefault('data', []).append(block)

    @staticmethod
    def rebase_log_links(report: dict, source_report: Path, destination: Path) -> None:
        """Make local log links relative to the directory of the joined report."""
        try:
            logs = report['sections']['result']['reports']['logs']
        except (KeyError, TypeError):
            return
        if not isinstance(logs, dict) or logs.get('item_type') != 'link':
            return

        source_directory = source_report.expanduser().resolve().parent
        destination_directory = destination.expanduser().resolve()

        def rebase(value):
            if isinstance(value, str) and value:
                path = Path(value).expanduser()
                if not path.is_absolute():
                    path = source_directory / path
                return os.path.relpath(path.resolve(), destination_directory)
            if isinstance(value, list):
                return [
                    [entry[0], rebase(entry[1]), *entry[2:]]
                    if isinstance(entry, list) and len(entry) >= 2
                    else entry
                    for entry in value
                ]
            return value

        logs['data'] = rebase(logs.get('data'))

    @staticmethod
    def merge_reports(
        logger,
        names: list[str],
        reports: list[dict],
        compare_items: list[str],
        *,
        raise_on_error: bool = False,
    ) -> dict | None:
        """
        Merges multiple reports into one consolidated report.
        The first report is considered the reference and subsequent reports are compared against it.
        """
        if not reports or len(names) != len(reports):
            logger.error('Report names and report objects are missing or misaligned')
            return None
        baseline = reports[0]
        ref = copy.deepcopy(baseline)
        if not isinstance(ref, dict):
            logger.error('Invalid reference report')
            return None
        source_labels = [
            str(report.get('report_name') or name)
            for name, report in zip(names, reports, strict=True)
        ]
        if len(set(source_labels)) != len(source_labels):
            logger.error('Every joined report must have a unique report_name')
            return None
        baseline_schema = baseline.get('artifact_schema_version')
        for report in reports[1:]:
            if report.get('artifact_schema_version') != baseline_schema:
                logger.error('Reports use incompatible artifact_schema_version values')
                return None
        try:
            ref_result = ReportJoiner._result_reports(ref)
            ref_chart = ref_result['chart']['data']['series']
            for series in ref_chart:
                series['name'] = source_labels[0]
            ref_pgbench = ref_result['pgbench_outputs']
            ref_pgbench['data'] = [
                [
                    source_labels[0],
                    ref_pgbench.get('data', []),
                ]
            ]
            if 'logs' in ref_result:
                ref_logs = ref_result['logs']
                ref_logs['data'] = [
                    [
                        source_labels[0],
                        ref_logs.get('data', ''),
                    ]
                ]
            ReportJoiner.label_chart_groups(ref, source_labels[0])
        except ValueError as exc:
            logger.error(str(exc))
            return None

        for index, (_name, report) in enumerate(zip(names, reports, strict=True)):
            if index == 0:
                continue
            try:
                ReportJoiner._result_reports(report)
                ReportJoiner.compare_reports(
                    logger,
                    baseline,
                    report,
                    compare_items,
                    diff_target=ref,
                    reference_label=source_labels[0],
                    comparison_label=source_labels[index],
                )
            except ValueError as ve:
                message = f'Comparison failed: {ve}'
                logger.error(message)
                if raise_on_error:
                    raise ValueError(message) from ve
                return None
            ReportJoiner.add_result(ref, report, source_label=source_labels[index])
            try:
                ReportJoiner.add_chart_groups(ref, report, source_labels[index])
            except ValueError as exc:
                logger.error(str(exc))
                return None

        evidence = [
            {
                'report_name': label,
                'benchmark_runs': copy.deepcopy(report.get('benchmark_runs', [])),
            }
            for label, report in zip(source_labels, reports, strict=True)
            if 'benchmark_runs' in report
        ]
        if evidence:
            ref['joined_benchmark_runs'] = evidence
        maximum_tps = []
        for label, report in zip(source_labels, reports, strict=True):
            maximum = ReportJoiner._maximum_tps(report)
            if maximum is not None:
                maximum_tps.append({'report_name': label, 'maximum_tps': maximum})
        if maximum_tps:
            ref['joined_maximum_tps'] = maximum_tps

        ref['header'] = f'Result of joined reports {get_datetime_report("%d/%m/%Y %H:%M:%S")}'
        return ref

    @staticmethod
    def join_reports(
        raw_args: dict,
        join_tasks: str,
        reference_report: str,
        input_dir: str,
        report_name: str,
        logger,
        report_paths: list[str] | None = None,
        raise_on_error: bool = False,
    ) -> dict | None:
        """
        Main method that joins multiple reports:
          1. Displays run parameters.
          2. Loads join tasks items.
          3. Loads and orders JSON reports from the input directory.
          4. Merges the reports based on compare items.
          5. Sets the final report name and description.
        Returns the joined report or None in case of failure.
        """
        if raw_args and isinstance(raw_args, dict):
            display_user_configuration(raw_args, logger)

        if not join_tasks:
            logger.error('No join_tasks specified.')
            return None

        try:
            task = load_join_task(join_tasks)
        except (OSError, ValueError) as exc:
            logger.error(str(exc))
            return None
        compare_items = list(task['items'])
        if not compare_items:
            logger.error('No compare_items found.')
            return None

        tasks_list = '\n'.join(compare_items)
        logger.info(f'Compare items "{join_tasks}" loaded successfully:\n{tasks_list}')

        loaded = (
            ReportJoiner.load_report_paths(logger, report_paths, reference_report)
            if report_paths
            else ReportJoiner.load_reports(logger, input_dir, reference_report)
        )
        if not loaded:
            logger.error('No reports loaded from input directory.')
            return None

        names, rpts = loaded
        logger.info(f'Loaded {len(names)} report(s): {", ".join(names)}')
        if len(rpts) < 2:
            logger.error('At least two reports are required for join mode.')
            return None

        output_directory = Path((raw_args or {}).get('output_dir') or 'report')
        source_paths = (
            {
                Path(value).expanduser().resolve().name: Path(value).expanduser().resolve()
                for value in report_paths
            }
            if report_paths
            else {name: Path(input_dir).expanduser().resolve() / name for name in names}
        )
        for name, report in zip(names, rpts, strict=True):
            ReportJoiner.rebase_log_links(report, source_paths[name], output_directory)

        joined = ReportJoiner.merge_reports(
            logger,
            names,
            rpts,
            compare_items,
            raise_on_error=raise_on_error,
        )
        if not joined:
            logger.error('Merge of reports failed.')
            return None

        logger.info('Reports merged successfully.')
        joined['report_name'] = report_name or f'join_{get_default_report_name()}'
        joined['join_metadata'] = {
            'reference_report': names[0],
            'source_reports': names,
            'join_task': task['id'],
            'join_task_schema_version': task['schema_version'],
            'join_task_title': task['title'],
            'controlled_dimensions': list(task['controlled_dimensions']),
            'variable_dimensions': list(task['variable_dimensions']),
            'comparison_items': list(compare_items),
        }

        all_names = '\n'.join(names)
        tasks_content = json.dumps(task, indent=2, ensure_ascii=False, sort_keys=True)

        joined['description'] = f'\nComparison Reports:\n{all_names}\n\nJoined by:\n{tasks_content}'
        logger.info('Join reports process completed successfully.')
        return joined
