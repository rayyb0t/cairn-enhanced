# When each worker CLI's session id becomes known

The repair agent (Phase 2) reads a run's transcript while the run is still going, at 75–80% of its
budget, so it needs the run's session id early. The conclude fallback needs the same id to resume
the session. This note records what the code does today. Line numbers are for commit `c4a0e2a`.

## Summary

| CLI | Who picks the id | When the dispatcher has it today | Where the session file is |
|---|---|---|---|
| claudecode | The dispatcher, before the run | Before the run starts | `$HOME/.claude/projects/<folder for the working directory>/<id>.jsonl` (Claude Code's default store) |
| codex | Codex, printed as `session id: <uuid>` in its stderr header | After the run ends | `$HOME/.codex/sessions/YYYY/MM/DD/rollout-…-<id>.jsonl` (Codex's default store) |
| pi | pi, in its first stdout event `{"type":"session","id":…}` | After the run ends | `/tmp/cairn-pi/<worker>/sessions` |

`$HOME` is `/home/kali` in the official worker image (`container/Dockerfile`: `USER kali`,
`WORKDIR /home/kali/workspace`), and your own home in local mode. pi's folder is the same path in
both modes.

## The code

claudecode is seeded:

- `SeedSessionDriver.prepare_session` returns a new UUID (`cairn/src/cairn/dispatcher/workers/base.py:57-59`).
- `ClaudeCodeDriver.build_execute` passes it as `--session-id` (`workers/adapters/claudecode.py:38-51`).
- The conclude fallback resumes it with `claude -r <id>` (`claudecode.py:53-62`).

codex and pi are parsed from the output:

- The base `prepare_session` returns `None` (`base.py:31-32`), so nothing is passed at start.
- codex: `RegexSessionDriver.extract_session` searches stderr for `session id:\s*([0-9a-fA-F-]+)` (`base.py:62-71`). The conclude fallback runs `codex exec resume <id>` (`adapters/codex.py:73-107`).
- pi: `PiDriver.extract_session` takes the `id` of the first event whose `type` is `session` (`adapters/pi.py:119-128`). `--session` is added only when an id already exists (`pi.py:71-72`), so a new run never gets one. The session folder is `/tmp/cairn-pi/<worker>/sessions` (`pi.py:191-197`).

Every task reads the id only after the process has returned:

- explore: `prepare_session` at `tasks/explore.py:108`, `extract_session` at `explore.py:123`, after `_run_process` returns.
- bootstrap: `tasks/bootstrap.py:100` and `:115`.
- reason: `tasks/reason.py:118` and `:133`.

When no id is found, the conclude fallback is skipped and logged as
`conclude fallback unavailable … has_session=False` (`explore.py:260-269`, `bootstrap.py:250`).
The baseline counts these as `unavailable`.

No log line contains a session id. One indirect source exists: the `explore timed out` and
`bootstrap timed out` lines carry the first 1200 characters of stdout and stderr. For codex the
stderr preview normally starts with the header that holds the id, and for pi the stdout preview
starts with the session event. `export_eval_set.py` reads the id from there when it can. For
claudecode, the seeded id is never printed, so the eval set finds the file by searching for the
intent's prompt.

## Is the output readable while the run is going?

- Container mode: yes. `ManagedProcess._read_stream` appends each chunk to its stdout and stderr lists as Docker streams it (`runtime/process.py:118-140`). The header is in memory moments after the CLI prints it, but nothing reads it until `communicate()` returns.
- Local mode: not reliably. `LocalProcess._drain` reads the text pipe with `read(65536)` (`runtime/local_process.py:126-135`), which returns only after 65,536 characters or end of file. A short header can wait in the pipe until the run ends. Checked with a child that prints one line and keeps running for 2 s: the reader received nothing until the child exited.

## What Phase 2 needs to register runs with their session id early

1. claudecode: nothing new. The id is known before the run starts, so the register call can send it.
2. codex and pi: watch the output during the run, then send a second register call with the same run id once the session id appears.
   - Container mode: read the chunks `ManagedProcess` already collects.
   - Local mode: the repair path needs a reader that returns as data arrives, such as `readline()` or `read1()` instead of `read(65536)`. Today's reader stays as it is for runs with Repair off.
3. Optional, not verified: if pi accepts a new id through `--session` at start, or codex accepts a chosen id, both could be seeded like claudecode and the parsing dropped. Check each CLI's own documentation for the version you run.
4. The registry must also save the worker name and CLI type, because the transcript location depends on both (pi's folder is per worker).
