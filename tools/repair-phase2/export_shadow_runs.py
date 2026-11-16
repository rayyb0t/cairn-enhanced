#!/usr/bin/env python3
"""Write the live shadow runs of one project as Phase 0-style folders for blind labelling (spec §15.6).

Runs in the repair agent's environment, as the agent's user when it has one:

    uv run --project repair-agent python tools/repair-phase2/export_shadow_runs.py --server http://127.0.0.1:8000 \\
        --project proj_031 --agent-token-file /etc/cairn-repair/agent.token --out shadow-proj_031

A thin command line over `cairn_repair_agent.export`, which documents what it writes.
"""

from __future__ import annotations

import sys

from cairn_repair_agent.export import main

if __name__ == "__main__":
    sys.exit(main())
