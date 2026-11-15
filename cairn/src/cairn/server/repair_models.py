"""Repair layer (shadow mode): the request and response models of the repair endpoints.

Request models ignore unknown fields, so a newer client works with this server, and refuse NaN and Infinity: FastAPI
parses bodies with json.loads, which accepts them, and a stored one would make every later answer about that run
fail. Every id rule is a full match. A body that fails its model gets FastAPI's own 422.
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

from cairn.server.models import IntentIssue
from cairn.server.repair import (
    DIGEST_MAX_CHARS,
    EVIDENCE_MAX_CHARS,
    EVIDENCE_MAX_ITEMS,
    MODEL_MAX_CHARS,
    NEXT_STEP_MAX_CHARS,
    RAW_OUTPUT_MAX_CHARS,
    REASON_MAX_CHARS,
    clean_text,
)

# The largest budget, and so the bound of every number that enters time arithmetic.
MAX_SECONDS = 604800
# The dispatcher's run ids (uuid4 hex). A path parameter: the route checks it with valid_run_id (a coded 422).
RUN_ID_PATTERN = r"[A-Za-z0-9][A-Za-z0-9_.-]{7,63}"
# One rule for every session id (the dispatcher, this server and the copy), plus: no "..".
SESSION_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
REVIEW_ID_PATTERN = r"^[A-Za-z0-9_.:-]{8,64}$"
CLI_TYPE_PATTERN = r"^[a-z0-9_-]{1,32}$"
OUTCOME_PATTERN = r"^[a-z_]{1,32}$"

Fallback = Literal["model_timeout", "model_error", "invalid_output", "transcript_unreadable", "digest_error"]
ReviewStatus = Literal["pending", "open", "late", "reviewed", "not_reviewed", "not_reviewable", "closed"]


def valid_run_id(value: str) -> bool:
    """The run id rule as a full match: with re.match, "$" would also accept a trailing newline."""
    return re.fullmatch(RUN_ID_PATTERN, value) is not None


class _RepairRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", allow_inf_nan=False)


# --------------------------------------------------------------------------- requests

LocatorKey = Annotated[str, StringConstraints(max_length=64)]
LocatorValue = Annotated[str, StringConstraints(max_length=1024)]


class RunRegisterRequest(_RepairRequest):
    """PUT /repair/runs/{run_id} (dispatcher token)."""

    project_id: str = Field(min_length=1, max_length=128)
    intent_id: str = Field(min_length=1, max_length=128)
    worker: str = Field(min_length=1, max_length=128)
    cli_type: str = Field(pattern=CLI_TYPE_PATTERN)
    execution: Literal["container", "local"]
    budget_seconds: int = Field(ge=1, le=MAX_SECONDS)
    session_id: str | None = Field(default=None, pattern=SESSION_ID_PATTERN)
    # Used only when the PUT creates the row, and there checked against budget_seconds by the route.
    started_seconds_ago: float = Field(default=0.0, ge=0)
    # Opaque to the server: stored and returned verbatim, never interpreted.
    locator: dict[LocatorKey, LocatorValue | None] = Field(default_factory=dict, max_length=8)

    @field_validator("session_id")
    @classmethod
    def no_parent_reference(cls, value: str | None) -> str | None:
        if value is not None and ".." in value:
            raise ValueError("must not contain ..")
        return value


class RunEndRequest(_RepairRequest):
    """POST /repair/runs/{run_id}/end (dispatcher token): once when the process exits, once with the outcome."""

    outcome: str | None = Field(default=None, pattern=OUTCOME_PATTERN)
    timed_out: bool | None = None
    cancelled: bool | None = None
    returncode: int | None = None
    exited_seconds_ago: float | None = Field(default=None, ge=0, le=MAX_SECONDS)


EvidenceItem = Annotated[str, StringConstraints(min_length=1, max_length=EVIDENCE_MAX_CHARS)]


class ReviewRequest(_RepairRequest):
    """POST /repair/runs/{run_id}/reviews (agent token). The limits equal the stored limits."""

    review_id: str = Field(pattern=REVIEW_ID_PATTERN)
    window: int = Field(ge=1, le=1000)
    decision: Literal["continue", "extend", "detach"]
    reason: str = Field(min_length=1, max_length=REASON_MAX_CHARS)
    evidence: list[EvidenceItem] = Field(default_factory=list, max_length=EVIDENCE_MAX_ITEMS)
    next_step: str | None = Field(default=None, max_length=NEXT_STEP_MAX_CHARS)
    fallback: Fallback | None = None
    expected_version: int | None = None
    # The run's elapsed_seconds in the poll that started the review: the digest's time.
    digest_elapsed_seconds: float = Field(ge=0, le=MAX_SECONDS)
    transcript_cut_seconds: float | None = Field(default=None, ge=0)
    model: str | None = Field(default=None, max_length=MODEL_MAX_CHARS)
    digest: str | None = Field(default=None, max_length=DIGEST_MAX_CHARS)
    raw_output: str | None = Field(default=None, max_length=RAW_OUTPUT_MAX_CHARS)

    @field_validator("reason")
    @classmethod
    def not_blank_after_cleaning(cls, value: str) -> str:
        if not clean_text(value):
            raise ValueError("must not be blank")
        return value

    @model_validator(mode="after")
    def fallback_is_a_continue(self) -> ReviewRequest:
        if self.fallback is not None and self.decision != "continue":
            raise ValueError("a fallback review must have decision continue")
        return self


class UpdateProjectRepairRequest(BaseModel):
    """PUT /projects/{project_id}/repair. A plain bool, as bootstrap_enabled: a value it cannot read is a 422."""

    enabled: bool


# --------------------------------------------------------------------------- responses


class ReviewRules(BaseModel):
    open_fraction: float
    close_fraction: float
    accept_margin_seconds: int
    min_window_seconds: int
    digest_time_margin_seconds: int
    reviewable_cli_types: list[str]


class RunIntent(BaseModel):
    description: str
    from_: list[str] = Field(alias="from")

    model_config = ConfigDict(populate_by_name=True)


class RunWindow(BaseModel):
    number: int
    start_at: str
    end_at: str
    seconds: int
    review_opens_at: str
    review_closes_at: str
    review_accept_until: str
    reviewable: bool
    review_status: ReviewStatus


class ReviewSummary(BaseModel):
    window: int
    review_id: str
    source: Literal["agent", "server"]
    decision: str
    issue_id: int | None = None
    created_at: str


class StoredReview(ReviewSummary):
    """A review as stored: its detail (any keys) plus the digest, its SHA-256 and the raw output."""

    model_config = ConfigDict(extra="allow")

    digest: str | None = None
    digest_sha256: str | None = None
    raw_output: str | None = None


class _RunViewFields(BaseModel):
    run_id: str
    project_id: str
    intent_id: str
    intent: RunIntent
    worker: str
    cli_type: str
    execution: str
    session_id: str | None = None
    locator: dict[str, Any]
    budget_seconds: int
    started_at: str
    deadline_at: str
    version: int
    slices_used: int
    # running, over_budget (live past its deadline) or ended
    state: str
    elapsed_seconds: int | None = None
    remaining_seconds: int | None = None
    window: RunWindow
    registered_at: str
    updated_at: str
    ended_at: str | None = None
    end_reason: str | None = None
    last_alive_at: str | None = None
    outcome: str | None = None
    fact_id: str | None = None


class RunView(_RunViewFields):
    """A run as register, /end and GET /repair/runs return it, with review summaries."""

    reviews: list[ReviewSummary]


class RunDetailView(_RunViewFields):
    """GET /repair/runs/{run_id}: the run with its full stored reviews."""

    reviews: list[StoredReview]


class RunRegisterResponse(BaseModel):
    created: bool
    run: RunView


class RunEndResponse(BaseModel):
    run: RunView


class RunListResponse(BaseModel):
    server_time: str
    shadow: bool
    slice_seconds: int
    review_rules: ReviewRules
    runs: list[RunView]
    next_cursor: str | None = None


class ReviewRecord(BaseModel):
    run_id: str
    window: int
    review_id: str
    source: Literal["agent", "server"]
    decision: str
    issue_id: int | None = None
    created_at: str


class ReviewResponse(BaseModel):
    created: bool
    mode: Literal["shadow"]
    applied: Literal[False]
    review: ReviewRecord
    issue: IntentIssue | None = None


class RolesConfigured(BaseModel):
    agent: bool
    dispatcher: bool


class RepairSettingsView(BaseModel):
    """GET /repair/settings: constants, read-only; there is no update model."""

    shadow: bool
    slice_seconds: int
    max_extensions_per_run: int
    max_extended_runs: int
    max_detached_per_project: int
    max_detached_total: int
    max_detached_seconds: int
    review: ReviewRules
    roles_configured: RolesConfigured
    agent_last_seen_at: str | None = None
