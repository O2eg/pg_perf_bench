"""Remote helper executed with the installed Patroni interpreter (not imported locally)."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def member_action(action, data_path, config_path, timeout):
    from patroni.config import Config
    from patroni.request import PatroniRequest

    config = Config(config_path, validator=None)
    data_dir = config.get('postgresql', {}).get('data_dir')
    if not data_dir or Path(data_dir).resolve() != Path(data_path).resolve():
        return None
    restapi = config.get('restapi', {})
    address = restapi.get('connect_address') or restapi.get('listen')
    if not address:
        raise RuntimeError('Patroni REST API address is missing')
    host, port = address.rsplit(':', 1)
    if host in {'0.0.0.0', '*'}:
        host = '127.0.0.1'
    elif host in {'::', '[::]'}:
        host = '[::1]'
    elif ':' in host and not host.startswith('['):
        host = f'[{host}]'
    scheme = 'https' if restapi.get('certfile') else 'http'
    base_url = f'{scheme}://{host}:{int(port)}'
    http = PatroniRequest(config)

    def request(method, endpoint, body=None):
        response = http.request(
            method, base_url + endpoint, body, timeout=timeout, retries=False, redirect=False
        )
        if response.status != 200:
            raise RuntimeError(f'Patroni {method} {endpoint} returned HTTP {response.status}')
        return response

    status = json.loads(request('GET', '/patroni').data)
    identity = status.get('patroni', {})
    if identity.get('name') != config.get('name') or identity.get('scope') != config.get('scope'):
        raise RuntimeError('Patroni REST API belongs to a different member')
    if status.get('state') != 'running' or status.get('role') not in {'master', 'primary'}:
        raise RuntimeError('The selected Patroni member is not a running primary')
    if action == 'restart':
        # A synchronous restart of this primary only. Never retry POST:
        # a lost response does not mean PostgreSQL was not restarted.
        request('POST', '/restart', {'role': status['role']})
    return {
        'name': config['name'],
        'scope': config['scope'],
        'data_directory': str(Path(data_dir).resolve()),
        'postmaster_start_time': status['postmaster_start_time'],
    }


if __name__ == '__main__':
    try:
        result = member_action(*sys.argv[1:4], float(sys.argv[4]))
    except RuntimeError as exc:
        result = {'error': str(exc)}
    except Exception as exc:
        result = {
            'error': 'Cannot read Patroni configuration or contact its REST API '
            f'({type(exc).__name__}); check configuration access, API address and TLS settings'
        }
    print(json.dumps(result))
