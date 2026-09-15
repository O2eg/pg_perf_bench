import asyncio
import json
from copy import deepcopy
from unittest.mock import AsyncMock, MagicMock

import pytest

from pg_perf_bench.cli import _collection_warnings
from pg_perf_bench.const import REPORT_TEMPLATE_FOLDER, SQL_COMMANDS_PATH
from pg_perf_bench.managed import add_managed_report_metadata
from pg_perf_bench.report.commands import fill_info_report, run_sql_command
from pg_perf_bench.report.processing import get_report_structure, save_report


def replication_section(template='benchmark_report_struct.json'):
    return get_report_structure(REPORT_TEMPLATE_FOLDER / template)['sections']['replication']


def test_replication_items_are_present_in_all_database_reports_and_packaged():
    section = replication_section()
    for template in ('db_info_report_struct.json', 'all_info_report_struct.json'):
        assert replication_section(template) == section
    assert (
        'replication'
        not in get_report_structure(REPORT_TEMPLATE_FOLDER / 'sys_info_report_struct.json')[
            'sections'
        ]
    )
    for item in section['reports'].values():
        assert (SQL_COMMANDS_PATH / item['sql_command_file']).is_file()


@pytest.mark.parametrize(
    'name',
    [
        'replication_senders',
        'replication_slots',
        'replication_receiver',
        'replication_subscriptions',
        'replication_commit_overrides',
    ],
)
def test_empty_replication_state_has_specific_message_without_warning(name):
    item = deepcopy(replication_section()['reports'][name])
    asyncio.run(run_sql_command(MagicMock(), MagicMock(fetch=AsyncMock(return_value=[])), item))
    assert item['data'] == item['empty_message']
    assert item['collection_status'] == 'empty'
    assert item['item_type'] == 'plain_text'
    assert _collection_warnings({'sections': {'replication': {'reports': {name: item}}}}) == []


def test_replication_permission_failure_is_not_reported_as_no_replication():
    item = deepcopy(replication_section()['reports']['replication_slots'])
    db = MagicMock(fetch=AsyncMock(side_effect=PermissionError('permission denied')))
    asyncio.run(run_sql_command(MagicMock(), db, item))
    assert item['collection_status'] == 'error'
    assert 'permission denied' in item['data']
    assert item['data'] != item['empty_message']


def test_managed_replication_is_collected_via_sql_and_visible_in_json_and_html(tmp_path):
    report = get_report_structure(REPORT_TEMPLATE_FOLDER / 'benchmark_report_struct.json')
    add_managed_report_metadata(report, {'encoding': 'utf-8', 'content': 'cloud instance'})
    section = report['sections']['replication']
    assert all('sql_command_file' in item for item in section['reports'].values())
    report = {
        'header': 'Report',
        'report_name': 'replication',
        'sections': {'replication': section},
    }

    class Record(dict):
        def __iter__(self):
            return iter(self.values())

    db = MagicMock(fetch=AsyncMock(return_value=[Record(property='Server role', value='primary')]))
    asyncio.run(fill_info_report(MagicMock(), None, db, {'managed_postgresql': True}, report))
    assert db.fetch.await_count == len(section['reports'])
    paths = save_report(MagicMock(), report, tmp_path)
    saved = json.loads((tmp_path / 'replication.json').read_text())
    assert saved['sections']['replication']['reports']['replication_mode']['data'] == [
        ['Server role', 'primary'],
    ]
    assert (
        'Replication mode and commit acknowledgement' in (tmp_path / 'replication.html').read_text()
    )
    assert paths['html'].endswith('replication.html')
