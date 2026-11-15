"""Session ids of explore runs (spec 8.7): the one id rule, Claude Code's argv seed, and the output probe.

One rule for every id that the repair side puts into a request body, a path, a command or a log line:
[A-Za-z0-9][A-Za-z0-9._-]{0,127} in full, without "..". It is matched with re.fullmatch, never re.match, whose $
also matches before a final newline. A session id that fails it is never used: the run is registered without
one, and one WARNING gives only the id's length and the first 12 hex digits of its SHA-256. No log line holds a
session id. pi's id can be forged (pi runs under a login shell, whose ~/.profile an earlier run can write), so
this rule is what keeps such an id out of paths, requests and logs.

Claude Code's id is in its execute argv. codex and pi print theirs; the probe reads the execute process's own
output buffers while it runs, and the end-of-run parse reads its full output once, exactly as
tasks/explore.py does. Both call the driver's own extract_session inside one guard.
"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Protocol

LOG = logging.getLogger(__name__)

SESSION_ID_RULE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
RUN_ID_RULE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{7,63}")  # spec 6.1; the dispatcher's run ids are uuid4().hex

PROBE_INTERVAL_SECONDS = 1.0  # an id reaches the server about 1 s after it reaches the buffer
PROBE_MAX_CHARS = 1_048_576  # probing stops once the stream the driver reads holds this much without an id

# The output read so far, kept by the process classes: ManagedProcess._stdout and _stderr (runtime/process.py)
# and LocalProcess._stdout_chunks and _stderr_chunks (runtime/local_process.py). They are only read: a copy of
# the list is taken, and neither the process nor its reader threads are touched.
STDOUT_BUFFERS = ("_stdout", "_stdout_chunks")
STDERR_BUFFERS = ("_stderr", "_stderr_chunks")

# The stream each driver parses its session id from: codex's stderr header (workers/base.py RegexSessionDriver)
# and pi's first stdout session event (workers/adapters/pi.py). Claude Code's id is in its argv; mock has none.
OUTPUT_ID_STREAMS = {"codex": "stderr", "pi": "stdout"}


class SessionParser(Protocol):
    def extract_session(self, session: str | None, stdout: str, stderr: str) -> str | None: ...


def valid_session_id(value: object) -> bool:
    return isinstance(value, str) and SESSION_ID_RULE.fullmatch(value) is not None and ".." not in value


def valid_project_id(value: object) -> bool:
    """The session-id rule, which also guards a project id before it names a copy folder (spec 8.7, 8.8)."""
    return valid_session_id(value)


def valid_run_id(value: object) -> bool:
    return isinstance(value, str) and RUN_ID_RULE.fullmatch(value) is not None


def session_id_fingerprint(value: str) -> tuple[int, str]:
    """What a log line may say about a rejected id: its length and the first 12 hex digits of its SHA-256."""
    return len(value), hashlib.sha256(value.encode("utf-8", "surrogatepass")).hexdigest()[:12]


def accept_session_id(value: object, *, project_id: str, intent_id: str, worker: str, run_id: str) -> str | None:
    """value if it is an id that passes the rule; otherwise None, with one WARNING for a rejected string."""
    if value is None:
        return None
    if isinstance(value, str) and valid_session_id(value):
        return value
    if isinstance(value, str):
        length, digest = session_id_fingerprint(value)
        LOG.warning(
            "repair shadow session rejected project=%s intent=%s worker=%s run=%s len=%s sha256=%s",
            project_id,
            intent_id,
            worker,
            run_id,
            length,
            digest,
        )
    return None


def argv_session_seed(cli_type: str, command: object) -> str | None:
    """The value after --session-id in Claude Code's execute argv, before the "--" that precedes the prompt.

    workers/adapters/claudecode.py puts the id there. The value is not checked here: pass it to
    accept_session_id. None for every other CLI, and when the argv holds no such value.
    """
    if cli_type != "claudecode" or not isinstance(command, list):
        return None
    end = command.index("--") if "--" in command else len(command)
    for index in range(end - 1):
        if command[index] == "--session-id":
            value = command[index + 1]
            return value if isinstance(value, str) else None
    return None


def output_snapshot(process: object, stream: str) -> list[str] | None:
    """A copy of the process's own buffer for stream ("stdout" or "stderr"), or None if it keeps none.

    list[:] copies in one step, which is atomic under CPython's GIL while the reader thread appends.
    """
    for name in STDERR_BUFFERS if stream == "stderr" else STDOUT_BUFFERS:
        buffer = getattr(process, name, None)
        if isinstance(buffer, list):
            return buffer[:]
    return None


def complete_lines(chunks: list[str], limit: int = PROBE_MAX_CHARS) -> tuple[str, int]:
    """The first limit characters of the stream cut after their last newline, and how many characters were seen.

    The second value stops counting at the chunk that reaches limit. Only whole lines are returned, so a parse of
    them finds either nothing or the id that the parse of the full output finds: codex's pattern cannot cross a
    newline, and pi takes the first session event among complete lines.
    """
    parts: list[str] = []
    size = 0
    for chunk in chunks:
        if not isinstance(chunk, str):
            return "", size
        parts.append(chunk)
        size += len(chunk)
        if size >= limit:
            break
    text = "".join(parts)[:limit]
    return text[: text.rfind("\n") + 1], size


class OutputSessionProbe:
    """Finds a codex or pi session id in the execute process's output while it runs, for one run (spec 8.7).

    Used only from the run's reporter thread. A probe looks only when the buffer's chunk count has changed, and
    calls the driver's own extract_session on whole lines of the stream that driver reads. An early id is always
    the final id, so probing ends at the first id, accepted or rejected; it also ends once the stream holds
    PROBE_MAX_CHARS without one. If extract_session raises (pi's parser lets a RecursionError through on a deeply
    nested line), the class name is logged once, and the run's later probes and its end-of-run parse are skipped:
    the run then has no id from its output, and its copy is found by its prompt marker instead (spec 8.8).
    """

    def __init__(
        self,
        driver: SessionParser,
        cli_type: str,
        *,
        project_id: str,
        intent_id: str,
        worker: str,
        run_id: str,
    ) -> None:
        self._driver = driver
        self._stream = OUTPUT_ID_STREAMS.get(cli_type)
        self._project_id = project_id
        self._intent_id = intent_id
        self._worker = worker
        self._run_id = run_id
        self._last_chunk_count: int | None = None
        self.found: str | None = None
        self.rejected = False
        self.failed = False
        self.exhausted = False

    @property
    def reads_output(self) -> bool:
        """Whether this CLI's driver parses its session id from the output (codex and pi)."""
        return self._stream is not None

    @property
    def active(self) -> bool:
        """Whether another probe can still find an id."""
        return self.reads_output and self.found is None and not (self.rejected or self.failed or self.exhausted)

    def probe(self, process: object) -> str | None:
        """The id, the first time one that passes the rule is in the buffer; None otherwise."""
        stream = self._stream
        if stream is None or not self.active:
            return None
        chunks = output_snapshot(process, stream)
        if chunks is None or len(chunks) == self._last_chunk_count:
            return None
        self._last_chunk_count = len(chunks)
        text, size = complete_lines(chunks)
        value = None
        if text:
            value = self._extract(text, "") if stream == "stdout" else self._extract("", text)
        if value is None:
            if size >= PROBE_MAX_CHARS and not self.failed:
                self.exhausted = True
            return None
        return self._accept(value)

    def final(self, stdout: str, stderr: str) -> str | None:
        """The end-of-run parse of the full output, the same parse as tasks/explore.py, with the rule applied.

        None for a CLI whose id is not in its output, after a probe raised, and after an id was rejected (the same
        id would be rejected again).
        """
        if not self.reads_output or self.failed or self.rejected:
            return None
        value = self._extract(stdout, stderr)
        return None if value is None else self._accept(value)

    def _extract(self, stdout: str, stderr: str) -> object:
        try:
            return self._driver.extract_session(None, stdout, stderr)
        except Exception as exc:
            self.failed = True
            LOG.warning(
                "repair shadow session probe failed project=%s intent=%s worker=%s run=%s error=%s",
                self._project_id,
                self._intent_id,
                self._worker,
                self._run_id,
                type(exc).__name__,
            )
            return None

    def _accept(self, value: object) -> str | None:
        accepted = accept_session_id(
            value,
            project_id=self._project_id,
            intent_id=self._intent_id,
            worker=self._worker,
            run_id=self._run_id,
        )
        if accepted is None:
            self.rejected = True
        else:
            self.found = accepted
        return accepted
