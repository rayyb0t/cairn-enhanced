#!/usr/bin/env python3
"""Step 0 of the gate replay (spec §15.3): remove worker secrets from an eval set's session files by exact value, as
the dispatcher's live copy does, before any of them reaches a reviewer.

Run it in the cairn environment, as yourself, since it reads dispatch.yaml exactly as the dispatcher does and, in
local mode, your CLIs' credential files (the repair agent never does):

    uv run --project cairn python tools/repair-phase2/redact_eval_set.py eval-proj_027 --dispatch-config dispatch.yaml

It loads dispatch.yaml with DispatchConfig and applies cairn.dispatcher.repair.redaction: secret_values collects every
worker's secret values (common_env included) and every reviewer's REVIEW_API_KEY; when the execution mode is local,
also those of your environment and of the local CLIs' credential files, resolved in each worker's merged environment
({**os.environ, **worker.env}: HOME, CLAUDE_CONFIG_DIR, CODEX_HOME, PI_CODING_AGENT_DIR); and URL passwords. redact_exact
replaces each value, raw and JSON-escaped, with [REDACTED:<name>]. The dispatcher's repair token is not among them:
the runs of a Phase 0 set ran before it existed, and a live copy (export_shadow_runs.py) is already redacted.

For each intent folder (a subfolder holding intent.json), every session file *.jsonl is redacted to
transcript.redacted.jsonl beside it; with several session files (several timed-out attempts of the intent), the k-th
in name order goes to attempt-<k>/transcript.redacted.jsonl, so that every output is called transcript.redacted.jsonl
and the Compose copy of §15.3, whose rsync includes 'transcript.redacted.jsonl' (a name rsync matches at any depth),
carries them all. redaction.json records, for each, its name, size and modification time, its output and the counts
per variable name or credential-file label, never a value. The replay reads only those outputs and refuses a session
file that redaction.json does not name or that changed since. The credential files are read as they are today: a token
rotated since a Phase 0 run is not among the values, and that run's copy of it gets only the agent's pattern layer
(§9.6).

It removes or replaces only what it wrote itself, as its own earlier redaction.json records it:
- A folder without a session file is left as it is: a folder of export_shadow_runs.py, whose transcript.redacted.jsonl
  is the dispatcher's redacted copy (§15.6), or a copy of the eval set made without the session files (§15.3, Compose).
- A folder whose redaction.json this tool did not write (export_shadow_runs.py's, or one it cannot read), or that holds
  a redacted output its redaction.json does not name, is left as it is too; if it holds session files, they stay
  unredacted, the replay refuses them, and this tool exits 3 after the other folders.
- An output of an earlier run whose session file is gone is removed, when this tool's redaction.json names it.

Each folder is redacted whole or not at all. Every session file is read and redacted into a temporary file first (a
dot name that is neither a session file's nor an output's, so neither this tool nor the replay reads it); a session
file that cannot be read (a symlink, a FIFO, one too large), a full disk or an interrupt then stops the run with the
folder as it was. Only then are the earlier outputs removed (the earlier redaction.json names each), the new
redaction.json written, and the new outputs moved into place, in that order: the folder never holds an output that the
redaction.json in it does not name, nor an earlier output where the new redaction.json names a new one. So a run
stopped at any point, even killed, leaves a folder that the next run redacts again; meanwhile the replay refuses a case
whose output is missing.

Keep the unredacted session files readable by you alone (chmod 0600); this tool says which are not.
Exit codes: 0 done; 2 dispatch.yaml cannot be loaded; 3 the eval set cannot be read, or a folder's session files were
left unredacted (above).
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import stat
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from cairn.dispatcher.config import DispatchConfig
from cairn.dispatcher.repair import redaction

INTENT_FILE = "intent.json"
REDACTED_NAME = "transcript.redacted.jsonl"
REDACTION_REPORT = "redaction.json"
WRITTEN_BY = "redact_eval_set.py"
EXPORT_WRITTEN_BY = "export_shadow_runs.py"
ATTEMPT_FOLDER = re.compile(r"attempt-[1-9][0-9]{0,5}")
REDACTED_OUTPUT = re.compile(r"(?:attempt-[1-9][0-9]{0,5}/)?transcript\.redacted\.jsonl")
SOURCE_MAX_BYTES = 1024 * 1024 * 1024
REPORT_MAX_BYTES = 4 * 1024 * 1024
EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_INPUT = 3


class EvalSetError(Exception):
    """The eval set cannot be read (exit code 3)."""


def redacted_name(index: int, count: int) -> str:
    """The output of the index-th (1-based) of count session files of a folder, the replay's naming: beside the one
    session file, or in the folder attempt-<index> with several, always under the name the Compose copy carries."""
    return REDACTED_NAME if count == 1 else f"attempt-{index}/{REDACTED_NAME}"


def is_redacted_output(name: str) -> bool:
    return REDACTED_OUTPUT.fullmatch(name) is not None


def is_session_name(name: str) -> bool:
    """A session file's name: any *.jsonl in the intent folder itself but its redacted output, as the replay counts
    them (a name with a path in it, which redaction.json may hold, is none)."""
    return name.endswith(".jsonl") and name != REDACTED_NAME and "/" not in name and "\0" not in name


def session_files(folder: Path) -> list[str]:
    """The folder's session files: every *.jsonl in it that is not its redacted output, by name."""
    return sorted(entry.name for entry in folder.iterdir() if is_session_name(entry.name))


def _real_folder(path: Path) -> bool:
    """A folder, not a symlink to one."""
    try:
        return stat.S_ISDIR(os.lstat(path).st_mode)
    except OSError:
        return False


def present_outputs(folder: Path) -> list[str]:
    """The redacted outputs in an intent folder, by their names relative to it: transcript.redacted.jsonl, and
    attempt-<k>/transcript.redacted.jsonl in each attempt folder that is a folder (a symlink is never followed)."""
    found = [REDACTED_NAME] if os.path.lexists(folder / REDACTED_NAME) else []
    for entry in folder.iterdir():
        if ATTEMPT_FOLDER.fullmatch(entry.name) and _real_folder(entry) and os.path.lexists(entry / REDACTED_NAME):
            found.append(f"{entry.name}/{REDACTED_NAME}")
    return sorted(found)


@dataclass(frozen=True)
class Previous:
    """The folder's redaction.json before this run: whether this tool wrote it, and the outputs it names."""

    ours: bool
    outputs: frozenset[str] = frozenset()
    problem: str = ""


def _read_report(path: Path) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("not a regular file")
        data = os.read(fd, REPORT_MAX_BYTES + 1)
    finally:
        os.close(fd)
    if len(data) > REPORT_MAX_BYTES:
        raise ValueError("too large")
    return data


def previous_report(folder: Path) -> Previous | None:
    """The folder's redaction.json, None without one. It is this tool's when it names only session files of the
    folder itself and outputs of this tool's naming; export_shadow_runs.py's names the dispatcher's copy instead."""
    path = folder / REDACTION_REPORT
    if not os.path.lexists(path):
        return None
    try:
        report = json.loads(_read_report(path))
    except (OSError, ValueError, RecursionError):
        return Previous(ours=False, problem="cannot be read")
    if not isinstance(report, dict) or not isinstance(report.get("files"), list):
        return Previous(ours=False, problem="is not one that redact_eval_set.py writes")
    if report.get("written_by") == EXPORT_WRITTEN_BY:
        return Previous(ours=False, problem="was written by export_shadow_runs.py (the dispatcher's redacted copy)")
    if report.get("written_by") not in (None, WRITTEN_BY):
        return Previous(ours=False, problem="was not written by redact_eval_set.py")
    outputs: set[str] = set()
    for entry in report["files"]:
        source = entry.get("source") if isinstance(entry, dict) else None
        output = entry.get("output") if isinstance(entry, dict) else None
        if not isinstance(source, str) or not is_session_name(source):
            return Previous(ours=False, problem="names a source that is no session file of this folder")
        if not isinstance(output, str) or not is_redacted_output(output):
            return Previous(ours=False, problem="names an output that is no redacted transcript")
        outputs.add(output)
    return Previous(ours=True, outputs=frozenset(outputs))


def read_source(path: Path) -> tuple[bytes, os.stat_result]:
    """A session file: a regular file, never through a symlink or from a FIFO."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise EvalSetError(f"{path} cannot be read: {type(exc).__name__}") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise EvalSetError(f"{path} is not a regular file")
        if info.st_size > SOURCE_MAX_BYTES:
            raise EvalSetError(f"{path} is larger than {SOURCE_MAX_BYTES} bytes")
        chunks: list[bytes] = []
        while chunk := os.read(fd, 1 << 20):
            chunks.append(chunk)
        return b"".join(chunks), info
    finally:
        os.close(fd)


def temporary_name(name: str) -> str:
    """The temporary file of name: a dot name ending in .tmp, which is neither a session file (*.jsonl) nor an output,
    so that neither this tool nor the replay reads it, and the Compose copy of §15.3 does not carry it."""
    return f".{name}.tmp"


def write_temporary(folder: Path, name: str, data: bytes) -> Path:
    """data in name's temporary file in folder, flushed to disk; a leftover of an earlier run stopped on its way is
    replaced. The caller moves it into place, or removes it."""
    temporary = folder / temporary_name(name)
    temporary.unlink(missing_ok=True)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return temporary


def write_atomic(folder: Path, name: str, data: bytes) -> None:
    """name in folder, written to a temporary file there, flushed to disk and moved into place."""
    temporary = write_temporary(folder, name, data)
    try:
        os.replace(temporary, folder / name)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def output_folder(folder: Path, output: str, made: list[Path]) -> Path:
    """Where an output goes: the intent folder, or its attempt folder, made if missing (and then added to `made`) and
    never a symlink."""
    if "/" not in output:
        return folder
    target = folder / output.split("/", 1)[0]
    try:
        os.mkdir(target, 0o755)
    except FileExistsError:
        pass
    except OSError as exc:
        raise EvalSetError(f"{target} cannot be made: {type(exc).__name__}") from None
    else:
        made.append(target)
    if not _real_folder(target):
        raise EvalSetError(f"{target} is not a folder (a symlink is never followed)")
    return target


def remove_output(folder: Path, output: str) -> None:
    """Remove an output this tool wrote earlier, and its attempt folder once empty; never through a symlink."""
    parent = folder
    if "/" in output:
        parent = folder / output.split("/", 1)[0]
        if not _real_folder(parent):
            return
    target = parent / REDACTED_NAME
    try:
        if stat.S_ISREG(os.lstat(target).st_mode):
            target.unlink()
    except FileNotFoundError:
        pass
    if parent != folder:
        # Not empty: it holds the new output's temporary file, or something this tool did not write, which stays.
        with contextlib.suppress(OSError):
            os.rmdir(parent)


def encode_report(report: Mapping[str, Any]) -> bytes:
    return (json.dumps(report, indent=2, ensure_ascii=True) + "\n").encode("ascii")


@dataclass
class FolderResult:
    """One intent folder: the content of the redaction.json written into it, or why it was left as it is (`left`);
    `unredacted` when it holds session files that were not redacted."""

    report: dict[str, Any] | None = None
    left: str = ""
    unredacted: bool = False
    removed: list[str] = field(default_factory=list)


def redact_folder(folder: Path, values: Mapping[str, str]) -> FolderResult:
    """Redact every session file of one intent folder and write its redaction.json, replacing or removing only what
    this tool wrote before; whole or not at all (the module's docstring says how)."""
    sources = session_files(folder)
    present = present_outputs(folder)
    previous = previous_report(folder)
    if not sources:
        if present or previous is not None:
            return FolderResult(
                left="no session file: its redacted transcripts and redaction.json are kept as they are"
            )
        return FolderResult(left="no session file")
    if previous is not None and not previous.ours:
        return FolderResult(
            left=f"its redaction.json {previous.problem}: nothing is changed, and its session files stay unredacted; "
            "move them to an intent folder of their own",
            unredacted=True,
        )
    named = previous.outputs if previous is not None else frozenset()
    foreign = [name for name in present if name not in named]
    if foreign:
        return FolderResult(
            left=f"it holds {', '.join(foreign)}, which no redaction.json of this tool names: nothing is changed, and "
            "its session files stay unredacted; move that file away and run this again",
            unredacted=True,
        )
    entries: list[dict[str, Any]] = []
    totals: dict[str, int] = {}
    staged: list[tuple[Path, Path]] = []  # (temporary file, the output it becomes)
    made: list[Path] = []  # attempt folders this run made
    try:
        # 1. Every session file, read and redacted into a temporary file beside its output: nothing a reader sees
        #    changes, so a session file that cannot be read stops the run with the folder as it was.
        for index, name in enumerate(sources, start=1):
            data, info = read_source(folder / name)
            redacted, counts = redaction.redact_exact(data, values)
            output = redacted_name(index, len(sources))
            target = output_folder(folder, output, made)
            staged.append((write_temporary(target, REDACTED_NAME, redacted), target / REDACTED_NAME))
            for label, count in counts.items():
                totals[label] = totals.get(label, 0) + count
            entries.append(
                {
                    "source": name,
                    "source_bytes": info.st_size,
                    "source_mtime_ns": info.st_mtime_ns,
                    "output": output,
                    "output_sha256": hashlib.sha256(redacted).hexdigest(),
                    "redactions": dict(sorted(counts.items())),
                }
            )
        report = {"version": 1, "written_by": WRITTEN_BY, "files": entries, "redactions": dict(sorted(totals.items()))}
        # 2. The earlier outputs go (the earlier redaction.json names each; one whose session file is gone would be
        #    read as a transcript), then the new redaction.json is written, then the outputs it names move into place.
        for name in present:
            remove_output(folder, name)
        write_atomic(folder, REDACTION_REPORT, encode_report(report))
        for temporary, final in staged:
            os.replace(temporary, final)
    except BaseException:
        for temporary, _ in staged:
            temporary.unlink(missing_ok=True)  # one already moved into place is no longer there
        for attempt in made:
            with contextlib.suppress(OSError):  # only when empty: an output already moved into it stays
                os.rmdir(attempt)
        raise
    written = {entry["output"] for entry in entries}
    return FolderResult(report=report, removed=sorted(name for name in present if name not in written))


def intent_folders(eval_dir: Path) -> list[Path]:
    return sorted(
        (entry for entry in eval_dir.iterdir() if entry.is_dir() and (entry / INTENT_FILE).is_file()),
        key=lambda entry: entry.name,
    )


def load_dispatch_config(path: Path) -> DispatchConfig:
    """dispatch.yaml as the dispatcher loads it (DispatchConfig.load); main prints a failure's fields, never values."""
    return DispatchConfig.load(path)


def _config_problem(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        found = exc.errors(include_url=False, include_context=False, include_input=False)
        return "; ".join(
            f"{'.'.join(str(part) for part in error['loc']) or 'file'}: {error['msg']}" for error in found[:5]
        )
    return type(exc).__name__


def _write(line: str) -> None:
    sys.stdout.write(line + "\n")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="redact_eval_set.py",
        description="Remove worker secrets from an eval set's session files by exact value, as the live copy does.",
    )
    parser.add_argument("eval_dir", metavar="EVAL_DIR", help="the eval set, such as eval-proj_027")
    parser.add_argument("--dispatch-config", required=True, metavar="PATH", help="your dispatch.yaml")
    args = parser.parse_args(argv)
    try:
        config = load_dispatch_config(Path(args.dispatch_config))
    except Exception as exc:
        sys.stderr.write(f"redact_eval_set: dispatch.yaml cannot be loaded: {_config_problem(exc)}\n")
        return EXIT_CONFIG
    execution = config.runtime.execution
    values = redaction.secret_values(config, execution, os.environ, None)
    eval_dir = Path(args.eval_dir)
    try:
        if not eval_dir.is_dir():
            raise EvalSetError(f"no eval set folder: {eval_dir}")
        folders = intent_folders(eval_dir)
        if not folders:
            raise EvalSetError(f"{eval_dir} holds no intent folder (a subfolder with intent.json)")
        total = 0
        redacted_folders = 0
        unredacted: list[str] = []
        for folder in folders:
            result = redact_folder(folder, values)
            report = result.report
            if report is None:
                _write(f"left intent={folder.name}: {result.left}")
                if result.unredacted:
                    unredacted.append(folder.name)
                continue
            redacted_folders += 1
            count = sum(report["redactions"].values())
            total += count
            _write(f"redacted intent={folder.name} files={len(report['files'])} redactions={count}")
            for label, found in report["redactions"].items():
                if found:
                    _write(f"  {label}: {found}")
            for entry in report["files"]:
                mode = stat.S_IMODE(os.stat(folder / entry["source"], follow_symlinks=False).st_mode)
                if mode & 0o077:
                    _write(f"  note: {entry['source']} is readable by others than you (mode {mode:04o}); chmod 0600 it")
            for name in result.removed:
                _write(f"  removed {name}: its session file is gone")
    except (EvalSetError, OSError) as exc:
        sys.stderr.write(f"redact_eval_set: {exc}\n")
        return EXIT_INPUT
    _write(
        f"redacted {redacted_folders} intent folders, left {len(folders) - redacted_folders} as they were, "
        f"execution={execution}, {len(values)} secret values known, {total} redactions"
    )
    if unredacted:
        sys.stderr.write(
            f"redact_eval_set: session files left unredacted in {len(unredacted)} intent folders: "
            f"{', '.join(unredacted)} (see their lines above); the replay refuses them\n"
        )
        return EXIT_INPUT
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
