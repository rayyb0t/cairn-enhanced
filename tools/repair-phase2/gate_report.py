#!/usr/bin/env python3
"""Score the replay's decisions, or the live shadow decisions, against your Phase 0 labels (spec §15.5, §15.6).

Runs in the repair agent's environment, as the agent's user when it has one:

    uv run --project repair-agent python tools/repair-phase2/gate_report.py --labels eval-proj_027/labels.csv \\
        --decisions replay-027/decisions.jsonl --injected replay-027/decisions-injected.jsonl \\
        --out replay-027/gate_report.json

A thin command line over `cairn_repair_agent.gate`, which defines the bar once and documents the report.
"""

from __future__ import annotations

import sys

from cairn_repair_agent.gate import main

if __name__ == "__main__":
    sys.exit(main())
