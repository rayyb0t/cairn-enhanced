"""Repair layer (shadow mode): the existing-endpoint script, and its clock. No tests here.

One script of calls to every existing endpoint, on its success and error paths, replayed with every Repair switch
off. Two users share it, so that they cover the same calls:

- the server tests SV43, SV44 and SV46 (test_repair_server_existing.py), which replay it in this process;
- the switch-off differential (tools/repair-phase2/switch_off_differential.py), which replays it against the base
  commit and against this tree, each in its own process, and loads this file by its path.

So this module imports nothing but the standard library: the differential's runs import the server from the tree
under test, which for the base commit has no repair code. It lives here, with the cairn tests, so that the cairn
suite runs from `cairn/` alone, as in the image the root Dockerfile builds.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

SCRIPT_CLOCK_START = datetime(2026, 11, 14, 10, 0, 0, tzinfo=UTC)


@dataclass(frozen=True)
class Call:
    method: str
    path: str
    body: Any = None
    raw: bytes | None = None  # sent as is, with Content-Type: application/json


@dataclass(frozen=True)
class Advance:
    seconds: int


def _intent(description: str, creator: str = "reasoner", worker: str | None = None, source: str = "origin") -> dict:
    return {"from": [source], "description": description, "creator": creator, "worker": worker}


# Every existing endpoint, on its success and error paths. The settings put the leases at 30 s and 40 s, and
# the two Advance steps let them lapse, so expire_workers and expire_reason_leases change rows too.
SCRIPT: list[Call | Advance] = [
    Call("GET", "/settings"),
    Call("PUT", "/settings", {"intent_timeout": 30, "reason_timeout": 40}),
    Call("PUT", "/settings", {"intent_timeout": 1, "reason_timeout": 40}),
    Call("GET", "/settings"),
    Call("POST", "/projects", {"title": "a", "origin": "o", "goal": "g", "hints": [{"content": "c", "creator": "h"}]}),
    Call("POST", "/projects", {"title": "b", "origin": "o", "goal": "g", "bootstrap_enabled": False}),
    Call("POST", "/projects", {"title": "c", "origin": "o", "goal": "g", "repair_enabled": False}),
    Call("POST", "/projects", {"title": "d", "origin": "o", "goal": "g"}),
    Call("POST", "/projects", {"title": "e", "origin": "o", "goal": "g"}),
    Call("POST", "/projects", {"title": "f", "origin": "o", "goal": "g"}),
    Call("POST", "/projects", {"title": "bad", "origin": "o", "goal": "g", "bootstrap_enabled": "sometimes"}),
    Call("POST", "/projects", {"title": " ", "origin": "o", "goal": "g"}),
    Call("POST", "/projects", {"title": "x", "origin": "o"}),
    Call("POST", "/projects", raw=b'{"title": "x",'),
    Call("GET", "/projects"),
    Call("GET", "/projects/proj_001"),
    Call("GET", "/projects/nope"),
    # Intents of proj_001: claims, refusals, concludes with and without the JSON-repair report.
    Call("POST", "/projects/proj_001/intents", _intent("d1")),
    Call("POST", "/projects/proj_001/intents", _intent("d2", creator="w", worker="w")),
    Call("POST", "/projects/proj_001/intents", _intent("bootstrap", creator="dispatcher.bootstrap")),
    Call("POST", "/projects/proj_001/intents", _intent("bad", source="goal")),
    Call("POST", "/projects/proj_001/intents", _intent("bad", worker="x")),
    Call("POST", "/projects/proj_001/intents", _intent("bad", source="f999")),
    Call("POST", "/projects/proj_001/intents", {"from": [], "description": "bad", "creator": "r"}),
    Call("POST", "/projects/proj_001/intents/i001/heartbeat", {"worker": "x"}),
    Call("POST", "/projects/proj_001/intents/i001/heartbeat", {"worker": "y"}),
    Call("POST", "/projects/proj_001/intents/i002/release", {"worker": "z"}),
    Call("POST", "/projects/proj_001/intents/i002/release", {"worker": "w"}),
    Call("POST", "/projects/proj_001/intents/i003/heartbeat", {"worker": "x"}),
    Call("POST", "/projects/proj_001/intents/i999/heartbeat", {"worker": "x"}),
    Call(
        "POST",
        "/projects/proj_001/intents/i001/conclude",
        {"worker": "x", "description": "fact", "parse_repair": {"strategy": "lenient_decode", "error": "e"}},
    ),
    Call("POST", "/projects/proj_001/intents/i003/conclude", {"worker": "x", "description": "boot fact"}),
    Call("POST", "/projects/proj_001/intents/i001/conclude", {"worker": "x", "description": "again"}),
    Call("POST", "/projects/proj_001/intents", _intent("d4", creator="w", worker="w", source="f001")),
    Advance(60),
    Call("GET", "/projects"),
    Call("POST", "/projects/proj_001/intents/i004/heartbeat", {"worker": "v"}),
    # Hints.
    Call("POST", "/projects/proj_001/hints", {"content": "more", "creator": "h"}),
    Call("POST", "/projects/proj_001/hints", {"content": " ", "creator": "h"}),
    Call("POST", "/projects/nope/hints", {"content": "x", "creator": "h"}),
    # The reason lease.
    Call("POST", "/projects/proj_001/reason/claim", {"worker": "r", "trigger": "t"}),
    Call("POST", "/projects/proj_001/reason/claim", {"worker": "s", "trigger": "t"}),
    Call("POST", "/projects/proj_001/reason/heartbeat", {"worker": "r"}),
    Call("POST", "/projects/proj_001/reason/release", {"worker": "s"}),
    Call("POST", "/projects/proj_001/reason/release", {"worker": "r"}),
    Call("POST", "/projects/proj_001/reason/heartbeat", {"worker": "r"}),
    Call("POST", "/projects/proj_001/reason/claim", {"worker": "r", "trigger": "t2"}),
    Advance(60),
    Call("POST", "/projects/proj_001/reason/claim", {"worker": "s", "trigger": "t3"}),
    Call("POST", "/projects/proj_001/reason/release", {"worker": "s"}),
    Call("POST", "/projects/nope/reason/claim", {"worker": "s", "trigger": "t"}),
    # Title and export.
    Call("PUT", "/projects/proj_001/title", {"title": "renamed"}),
    Call("PUT", "/projects/proj_001/title", {"title": " "}),
    Call("PUT", "/projects/nope/title", {"title": "x"}),
    Call("GET", "/projects/proj_001/export?format=yaml"),
    Call("GET", "/projects/proj_001/export?format=timeline"),
    Call("GET", "/projects/proj_001/export?format=invalid"),
    Call("GET", "/projects/nope/export"),
    # proj_002 gets intents and an issue, is stopped and started again, and is deleted with them below.
    Call("POST", "/projects/proj_002/intents", _intent("e1")),
    Call("POST", "/projects/proj_002/intents", _intent("e2", creator="w", worker="w")),
    Call("POST", "/projects/proj_002/intents/i001/heartbeat", {"worker": "x"}),
    Call(
        "POST",
        "/projects/proj_002/intents/i001/conclude",
        {"worker": "x", "description": "f", "parse_repair": {"strategy": "envelope_repair"}},
    ),
    Call("PUT", "/projects/proj_002/status", {"status": "stopped"}),
    Call("PUT", "/projects/proj_002/status", {"status": "stopped"}),
    Call("POST", "/projects/proj_002/intents", _intent("e3")),
    Call("POST", "/projects/proj_002/hints", {"content": "while stopped", "creator": "h"}),
    Call("PUT", "/projects/proj_002/status", {"status": "active"}),
    Call("PUT", "/projects/proj_002/status", {"status": "done"}),
    Call("PUT", "/projects/nope/status", {"status": "active"}),
    # proj_004 ends stopped (which releases its intent's claim); proj_005 ends completed.
    Call("POST", "/projects/proj_004/intents", _intent("g1", creator="w", worker="w")),
    Call("PUT", "/projects/proj_004/status", {"status": "stopped"}),
    Call("POST", "/projects/proj_005/intents", _intent("h1")),
    Call("POST", "/projects/proj_005/intents/i001/conclude", {"worker": "x", "description": "done"}),
    Call("POST", "/projects/proj_005/complete", {"from": ["f001"], "description": "finished", "worker": "r"}),
    # proj_001 is completed, then reopened: reopen deletes its completion intent.
    Call("POST", "/projects/proj_001/complete", {"from": ["f999"], "description": "x", "worker": "r"}),
    Call("POST", "/projects/proj_001/complete", {"from": ["goal"], "description": "x", "worker": "r"}),
    Call("POST", "/projects/proj_001/complete", {"from": ["f001", "f002"], "description": "solved", "worker": "r"}),
    Call("POST", "/projects/proj_001/complete", {"from": ["f001"], "description": "again", "worker": "r"}),
    Call("PUT", "/projects/proj_001/status", {"status": "stopped"}),
    Call("POST", "/projects/proj_001/intents", _intent("late")),
    Call("POST", "/projects/proj_002/reopen", {"description": "x", "creator": "h"}),
    Call("POST", "/projects/proj_001/reopen", {"description": " ", "creator": "h"}),
    Call("POST", "/projects/proj_001/reopen", {"description": "fix", "creator": "h"}),
    Call("GET", "/projects"),
    Call("GET", "/projects/proj_001"),
    Call("GET", "/projects/proj_002"),
    Call("GET", "/projects/proj_003"),
    Call("GET", "/projects/proj_001/export?format=timeline"),
    # Deletes: proj_002 with its two intents and their sources and issue, proj_006 with none.
    Call("DELETE", "/projects/proj_002"),
    Call("DELETE", "/projects/proj_002"),
    Call("DELETE", "/projects/proj_006"),
    Call("DELETE", "/projects/nope"),
    Call("GET", "/projects"),
    Call("GET", "/projects/proj_002"),
]


class FakeClock:
    """utcnow() for every server module: one second later at every reading, from a fixed start."""

    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> str:
        self.now += timedelta(seconds=1)
        return self.now.strftime("%Y-%m-%dT%H:%M:%SZ")

    def advance(self, seconds: int) -> None:
        self.now += timedelta(seconds=seconds)
