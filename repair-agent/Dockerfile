FROM ghcr.io/astral-sh/uv:python3.13-trixie

# Only the package: never a repair.yaml, key or token left in this folder (.dockerignore), and never the
# repository root, which holds dispatch.yaml with worker keys, secrets/ and datas/.
COPY pyproject.toml uv.lock /repair-agent/
COPY src/ /repair-agent/src/
# COPY keeps the checkout's mode bits; the agent's ownership check refuses a group-writable package folder.
RUN chmod -R go-w /repair-agent/pyproject.toml /repair-agent/uv.lock /repair-agent/src
WORKDIR /repair-agent
RUN uv sync --frozen --no-dev
ENV PATH="/repair-agent/.venv/bin:$PATH"
# The agent's state folder, where it pins its reviewer: root's (the agent runs as root here), mode 0700. Compose mounts
# the named volume cairn-repair-state here, which takes this folder's owner and mode when it is created.
RUN install -d -m 0700 /var/lib/cairn-repair/state
