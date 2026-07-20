import json
from unittest.mock import MagicMock

import pytest

from pg_perf_bench.report.processing import save_report


def test_save_report_is_portable_atomic_and_redacts_secret_fields(tmp_path):
    report = {
        'header': 'Smoke report',
        'report_name': 'smoke',
        'password': 'sensitive-password',
        'sections': {},
    }
    artifacts = save_report(MagicMock(), report, tmp_path)

    json_path = tmp_path / 'smoke.json'
    html_path = tmp_path / 'smoke.html'
    assert artifacts == {
        'json': str(json_path.resolve()),
        'html': str(html_path.resolve()),
    }
    saved = json.loads(json_path.read_text(encoding='utf-8'))
    assert saved['password'] == '***'
    html = html_path.read_text(encoding='utf-8')
    assert 'sensitive-password' not in html
    assert 'echarts' in html.lower()
    assert '<script src=' not in html
    assert not list(tmp_path.glob('*.tmp'))


@pytest.mark.parametrize('name', ['../outside', 'nested/report', '.', '..'])
def test_save_report_rejects_path_traversal(tmp_path, name):
    with pytest.raises(ValueError, match='Unsafe report name'):
        save_report(MagicMock(), {'report_name': name}, tmp_path)
