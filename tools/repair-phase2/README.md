# Repair layer, Phase 2 (shadow mode): runbook

Phase 2 adds the repair layer in shadow mode. For each long explore run of a project you switch on,
the dispatcher registers the run and copies its transcript, and the repair agent reviews it once in
its 75–80% window with the reviewer you configure and records one decision. Nothing is applied: the
run goes on exactly as it would without Repair. Every project's switch is off by default, and with
every switch off Cairn behaves as it does today.

There are two ways to deploy it:

- **Host install.** The server and the dispatcher run on your machine as you, and the agent runs as
  its own user, `cairn-repair`. Follow the **Host** parts of each step. This is the path if you run
  Cairn directly on Linux without Docker, for example on Kali with Claude Code explore and reason
  workers signed in with your Claude account (local mode).
- **Compose.** `docker compose -f docker-compose.yaml -f docker-compose.repair.yaml up -d --build`
  starts the server, the dispatcher and the agent (`cairn-repair`). Follow the **Compose** parts.

Run every command from the root of your checkout. The examples assume it is `~/cairn`, that is
`/home/<you>/cairn`; replace `<you>` with your user name, or adjust the paths if your checkout is
elsewhere.

## What goes where

| What | Host | Compose |
| --- | --- | --- |
| The reviewers, each with its own key | the `repair:` section of `dispatch.yaml` (mode 0600) | the `repair:` section of `./dispatch.yaml` (mode 0600) |
| Agent token | `secrets/repair-agent.token` (yours, 0600), and the agent's own copy `/etc/cairn-repair/agent.token` (`cairn-repair`, 0400) | `secrets/repair-agent.token` (yours, 0600), mounted read-only |
| Dispatcher token | `secrets/repair-dispatcher.token` (yours, 0600) | `secrets/repair-dispatcher.token` (yours, 0600), mounted read-only |
| The agent's own configuration | `/etc/cairn-repair/repair.yaml` (`root:cairn-repair`, 0640), from `repair-agent/repair.example.yaml` | `./repair.compose.yaml`, from `repair-agent/repair.compose.example.yaml` |
| Copy root: transcript copies, and the agent's settings file `agent-settings.json` | `/var/lib/cairn-repair/transcripts` (yours, group `cairn-repair`, 2750) | `./datas/repair-transcripts`, mounted read-only into the agent |
| The agent's code | `/opt/cairn-repair` (root's, readable by everyone) | the `cairn-repair-agent` image, built from `repair-agent/` alone |

There is exactly one place for a reviewer's key: its entry in the `repair:` section of
`dispatch.yaml`. The agent never reads `dispatch.yaml`, which also holds your workers' settings: the
dispatcher writes what the agent needs (the server's address, the optional `projects` list and the
reviewers, each with its key) into `agent-settings.json` in the copy root at each start, and the
agent reads that file at its own start. `repair.yaml`, `repair.compose.yaml` and `secrets/` are
git-ignored, as `dispatch.yaml` and `datas/` already were.

**Compose: create every mounted file before the first `up`.** That is the two token files (step 1)
and `./repair.compose.yaml` (step 4); also add step 3's `repair:` section to `./dispatch.yaml` first.
Compose creates a missing bind-mount source as an empty directory owned by root, and the service that
mounts it cannot use it: the agent does not start, the dispatcher leaves runs unobserved, the server leaves
that role unconfigured. Remove such a directory (`sudo rm -r secrets/repair-agent.token`, for
example) before you write the file.

## 1. Secrets

Create two distinct tokens, one for the agent and one for the dispatcher, each in its own file, owned
by you, mode 0600:

```bash
install -d -m 0700 secrets
(umask 077; python3 -c 'import secrets; print(secrets.token_hex(32))' > secrets/repair-agent.token)
(umask 077; python3 -c 'import secrets; print(secrets.token_hex(32))' > secrets/repair-dispatcher.token)
ls -l secrets
```

Each token is 64 characters; any value of at least 32 printable ASCII characters works, but the two
must differ. Never export a token value in a shell that also starts the dispatcher: in local mode
every variable set on the dispatcher reaches every worker.

- **Host:** the server and the dispatcher run as you and read both files there. The agent gets its
  own copy of the agent token in `/etc/cairn-repair/` (step 4 d).
- **Compose:** the containers run as root and mount both files read-only.

A reviewer's key is not a file: you key it into its reviewer entry in `dispatch.yaml`, as you key in
a worker's, and nowhere else (step 3).

## 2. Server

The server reads both token files at its start. The variables name files, never values.

- **Host:**

  ```bash
  CAIRN_REPAIR_AGENT_TOKEN_FILE=$PWD/secrets/repair-agent.token CAIRN_REPAIR_DISPATCHER_TOKEN_FILE=$PWD/secrets/repair-dispatcher.token uv run --project cairn cairn serve
  ```

- **Compose:** `docker-compose.repair.yaml` sets both; they take effect at the `up` of step 4.

Once the server runs with them, check:

```bash
curl -s --noproxy '*' http://127.0.0.1:8000/repair/settings
```

`roles_configured` must be `{"agent": true, "dispatcher": true}`. Every `curl` to the server in this
runbook has `--noproxy '*'`: without it, curl sends even a request to `127.0.0.1` to the proxy that
`http_proxy` or `all_proxy` names, if your shell exports one (in local mode,
`dispatch.local.example.yaml` suggests it), and step 6's request carries the agent token.

The server configures no logging, so under `cairn serve` its INFO line `repair tokens configured …`
does not show. A refused token shows on the server's standard error as `repair token rejected
role=<role> reason=<reason>`, where the reason is `file_unreadable`, `not_ascii`, `shorter_than_32`
or `identical` (the two tokens are equal).

## 3. Dispatcher

Add a `repair:` section to your `dispatch.yaml`. It holds the dispatcher's repair settings and the
reviewers, which are configured the way workers are; the dispatcher has no other repair file. Keep
`dispatch.yaml` at mode 0600 (`chmod 0600 dispatch.yaml`): it now also holds the reviewers' keys.

Write the section by two rules, which keep a reviewer's key out of the load errors that pydantic
prints (below):

- Put the section before `workers:`, for example right after `server:`, never at the end of the
  file. `dispatch.example.yaml` and `dispatch.local.example.yaml` end with `workers:`, so do not
  append the section to a copy of either.
- In each reviewer's `env`, write `REVIEW_API_KEY` between `REVIEW_MODEL` and `REVIEW_BASE_URL`, as
  in the examples below: not last, and not first either, because a YAML syntax error in an `env`
  often prints the first line of that mapping.

A load error prints pieces of its input on the dispatcher's standard error (in Compose,
`docker compose -f docker-compose.yaml -f docker-compose.repair.yaml logs cairn-dispatcher`). A
misspelt `repair:` key (`repairs:`), a `repair:` indented into a worker entry, and a dispatcher from
before Phase 2 (step 9) print the section's first 25 and last 24 characters, and an error about the
whole file (a missing top-level key, duplicate worker names) prints its first and last characters.
Written this way, none of them holds a reviewer's key; with `REVIEW_API_KEY` last in its `env`, they
hold the end of the last reviewer's key. A YAML syntax error (an unclosed quote) prints the lines
around it, which can hold part of a key, so never paste the output of a dispatcher that fails to load
`dispatch.yaml` elsewhere as it is.

The section's keys:

- `token_file`: on a host, the absolute path of `secrets/repair-dispatcher.token`; in Compose,
  `/run/secrets/repair-dispatcher.token`, where the override mounts it.
- `transcript_root`: `/var/lib/cairn-repair/transcripts`, on a host and in Compose, a folder used for
  nothing else: keep nothing of your own in it, since the dispatcher removes the copies of deleted
  projects from it (step 8). In Compose the dispatcher runs as root, so the default would be under
  `/root/.local/share`, outside the override's mount, and the agent would find neither copies nor its
  settings file.
- `projects`: leave it null, and every project you switch on is reviewed; a list of project ids
  narrows that to the projects it names.
- `reviewers`: one entry per model, each with `name` (letters, digits, `.`, `_` and `-`, starting with
  a letter or digit, at most 64 characters, unique), `env` and, for a Claude API key,
  `api_format: anthropic_messages`. In `env`, in this order:
  - `REVIEW_MODEL`: the model name the endpoint serves.
  - `REVIEW_API_KEY`: the reviewer's own key. A Claude reviewer (`anthropic_messages`) needs one; an
    OpenAI-compatible one (`openai_chat`, the default) only if its endpoint does, so leave it out for a
    local model server without authentication.
  - `REVIEW_BASE_URL`: its base URL as the agent reaches it. With `openai_chat`, an OpenAI-compatible
    base URL, such as `https://<your provider>/v1`; `/chat/completions` is appended. With
    `anthropic_messages`, `https://api.anthropic.com`, without `/v1`; `/v1/messages` is appended.

**Host**, for example a Claude reviewer and an OpenAI-compatible one. Put your model names and keys in
place of the placeholders: the dispatcher refuses a value that still holds `<` or `>`.

```yaml
repair:
  token_file: "/home/<you>/cairn/secrets/repair-dispatcher.token"
  transcript_root: "/var/lib/cairn-repair/transcripts"
  projects: null                                       # null: every project you switch on; or a list of project ids
  reviewers:
    - name: "reviewer_claude"
      api_format: anthropic_messages                   # the Claude Messages API
      env:
        REVIEW_MODEL: "<model name>"
        REVIEW_API_KEY: "<your Claude API key>"        # between REVIEW_MODEL and REVIEW_BASE_URL
        REVIEW_BASE_URL: "https://api.anthropic.com"   # without /v1
    - name: "reviewer_openai_compatible"               # api_format left out: openai_chat
      context_window: 16384                            # a model server's context, never more (below)
      env:
        REVIEW_MODEL: "<model name>"
        REVIEW_API_KEY: "<that provider's key for the reviewer>"
        REVIEW_BASE_URL: "https://<your provider>/v1"  # or a model server on this host: http://127.0.0.1:<port>/v1
```

**Compose**, the same reviewers; only `token_file` differs, and a model server on the host has another
address (Compose notes, below):

```yaml
repair:
  token_file: "/run/secrets/repair-dispatcher.token"
  transcript_root: "/var/lib/cairn-repair/transcripts"
  projects: null
  reviewers:
    - name: "reviewer_claude"
      api_format: anthropic_messages
      env:
        REVIEW_MODEL: "<model name>"
        REVIEW_API_KEY: "<your Claude API key>"        # between REVIEW_MODEL and REVIEW_BASE_URL
        REVIEW_BASE_URL: "https://api.anthropic.com"
    - name: "reviewer_openai_compatible"
      context_window: 16384                            # a model server's context, never more (below)
      env:
        REVIEW_MODEL: "<model name>"
        REVIEW_API_KEY: "<that provider's key for the reviewer>"
        REVIEW_BASE_URL: "https://<your provider>/v1"  # or a model server on the host: http://host.docker.internal:<port>/v1
```

**The Claude reviewer's key is a Claude API key**, which you create in the Claude Console
(https://platform.claude.com/settings/keys), an account with its own billing, and key in here once.
It is not the Claude account login that your Claude Code workers sign in with: this call cannot use
that login (the API answers a login token sent as `x-api-key` with 401), so there is nothing to take
from Claude Code's files. The reviewer's use is billed to its key, never to that login's
subscription, and does not count against the subscription's usage limits. With
`api_format: anthropic_messages`, a missing `REVIEW_API_KEY` or `structured_output: true` stops the
agent at its start, not the dispatcher.

Give each reviewer its own key, never a worker's. A key that a worker also holds is accepted, and
logged at the dispatcher's start as `repair reviewer shares a worker key reviewer=<name>
worker=<names>`, with names only, when it is in a worker entry's `env` or, in local mode, in the
dispatcher's environment; a key that only a local CLI's credential file holds is not checked.

A reviewer's optional settings sit beside its `env`: `timeout_seconds`, `max_tokens`,
`context_window`, `max_attempts`, `structured_output`, `ca_bundle`, `proxy` and `price`
(`input_per_mtok` and `output_per_mtok`, the price per million tokens, for the replay's cost ceiling).
Left out, each has its default. A `ca_bundle` is a path the agent opens: on a host a file in
`/etc/cairn-repair/` (step 4 d), in Compose a file you mount read-only into `cairn-repair`.

**A model server on this host needs `context_window`**, the context in tokens that the server gives
the model. Give the server a context of at least 16,384 tokens (Ollama: `OLLAMA_CONTEXT_LENGTH=16384`
in the server's environment, or `PARAMETER num_ctx 16384` in a Modelfile) and set the reviewer's
`context_window` to exactly that value, never more, as both examples do; change both together. Such
a server cuts a prompt longer than its context without an error, keeping only its first few tokens
and the tail, so the model sees neither the system prompt nor the trusted header, only the run's
own text. With `context_window` set, the agent sizes every digest so that the prompt and the answer
fit, and the server never cuts it. Without it, or with a value larger than the server's real
context, a run whose output is mostly digits, hex or rare characters can make the prompt three to
four times the agent's estimate, and the agent's own check (`context_truncated`) catches a cut only
when the server's context is below a few thousand tokens. A hosted API answers an over-long prompt
with an error instead: set its `context_window` to its model's context, or leave it out.

A reviewer that the agent can reach only through a proxy needs `proxy: "http://<host>:<port>"` beside
its `env`: its model client uses that proxy and no other. The agent removes `http_proxy`,
`https_proxy` and `all_proxy` from its environment at its start, however it is started, and the proxy
your workers use (in `common_env`, or exported in the dispatcher's shell) is not passed to it. In
Compose, a proxy on the host is reached at `host.docker.internal` and must listen on the Docker bridge
address, as a model server on the host must (Compose notes).

The agent reviews live runs with the first entry; the gate replay runs every entry on the same cases.
Once the gate has passed, put first the reviewer that passed, a local one if both did, and restart
the dispatcher, then the agent (step 4).

- **Host:** create the `cairn-repair` user, the copy root and your membership of its group first
  (step 4 a and b), then start the dispatcher from a new login:

  ```bash
  uv run --project cairn cairn dispatch --config dispatch.yaml
  ```

- **Compose:** the dispatcher reads the section at the `up` of step 4.

A mistake in the section stops the dispatcher at its start with pydantic's message, which names the
key, as any `dispatch.yaml` error does; never paste that output elsewhere as it is (above). At its
start the dispatcher logs

```
repair shadow configured transcript_root=/var/lib/cairn-repair/transcripts reviewers=2 projects=all
repair shadow agent settings written path=/var/lib/cairn-repair/transcripts/agent-settings.json reviewers=2
```

A token problem shows as `repair shadow not configured, runs are not reported reason=<reason>`
(`token_unreadable`, `not_ascii` or `shorter_than_32`).

## 4. Agent

The agent's own configuration, `repair.yaml`, holds its token file, the copy root and its isolation
and polling settings, and no server address, project list, model or key. It reads those from the
dispatcher's settings file, `<transcript_root>/agent-settings.json`, at its own start, and without it
stops with `agent settings missing: …`. So start it after the dispatcher of step 3, and restart it
after the dispatcher whenever you change the `repair:` section, once the dispatcher has logged
`repair shadow agent settings written`.

### Compose

Write `./repair.compose.yaml` at the repository root, where the override mounts it (not in
`repair-agent/`), from the example, and replace any `<…>` placeholder you add (an unedited one stops
the agent at its start). Then start everything:

```bash
cp repair-agent/repair.compose.example.yaml repair.compose.yaml
docker compose -f docker-compose.yaml -f docker-compose.repair.yaml up -d --build
```

This also starts the server and the dispatcher with steps 2 and 3. After a change to the `repair:`
section, restart the dispatcher, wait for the line of that start, then restart the agent; restarting
both at once can start the agent on the earlier settings file:

```bash
docker compose -f docker-compose.yaml -f docker-compose.repair.yaml restart cairn-dispatcher &&
since=$(docker inspect -f '{{.State.StartedAt}}' cairn-dispatcher) &&
until docker compose -f docker-compose.yaml -f docker-compose.repair.yaml logs --since "$since" cairn-dispatcher | grep 'repair shadow agent settings written'; do sleep 1; done &&
docker compose -f docker-compose.yaml -f docker-compose.repair.yaml restart cairn-repair
```

`--since` keeps only what the dispatcher logged since this restart: its log also holds the line of
every earlier start, which a plain `grep` would find at once, without waiting. The line normally comes
within seconds. If it does not, press Ctrl-C, which also skips the agent's restart, and look at the
dispatcher's log: a mistake in `dispatch.yaml` stops the dispatcher at its start (step 3; never paste
that output elsewhere as it is), and a failed write logs `repair shadow agent settings not written …`.

### Host

The agent runs as its own user, `cairn-repair`, from code and a configuration that only root or that
user can change; started as your user or as root, it refuses to run. Its reviewer settings come from
the dispatcher's settings file, which your user owns. Run these from your own shell, so that `$USER`
is you, in your checkout.

**a. The user.** Never add it to the `docker` group.

```bash
sudo useradd --system --user-group --no-create-home --shell /usr/sbin/nologin cairn-repair
```

**b. The copy root**, outside your home, and your membership of its group:

```bash
sudo install -d -m 0755 /var/lib/cairn-repair
sudo install -d -m 2750 -o "$USER" -g cairn-repair /var/lib/cairn-repair/transcripts
sudo usermod -aG cairn-repair "$USER"
```

Then log out and in again, check that `id -nG` lists `cairn-repair`, and start the dispatcher (step 3)
from that new login. The dispatcher gives every copy, and the settings file, the copy root's group,
so your user, which runs the dispatcher, must be in that group. Without the membership the dispatcher
logs `repair shadow transcript copy group not set root_gid=<gid>`, and every review becomes
`transcript_unreadable`: the agent starts, but each run folder and copy keeps your user's own group,
which the agent cannot read.

**c. The code**, owned by root and readable by everyone. Copy what the image copies, and the gate
tools for a replay run as `cairn-repair`, then make them root's before root runs anything on them:

```bash
sudo install -d -m 0755 /opt/cairn-repair /opt/cairn-repair/tools
sudo cp -r repair-agent/pyproject.toml repair-agent/uv.lock repair-agent/src /opt/cairn-repair/
sudo cp -r tools/repair-phase2 /opt/cairn-repair/tools/
sudo chown -R root:root /opt/cairn-repair
sudo chmod -R u=rwX,go=rX /opt/cairn-repair
```

Do not use `cp -a`: a copied file that keeps your ownership, such as `uv.lock`, could be changed by a
local worker and would choose what the next step installs as root. Install the dependencies as root
with a `uv` that root owns, never the one in your home, which a local worker can replace and which is
not on `sudo`'s path anyway. uv's standalone installer, run as root, puts one in `/usr/local/bin`
(or use your distribution's package, with its path in place of `/usr/local/bin/uv` below):

```bash
curl -LsSf https://astral.sh/uv/install.sh | sudo env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh
ls -l /usr/local/bin/uv
```

Then install, and run the same `chown` and `chmod` again for everything the sync added:

```bash
sudo -H sh -c 'cd /opt/cairn-repair && UV_PYTHON_INSTALL_DIR=/opt/cairn-repair/python /usr/local/bin/uv sync --frozen --no-dev'
sudo chown -R root:root /opt/cairn-repair
sudo chmod -R u=rwX,go=rX /opt/cairn-repair
```

Removing write bits alone (`chmod -R go-w`) is not enough: files copied from a checkout made under
umask 027 or 077 keep modes that `cairn-repair` cannot read, and uv links dependency files from root's
cache with the mode they were cached with, so the agent would fail at import. The interpreter, the
code and the dependencies now sit under `/opt/cairn-repair` (or, when uv uses your system Python, in
the system's own folders), owned by root and readable by `cairn-repair`.

Repeat this step after every update of `repair-agent/`, after removing the previous copy, so that no
file removed upstream stays behind:

```bash
sudo rm -r /opt/cairn-repair/src /opt/cairn-repair/tools/repair-phase2
```

**d. Configuration and token**, in `/etc/cairn-repair/`:

```bash
sudo install -d -o root -g cairn-repair -m 0750 /etc/cairn-repair
sudo install -o root -g cairn-repair -m 0640 repair-agent/repair.example.yaml /etc/cairn-repair/repair.yaml
sudoedit /etc/cairn-repair/repair.yaml
sudo install -o cairn-repair -g cairn-repair -m 0400 secrets/repair-agent.token /etc/cairn-repair/agent.token
```

`agent.token` is the agent's own copy of the agent token. The server keeps its copy in `secrets/`,
which your user, and so a local worker, can read. No model key goes here: a reviewer's key is in
`dispatch.yaml` (step 3).

In `repair.yaml` (edit it with `sudoedit`, which keeps its owner and mode):

- `token_file: "/etc/cairn-repair/agent.token"` and `transcripts.root:
  "/var/lib/cairn-repair/transcripts"`, as in the example.
- `dispatch_config_to_check`: the absolute path of the dispatcher's `dispatch.yaml` (mode 0600), such
  as `/home/<you>/cairn/dispatch.yaml`. The agent checks at its start that it cannot read it.
- `isolation.unreadable_paths`: the absolute path of `secrets/repair-dispatcher.token`, such as
  `/home/<you>/cairn/secrets/repair-dispatcher.token`. If you moved a CLI's folder with
  `CLAUDE_CONFIG_DIR`, `CODEX_HOME` or `PI_CODING_AGENT_DIR`, add each credential file in it, by name:
  `.credentials.json`, `settings.json` and `.claude.json` under `CLAUDE_CONFIG_DIR`; `auth.json` and
  `config.toml` under `CODEX_HOME`; `auth.json` and `models.json` under `PI_CODING_AGENT_DIR`. A folder
  listed instead is tested only for its own read bit, so one of mode 0711 would pass while the files in
  it stay readable.

A relative path or an unedited `<…>` placeholder stops the agent at its start. Put any CA file that
`server_ca_bundle` or a reviewer's `ca_bundle` names in `/etc/cairn-repair/` too (`root:root`, 0644),
because the agent checks it as it checks `repair.yaml`. Through the group your user can read
`repair.yaml`, which holds no secret; it can change nothing under `/etc/cairn-repair/` or
`/opt/cairn-repair/`.

**e. Your CLIs' files**, unreadable by `cairn-repair`. For Claude Code, codex and pi, those of these
that exist:

```bash
chmod 0700 ~/.claude ~/.codex ~/.pi
chmod 0600 ~/.claude.json
```

or close your whole home with `chmod 0700 ~`. With a closed home, keep what `cairn-repair` must read
outside it: the copy root already is, and so must be the eval set of a replay run as that user, in a
folder of its own, not inside the copy root.

**f. Start the agent** as `cairn-repair`:

```bash
sudo -u cairn-repair /opt/cairn-repair/.venv/bin/cairn-repair --config /etc/cairn-repair/repair.yaml
```

or with a systemd unit, for example `/etc/systemd/system/cairn-repair.service`:

```ini
[Unit]
Description=Cairn repair agent (shadow mode)
Wants=network-online.target
After=network-online.target

[Service]
User=cairn-repair
Group=cairn-repair
ExecStart=/opt/cairn-repair/.venv/bin/cairn-repair --config /etc/cairn-repair/repair.yaml
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
```

On a host the agent logs this WARNING at every start:

```
repair reviewer settings are writable by the dispatcher's user, local workers can change them path=/var/lib/cairn-repair/transcripts/agent-settings.json
```

That is expected: the settings file is yours, and in local mode your workers run as you, so they can
change it too (and read the reviewers' keys in it and in `dispatch.yaml`). Phase 2 accepts this
because nothing is applied; the reviewer's settings must be protected before Phase 3 applies
anything.

### Check

The agent logs `repair agent started … reviewer=<your first reviewer> … projects=all`, for example:

```
repair agent started server=http://127.0.0.1:8000 reviewer=reviewer_claude model=<model name> endpoint=https://api.anthropic.com:443 digest_tokens=8000 poll=5s projects=all transcripts=/var/lib/cairn-repair/transcripts dry_run=false
```

`projects=` gives the number of projects listed in the `repair:` section, or `all`. A reviewer at a
plain `http://` address that is not loopback also logs `repair model endpoint is plain http on a
non-loopback host endpoint=…`; in Compose, with a model server on the host, that is expected.

A refusal names what to fix. A missing, unreadable or invalid settings file stops the agent at its
start with `agent settings missing: …` (start the dispatcher with its `repair:` section first),
`agent settings unreadable: …` (the agent's user must hold the copy root's group: step 4 b) or
`agent settings invalid: <path>: <field>: <rule>`. The isolation check stops it with
`repair agent isolation check failed reasons=<reasons>`:

| Reason | What to fix |
| --- | --- |
| `docker_socket`, `docker_host` | The agent can reach a Docker or Podman socket, or `DOCKER_HOST` is set. Never give it either. |
| `dispatch_config_readable` | The agent can read `dispatch.yaml`: `chmod 0600 dispatch.yaml`. |
| `dispatch_config_missing` | On a host, `dispatch_config_to_check` must name your `dispatch.yaml` (step 4 d). |
| `same_user` | The agent runs as your user: start it as `cairn-repair` (step 4 f). |
| `copy_root_writable` | The agent can write the copy root: it must be yours, group `cairn-repair`, 2750 (step 4 b). |
| `worker_credentials_readable` | The agent can read a CLI credential file in your home (step 4 e). |
| `readable:<path>` | The agent can read a path listed in `isolation.unreadable_paths`: `chmod 0600` it. |
| `config_writable` | `repair.yaml` or a CA file it names can be changed by another user (step 4 d). |
| `secret_writable` | `agent.token` can be changed by another user (step 4 d). |
| `package_writable` | The agent's code can be changed by another user: repeat step 4 c's `chown` and `chmod`. |
| `settings_writable` | `agent-settings.json` belongs to someone other than you or root, or is group- or world-writable. The dispatcher writes it 0640; do not change it by hand. |

## 5. Switch on

Tick "Repair long explores" in the New Project dialog. For an existing project, click the Repair
button in its header twice within 3 s, or run:

```bash
curl -X PUT --noproxy '*' http://127.0.0.1:8000/projects/<id>/repair -H 'Content-Type: application/json' -d '{"enabled": true}'
```

Without the `Content-Type` header, curl sends the body as a form and the server answers 422. The
server's standard error shows `repair switch project=<id> enabled=True client=<address>`. Switching a
project on is your consent to send its runs' digests to the first reviewer listed in
`dispatch.yaml`. With `projects: null` nothing else is needed. With a list, add the project id to it,
then restart the dispatcher, then the agent (step 4).

## 6. Verify

During a long explore run (budgets under 350 s are never reviewed):

- the dispatcher logs `repair shadow run registered project=<id> …`;
- `GET /repair/runs` with the agent token lists it:

  ```bash
  printf 'X-Cairn-Repair-Token: %s\n' "$(cat secrets/repair-agent.token)" | curl -s --noproxy '*' -H @- http://127.0.0.1:8000/repair/runs
  ```

  (`printf` is a shell builtin and `-H @-` reads the header from standard input, so the token never
  appears in a process list; `--noproxy '*'` keeps it from a proxy that your shell exports, which
  would otherwise receive the request with the token, step 2);
- at 70% of the run's budget, `<copy root>/<project_id>/<run_id>/meta.json` appears;
- at 75–80%, the run's intent node shows "Repair review: …".

## 7. Stop

Turn the switch off: two clicks on the Repair button in the project's header, or step 5's `PUT` with
`{"enabled": false}`. Then stop the agent (Ctrl-C, `sudo systemctl stop cairn-repair`, or
`docker compose -f docker-compose.yaml -f docker-compose.repair.yaml stop cairn-repair`). Runs in
flight keep their path and end normally; with the agent stopped, their windows are recorded as not
reviewed.

With every switch off, a `repair:` section still runs the dispatcher's housekeeping: it writes the
settings file at each start and removes the copies of projects you delete. To stop completely, once
those runs have ended:

- **Host:** remove the section from `dispatch.yaml`, restart the dispatcher, and delete
  `/var/lib/cairn-repair/transcripts/agent-settings.json`, which holds the reviewers' keys.
- **Compose:** remove the section from `./dispatch.yaml`, run
  `docker compose -f docker-compose.yaml up -d --remove-orphans`, which recreates the server and the
  dispatcher without the override and removes the agent's container, then delete
  `datas/repair-transcripts/agent-settings.json` with `sudo` (it belongs to root).

Copies left in the copy root then stay until you delete them. Deleting a project deletes its repair
rows in the same transaction, whether Repair runs or not.

## 8. Removal

Repair data follows Cairn's own data handling. Deleting a project deletes its runs, reviews, digests
and raw model output in the server's database, in the same transaction. While `dispatch.yaml` has
`repair:`, the dispatcher removes the project's copy folder within about a minute, or at its next
start if it was down; and when a completed project's container or work folder is removed because
`completed_action` is `remove`, its copies go too. Under `stop` or `keep`, and for a stopped project,
the copies stay, as the container or folder does, until you delete the project. There is no
time-based retention and no cron job.

## 9. Rollback

The previous server runs unchanged on a migrated database: it ignores the new column and tables, and
it still deletes a project's repair rows with the project, because the foreign key is part of the
table in the database file. A previous dispatcher ignores the switch, but refuses a `dispatch.yaml`
that has a `repair:` section: remove the section, its `repair:` line included, before you start it.
Started with the section still there, its error prints the section's first 25 and last 24 characters:
with step 3's order, no reviewer's key is among them. Nothing needs to be dropped.

The repair tables keep the digests and raw model output (target data) of the projects you keep, and
the copy root keeps their copies and `agent-settings.json`, which holds the reviewers' keys. If you do
not want to keep them, delete the copy root's contents, and stop the server and drop both tables,
before or after you check out the previous version:

```bash
python3 -c "import sqlite3; c = sqlite3.connect('file:<database>?mode=rw', uri=True); c.executescript('DROP TABLE IF EXISTS repair_reviews; DROP TABLE IF EXISTS repair_runs;')"
```

`<database>` is the server's SQLite file: `~/.local/share/cairn/cairn.db` on a host,
`datas/cairn/cairn.db` in Compose (owned by root there, so run this, and the deletion of the copy
root's contents, with `sudo`). A Phase 2 server started on that file again creates both tables empty.

## Compose notes

- **Image.** The agent's image is built with `repair-agent/` as its context, never the repository
  root, which holds `dispatch.yaml`, `secrets/` and `datas/`. Its `Dockerfile` copies only
  `pyproject.toml`, `uv.lock` and `src/`, removes group and world write permission from them, and
  installs the dependencies at build time; `repair-agent/.dockerignore` keeps any `repair.yaml`, key
  or token left in that folder out of the image. No `.dockerignore` is added at the repository root,
  whose images are built as before.
- **What the agent's container gets.** Read-only: `./repair.compose.yaml`, the agent token and the
  copy root. It mounts neither the Docker socket nor `dispatch.yaml`, and no key: the reviewers' keys
  reach it in the settings file.
- **Order.** The agent refuses to start until the dispatcher has written its settings file.
  `depends_on` starts the dispatcher first, and `restart: unless-stopped` starts the agent again if it
  came up before the file was written.
- **User.** The agent runs as root in its container, like today's images. The dispatcher, root in
  its own container, writes the copies and the settings file as `root:root`, folders 0750 and files
  0640, so the agent reads them, and the isolation check passes: no Docker socket and no
  `DOCKER_HOST`, `dispatch_config_to_check: null` (accepted only inside a container), and the copy
  root, the configuration and the token on read-only mounts.
- **A non-root agent** is possible but not the default. Before the first `up`, create the copy root
  with a group that user holds (`sudo install -d -m 2750 -o root -g <gid> datas/repair-transcripts`);
  add `group_add: ["<gid>"]` and `user: "<uid>"` to `cairn-repair` in your copy of the override, never
  a `USER` line in the image; make `secrets/repair-agent.token` owned by that uid with mode 0400 and
  `./repair.compose.yaml` readable by it; and before the build make the agent's files readable by
  others in your checkout (`chmod -R a+rX repair-agent/pyproject.toml repair-agent/uv.lock
  repair-agent/src`), because the image keeps their mode bits and only removes write permission.
- **A model server on the host.** Inside the agent's container, `127.0.0.1` is the container itself.
  A server on the host is reached as `http://host.docker.internal:<port>/v1`, the `REVIEW_BASE_URL` of
  its reviewer entry, which `extra_hosts` maps to the host's Docker bridge address (`docker0`,
  172.17.0.1 unless the daemon is configured otherwise; `ip -4 addr show docker0`). Bind the server to
  that address (listen on `172.17.0.1:<port>`), never to `0.0.0.0`. That address is not loopback:
  the server may then check no `Host` header, every container on a Docker network can reach
  it, and so can a machine on your local network that routes 172.17.0.1 through your host. So let a
  host firewall admit that port only on `lo` and the Docker bridge interfaces (`docker0` and the
  Compose network's `br-…`), and drop it on every other interface. The agent then logs
  `repair model endpoint is plain http on a non-loopback host endpoint=http://host.docker.internal:<port>`
  at every start; the traffic stays on the host's Docker bridge, so that warning is expected. The host
  does not resolve `host.docker.internal`, so in Compose run the gate replay in the agent's image.
  A reviewer at a hosted provider, such as the Claude API, needs none of this.
