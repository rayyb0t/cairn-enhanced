#!/usr/bin/env python3
"""Replay a Phase 0 evaluation set through the live repair agent's code, for every listed reviewer (spec §15.3).

Runs in the repair agent's environment, as the agent's user when it has one:

    uv run --project repair-agent python tools/repair-phase2/replay_eval_set.py eval-proj_027 \\
        --config repair.yaml --project-json eval-proj_027/project.json --dry-run --out replay-027

A thin command line over `cairn_repair_agent.replay`, which holds the replay and documents it.
"""

from __future__ import annotations

import sys

from cairn_repair_agent.replay import main

if __name__ == "__main__":
    sys.exit(main())
