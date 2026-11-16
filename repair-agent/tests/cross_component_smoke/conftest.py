"""The `slow` marker of spec §16's XC5, and the rule that makes a test marked with it opt-in.

A `slow` test runs for minutes on real processes, so it runs only when the marker is selected, for example with
`uv run pytest -m slow tests/cross_component_smoke`; otherwise it is reported as skipped.
"""

from __future__ import annotations

import re

import pytest

SLOW_SELECTED = re.compile(r"(?<!not )\bslow\b")


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "slow: an opt-in end-to-end test that runs for minutes; select it with -m slow")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if SLOW_SELECTED.search(config.getoption("markexpr") or ""):
        return
    skip = pytest.mark.skip(reason="opt-in: select it with -m slow")
    for item in items:
        if item.get_closest_marker("slow") is not None:
            item.add_marker(skip)
