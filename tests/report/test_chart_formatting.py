import json
import shutil
import subprocess

import pytest

from pg_perf_bench.const import REPORT_TEMPLATE_FOLDER


def evaluate_chart_helpers(expression):
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node.js is required to execute chart formatting helpers')
    template = (REPORT_TEMPLATE_FOLDER / 'report.html').read_text(encoding='utf-8')
    helpers = template[
        template.index('      function chartAxisScale(') : template.index(
            '      const chartOption = '
        )
    ]
    result = subprocess.run(
        [
            node,
            '-e',
            'const asText = value => String(value ?? "");\n'
            + helpers
            + '\nconsole.log(JSON.stringify('
            + expression
            + '));',
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def test_chart_units_match_pg_diag_and_use_stack_total():
    result = evaluate_chart_helpers("""(() => {
      const cases = [
        ['bytes', null, 64 * 1024 ** 3, false],
        ['bytes/s', null, 19162.5, false],
        ['iops', null, 12000, false],
        ['count/s', 'transactions', 140000, false],
        ['packets/s', null, 12345, false],
        ['transactions/s', null, 140000, false],
        ['ms', null, 0.125, false],
        ['%', null, 12.3456, false],
        ['bytes', null, 600, true],
        ['bytes', null, 0, false],
      ];
      return cases.map(([unit, quantity, value, stacked]) => {
        const series = [{data: [{x: 1, y: value}]}];
        if (stacked) series.push({data: [{x: 1, y: value}]});
        const original = JSON.stringify(series);
        const scale = chartAxisScale(series, unit, quantity, stacked);
        return [scale.label, formatChartAxisValue(value, unit, scale),
          formatChartTooltipValue(value, unit, scale), JSON.stringify(series) === original];
      });
    })()""")
    assert result == [
        ['GiB', '64', '64 GiB', True],
        ['KiB/s', '18.713', '18.713 KiB/s', True],
        ['kIOPS', '12', '12 kIOPS', True],
        ['ktransactions/s', '140', '140 ktransactions/s', True],
        ['kpackets/s', '12.345', '12.345 kpackets/s', True],
        ['ktransactions/s', '140', '140 ktransactions/s', True],
        ['ms', '0.125', '0.125 ms', True],
        ['%', '12.35', '12.35%', True],
        ['KiB', '0.586', '0.586 KiB', True],
        ['B', '0', '0 B', True],
    ]


def test_tooltip_uses_axis_units_sorts_values_and_omits_missing_samples():
    tooltip = evaluate_chart_helpers("""chartTooltip([
      {seriesName: 'missing', value: ['point', null], seriesIndex: 0},
      {seriesName: 'small', value: ['point', 1024], seriesIndex: 1},
      {seriesName: '<large>', value: ['point', 2048], seriesIndex: 2},
    ], 'bytes', 'category', {factor: 1024, label: 'KiB'})""")
    assert 'missing' not in tooltip
    assert '2 KiB' in tooltip and '1 KiB' in tooltip
    assert tooltip.index('&lt;large&gt;') < tooltip.index('small')
    assert '<large>' not in tooltip
