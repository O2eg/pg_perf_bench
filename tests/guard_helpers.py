"""Async controller double for tests of unrelated benchmark/report boundaries."""

from unittest.mock import AsyncMock, MagicMock


def mock_guard(*args, **kwargs):
    return MagicMock(
        open=AsyncMock(),
        close=AsyncMock(),
        assert_held=AsyncMock(),
        reopen_after_restart=AsyncMock(),
        replicas={},
    )
