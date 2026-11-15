# Repair layer, Phase 0: check before building

Phase 0 of the repair-layer design changes no Cairn code. It answers four questions with your own
setup and data before Phase 2 builds anything on the answers:

1. How does a worker run really stop today, in container mode and in local mode? (`kill_check.py`)
2. When does each worker CLI's session id become known? (`SESSION_IDS.md`)
3. How often do explore runs hit their time limit, and how does the conclude fallback end? (`baseline_from_logs.py`)
4. At the 75–80% mark, were the capped runs progressing or stuck? (`export_eval_set.py` plus your labels, using `LABELLING_GUIDE.md`)

The labels from step 4 are the answer key for Phase 2's shadow mode, where the repair agent decides
but nothing is applied.

## Before you start

Run everything from the repository root, with the same Python environment that runs Cairn. The
tools import the dispatcher code from `cairn/src` of this checkout. `kill_check.py` needs Docker on
the same machine in container mode; its local mode needs no Docker.

If you run the dispatcher with Docker Compose, save its log first:

```bash
docker logs cairn-dispatcher > dispatcher.log 2>&1
```

## 1. Kill check

`kill_check.py` checks container mode by default. If you run Cairn with `runtime.execution: local`,
use its local mode as well: see [Local mode](#local-mode).

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

### What container mode found here

These results are from `debian:bookworm-slim` on Docker 29.9.2, in the development sandbox, with the
fix to `ManagedProcess.kill()` described below. Run it with your worker image to confirm.

| Scenario | Result | Still alive in the container afterwards |
|---|---|---|
| Wrapper expires (`timeout -k 2s 4s`), agent exits on SIGTERM | Exit 124, run over after 6.1 s | Child that ignores SIGTERM; `setsid` child |
| Wrapper expires, agent ignores SIGTERM | Exit 137, after 8.1 s (SIGKILL after 2 s) | `setsid` child |
| `ManagedProcess.kill()` | The call took 0.09 s; Docker reported the run over 2.0 s later | `setsid` child |
| `communicate()` gives up before the wrapper | Returns 137 with `timed_out=True` after 4.1 s, and the run is over | `setsid` child |
| `ManagedProcess.cancel()` | `communicate()` returned 137, 2.1 s after the cancel | `setsid` child |

Before that fix, `kill()` and `cancel()` stopped nothing in container mode:

- `exec_inspect` reports the wrapper's **host** PID. In an earlier run before the fix (same image and Docker version) it was 6434, while the wrapper's PID inside the container was 7.
- The old `_kill_pid` ran `kill -KILL 6434` inside the container's own PID namespace, where no process has that PID.
- Its first try, `kill` without a shell, exited 127 because this image has no `kill` binary. Its second try, `/bin/sh -lc "kill -KILL 6434"`, came back with exit code `None`, because `exec_run` returns before the command finishes, and `_kill_pid` accepted `None` as success. Run again with its output attached, the same command exits 1: `kill: No such process`.
- So `kill()` stopped nothing, `communicate()`'s timeout returned 137 after 7.1 s while the run went on, and after `cancel()` the run went on until its wrapper's 10 s deadline: `communicate()` returned 124, 11.7 s after the cancel.

Now `kill()` runs one short helper exec in the run's container. It sends SIGKILL to the process group of the run's `timeout` wrapper, the group the wrapper itself signals at its deadline, once it has checked that the wrapper is this run's (`ManagedProcess.kill()` in `cairn/src/cairn/dispatcher/runtime/process.py`). On dispatcher code from before the fix, `managed_kill` still records what `_kill_pid` ran and which exit code it accepted.

What that means for the design:

- `kill()` and `cancel()` end a container run, and so does a lost heartbeat lease, which calls `kill()` (`runtime/heartbeat.py:112-123`). That holds on images like this one: the helper needs `/bin/sh`, `sha256sum`, a readable `/proc`, and a `timeout` whose process keeps the run's command line (GNU coreutils as here, or uutils). With BusyBox's `timeout` (Alpine and BusyBox images), which replaces itself with the command, it finds nothing to kill: the run goes on until its wrapper's deadline, the dispatcher logs a warning, and `managed_kill` shows the run still going.
- Children that start their own session (`setsid`) outlive every path, the wrapper's own deadline included: nothing signals their group. They keep running inside the container until it is stopped or removed; a stopped project's container is stopped by the scheduler (`_cleanup_stopped_containers`).
- A child that ignores SIGTERM outlives the wrapper's deadline unless the agent is still running K s after the SIGTERM (as when it ignores SIGTERM too): only then does GNU `timeout`, as in this image, send SIGKILL to the whole group. The uutils `timeout` of Ubuntu 26.04 sends that SIGKILL to the agent alone, so there every child that ignores SIGTERM survives the deadline. It does not outlive `kill()`, which sends SIGKILL to the whole group.
- Docker reported the run as still going about 2 s past its end whenever a surviving child still held the run's output. With no survivor, or a survivor with its own stdio, it ended at once.
- Local mode behaves differently: see below.

### Local mode

With `runtime.execution: local`, workers are processes on your own machine, started by
`LocalBackend` and `LocalProcess`. Local mode runs the same kind of stand-in agent and children
through that code, and needs no Docker. It runs four scenarios in about a minute:

- `timeout_expiry`: `LocalProcess`'s own timeout expires, and the agent exits on SIGTERM.
- `timeout_expiry_stubborn`: the same, but the agent ignores SIGTERM, so SIGKILL follows after the grace period.
- `local_kill`: `LocalProcess.kill()` while `communicate()` waits on another thread, as when a heartbeat lease is lost.
- `cancel`: `TaskCancellation.cancel()`, which is how the scheduler stops a task whose project is no longer active.

Container mode's `communicate_timeout` scenario has no local counterpart:
`LocalProcess.communicate()` waits for the process's own timeout and ignores the longer one that the
dispatcher passes in.

```bash
uv run --project cairn python tools/repair-phase0/kill_check.py --mode local
```

It prints the same table as container mode, plus a `flags` column with `timed_out` and
`cancelled`, and writes `kill_check_local_results.json`. A `--json-out` path in a directory that
does not exist is refused before anything starts. To see what it would start, without starting
anything:

```bash
uv run --project cairn python tools/repair-phase0/kill_check.py --mode local --dry-run
```

It runs on your machine, so it only ever touches processes it started itself:

- It knows each process it starts by the PID it learned when starting it: the agent's from Python's
  `Popen`, and each test child's from a file the child writes, as its first step, into its working
  directory. That directory is inside a new temporary directory that only your user can enter.
- A process counts as its own only if it has one of those PIDs and its command line still ends with
  exactly the one the tool built for it (everything after the program name, which the macOS
  framework Python rewrites). A process the tool did not start is never touched, whatever its
  command line says. That includes a `grep` or `pgrep` you run to watch the tool, and even a
  `watch pgrep -f "$(ps -o args= -p <pid>)"` that repeats one of its command lines word for word.
- Those command lines carry this run's random marker, for example
  `cairn-killcheck-local-8842903dc305873d`, which the tool prints when it starts.
- To stop its processes, it first asks them to exit, with a `stop` file in their working directory,
  and waits about a second. Only a process still running then gets SIGKILL, and only after its
  command line has been read again right before the signal. It reads `/proc` on Linux and `ps` on
  macOS.
- On Linux (kernel 5.3 or later) that signal goes through a pidfd, so it cannot reach another
  process that took over the PID in between. macOS has no pidfd, so this guarantee is Linux-only: on
  macOS, a process that took over the PID in that instant could be hit. That is why the tool asks
  first and kills only what did not exit.
- Each scenario stops whatever it left running. At the end, a final cleanup confirms that none of the
  run's processes is left, and the tool says so. It confirms that only when it knows every test
  child's PID, or when the agent has exited by itself, which it does only once all its children have
  ended. A child that is still starting up is waited for, up to 15 s after the agent started.
  Otherwise the tool prints a warning naming what it could not confirm.
- The first Ctrl-C, SIGTERM or SIGHUP stops the run, and everything is cleaned up first. This holds
  from before the tool creates anything. One that comes while a scenario cleans up waits until that
  cleanup has finished, so the summary counts what it found. Any that follow are ignored, so the tool
  still prints what it cleaned up. It then exits with code 130 and writes no results.
- Each test child also exits by itself within a second once the tool's process or its temporary
  directory is gone, even after `kill -9`, and after a few minutes in any case.
- The runs' working directories are in that temporary directory, which the tool removes at the end.
  Only `kill -9` or another fatal signal, such as `Ctrl-\` (SIGQUIT), leaves it behind:
  `cairn-phase0-local-*` in your temporary directory, holding a few small files.

A scenario reports an error instead of results whenever its results could be wrong:

- the agent and its three children did not all start and set themselves up within 15 s, or the agent
  exited before they had;
- `communicate()` raised;
- `communicate()` had not returned by the end of the scenario's time (with the defaults, 28 s after
  the call in the timeout scenarios, 24 s after `kill()` or the cancel): in the dispatcher, the task
  thread would hang;
- its working directory was removed while it ran, for example by cleaning up `cairn-phase0-local-*`
  during a run, which also makes the test children exit;
- a test child exited on its own before the scenario ended, for example because the tool was
  suspended with Ctrl-Z for longer than the children live;
- one of its test processes was stopped by something else, for example the OOM killer or a `pkill`
  in another terminal:
  - The tool checks that all four still run right before the kill path starts. In the timeout
    scenarios it also checks that the agent did not exit before the timeout could fire, and that
    `communicate()` reports `timed_out=True`.
  - It notes every signal that `LocalProcess` sends, with the agent's process group right before it
    goes out. From the moment all four test processes run until the survivors are counted, it lists
    them about every 0.2 s. That covers the grace period before the SIGKILL in
    `timeout_expiry_stubborn`, and the 10 s that `communicate()` can go on waiting for the output
    pipes.
  - A test process that is gone at the end counts as stopped by the kill path only if one of those
    signals explains it. The signal must have gone to that process's group, and it must end that
    process: SIGTERM does not end the child that ignores it, nor anything in
    `timeout_expiry_stubborn`, whose children inherit the agent's `trap '' TERM`. It must also have
    been sent at most half a second before the last listing that still showed the process, and
    before the end of the first listing that no longer did. For the agent, its exit status must
    name that same signal. The `setsid` child is in no group that the path signals, so its end is
    never accepted.
  - A test process that survives a signal noted for its own process group, which ends it, is an
    error too, when the agent had already been reaped as that signal went out: then the signal never
    reached it. This happens when the agent is ended from outside and reaped in the instant before
    `LocalProcess` sends the signal: `LocalProcess` then finds no process group and sends nothing.
  - Not covered: a kill from outside up to about 0.2 s before, or 0.7 s after, the path's own
    signal to a process that this signal ends anyway. The tool cannot tell those two apart, but
    the answer is the same: that process does not survive the path. If that kill keeps the path's
    signal from going out at all, the rest of the group survives it, which is reported as an error
    (above).

It exits with code 0 when every scenario gave results and the cleanup is confirmed, and 1 otherwise.
If it ever reports processes it could not stop, it prints a `pgrep` command to look for them and a
`pkill` command to stop them once you have checked that list. That `pkill` matches command lines
that end the way the tool's do, and reads them when it runs. It never gives you PIDs to kill later:
by then a PID may belong to another process.

To check by hand, list the processes that mention a local marker, with their command lines. No
output means none are running. On Linux, `-a` shows the command lines (`-l` there shows only the
process names):

```bash
pgrep -fa cairn-killcheck-local-
```

On macOS, `-l` shows them (`-a` there means something else):

```bash
pgrep -fl cairn-killcheck-local-
```

### What local mode found here

These results are from Linux, Python 3.13 and `dash` as `/bin/sh`, in the development sandbox, with
the defaults (`--timeout 4 --kill-after 2`). Run it on your machine to confirm, especially on macOS.

| Scenario | Result | Still alive afterwards |
|---|---|---|
| `LocalProcess` timeout (4 s), agent exits on SIGTERM | Exit -15, `timed_out=True`. Agent gone at 4.0 s, `communicate()` returned at 14.0 s | Child that ignores SIGTERM; `setsid` child |
| Timeout, agent ignores SIGTERM | Exit -9, `timed_out=True`. SIGKILL to the group after the 2 s grace: agent gone at 6.0 s, returned at 16.0 s | `setsid` child |
| `LocalProcess.kill()` | Exit -15, `timed_out=False`, `cancelled=False`. Agent gone at once, returned 10.0 s after the call | Child that ignores SIGTERM; `setsid` child |
| `TaskCancellation.cancel()` | Exit -15, `cancelled=True`. Agent gone at once, returned 10.0 s after the cancel | Child that ignores SIGTERM; `setsid` child |

What that means for the design:

- As in container mode since the fix, every kill path stops the agent itself. Unlike container mode's `kill()` and `cancel()`, which send SIGKILL to the whole group at once, `LocalProcess.kill()` and `cancel()` start with SIGTERM, so a child that ignores SIGTERM survives them.
- `LocalProcess` sends SIGTERM to the agent's process group, and SIGKILL only if the agent is still alive after the grace period.
  - A child that ignores SIGTERM survives unless the agent ignores SIGTERM too.
  - A child in its own session (`setsid`) always survives, because it is not in that group.
- Survivors are processes on your machine, and nothing in Cairn stops them later: `LocalBackend` has no container to remove.
- While a survivor holds the run's stdout or stderr, `communicate()` returns 10 s after the agent has gone. `LocalProcess` waits up to 5 s for each stream (`STREAM_JOIN_TIMEOUT_SECONDS`), so the task thread is held that much longer.
- A run ended by a signal reports a negative exit code (-15 or -9), never 124 or 137. `did_timeout()` therefore rests on the `timed_out` flag alone: true after the timeout, false after `kill()` and `cancel()`.

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

The local-mode tests start real processes, recognised and cleaned up the same way as the tool's own,
and decoys that mention a marker or repeat one of the tool's command lines, to check that the tool
leaves those alone. Others stop test processes from outside, before, during and right after the
kill path, interrupt cleanups, or kill the tool, to check that no wrong answer comes out and nothing
is left running. One of them runs the whole local check, which takes about 50 s; the suite takes
about four to five minutes. They are skipped where local mode cannot run.
