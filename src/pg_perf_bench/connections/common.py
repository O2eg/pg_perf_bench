from pg_perf_bench.connections.docker import DockerConnection
from pg_perf_bench.connections.local import LocalConnection
from pg_perf_bench.connections.ssh import SSHConnection
from pg_perf_bench.const import ConnectionType


def get_connection(type):
    if type == ConnectionType.SSH:
        return SSHConnection
    if type == ConnectionType.DOCKER:
        return DockerConnection
    if type == ConnectionType.LOCAL:
        return LocalConnection
