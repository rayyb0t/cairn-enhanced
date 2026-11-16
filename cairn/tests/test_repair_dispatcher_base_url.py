"""Build step RB1 (the final review's part B, R3_SECURITY-1): a reviewer's REVIEW_BASE_URL with a backslash.

urllib3, whose parser requests uses, ends a URL's host at a backslash, and urllib.parse does not: in
`https://listener.example\\@<owner host>` the agent would show the owner's host while requests connected to the
listener. The repair agent refuses such a URL at its start (`config.url_problem`); the dispatcher refuses a
REVIEW_BASE_URL that holds a backslash too, so the error shows at the dispatcher's start, in the style of its other
REVIEW_BASE_URL rule, naming the key and never the value. The check is one of the repair: section's: a dispatch.yaml
without that section loads exactly as before, a backslash in a worker's env included.

Every value is synthetic: listener.example and provider.example hosts, documentation addresses, a fake key.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError
from repair_dispatcher_support import FAKE_REVIEWER_KEY, dispatch_payload, repair_section, write_dispatch_yaml

from cairn.dispatcher.config import DispatchConfig

KEY = "repair.reviewers.0.env.REVIEW_BASE_URL"
RULE = "REVIEW_BASE_URL must not contain a backslash"
SLOT = "rb1-review-base-url-slot"

# The finding's two shapes (a hosted reviewer, and a local one shown as loopback), and a backslash in the path.
BACKSLASH_URLS = {
    "hosted": "https://listener.example\\@api.provider.example",
    "local": "http://192.0.2.7:8080\\@127.0.0.1:11434/v1",
    "in_the_path": "http://127.0.0.1:11434/v1\\chat",
}
# How a worker that edits dispatch.yaml can write such a value: a plain scalar, single-quoted, double-quoted with \\.
SPELLINGS = {
    "plain": lambda url: url,
    "single_quoted": lambda url: f"'{url}'",
    "double_quoted": lambda url: '"' + url.replace("\\", "\\\\") + '"',
}


def dispatch_yaml_with_base_url(path: Path, spelt: str) -> Path:
    """dispatch.yaml with the example repair: section, whose first reviewer's REVIEW_BASE_URL is `spelt` as written."""
    section = repair_section()
    section["reviewers"][0]["env"]["REVIEW_BASE_URL"] = SLOT
    text = yaml.safe_dump(dispatch_payload(section), sort_keys=False)
    assert text.count(SLOT) == 1
    path.write_text(text.replace(SLOT, spelt), encoding="utf-8")
    return path


@pytest.mark.parametrize("spelling", list(SPELLINGS))
@pytest.mark.parametrize("shape", list(BACKSLASH_URLS))
def test_a_review_base_url_with_a_backslash_stops_the_dispatcher_at_its_start(
    tmp_path: Path, shape: str, spelling: str
) -> None:
    """R3_SECURITY-1: a REVIEW_BASE_URL that holds a backslash, however the YAML spells it, stops the dispatcher's
    config load with `REVIEW_BASE_URL must not contain a backslash`, under its key, with the input hidden; no part of
    the URL is printed. The agent refuses the same value at its start."""
    url = BACKSLASH_URLS[shape]
    path = dispatch_yaml_with_base_url(tmp_path / "dispatch.yaml", SPELLINGS[spelling](url))
    assert yaml.safe_load(path.read_text(encoding="utf-8"))["repair"]["reviewers"][0]["env"]["REVIEW_BASE_URL"] == url
    with pytest.raises(ValidationError) as caught:
        DispatchConfig.load(path)
    message = str(caught.value)
    assert f"\n{KEY}\n" in message, message
    assert RULE in message and "input_value=<hidden>" in message, message
    for piece in ("listener.example", "api.provider.example", "192.0.2.7", "127.0.0.1:11434", "\\"):
        assert piece not in message, (piece, message)
    assert FAKE_REVIEWER_KEY not in message


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:11434/v1",
        "https://api.provider.example",
        "https://user:password@API.Provider.Example:8443/v1",
        "http://[2001:db8::7]:11434/v1",
        "http://host.docker.internal:11434/v1",
    ],
)
def test_a_review_base_url_without_a_backslash_loads_as_written(tmp_path: Path, url: str) -> None:
    """The rule refuses only the backslash: these REVIEW_BASE_URLs load, and are kept as written, for the settings
    file that the dispatcher passes them on in."""
    path = dispatch_yaml_with_base_url(tmp_path / "dispatch.yaml", f"'{url}'")
    config = DispatchConfig.load(path)
    assert config.repair is not None
    assert url == config.repair.reviewers[0].env.REVIEW_BASE_URL


def test_without_a_repair_section_a_backslash_elsewhere_loads_as_before(tmp_path: Path) -> None:
    """With no repair: section the check never runs, and nothing changes: a dispatch.yaml whose worker env and
    common_env hold a backslash, even in a base URL of a worker's own, loads with every value as written and no repair
    section, as it does without build step RB1."""
    payload = dispatch_payload()
    assert "repair" not in payload
    worker_url = "http://192.0.2.7:8080\\@127.0.0.1:11434/v1"
    payload["workers"][0]["env"] = {"OPENAI_BASE_URL": worker_url, "WINDOWS_PATH": "C:\\cairn\\work"}
    payload["common_env"] = {"SHARED_PATH": "\\\\server\\share"}
    path = write_dispatch_yaml(tmp_path / "dispatch.yaml", payload)
    config = DispatchConfig.load(path)
    assert config.repair is None
    # common_env is merged into each worker's env, as today.
    assert config.workers[0].env == {
        "OPENAI_BASE_URL": worker_url,
        "WINDOWS_PATH": "C:\\cairn\\work",
        "SHARED_PATH": "\\\\server\\share",
    }
    assert config.common_env == {"SHARED_PATH": "\\\\server\\share"}
    assert config.model_dump(exclude={"repair"}) == DispatchConfig.model_validate(payload).model_dump(
        exclude={"repair"}
    )
