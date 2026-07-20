from .common import get_connection
from .docker import DockerConnection
from .local import LocalConnection
from .ssh import SSHConnection

__all__ = [
    'DockerConnection',
    'LocalConnection',
    'SSHConnection',
    'get_connection',
]
