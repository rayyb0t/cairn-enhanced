#!/usr/bin/env python3
"""End-to-end smoke of shadow mode (spec §16, XC5), as one command: a real run of the server, the dispatcher and the
repair agent on this machine, with a fake worker CLI and a stub reviewer, both on loopback.

Run it from the repository root in the repair agent's environment, whose dev group holds cairn too:

    uv run --project repair-agent python tools/repair-phase2/smoke_shadow.py

It takes about seven minutes, mostly the run's 360 s budget (the smallest budget reviewed is 350 s, spec Q5), and
touches nothing of yours: everything lives in a new temporary folder (`--keep` keeps it), every process gets an
environment without proxy variables, and only loopback addresses are used.

- **Worker.** Local mode, one claudecode worker for reason and explore. `claude` on the dispatcher's PATH is a fake
  written into the folder: for a reason run it proposes one intent (later, completes the project); for an explore run
  it writes a claudecode-format session file under the folder's CLAUDE_CONFIG_DIR, as Claude Code 2.1.98 does (the
  prompt, a finished call, a message, a call that keeps running), and sleeps until the dispatcher's timeout stops it;
  for the conclude fallback it answers with a fact.
- **Server.** `cairn serve` on a free port and a database in the folder, with both role tokens (spec §7).
- **Dispatcher.** `cairn dispatch` with a dispatch.yaml whose `repair:` section lists one reviewer, a stub
  OpenAI-compatible server in this process, which answers `extend` citing the first tool call when the digest lists
  one. The dispatcher writes the agent's settings file at its start (spec §8.3).
- **Agent.** `cairn-repair --allow-shared-user` (it runs as you here), reading that settings file.

It creates one project with Repair on and waits for each step of the shadow path: the run registered, its transcript
copied under `<root>/<project_id>/<run_id>/`, the agent's review recorded, the `repair_review` issue on the intent, the
run's `/end` and its outcome. Exit 0 when every step showed within `--timeout`, else 1 with the missing steps and the
logs' last lines; 2 when something could not start.

Nothing it starts outlives it. The server, the dispatcher and the agent stay in its process group, so a signal to the
group (Ctrl-C, timeout(1)) reaches them too. SIGTERM or SIGHUP to the smoke alone stops them, as its end does, and
removes the folder (unless `--keep`): exit 128 + the signal's number. On Linux each of them also asks the kernel for a
SIGTERM when the smoke dies (prctl PR_SET_PDEATHSIG, kept across exec), so even a SIGKILL of the smoke alone, which no
handler sees, stops them; only the folder is then left. The fake worker exits when the dispatcher does.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import requests

BUDGET_SECONDS = 360
MIN_REVIEWED_BUDGET = 350
CONCLUDE_SECONDS = 60
POLL_SECONDS = 2
STARTUP_SECONDS = 60
STOP_SECONDS = 15
MODEL = "smoke-test-model"
STEPS = ("registered", "copied", "reviewed", "issue", "ended", "outcome")
PROXY_VARIABLES = ("http_proxy", "https_proxy", "all_proxy", "no_proxy", "ftp_proxy")
INTENT = "Smoke: list the files of the lab folder"
EXIT_OK = 0
EXIT_MISSING = 1
EXIT_START = 2
STOP_SIGNALS = (signal.SIGTERM, signal.SIGHUP)

# What every process the smoke starts runs first: on Linux, ask for SIGTERM when the parent, the smoke, dies
# (prctl(PR_SET_PDEATHSIG), which exec keeps), check that the smoke did not die before that, then exec the command
# itself, which keeps this process id. Elsewhere there is no prctl, and the smoke's own signal handlers are all.
DIE_WITH_PARENT = r"""
import ctypes, os, signal, sys
try:
    ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGTERM, 0, 0, 0)  # 1 is PR_SET_PDEATHSIG
except (AttributeError, OSError):
    pass
if os.getppid() != int(sys.argv[1]):
    sys.exit(1)
os.execvp(sys.argv[2], sys.argv[2:])
"""

# The fake `claude`: argv as dispatcher/workers/adapters/claudecode.py builds it ("--session-id ID ... -p -- PROMPT"
# for a run, "-r ID ... -p -- PROMPT" for the conclude fallback), and session files where Claude Code 2.1.98 writes
# them: ${CLAUDE_CONFIG_DIR:-~/.claude}/projects/<cwd, every character outside [a-zA-Z0-9] as ->/<session id>.jsonl.
FAKE_CLAUDE = r"""
import datetime
import json
import os
import re
import sys
import time

args = sys.argv[1:]
if args[:1] in (["--help"], ["--version"]):  # the dispatcher's start-up check in local mode
    print("fake claude for the repair smoke (synthetic)")
    sys.exit(0)
prompt = args[args.index("--") + 1] if "--" in args and args.index("--") + 1 < len(args) else ""
if "--session-id" in args:
    session = args[args.index("--session-id") + 1]
elif "-r" in args:
    session = args[args.index("-r") + 1]
else:
    print("fake claude: no session", file=sys.stderr)
    sys.exit(2)
config = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.environ["HOME"], ".claude")
cwd = os.path.realpath(os.getcwd())
folder = os.path.join(config, "projects", re.sub(r"[^a-zA-Z0-9]", "-", cwd))
os.makedirs(folder, exist_ok=True)
path = os.path.join(folder, session + ".jsonl")


def now():
    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def write(record):
    record.update(timestamp=now(), sessionId=session, cwd=cwd)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")


def assistant(content):
    write({"type": "assistant", "message": {"role": "assistant", "model": "MODEL", "content": content}})


def answer(payload):
    text = json.dumps(payload)
    assistant([{"type": "text", "text": text}])
    print(text)
    sys.exit(0)


write({"type": "user", "message": {"role": "user", "content": prompt}})
if "-r" in args:  # the conclude fallback
    answer({"accepted": True, "data": {"description": "Smoke: the lab folder holds lab.conf (synthetic)."}})
if "### Open Intents" in prompt:  # a reason run: one intent, then complete the project
    state = os.path.join(os.environ["SMOKE_STATE_DIR"], "reasoned")
    if not os.path.exists(state):
        open(state, "w").close()
        answer({"accepted": True, "data": {"intents": [{"from": ["origin"], "description": "INTENT"}]}})
    answer({"accepted": True, "data": {"complete": {"from": ["origin"], "description": "Smoke finished (synthetic)."}}})
# An explore run: a finished call, a message, and a call that keeps running until the timeout stops this process.
assistant([{"type": "tool_use", "id": "toolu_smoke_1", "name": "Bash", "input": {"command": "ls -la lab"}}])
write(
    {
        "type": "user",
        "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_smoke_1", "content": "lab.conf", "is_error": False}]},
        "toolUseResult": {"stdout": "lab.conf", "stderr": "", "interrupted": False},
    }
)
assistant([{"type": "text", "text": "Listed the lab folder; unpacking the lab archive next."}])
assistant([{"type": "tool_use", "id": "toolu_smoke_2", "name": "Bash", "input": {"command": "tar -xf lab-archive.tar"}}])
parent = os.getppid()
deadline = time.monotonic() + 1800
while time.monotonic() < deadline and os.getppid() == parent:
    time.sleep(1)
""".replace('"MODEL"', json.dumps(MODEL)).replace('"INTENT"', json.dumps(INTENT))


@dataclass
class Reviewer:
    """The stub reviewer: an OpenAI-compatible endpoint on loopback, answering extend when it can cite E1."""

    requests: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)
    server: ThreadingHTTPServer | None = None

    def start(self) -> str:
        stub = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                with stub.lock:
                    stub.requests += 1
                try:
                    digest = json.loads(body)["messages"][-1]["content"]
                except (ValueError, KeyError, IndexError, TypeError):
                    digest = ""
                if re.search(r"^E1 ", digest, re.MULTILINE):
                    decision = {
                        "decision": "extend",
                        "reason": "The run listed the lab folder and is unpacking the archive (smoke stub).",
                        "evidence": ["E1 the lab folder was listed"],
                        "next_step": "finish unpacking the lab archive",
                    }
                else:
                    decision = {
                        "decision": "continue",
                        "reason": "Nothing to cite (smoke stub).",
                        "evidence": [],
                        "next_step": "",
                    }
                reply = {
                    "model": MODEL,
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": json.dumps(decision)},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 100000, "completion_tokens": 60},
                }
                data = json.dumps(reply).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, format: str, *args: Any) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, name="smoke-reviewer", daemon=True).start()
        return f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def stop(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def clean_environment(**extra: str) -> dict[str, str]:
    """The environment of every process the smoke starts: no proxy variable, so nothing leaves loopback."""
    environment = {name: value for name, value in os.environ.items() if name.lower() not in PROXY_VARIABLES}
    environment["NO_PROXY"] = environment["no_proxy"] = "*"
    environment.update(extra)
    return environment


def write_secret(path: Path) -> str:
    value = secrets.token_hex(32)
    path.write_text(value + "\n", encoding="ascii")
    path.chmod(0o600)
    return value


@dataclass
class Layout:
    base: Path
    port: int
    reviewer_url: str
    budget: int

    @property
    def server(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def path(self, *parts: str) -> Path:
        return self.base.joinpath(*parts)

    def write(self) -> str:
        """Every file of the smoke; returns the agent token."""
        for folder in ("bin", "home", "claude-config", "work", "copies", "secrets", "state", "agent-state", "logs"):
            self.path(folder).mkdir(mode=0o700)
        self.path("copies").chmod(0o750)
        fake = self.path("bin", "claude")
        fake.write_text(f"#!{sys.executable}\n{FAKE_CLAUDE}", encoding="utf-8")
        fake.chmod(0o700)
        agent_token = write_secret(self.path("secrets", "agent.token"))
        write_secret(self.path("secrets", "dispatcher.token"))
        self.path("dispatch.yaml").write_text(
            f'server: "{self.server}"\n'
            "runtime:\n  execution: local\n  worker_healthcheck: disabled\n  max_workers: 1\n"
            "  max_running_projects: 1\n  max_project_workers: 1\n  interval: 2\n  healthcheck_timeout: 5\n"
            '  prompt_group: "default"\n'
            "tasks:\n  bootstrap:\n    timeout: 120\n    conclude_timeout: 30\n  reason:\n    timeout: 45\n"
            f"  explore:\n    timeout: {self.budget}\n    conclude_timeout: {CONCLUDE_SECONDS}\n"
            f'local:\n  workspace_root: "{self.path("work")}"\n  completed_action: keep\n'
            "workers:\n"
            '  - name: "smoke-claude"\n    type: "claudecode"\n    task_types: [reason, explore]\n'
            "    max_running: 1\n    priority: 0\n"
            f'    env:\n      CLAUDE_CONFIG_DIR: "{self.path("claude-config")}"\n'
            "repair:\n"
            f'  token_file: "{self.path("secrets", "dispatcher.token")}"\n'
            f'  transcript_root: "{self.path("copies")}"\n'
            "  reviewers:\n"
            '    - name: "smoke-reviewer"\n'
            "      env:\n"
            f'        REVIEW_MODEL: "{MODEL}"\n'
            f'        REVIEW_BASE_URL: "{self.reviewer_url}"\n',
            encoding="utf-8",
        )
        self.path("agent-state").chmod(0o700)  # where the agent pins its reviewer: its own, 0700
        self.path("repair.yaml").write_text(
            f'token_file: "{self.path("secrets", "agent.token")}"\n'
            f'dispatch_config_to_check: "{self.path("dispatch.yaml")}"\n'
            "poll_interval: 2\n"
            "transcripts:\n"
            f'  root: "{self.path("copies")}"\n'
            "state:\n"
            f'  dir: "{self.path("agent-state")}"\n',
            encoding="utf-8",
        )
        return agent_token


@dataclass
class Process:
    name: str
    popen: subprocess.Popen[bytes]
    log: Path
    handle: Any = field(repr=False, default=None)

    def tail(self, lines: int = 25) -> list[str]:
        try:
            return self.log.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]
        except OSError:
            return []

    def stop(self) -> None:
        try:
            if self.popen.poll() is None:
                self.popen.send_signal(signal.SIGTERM)
                try:
                    self.popen.wait(STOP_SECONDS)
                except subprocess.TimeoutExpired:
                    self.popen.kill()
                    self.popen.wait(STOP_SECONDS)
        finally:
            if self.handle is not None:
                self.handle.close()


def start(name: str, argv: list[str], layout: Layout, environment: dict[str, str]) -> Process:
    """Start one process, in the smoke's process group, through DIE_WITH_PARENT, so it dies with the smoke."""
    log = layout.path("logs", f"{name}.log")
    handle = open(log, "wb")  # noqa: SIM115 - the child writes to it; Process.stop closes it
    launcher = [sys.executable, "-c", DIE_WITH_PARENT, str(os.getpid()), *argv]
    try:
        popen = subprocess.Popen(launcher, cwd=layout.base, env=environment, stdout=handle, stderr=subprocess.STDOUT)
    except OSError:
        handle.close()
        raise
    return Process(name, popen, log, handle)


class Stopped(BaseException):
    """SIGTERM or SIGHUP reached the smoke: it stops what it started and removes its folder, as at its end. A
    BaseException, like KeyboardInterrupt, so that no `except Exception` on the way swallows it."""

    def __init__(self, signum: int) -> None:
        super().__init__(signal.Signals(signum).name)
        self.signum = signum


def install_stop_handlers() -> dict[signal.Signals, Any]:
    """Turn SIGTERM and SIGHUP into Stopped, once: later ones are ignored while the smoke stops its processes.
    Returns the handlers they replace, for restore_handlers."""

    def stop(signum: int, _frame: object) -> None:
        for name in STOP_SIGNALS:
            signal.signal(name, signal.SIG_IGN)
        raise Stopped(signum)

    return {name: signal.signal(name, stop) for name in STOP_SIGNALS}


def restore_handlers(previous: dict[signal.Signals, Any]) -> None:
    for name, handler in previous.items():
        signal.signal(name, handler)


def wait_for(condition: Callable[[], bool], seconds: float, processes: list[Process]) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        if any(process.popen.poll() is not None for process in processes):
            return False
        time.sleep(0.5)
    return False


@dataclass
class Smoke:
    layout: Layout
    agent_token: str
    session: requests.Session
    project_id: str | None = None
    run_id: str | None = None
    seen: dict[str, str] = field(default_factory=dict)
    started: float = field(default_factory=time.monotonic)

    def get(self, path: str, *, agent: bool = False, **params: str) -> Any:
        headers = {"X-Cairn-Repair-Token": self.agent_token} if agent else {}
        response = self.session.get(self.layout.server + path, headers=headers, params=params or None, timeout=10)
        response.raise_for_status()
        return response.json()

    def note(self, step: str, detail: str) -> None:
        if step in self.seen:
            return
        self.seen[step] = detail
        sys.stdout.write(f"[{time.monotonic() - self.started:6.1f} s] {step}: {detail}\n")
        sys.stdout.flush()

    def check(self) -> None:
        """One look at the server and the copy root, noting every step that has happened."""
        project_id = self.project_id
        assert project_id is not None
        if self.run_id is None:
            runs = self.get("/repair/runs", agent=True, project_id=project_id, include_ended="true")["runs"]
            if not runs:
                return
            self.run_id = runs[-1]["run_id"]
            self.note(
                "registered", f"run={self.run_id} budget={runs[-1]['budget_seconds']}s cli={runs[-1]['cli_type']}"
            )
        run_id = self.run_id
        assert run_id is not None
        run = self.get(f"/repair/runs/{run_id}", agent=True)
        folder = self.layout.path("copies", project_id, run_id)
        if (folder / "meta.json").is_file() and (folder / "transcript.jsonl").is_file():
            copied = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
            if copied.get("status") == "ok":
                self.note(
                    "copied",
                    f"{project_id}/{run_id}/transcript.jsonl at {copied.get('copied_elapsed_seconds')} s, "
                    f"located_by={copied.get('located_by')}",
                )
        review = next((item for item in run.get("reviews", []) if item.get("window") == 1), None)
        if review is not None and review.get("source") == "agent":
            self.note(
                "reviewed",
                f"decision={review.get('decision')} fallback={review.get('fallback')} model={review.get('model')}",
            )
        project = self.get(f"/projects/{project_id}")
        issues = [
            issue
            for intent in project.get("intents", [])
            for issue in intent.get("issues", [])
            if issue.get("kind") == "repair_review" and issue.get("run_id") == run_id
        ]
        if issues:
            detail = issues[0].get("detail", {})
            self.note(
                "issue",
                f"repair_review decision={detail.get('decision')} mode={detail.get('mode')} applied={detail.get('applied')}",
            )
        if run.get("ended_at") is not None:
            self.note("ended", f"end_reason={run.get('end_reason')}")
        if run.get("outcome") is not None:
            self.note("outcome", f"outcome={run.get('outcome')}")


def run(layout: Layout, timeout: float) -> int:
    agent_token = layout.write()
    session = requests.Session()
    session.trust_env = False
    processes: list[Process] = []
    smoke = Smoke(layout, agent_token, session)
    try:
        server_env = clean_environment(
            CAIRN_REPAIR_AGENT_TOKEN_FILE=str(layout.path("secrets", "agent.token")),
            CAIRN_REPAIR_DISPATCHER_TOKEN_FILE=str(layout.path("secrets", "dispatcher.token")),
        )
        cairn = [sys.executable, "-c", "import sys; from cairn.cli import main; sys.exit(main())"]
        processes.append(
            start(
                "server",
                [
                    *cairn,
                    "serve",
                    "--port",
                    str(layout.port),
                    "--db-path",
                    str(layout.path("cairn.db")),
                    "--no-access-log",
                ],
                layout,
                server_env,
            )
        )

        def server_ready() -> bool:
            try:
                answer = session.get(layout.server + "/repair/settings", timeout=2).json()
            except (requests.RequestException, ValueError):
                return False
            return answer.get("roles_configured") == {"agent": True, "dispatcher": True}

        if not wait_for(server_ready, STARTUP_SECONDS, processes):
            return report_start_failure("the server did not start with both role tokens", processes)
        sys.stdout.write(f"server {layout.server}: both role tokens configured\n")
        dispatcher_env = clean_environment(
            PATH=f"{layout.path('bin')}{os.pathsep}{os.environ.get('PATH', '')}",
            HOME=str(layout.path("home")),
            SMOKE_STATE_DIR=str(layout.path("state")),
        )
        processes.append(
            start(
                "dispatcher",
                [*cairn, "dispatch", "--config", str(layout.path("dispatch.yaml"))],
                layout,
                dispatcher_env,
            )
        )
        if not wait_for(lambda: layout.path("copies", "agent-settings.json").is_file(), STARTUP_SECONDS, processes):
            return report_start_failure("the dispatcher wrote no agent settings file", processes)
        sys.stdout.write("dispatcher: agent settings file written\n")
        agent_argv = [
            sys.executable,
            "-m",
            "cairn_repair_agent.cli",
            "--config",
            str(layout.path("repair.yaml")),
            "--allow-shared-user",
        ]
        processes.append(start("agent", agent_argv, layout, clean_environment()))
        if not wait_for(
            lambda: "repair agent started" in layout.path("logs", "agent.log").read_text(errors="replace"),
            STARTUP_SECONDS,
            processes,
        ):
            return report_start_failure("the repair agent did not start", processes)
        sys.stdout.write("agent: started\n")
        created = session.post(
            layout.server + "/projects",
            json={
                "title": "repair smoke",
                "origin": "A lab folder (synthetic).",
                "goal": "List its files (synthetic).",
                "bootstrap_enabled": False,
                "repair_enabled": True,
            },
            timeout=10,
        )
        created.raise_for_status()
        smoke.project_id = created.json()["project"]["id"]
        sys.stdout.write(
            f"project {smoke.project_id}: Repair on; waiting for the shadow path (about {layout.budget + 30} s)\n"
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and len(smoke.seen) < len(STEPS):
            try:
                smoke.check()
            except (requests.RequestException, ValueError, KeyError, OSError) as exc:
                sys.stdout.write(f"  (server read failed: {type(exc).__name__}; retrying)\n")
            if any(process.popen.poll() is not None for process in processes):
                break
            time.sleep(POLL_SECONDS)
        missing = [step for step in STEPS if step not in smoke.seen]
        if missing:
            sys.stdout.write(f"MISSING steps: {', '.join(missing)}\n")
            for process in processes:
                state = "running" if process.popen.poll() is None else f"exited {process.popen.returncode}"
                sys.stdout.write(f"--- {process.name} ({state}), last lines of {process.log}:\n")
                for line in process.tail():
                    sys.stdout.write(f"    {line}\n")
            return EXIT_MISSING
        sys.stdout.write("every step of the shadow path showed: register, copy, review, issue, /end, outcome\n")
        for process in processes:
            for line in process.log.read_text(encoding="utf-8", errors="replace").splitlines():
                if " repair shadow " in line or "repair review posted" in line or "repair model call" in line:
                    sys.stdout.write(f"  {process.name}: {line.strip()}\n")
        return EXIT_OK
    finally:
        for process in reversed(processes):
            process.stop()
        session.close()


def report_start_failure(message: str, processes: list[Process]) -> int:
    sys.stdout.write(f"could not start: {message}\n")
    for process in processes:
        state = "running" if process.popen.poll() is None else f"exited {process.popen.returncode}"
        sys.stdout.write(f"--- {process.name} ({state}), last lines of {process.log}:\n")
        for line in process.tail():
            sys.stdout.write(f"    {line}\n")
    return EXIT_START


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="smoke_shadow.py", description="End-to-end smoke of shadow mode (spec XC5).")
    parser.add_argument("--keep", action="store_true", help="keep the temporary folder (logs, database, copies)")
    parser.add_argument("--timeout", type=float, default=900.0, help="seconds to wait for every step (default: 900)")
    parser.add_argument(
        "--budget",
        type=int,
        default=BUDGET_SECONDS,
        help=f"the explore budget in seconds (default and least: {BUDGET_SECONDS})",
    )
    args = parser.parse_args(argv)
    if args.budget < MIN_REVIEWED_BUDGET + 10:
        parser.error(f"--budget must be at least {MIN_REVIEWED_BUDGET + 10}: shorter runs are never reviewed")
    for module in ("cairn.cli", "cairn_repair_agent.cli"):
        try:
            __import__(module)
        except ImportError:
            sys.stderr.write(f"smoke_shadow: {module} cannot be imported; run it with uv run --project repair-agent\n")
            return EXIT_START
    previous = install_stop_handlers()
    try:
        return smoke(args)
    except Stopped as stopped:  # a signal before the folder was made, or while it was being removed
        return 128 + stopped.signum
    finally:
        restore_handlers(previous)


def smoke(args: argparse.Namespace) -> int:
    base = Path(tempfile.mkdtemp(prefix="cairn-repair-smoke-"))
    reviewer = Reviewer()
    try:
        layout = Layout(base=base, port=free_port(), reviewer_url=reviewer.start(), budget=args.budget)
        sys.stdout.write(f"smoke folder {base}\n")
        sys.stdout.flush()
        code = run(layout, args.timeout)
        sys.stdout.write(f"stub reviewer answered {reviewer.requests} request(s)\n")
        return code
    except Stopped as stopped:  # run() has stopped the processes it started on its way out
        sys.stdout.write(f"stopped by {stopped}: the processes the smoke started are stopped\n")
        return 128 + stopped.signum
    finally:
        reviewer.stop()
        if args.keep:
            sys.stdout.write(f"kept {base}\n")
        else:
            shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
