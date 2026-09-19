import asyncio
import json
from unittest.mock import AsyncMock, Mock, patch

import pytest

from pg_perf_bench.cli import _collection_warnings
from pg_perf_bench.report.commands import run_shell_command

# Reduced examples of the lshw 02.18.x defects also covered by pg-diag.
LEGACY_CASES = [
    pytest.param(
        '[{"id":"host","capabilities":{"smp":true} {"id":"pnp00:00","class":"system"},\n]',
        [{'id': 'host', 'capabilities': {'smp': True}}, {'id': 'pnp00:00', 'class': 'system'}],
        id='unterminated-parent-before-child',
    ),
    pytest.param(
        '[{"id":"host","class":"system","capabilities":{"smp":true}\n]',
        [{'id': 'host', 'class': 'system', 'capabilities': {'smp': True}}],
        id='unterminated-last-object',
    ),
    pytest.param(
        '[{"id":"pci:0","clock":33000000 {"id":"pci:0"}, {"id":"isa"},\n},\n{"id":"pci:1"}]',
        [{'id': 'pci:0', 'clock': 33000000}, {'id': 'pci:0'}, {'id': 'isa'}, {'id': 'pci:1'}],
        id='orphan-parent-terminator-between-children',
    ),
    pytest.param(
        '[{"id":"parent","claimed":true {"id":"child"},\n}\n]',
        [{'id': 'parent', 'claimed': True}, {'id': 'child'}],
        id='orphan-parent-terminator-at-end',
    ),
    pytest.param(']\n', [], id='empty-class'),
    pytest.param(
        '[{"id":"host","description":"brace } and escaped \\" quote"\n]',
        [{'id': 'host', 'description': 'brace } and escaped " quote'}],
        id='braces-and-escaped-quotes-inside-strings',
    ),
]


def collect(output, script='lshw_system.sh'):
    conn = Mock(spec=['run_command'])
    conn.run_command = AsyncMock(return_value=output)
    item = {'shell_command_file': script, 'item_type': 'table'}
    with patch('pg_perf_bench.report.commands.get_script_text', return_value='lshw'):
        asyncio.run(run_shell_command(Mock(), conn, item))
    return item


@pytest.mark.parametrize(('output', 'records'), LEGACY_CASES)
def test_legacy_lshw_tables(output, records):
    item = collect(output)
    columns = list(dict.fromkeys(key for row in records for key in row))
    assert item['item_type'] == 'table'
    assert item['theader'] == columns
    assert item['data'] == [[row.get(key) for key in columns] for row in records]
    assert item['collection_status'] == ('ok' if records else 'empty')
    assert 'reason' not in item
    assert _collection_warnings({'sections': {'system': {'reports': {'lshw': item}}}}) == []


@pytest.mark.parametrize(('output', '_records'), LEGACY_CASES)
def test_other_shell_collectors_keep_strict_json_parsing(output, _records):
    item = collect(output, script='lsblk.sh')
    assert item['collection_status'] == 'error'
    assert item['item_type'] == 'plain_text'
    assert _collection_warnings({'sections': {'system': {'reports': {'lsblk': item}}}})


def test_valid_lshw_json_preserves_nested_data_and_string_contents():
    records = [
        {
            'id': 'host',
            'description': '} { and true {',
            'capabilities': {'quoted': '"', 'children': [{'size': 42}]},
        }
    ]
    with patch('pg_perf_bench.report.commands.parse_legacy_lshw_json') as repair:
        item = collect(json.dumps(records))
    repair.assert_not_called()
    assert item['collection_status'] == 'ok'
    assert item['data'] == [list(records[0].values())]


@pytest.mark.parametrize(
    'value',
    [
        'literal } { marker',
        'true { false { null { 42 { -3.5 {',
        '}, }, { and , } ] and , ]',
        'escaped " quote, backslash \\, unicode оборудование, } {',
        '"0" "1" "999"',
    ],
)
@pytest.mark.parametrize('broken_parent', [False, True])
def test_legacy_repair_preserves_string_keys_and_values(value, broken_parent):
    records = [{'id': 'host', value: {'description': value}}]
    if broken_parent:
        output = json.dumps(records)[0:-2] + ' {"id":"child"},\n]'
        records.append({'id': 'child'})
    else:
        output = json.dumps(records)[0:-2] + '\n]'
    item = collect(output)
    columns = list(dict.fromkeys(key for row in records for key in row))
    assert item['collection_status'] == 'ok'
    assert item['theader'] == columns
    assert item['data'] == [[row.get(key) for key in columns] for row in records]


@pytest.mark.parametrize('output', ['[{"broken": }]', '[{"id":"host"', 'not JSON', ''])
def test_unrecoverable_lshw_output_remains_an_error(output):
    with pytest.raises(json.JSONDecodeError) as original:
        json.loads(output)
    item = collect(output)
    assert item['collection_status'] == 'error'
    assert item['reason'] == str(original.value)


def test_invalid_table_rows_still_produce_partial_status():
    item = collect('[{"id":"host"}, 42]')
    assert item['collection_status'] == 'partial'
    assert item['data'] == [['host']]
    assert 'Skipped 1' in item['reason']
