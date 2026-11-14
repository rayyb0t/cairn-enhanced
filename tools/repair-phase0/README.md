# Repair layer, Phase 0: check before building

Phase 0 of the repair-layer design changes no Cairn code. It answers four questions with your own
setup and data before Phase 2 builds anything on the answers:

1. How does a worker run really stop today in container mode? (`kill_check.py`)
2. When does each worker CLI's session id become known? (`SESSION_IDS.md`)
3. How often do explore runs hit their time limit, and how does the conclude fallback end? (`baseline_from_logs.py`)
4. At the 75–80% mark, were the capped runs progressing or stuck? (`export_eval_set.py` plus your labels, using `LABELLING_GUIDE.md`)

The labels from step 4 are the answer key for Phase 2's shadow mode, where the repair agent decides
but nothing is applied.

## Before you start

Run everything from the repository root, with the same Python environment that runs Cairn. The
tools import the dispatcher code from `cairn/src` of this checkout. `kill_check.py` also needs Docker
on the same machine.

If you run the dispatcher with Docker Compose, save its log first:

```bash
docker logs cairn-dispatcher > dispatcher.log 2>&1
```

## 1. Kill check

This uses the same image your workers use, read from your `dispatch.yaml`. It runs five short
scenarios (about a minute in total). Each one runs in a throwaway container with no network, which
is removed afterwards.

```bash
uv run --project cairn python tools/repair-phase0/kill_check.py --config dispatch.yaml
```

It prints a table and writes `kill_check_results.json`. Use `--image` to test another image, or
`--dry-run` to see the plan without Docker. If the tool is ever interrupted hard, its containers
carry the label `cairn.phase0=kill-check`:

```bash
docker rm -f $(docker ps -aq --filter label=cairn.phase0=kill-check)
```

### What it found here

These results are from `debian:bookworm-slim` on Docker 29.9.2, in the development sandbox. Run it
with your worker image to confirm. The image decides which of `_kill_pid`'s commands actually runs:
this one has no `kill` binary.

| Scenario | Result | Still alive in the container afterwards |
|---|---|---|
| Wrapper expires (`timeout -k 2s 4s`), agent exits on SIGTERM | Exit 124, run over after 6.1 s | Child that ignores SIGTERM; `setsid` child |
| Wrapper expires, agent ignores SIGTERM | Exit 137, after 8.1 s (SIGKILL after 2 s) | `setsid` child |
| `ManagedProcess.kill()` | Nothing stopped | Everything, including the wrapper |
| `communicate()` gives up before the wrapper | Returns 137 with `timed_out=True` after 7.1 s | Everything: the run keeps going |
| `ManagedProcess.cancel()` | Waits for the wrapper: returned 11.7 s after the cancel, with a 10 s budget | Child that ignores SIGTERM; `setsid` child |

Why `kill()` does nothing:

- `exec_inspect` reports the wrapper's **host** PID. In this run it was 6434, while the wrapper's PID inside the container was 7.
- `_kill_pid` runs `kill -KILL 6434` inside the container's own PID namespace, where no process has that PID.
- Its first try, `kill` without a shell, exited 127 because this image has no `kill` binary.
- Its second try, `/bin/sh -lc "kill -KILL 6434"`, came back with exit code `None`. `exec_run` returns before the command finishes, and `_kill_pid` accepts `None` as success.
- Run again with its output attached, the same command exits 1: `kill: No such process`.

What that means for the design:

- In container mode, only the `timeout` wrapper and stopping or removing the container end a run.
  - A stopped project is still cleaned up, because the scheduler stops its container (`_cleanup_stopped_containers`).
  - When a heartbeat lease is lost, the lease calls `kill()` (`runtime/heartbeat.py:112-123`). That does nothing, so the run keeps going until its wrapper deadline, while the intent may already be dispatched again.
- Children that ignore SIGTERM, or that start their own session (`setsid`), outlive the wrapper and keep running inside the container.
  - A SIGTERM-ignoring child dies only when the agent itself also ignores SIGTERM, because only then does the wrapper send SIGKILL to the whole group.
- Docker reported the run as still going about 2 s past the deadline whenever a surviving child still held the run's output. With no survivor, or a survivor with its own stdio, it ended right at the deadline.
- Local mode behaves differently: `LocalProcess` signals its own process group. An earlier local run showed the SIGTERM-ignoring and `setsid` children surviving there too.

## 2. Session ids

Read `SESSION_IDS.md`. In short:

- claudecode's id is chosen by the dispatcher before the run starts.
- codex and pi print their ids, and today the dispatcher reads them only after the run ends.
- In local mode, the output reader cannot see them early either.

## 3. Baseline from the dispatcher logs

First check that the message patterns still match your checkout:

```bash
uv run --project cairn python tools/repair-phase0/baseline_from_logs.py --check-formats
```

Then pass the log files, oldest first (`.gz` is fine). Add `--project proj_027` to look at one
project only.

```bash
uv run --project cairn python tools/repair-phase0/baseline_from_logs.py dispatcher.log --out-dir phase0-baseline
```

It prints a summary and writes two files:

- `phase0-baseline/baseline_timeline.csv`: one row per explore or bootstrap attempt.
- `phase0-baseline/baseline_summary.json`

Neither file contains output previews, so both are safe to share. Lines without a log header (for
example tracebacks) are skipped and counted.

## 4. Evaluation set and labels

For one project, export every explore run that hit its limit. The server must be reachable. If it
needs a login, save `GET /projects/<id>` yourself and pass it with `--project-json` instead of `--server`.

```bash
uv run --project cairn python tools/repair-phase0/export_eval_set.py --project proj_027 --log dispatcher.log --config dispatch.yaml --server http://127.0.0.1:8000
```

It writes `eval-proj_027/`, with one folder per intent:

- `intent.json`: the intent, the fact it produced, the timings and the review window.
- The dispatcher's output previews.
- `transcript.md`: the commands that find and copy the agent's session file.

The folder also holds `labels.csv` and a copy of the labelling guide. From `dispatch.yaml`, only
worker names, worker types and the execution mode are read.

Fill in `labels.csv` by following `LABELLING_GUIDE.md`. The previews and transcripts contain
whatever the agents saw on the target, so keep the folder on your machine. `labels.csv` holds only
your labels and notes.

## Tests

```bash
uv run --project cairn --group dev python -m pytest tools/repair-phase0/tests -q
```
