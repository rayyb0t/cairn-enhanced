#!/usr/bin/env python3
"""Phase 0 evaluation set: the runs of one project that hit their time limit, ready to label.

For one project it reads the dispatcher logs (with baseline_from_logs.py) and the project from the
Cairn server (GET /projects/{id}), picks the attempts whose main run timed out, and writes:

  <out>/<intent>/intent.json                 the intent, the fact it produced, every timed-out
                                             attempt with its timings, and the 75-80% review window
  <out>/<intent>/execute_output_preview.txt  the dispatcher's log preview of the timed-out run
  <out>/<intent>/conclude_output_preview.txt the conclude fallback's preview, when one was logged
  <out>/<intent>/transcript.md               where the agent's session transcript is and how to copy it
  <out>/labels.csv                           intent_id,label,evidence,notes to fill in
  <out>/manifest.json                        what was exported and from where
  <out>/LABELLING_GUIDE.md                   how to label

The previews and transcripts contain whatever the agents saw on the target. Keep the folder local.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import shlex
import shutil
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

import baseline_from_logs as baseline

TOOL_DIR = Path(__file__).resolve().parent
LABEL_COLUMNS = ["intent_id", "label", "evidence", "notes"]
REVIEW_WINDOW = (0.75, 0.80)

# Codex prints "session id: <uuid>" in its stderr header; pi's first JSON event is {"type":"session","id":...}.
CODEX_SESSION_RE = re.compile(r"session id:\s*([0-9a-fA-F][0-9a-fA-F-]{7,})")
PI_SESSION_RE = re.compile(r'\{"type":\s*"session"[^{}]*?"id":\s*"([^"]+)"')
SAFE_RUN_RE = re.compile(r"[A-Za-z0-9 ,.:_-]+")

# Where each CLI keeps its sessions (see SESSION_IDS.md): under the worker's home ("home": /home/kali
# in the official worker image, your own home in local mode) or at a fixed path ("abs").
TRANSCRIPT_DIRS = {
    "claudecode": ("home", ".claude/projects"),
    "codex": ("home", ".codex/sessions"),
    "pi": ("abs", "/tmp/cairn-pi/{worker}/sessions"),
}


def load_project(project_id: str, server: str | None, project_json: Path | None, timeout: float) -> dict[str, Any]:
    if project_json is not None:
        data = json.loads(project_json.read_text(encoding="utf-8"))
    else:
        import requests

        url = f"{(server or '').rstrip('/')}/projects/{quote(project_id, safe='')}"
        response = requests.get(url, timeout=timeout)
        response.raise_for_status()
        data = response.json()
    if not isinstance(data, dict) or not isinstance(data.get("intents"), list):
        raise SystemExit("the project data does not look like GET /projects/{id} (no intents list)")
    return data


def load_worker_setup(config: Path | None) -> tuple[str | None, dict[str, str]]:
    """(execution mode, worker name -> CLI type) from dispatch.yaml. Only names and types are read."""
    if config is None:
        return None, {}
    import yaml

    data = yaml.safe_load(config.read_text(encoding="utf-8")) or {}
    runtime = data.get("runtime") or {}
    execution = runtime.get("execution", "container") if isinstance(runtime, dict) else "container"
    types = {}
    for worker in data.get("workers") or []:
        if isinstance(worker, dict) and isinstance(worker.get("name"), str) and isinstance(worker.get("type"), str):
            types[worker["name"]] = worker["type"]
    return execution, types


def session_id_from_previews(attempt: baseline.Attempt) -> tuple[str | None, str | None]:
    found = CODEX_SESSION_RE.search(attempt.execute_stderr_preview or "")
    if found:
        return found.group(1), "codex stderr header, in the 'explore timed out' log preview"
    found = PI_SESSION_RE.search(attempt.execute_stdout_preview or "")
    if found:
        return found.group(1), "pi session event, in the 'explore timed out' log preview"
    return None, None


def description_snippet(description: str, limit: int = 60) -> str | None:
    """A run of plain characters from the description: unchanged by JSON escaping, safe in quotes."""
    runs = sorted((m.group(0).strip() for m in SAFE_RUN_RE.finditer(description)), key=len, reverse=True)
    best = runs[0][:limit].strip() if runs else ""
    return best if len(best) >= 16 else None


def intent_marker(intent_id: str) -> str:
    # The prompt holds "## Current Intent\n```\n<id>\n```"; session files store it JSON-escaped.
    return "```\\n" + intent_id + "\\n```"


def review_window(attempt: baseline.Attempt) -> dict[str, Any] | None:
    if not attempt.execute_timeout_s:
        return None
    window: dict[str, Any] = {
        "from_s": round(attempt.execute_timeout_s * REVIEW_WINDOW[0]),
        "to_s": round(attempt.execute_timeout_s * REVIEW_WINDOW[1]),
    }
    if attempt.execute_started_at:
        start = datetime.strptime(attempt.execute_started_at, baseline.HEADER_DATEFMT)
        window["dispatcher_clock"] = [
            (start + timedelta(seconds=window["from_s"])).strftime(baseline.HEADER_DATEFMT),
            (start + timedelta(seconds=window["to_s"])).strftime(baseline.HEADER_DATEFMT),
        ]
    return window


def attempt_record(attempt: baseline.Attempt, worker_types: dict[str, str]) -> dict[str, Any]:
    session_id, session_source = session_id_from_previews(attempt)
    return {
        "task": attempt.task,
        "attempt": attempt.attempt,
        "worker": attempt.worker,
        "worker_type": worker_types.get(attempt.worker),
        "dispatched_at": attempt.dispatched_at,
        "execute_started_at": attempt.execute_started_at,
        "execute_timeout_s": attempt.execute_timeout_s,
        "execute_ms": attempt.execute_ms,
        "execute_outcome": attempt.execute_outcome,
        "review_window": review_window(attempt),
        "conclude_started_at": attempt.conclude_started_at,
        "conclude_timeout_s": attempt.conclude_timeout_s,
        "conclude_outcome": attempt.conclude_outcome,
        "conclude_ms": attempt.conclude_ms,
        "conclude_error": attempt.conclude_error,
        "rescue_strategy": attempt.rescue_strategy,
        "concluded_source": attempt.concluded_source,
        "final": attempt.final,
        "final_reason": attempt.final_reason,
        "session_id": session_id,
        "session_id_source": session_source,
    }


def find_command(mode: str, cli: str, worker: str, container: str, marker: str, snippet: str | None) -> str:
    """A shell command that lists the session files holding this intent's prompt."""
    kind, path = TRANSCRIPT_DIRS[cli]
    path = path.format(worker=worker)
    if mode == "local":
        command = f"grep -rlF -e {shlex.quote(marker)} " + (f"~/{path}" if kind == "home" else shlex.quote(path))
        return command + (f" | xargs grep -lF -e {shlex.quote(snippet)}" if snippet else "")
    if kind == "home":
        script, args = f'grep -rlF -e "$1" "$HOME/{path}"', [marker]
    else:
        script, args = 'grep -rlF -e "$1" "$2"', [marker, path]
    if snippet:
        script += f' | xargs grep -lF -e "${len(args) + 1}"'
        args.append(snippet)
    return f"docker exec {container} sh -c {shlex.quote(script)} _ " + " ".join(shlex.quote(a) for a in args)


def transcript_guide(
    project_id: str,
    intent: dict[str, Any] | None,
    intent_id: str,
    attempts: list[dict[str, Any]],
    execution: str | None,
) -> str:
    container = baseline.CONTAINER_PREFIX + project_id.replace("/", "-")
    marker = intent_marker(intent_id)
    snippet = description_snippet((intent or {}).get("description") or "")
    lines = [
        f"# Transcript for {intent_id}",
        "",
        "The dispatcher does not log session ids, so find the session file by what the prompt contains:",
        f"the intent id in its `## Current Intent` block (search text ```` {marker} ````)"
        + (f" and this part of the description: `{snippet}`." if snippet else "."),
        "",
        "The timed-out run and its conclude fallback share one session (the fallback resumes it).",
        "",
    ]
    modes = [execution] if execution in ("container", "local") else ["container", "local"]
    for record in attempts:
        clis = [record["worker_type"]] if record["worker_type"] in TRANSCRIPT_DIRS else list(TRANSCRIPT_DIRS)
        lines += [f"## Attempt {record['attempt']} (worker {record['worker']})", ""]
        if record["session_id"]:
            lines += [f"Session id: `{record['session_id']}` ({record['session_id_source']}).", ""]
        window = record["review_window"]
        if window:
            clock = window.get("dispatcher_clock")
            lines.append(
                f"Review point: {window['from_s']}-{window['to_s']} s after the run started"
                + (f" ({clock[0]} to {clock[1]} on the dispatcher's clock)." if clock else ".")
            )
            lines += ["Session files usually record UTC times: count from the run's first entry instead.", ""]
        for mode in modes:
            for cli in clis:
                command = find_command(mode, cli, record["worker"], container, marker, snippet)
                lines += [f"{cli}, {mode} mode:", "", "```bash", command, "```", ""]
        if "container" in modes:
            lines += [
                "Copy a file it lists out of the container, and keep it with this folder (it is local data):",
                "",
                "```bash",
                f"docker cp {container}:<path printed above> {shlex.quote(intent_id)}/",
                "```",
                "",
            ]
    return "\n".join(lines)


def preview_text(attempts: list[baseline.Attempt], stage: str) -> str | None:
    blocks = []
    for attempt in attempts:
        stdout = getattr(attempt, f"{stage}_stdout_preview")
        stderr = getattr(attempt, f"{stage}_stderr_preview")
        if not stdout and not stderr:
            continue
        blocks.append(
            f"Attempt {attempt.attempt} (worker {attempt.worker}), as logged by the dispatcher "
            f"(whitespace collapsed, at most 1200 characters each):\n\n"
            f"--- stdout preview ---\n{stdout or '(empty)'}\n\n--- stderr preview ---\n{stderr or '(empty)'}\n"
        )
    return "\n".join(blocks) if blocks else None


def export(
    project_id: str,
    logs: list[str],
    project: dict[str, Any],
    out_dir: Path,
    *,
    task: str = "explore",
    execution: str | None = None,
    worker_types: dict[str, str] | None = None,
) -> dict[str, Any]:
    worker_types = worker_types or {}
    parsed = baseline.parse_logs(logs, [project_id])
    selected = [a for a in parsed.attempts if a.execute_outcome == "timed_out" and (task == "all" or a.task == task)]
    by_intent: dict[str, list[baseline.Attempt]] = {}
    for attempt in selected:
        by_intent.setdefault(attempt.intent, []).append(attempt)
    intents = {i.get("id"): i for i in project.get("intents", []) if isinstance(i, dict)}
    facts = {f.get("id"): f for f in project.get("facts", []) if isinstance(f, dict)}

    out_dir.mkdir(parents=True, exist_ok=True)
    exported = []
    for intent_id in sorted(by_intent):
        attempts = by_intent[intent_id]
        intent = intents.get(intent_id)
        fact = facts.get(intent.get("to")) if intent and intent.get("to") else None
        records = [attempt_record(a, worker_types) for a in attempts]
        folder = out_dir / intent_id
        folder.mkdir(parents=True, exist_ok=True)
        document = {
            "project": project_id,
            "intent_id": intent_id,
            "found_on_server": intent is not None,
            "description": (intent or {}).get("description"),
            "from": (intent or {}).get("from"),
            "creator": (intent or {}).get("creator"),
            "created_at": (intent or {}).get("created_at"),
            "concluded_at": (intent or {}).get("concluded_at"),
            "produced_fact": {"id": fact.get("id"), "description": fact.get("description")} if fact else None,
            "timed_out_attempts": records,
        }
        (folder / "intent.json").write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        for stage, filename in (("execute", "execute_output_preview.txt"), ("conclude", "conclude_output_preview.txt")):
            text = preview_text(attempts, stage)
            if text:
                (folder / filename).write_text(text, encoding="utf-8")
        (folder / "transcript.md").write_text(
            transcript_guide(project_id, intent, intent_id, records, execution) + "\n", encoding="utf-8"
        )
        exported.append(
            {
                "intent_id": intent_id,
                "attempts": len(attempts),
                "final": [a.final for a in attempts],
                "produced_fact": fact.get("id") if fact else None,
            }
        )

    with (out_dir / "labels.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=LABEL_COLUMNS)
        writer.writeheader()
        for item in exported:
            writer.writerow({"intent_id": item["intent_id"], "label": "", "evidence": "", "notes": ""})
    guide = TOOL_DIR / "LABELLING_GUIDE.md"
    if guide.exists():
        shutil.copyfile(guide, out_dir / "LABELLING_GUIDE.md")
    manifest = {
        "project": project_id,
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "selection": {"task": task, "criterion": "the main run timed out (explore timed out / bootstrap timed out)"},
        "logs": parsed.files,
        "attempts_in_logs": sum(1 for a in parsed.attempts if task == "all" or a.task == task),
        "timed_out_attempts": len(selected),
        "intents": exported,
        "labels": "labels.csv: label each intent progressing, stuck or unclear (see LABELLING_GUIDE.md)",
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--project", required=True, help="project id, e.g. proj_027")
    parser.add_argument("--log", action="append", required=True, help="dispatcher log file, in time order (repeatable)")
    parser.add_argument("--server", default="http://127.0.0.1:8000", help="Cairn server URL (default: %(default)s)")
    parser.add_argument(
        "--project-json", type=Path, help="a saved GET /projects/{id} response, instead of asking the server"
    )
    parser.add_argument(
        "--config", type=Path, help="your dispatch.yaml, to name each worker's CLI (only names and types are read)"
    )
    parser.add_argument(
        "--task", choices=["explore", "bootstrap", "all"], default="explore", help="which runs (default: %(default)s)"
    )
    parser.add_argument("--out", type=Path, help="output folder (default: eval-<project>)")
    parser.add_argument("--timeout", type=float, default=15.0, help="server request timeout in seconds")
    args = parser.parse_args(argv)

    project = load_project(args.project, args.server, args.project_json, args.timeout)
    execution, worker_types = load_worker_setup(args.config)
    out_dir = args.out or Path(f"eval-{args.project}")
    manifest = export(
        args.project, args.log, project, out_dir, task=args.task, execution=execution, worker_types=worker_types
    )
    print(
        f"{manifest['timed_out_attempts']} timed-out attempt(s) on {len(manifest['intents'])} intent(s) "
        f"out of {manifest['attempts_in_logs']} {args.task} attempt(s) in the logs"
    )
    for item in manifest["intents"]:
        print(f"  {item['intent_id']}: {item['attempts']} attempt(s), ended {', '.join(item['final'])}")
    print(f"\nWrote {out_dir}/ (fill in {out_dir / 'labels.csv'}; see {out_dir / 'LABELLING_GUIDE.md'})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
