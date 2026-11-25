"""Pytest configuration for offline-first M0 contract and integration tests."""

from __future__ import annotations

import pytest


def pytest_configure(config: pytest.Config) -> None:
    """Ensure live/local-model markers are always registered even when invoked standalone."""
    config.addinivalue_line(
        "markers",
        "provider_live: tests requiring explicit cloud egress consent, credentials, and budget",
    )
    config.addinivalue_line(
        "markers",
        "model_local: tests requiring pre-downloaded local model weights under offline isolation",
    )
