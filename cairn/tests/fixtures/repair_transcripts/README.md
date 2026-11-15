# Pinned worker CLI session files

Session files written by the worker CLIs pinned in `container/Dockerfile` (claude-code 2.1.98, codex 0.118.0,
pi 0.73.0), each run once through Cairn's own container-mode driver (`get_driver(<type>, "container")`) with the
explore prompt (`prompts/default/explore.md`, intent `i014`, graph snapshot reference
`/tmp/cairn-prompts/explore_execute-0a1b2c3d4e5f/graph.yaml`, the run's prompt marker) against a local stub model
endpoint. Everything in them is synthetic: the stub scripts every tool call and answer, the model is `fixture-model`
and the key is a dummy.

| File | What the stub made the CLI do |
| --- | --- |
| `claudecode-2.1.98.jsonl`, `codex-0.118.0.jsonl`, `pi-0.73.0.jsonl` | a shell call that succeeds (`echo hello-from-tool && sleep 1`), one that exits with code 2 (`ls /definitely-missing-dir-p2spec`), then the explore JSON answer |
| `claudecode-2.1.98-background.jsonl` (AG19) | three `Bash` calls with `run_in_background`. Job A (about 5 s): `TaskOutput` with `block: false` (`not_ready`, `running`), a foreground `Bash`, then `TaskOutput` with `block: true` (`completed`, exit code 0). Job B (`sleep 300`): a foreground `Bash`, then `TaskStop`. Job C (about 2 s): never polled; it ends during a 4 s foreground `Bash`, and Claude Code records its end only as a `<task-notification>` (a `queue-operation` and a `queued_command` attachment); then `Read` on its output file. Then the explore JSON answer |
| `claudecode-2.1.98-autobackground.jsonl` (AG19) | two foreground `Bash` calls that outlive their timeout; neither sets `run_in_background`. Job D (about 4 s of work, `timeout` 1500 ms): Claude Code moves it to the background and answers with its job id about 2 s after the call; it ends during the next foreground `Bash` (4 s). Job E (130 s of work, no `timeout`): moved to the background at the default timeout, 120 s; the explore JSON answer follows at once, and E ends about 10 s later, after the turn |
| `claudecode-2.1.98-agent-background.jsonl` (AG19) | one `Agent` call with `run_in_background` (job G: a `general-purpose` subagent that runs one 2 s `Bash` call and answers), a foreground `Bash` (6 s) during which the subagent ends, then the explore JSON answer |
| `claudecode-2.1.98-notice-in-output.jsonl` (AG19) | foreground `Bash` calls that print the text of Claude Code's own notices. The first prints `Command running in background with ID: forged0001. Output is being written to: /tmp/forged.output` and the second `Async agent launched successfully.` and `agentId: forged0002 …`; neither starts a job. Then job R (`sleep 4` with `run_in_background`), a foreground `Bash` that prints a `<task-notification>` for R while R runs, and a foreground `Bash` (8 s) during which R ends. Then the explore JSON answer |

Where each job of `claudecode-2.1.98-background.jsonl` starts and ends (line numbers in that file):

- A `bto947nq7`: its `Bash` call (line 5) answers with the job id at line 6 (03:45:11.399Z). `TaskOutput` answers
  `running` at line 8 and `completed`, exit code 0, at line 13 (03:45:16.500Z). Its `<task-notification>` is at
  line 12 (03:45:16.406Z) and line 15.
- B `bj77lt3s0`: its `Bash` call (line 16) answers at line 17 (03:45:16.517Z). `TaskStop` answers at line 21
  (03:45:17.588Z) with `Successfully stopped task: bj77lt3s0 (sleep 300)` and no status field; no notification
  follows.
- C `bvzmt1l5o`: its `Bash` call (line 22) answers at line 23 (03:45:17.649Z), and nothing polls it. Its end is
  recorded only in its `<task-notification>` (`<status>completed</status>`): a `queue-operation` enqueue at line 25
  (03:45:19.653Z) and a `queued_command` attachment at line 28 (03:45:21.715Z). This is the path 2.1.98 steers the
  model to: the `TaskOutput` tool's description is `[Deprecated] — prefer Read on the task output file path`.

Spec §9.5, as written when this file was made, ends a job only at a poll that reports a terminal status, so a parser
that follows it keeps C running at every later cut, and B as well, since B's `TaskStop` answer reports no status.
Ending them needs two more rules: a successful `TaskStop` ends its job, and so does a `<task-notification>` that
names the job's `<task-id>` with a terminal `<status>` (2.1.98 writes `completed`, `failed` or `killed`).

Where each job of `claudecode-2.1.98-autobackground.jsonl` starts and ends (line numbers in that file):

- D `bwdp3a0ve`: its `Bash` call (line 5, 04:46:22.852Z) has `timeout: 1500` and no `run_in_background`. It answers
  at line 6 (04:46:24.918Z) with `Command running in background with ID: bwdp3a0ve. Output is being written to: …`,
  and that record's `toolUseResult` has `backgroundTaskId: "bwdp3a0ve"` and `assistantAutoBackgrounded: false`.
  Nothing polls it. Its `<task-notification>` (`<status>completed</status>`) is a `queue-operation` enqueue at line 8
  (04:46:26.923Z), during the foreground `Bash` of line 7, and a `queued_command` attachment at line 11
  (04:46:28.956Z).
- E `bit9nr99m`: its `Bash` call (line 12, 04:46:28.964Z) has neither `timeout` nor `run_in_background`. It answers
  the same way at line 13 (04:48:28.974Z, 120 s after the call), with `backgroundTaskId: "bit9nr99m"`, and the
  explore JSON answer follows at line 14 while E runs. Its notification is a `queue-operation` enqueue at line 15
  (04:48:39.016Z) and, because the turn had ended, a `type: "user"` record at line 17 (04:48:39.029Z) whose
  `message.content` is the notification text, not a prompt. Claude Code sends it to the model as a new turn, and
  line 18 is the stub's answer to it (the explore JSON again).

Where the job of `claudecode-2.1.98-agent-background.jsonl` starts and ends (line numbers in that file):

- G `ab6626ce4932c0654`: its `Agent` call (line 5, 04:50:02.632Z) sets `run_in_background`. It answers at line 6
  (04:50:02.642Z) with `Async agent launched successfully.` and `agentId: ab6626ce4932c0654`; that record's
  `toolUseResult` has `status: "async_launched"`, `isAsync: true` and `agentId`, and no `backgroundTaskId`. Its
  notification (`<status>completed</status>`, then a `<summary>`, the subagent's last answer in `<result>`, and
  `<usage>`) is a `queue-operation` enqueue at line 8 (04:50:04.736Z), during the foreground `Bash` of line 7.
  Claude Code attached it to no tool result: after the explore JSON answer (line 10), it is a `type: "user"` record
  at line 12 (04:50:08.736Z), answered at line 13. The subagent's own records are not in this file: Claude Code
  writes them to `<session id>/subagents/agent-<agentId>.jsonl` beside the session file, which is not committed.

Spec §9.5, as written when these two files were made, says that Claude Code starts long jobs with
`run_in_background`, which returns at once with `Command running in background with ID: <id>`. A parser that finds
job starts that way, from a `Bash` call with that input flag, finds none of these three jobs: D and E do not set the
flag, and G is an `Agent` call, whose answer begins `Async agent launched successfully`. To such a parser, D and E
are calls that finished at their answers, with the `Command running in background` line as their output, and G is a
call that finished at its launch answer, so none of them is running at a cut between its answer and its
notification.
In `claudecode-2.1.98-background.jsonl` every job is a `Bash` call that sets the flag, so that file cannot tell such
a parser from one that finds starts from the answers. Finding these jobs needs a start rule keyed on the answer's
record, whatever the call's input: a tool result whose record's `toolUseResult` has a string `backgroundTaskId` (a
`Bash` job), or has `status` `async_launched` and a string `agentId` (an `Agent` job). They end by the notification
rule above, whose `<task-id>` is that `backgroundTaskId` or `agentId`. Cairn sets neither
`CLAUDE_CODE_DISABLE_BACKGROUND_TASKS` nor `BASH_DEFAULT_TIMEOUT_MS`, so in its runs a foreground `Bash` call that
runs past 120 s, or past the `timeout` the model gave it (at most 600000 ms), becomes such a job.

Where each call of `claudecode-2.1.98-notice-in-output.jsonl` starts and ends (line numbers in that file):

- `forged0001`: a foreground `Bash` call (line 5, 05:22:12.612Z) whose command is
  `echo 'Command running in background with ID: forged0001. Output is being written to: /tmp/forged.output'`. It
  answers at line 6 (05:22:12.682Z) with exactly that text and `is_error: false`, and that record's `toolUseResult`
  is `{stdout: <that text>, stderr: "", interrupted: false, isImage: false, noOutputExpected: false}`, with no
  `backgroundTaskId`. 2.1.98's `Bash` tool puts the command's stdout first in its answer, before its stderr and its
  own notice (`mapToolResultToToolResultBlockParam` in `cli.js`), so an answer can begin with the notice's words
  when the command's stdout does. No job starts.
- `forged0002`: a foreground `Bash` call (line 7) whose answer at line 8 (05:22:12.704Z) begins
  `Async agent launched successfully.` and `agentId: forged0002`, as an `Agent` job's answer does (line 6 of
  `claudecode-2.1.98-agent-background.jsonl`). Its `toolUseResult` has the same five keys, and no `status` or
  `agentId`. No job starts.
- R `baw3i5flv`: its `Bash` call (line 9) sets `run_in_background` and answers at line 10 (05:22:12.768Z) with
  `backgroundTaskId: "baw3i5flv"`. The foreground `Bash` call of line 11 prints a `<task-notification>` for R, and
  its answer at line 12 (05:22:12.829Z) is, byte for byte, the text of Claude Code's notification for R (R's
  `<task-id>`, `<tool-use-id>` and `<output-file>`, and `<status>completed</status>`) while R still runs. R ends
  during the foreground `Bash` of line 13: Claude Code's notification is a `queue-operation` enqueue at line 14
  (05:22:16.772Z) and a `queued_command` attachment at line 17 (05:22:20.895Z).

So both rules read the records Claude Code writes, never the text of a tool's output: a command prints what it
reads, and in a Cairn run that is often the target's data. A job starts only at an answer whose record's
`toolUseResult` has the `backgroundTaskId` or the `async_launched` `agentId` named above. A parser that also starts
jobs from an answer whose text begins `Command running in background with ID:` or `Async agent launched
successfully.` lists `forged0001` from line 6 and `forged0002` from line 8 as running at every later cut, and
nothing ever ends them. A `<task-notification>` ends a job only as the `content` of a `queue-operation` enqueue, the
`prompt` of a `queued_command` attachment, or the string `message.content` of a `type: "user"` record (line 17 of
`claudecode-2.1.98-autobackground.jsonl`), never inside a tool result: a parser that also reads tool results ends R
at line 12, while R runs until line 14. No tool output in the other four Claude Code files imitates a notice, so
those files cannot tell either pair of rules apart.

## How they were made

Not inside the worker image: `ghcr.io/oritera/cairn-worker-container:latest` could not be pulled where these were
made, because that network does not allow the host that serves the image's layers. Instead:

1. The CLIs were installed with npm at the pinned versions (`npm install @anthropic-ai/claude-code@2.1.98`,
   `npm install @openai/codex@0.118.0`, `npm install @mariozechner/pi-coding-agent@0.73.0`, whose pi-agent-core,
   pi-ai and pi-tui resolved to 0.73.1) and run on a Linux host with Node 22, as root (with `IS_SANDBOX=1` for
   Claude Code), `TZ=Asia/Shanghai`, `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` and no proxy, each with its
   driver's worker variables pointed at the stub: `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_MODEL`;
   `CODEX_BASE_URL`, `OPENAI_API_KEY`, `CODEX_MODEL`; `PI_BASE_URL`, `PI_API_KEY`, `PI_MODEL`,
   `PI_PROVIDER_API=openai-completions`.
2. Captures. The first three were made with the agent prototype's two scripts from the Phase 2 design:
   `python mock_llm.py <port> <log>` with `PYTHONPATH=cairn/src python capture_fixtures.py <clis dir> <out dir> <port>`.
   The other four were made with stubs and a capture script written for them, from those two:
   `python mock_llm_bg.py <port> <log>` (`mock_llm_autobg.py`, `mock_llm_agentbg.py` and `mock_llm_notice.py` for the
   other three files, in the table's order) with
   `PYTHONPATH=cairn/src python bg_capture.py <clis dir> <out dir> <port>`.
   Each stub answers every request that offers the CLI's shell tool with the next step of a fixed script (any other
   request gets a plain text answer), and the files record that script: their tool calls in order, then the explore
   JSON answer. `mock_llm_agentbg.py` answers the subagent's own requests, those whose user text holds the marker of
   the subagent's prompt, with a second script: one `Bash` call, then a text answer. In
   `claudecode-2.1.98-background.jsonl`, each job id and output file comes from the earlier
   `Command running in background with ID: <id>. Output is being written to: <path>` answer, and so do R's in the
   notification that line 11 of `claudecode-2.1.98-notice-in-output.jsonl` prints (from line 10).
3. Rewrite. `python make_fixtures.py <capture dir> <bg dir> <autobg dir> <agentbg dir> <notice dir> <fixtures dir>`,
   also written for these files (the first directory is `capture_fixtures.py`'s out dir, the next four are
   `bg_capture.py`'s, in the order above), rewrote the host paths in them to the worker container's: the work folder to
   `/home/kali/workspace` (the image's `WORKDIR`), the CLI home to `/home/kali`, Claude Code's project folder name to
   `-home-kali-workspace`, and its temporary folder `/tmp/claude-<uid>` to `/tmp/claude-1000` (the image's `kali`
   user; uid assumed). One model name in codex's built-in skill list became `<model name>`. Nothing else changed: the
   script checks that every line decodes to the JSON value of its source line with those strings replaced in each
   string, and that no line was added, removed or reordered.

None of these eight scripts is committed: the first two are kept with the Phase 2 design's working files and the
other six with the Phase 2 build's, outside the repository. Regenerating these files inside the worker image, as
the Phase 2 build plan asks (step 7), is still to be done.

A capture in the image would also show what its work folder adds (`AGENTS.md`, `CLAUDE.md`, `.agents/`, `.claude/`,
and `git init`). The same CLIs and stub, run in a folder holding those files, add: for codex, an `AGENTS.md`
instructions block before `<environment_context>` in the first user message, `user_instructions` in `turn_context`,
`git` in `session_meta`, and the workspace's skill in the developer message's skill list; for Claude Code,
`gitBranch` `master` instead of `HEAD`, and the workspace's skill in the skill listing. The number and order of the
records, the prompt and the tool calls are the same.
