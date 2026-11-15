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
