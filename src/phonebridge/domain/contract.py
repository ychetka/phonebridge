"""phonebridge domain contract (roadmap item_03, issue #7).

Pure type surface: enums, identifier NewTypes, pydantic models and the
abstract :class:`LineManager` kernel interface. NO implementation lives
here - the in-memory LineManager lands with M1 (design doc §13).

Sources of truth:
- docs/api-contract.md - wire contract (error codes, method surfaces,
  event names, identifier taxonomy).
- docs/phone-bridge-service-design.md - §4 ownership model, §5 call
  lifecycle semantics.

Hazard rules applied (docs/CODING_HAZARDS.md):
- rule 6 (timezone-explicit timestamps): every datetime field is
  :data:`AwareDatetime`; naive values are rejected, aware values are
  normalized to UTC.
- rule 10 (no secrets/PII): caller numbers and provider tokens travel
  as :class:`RedactedString`; ``hide_input_in_errors`` keeps raw input
  out of ValidationError messages; ``model_dump()`` to logs is
  prohibited - use ``safe_dump()``/``to_log_dict()``.
- rule 7 (bounded resources): queue tickets are socket-bound and the
  queue is the ONLY park-and-wait mechanism; all other calls must
  complete or fail with a bounded wait (rule 2).
- rule 4 (isolation): the LineManager is a single-event-loop
  component; every method transition is atomic w.r.t. other calls and
  ingress events - the reentrancy invariant is "no await inside a
  state read-modify-write critical section".
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Collection
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Annotated, Any, Generic, Literal, NewType, TypeVar, cast

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    GetCoreSchemaHandler,
    Tag,
)
from pydantic_core import core_schema

__all__ = [
    "AcceptResult",
    "AcquireGranted",
    "AcquireQueued",
    "AcquireRefused",
    "AcquireResult",
    "AnswerAndAnnounceFallback",
    "AnswerConfidence",
    "AssignmentId",
    "AssignmentMode",
    "AssignmentRevokedEvent",
    "AudioReadyEvent",
    "AwareDatetime",
    "CallLease",
    "CallPhase",
    "CallStateChangedEvent",
    "CallToken",
    "ClaimResult",
    "ContractModel",
    "DialResult",
    "ErrorCode",
    "ErrorData",
    "ErrorReason",
    "EventEnvelope",
    "EventName",
    "FallbackDecision",
    "FallbackPolicy",
    "FallbackTriggeredEvent",
    "ForwardViaLineFallback",
    "IncomingCallEvent",
    "IncomingRingIngress",
    "Intent",
    "LeaseId",
    "LineAssignment",
    "LineId",
    "LineManager",
    "LineState",
    "LineStateChangedEvent",
    "LineSummary",
    "LivenessState",
    "MediaToken",
    "MissedCallEvent",
    "NoPickupFallback",
    "PhonebridgeError",
    "ProviderCallState",
    "ProviderCallStateUpdate",
    "ProviderLiveness",
    "ProviderRegistration",
    "QueueReadyEvent",
    "QueueTicket",
    "RecordAndTelegramFallback",
    "RedactedString",
    "RejectFallback",
    "SessionId",
    "TicketId",
]


# ---------------------------------------------------------------------------
# Identifier taxonomy (docs/api-contract.md "Identifier taxonomy", round 11)
#
# The contract owns the TYPES and their guarantees, NOT the generation
# strategy. Guarantees every implementation must honor:
# - globally unique, opaque, unguessable (no sequential/public meaning);
# - QueueTicket additionally requires an orderable/monotonic sequence
#   (see QueueTicket.seq) - that is the mandatory FIFO guarantee.
# ---------------------------------------------------------------------------

AssignmentId = NewType("AssignmentId", str)
"""Durable line<->owner/provider binding id; survives reconnects."""

CallToken = NewType("CallToken", str)
"""One-shot inbound-ring token; TTL = accept_window_sec (default 30 s).
Burns on call.accept or timeout."""

LeaseId = NewType("LeaseId", str)
"""Ephemeral single id of the active line hold AND the call.
Replaces any call_id everywhere - no call_id exists (round 11)."""

MediaToken = NewType("MediaToken", str)
"""Authorizes the binary media socket handshake; bound to a lease_id."""

LineId = NewType("LineId", str)
"""Canonical line identifier. The agent-side alias "default" is resolved
at the API boundary and never appears inside the domain (round 11)."""

TicketId = NewType("TicketId", str)
"""FIFO queue ticket id; pairs with QueueTicket.seq for ordering."""

SessionId = NewType("SessionId", str)
"""Agent session id returned by register_intent (wire: agent.register_intent).
Tickets and leases are scoped to it; disconnect kills tickets (socket-bound)."""


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class Intent(str, Enum):
    """Agent intent vocabulary (docs/api-contract.md "Common types")."""

    DIAL_OUT = "dial_out"
    RECEIVE_INBOUND = "receive_inbound"
    EXCLUSIVE_LINE = "exclusive_line"


class LineState(str, Enum):
    """Line allocation state machine (design §4.2).

    RINGING_LOCKED: inbound ring in flight; outbound acquire fails with
    LINE_BUSY_INCOMING until accept-window expiry or accept.
    DEGRADED/DOWN: provider heartbeat health states; ingress surface
    reports them via ProviderLiveness.
    """

    FREE = "FREE"
    BUSY = "BUSY"
    RESERVED = "RESERVED"
    RINGING_LOCKED = "RINGING_LOCKED"
    DEGRADED = "DEGRADED"
    DOWN = "DOWN"


class CallPhase(str, Enum):
    """CallLease lifecycle phase (design §5).

    ACTIVE is PROVISIONAL: the design's open validation spike (§5.5)
    has not yet proven whether oFono ``State=active`` fires on remote
    answer or already at cellular setup. Until proven, an ACTIVE lease
    does NOT guarantee the remote party answered - see CallLease's
    ``answered_at`` / ``answer_confidence`` fields which carry that
    uncertainty explicitly. A later PLAN_DELTA may redefine ACTIVE.
    """

    DIALING = "dialing"
    RINGING = "ringing"
    ALERTING = "alerting"
    ACTIVE = "active"
    TERMINATING = "terminating"
    ENDED = "ended"


class ProviderCallState(str, Enum):
    """Bounded provider-reported call state (wire: provider.call_state).

    Canonical oFono vocabulary (round 14 amendment): the provider
    reports what the line actually reports so the gateway can emit
    ``event.call_state_changed`` faithfully - a coarser vocabulary
    loses ``dialing``/``alerting`` and breaks early-media gating.
    Mapping to CallPhase (including the provisional-ACTIVE
    answer-confidence question) remains LineManager business.
    """

    RINGING = "ringing"
    DIALING = "dialing"
    ALERTING = "alerting"
    ACTIVE = "active"
    DISCONNECTED = "disconnected"


class AssignmentMode(str, Enum):
    """LineAssignment.mode (design §4.2)."""

    EXCLUSIVE = "exclusive"
    SHARED_POOL = "shared_pool"


class LivenessState(str, Enum):
    """Provider liveness fact reported via note_provider_liveness."""

    REACHABLE = "reachable"
    UNREACHABLE = "unreachable"


class ErrorCode(str, Enum):
    """Wire error codes - VERBATIM from docs/api-contract.md.

    No additional codes may be introduced in the domain: new error
    semantics travel as typed reason sub-codes in ErrorData.reason
    until the wire document itself is amended. (tests/test_domain_
    contract.py parses the doc table and asserts this enum matches.)
    """

    OWNER_MISMATCH = "OWNER_MISMATCH"
    LINE_EXCLUSIVE_NOT_OWNER = "LINE_EXCLUSIVE_NOT_OWNER"
    LINE_BUSY_INCOMING = "LINE_BUSY_INCOMING"
    LINE_NOT_FOUND = "LINE_NOT_FOUND"
    INVALID_INTENT = "INVALID_INTENT"
    STALE_TOKEN = "STALE_TOKEN"
    PROVIDER_UNREACHABLE = "PROVIDER_UNREACHABLE"
    DIAL_REJECTED = "DIAL_REJECTED"


class ErrorReason(str, Enum):
    """Typed operational reason sub-codes for ErrorData.reason.

    These are NOT wire error codes. They are the leading deferred
    candidates for the NEXT contract round (documented in
    .changes/domain-contract.md); until promoted by the wire document
    they carry operational detail under an existing ErrorCode, e.g.
    STALE_TOKEN + reason ACCEPT_WINDOW_EXPIRED.
    """

    ACCEPT_WINDOW_EXPIRED = "ACCEPT_WINDOW_EXPIRED"
    QUEUE_FULL = "QUEUE_FULL"
    TICKET_NOT_FOUND = "TICKET_NOT_FOUND"
    INVALID_CALL_STATE = "INVALID_CALL_STATE"
    LINE_NOT_CLAIMABLE = "LINE_NOT_CLAIMABLE"
    MEDIA_AUTH_FAILED = "MEDIA_AUTH_FAILED"
    # Typed sub-codes for DIAL_REJECTED (docs/api-contract.md, Dial
    # validation) - consumed by the issue-#25 dial-validation work.
    INVALID_FORMAT_NOT_E164 = "INVALID_FORMAT_NOT_E164"
    UNKNOWN_COUNTRY_CODE = "UNKNOWN_COUNTRY_CODE"
    COUNTRY_MISMATCH = "COUNTRY_MISMATCH"
    INVALID_NUMBER = "INVALID_NUMBER"
    UNSUPPORTED_NUMBER_TYPE = "UNSUPPORTED_NUMBER_TYPE"
    LINE_POLICY_REJECT = "LINE_POLICY_REJECT"


class EventName(str, Enum):
    """Domain event names carried on EventEnvelope.

    Wire spellings under ``event.*`` (incoming_call, line_acquired,
    queue_updated, ...) are an adapter-boundary mapping concern.
    """

    INCOMING_CALL = "incoming_call"
    CALL_STATE_CHANGED = "call_state_changed"
    QUEUE_READY = "queue_ready"
    ASSIGNMENT_REVOKED = "assignment_revoked"
    AUDIO_READY = "audio_ready"
    FALLBACK_TRIGGERED = "fallback_triggered"
    LINE_STATE_CHANGED = "line_state_changed"
    MISSED_CALL = "missed_call"


# ---------------------------------------------------------------------------
# Scalars
# ---------------------------------------------------------------------------


def _require_aware_utc(value: datetime) -> datetime:
    """Enforce CODING_HAZARDS rule 6: reject naive datetimes; normalize
    aware values to canonical UTC."""
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError("naive datetime rejected: contract timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


AwareDatetime = Annotated[datetime, AfterValidator(_require_aware_utc)]
"""Annotated datetime that REJECTS naive input and normalizes to UTC."""

AnswerConfidence = Literal["provisional", "confirmed"]
"""Confidence that the remote party actually answered (see CallPhase.ACTIVE)."""

_MASK = "****"
_MASK_PREFIX_LEN = 4
_MASK_SUFFIX_LEN = 2
_MASK_FULL_LEN = 4
"""Values up to this length mask completely - a 4-char secret exposes
nothing. Above it the visible frame is capped at half the length, so a
masked rendering never reveals more than half the raw characters."""


class RedactedString(str):
    """PII-safe str subclass (api-contract.md "Common types"; CODING_HAZARDS
    rule 10).

    The instance IS a str holding the raw value, so JSON/wire
    serialization (``model_dump_json()``, ``json.dumps``) emits the
    full value with no special-casing - while ``repr()`` / ``str()``
    render a MASKED form (e.g. "+373****45"), so accidental
    f-string/logging use cannot leak PII.

    - ``.expose()`` returns the raw value for in-memory use.
    - ``.to_wire()`` returns the raw value for wire serialization.
    - ``__format__`` applies any format spec to the MASKED value, so
      ``f"{s:>20}"`` / ``format(s, ">20")`` pad/align the mask and can
      never emit the raw string (unlike inherited ``str.__format__``).
    - ``model_dump()`` (python mode) keeps this object - its masked
      repr is safe, but for logs use ``safe_dump()``/``to_log_dict()``
      which render explicit masked strings.
    - Equality/hash are str-native (compare by raw content); slicing
      returns raw fragments - deliberate access, same as .expose().
    """

    def __new__(cls, value: str) -> RedactedString:
        if not isinstance(value, str):
            raise TypeError(f"RedactedString expects str, got {type(value).__name__}")
        return super().__new__(cls, value)

    @staticmethod
    def _mask(value: str) -> str:
        """ "+37379012345" -> "+373****45"; never exposes more than half
        the raw characters - short secrets degrade to a minimal suffix
        ("token42" -> "t****42"), and values of _MASK_FULL_LEN chars or
        less mask completely."""
        n = len(value)
        if n <= _MASK_FULL_LEN:
            return _MASK
        budget = n // 2
        suffix = min(_MASK_SUFFIX_LEN, budget)
        prefix = min(_MASK_PREFIX_LEN, budget - suffix)
        return f"{value[:prefix]}{_MASK}{value[n - suffix :]}"

    def expose(self) -> str:
        """Raw value (plain str) for in-memory use. Never log it."""
        return str.__str__(self)

    def to_wire(self) -> str:
        """Raw value (plain str) for explicit wire serialization."""
        return str.__str__(self)

    def __repr__(self) -> str:
        return f"RedactedString({self._mask(str.__str__(self))!r})"

    def __str__(self) -> str:
        return self._mask(str.__str__(self))

    def __format__(self, format_spec: str) -> str:
        """Apply the spec to the MASKED value: ``f"{s:>20}"`` aligns the
        mask, so no format spec can ever emit the raw string."""
        return str.__format__(self._mask(str.__str__(self)), format_spec)

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source_type: Any, handler: GetCoreSchemaHandler
    ) -> core_schema.CoreSchema:
        """Validate any str into the redacting subclass; JSON mode uses
        an explicit serializer so ``model_dump_json()`` always emits
        ``.to_wire()`` (raw value for the socket) regardless of how
        pydantic-core treats the str subclass - Python-mode dumps keep
        the object with its masked repr (round 15)."""
        return core_schema.no_info_after_validator_function(
            cls,
            core_schema.str_schema(),
            serialization=core_schema.plain_serializer_function_ser_schema(
                lambda v: v.to_wire(),
                return_schema=core_schema.str_schema(),
                when_used="json",
            ),
        )


# ---------------------------------------------------------------------------
# Base model + errors
# ---------------------------------------------------------------------------


def _mask_for_log(value: Any) -> Any:
    """Recursively replace RedactedString with its masked string form."""
    if isinstance(value, RedactedString):
        return str(value)
    if isinstance(value, dict):
        return {k: _mask_for_log(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_mask_for_log(v) for v in value]
    return value


class ContractModel(BaseModel):
    """Base for every domain contract model.

    - ``hide_input_in_errors=True``: a ValidationError must never echo
      raw caller input - a rejected ``{"caller": "+373..."}`` payload
      must not write the number into the error string (rule 10).
    - ``extra="forbid"``: the contract is closed; unknown keys are a
      defect, not silently dropped.
    - ``safe_dump()``/``to_log_dict()`` are the ONLY allowed log dumps:
      raw ``model_dump()``/``model_dump_json()`` emit full PII wire
      values and are PROHIBITED for logging.
    """

    model_config = ConfigDict(hide_input_in_errors=True, extra="forbid")

    def safe_dump(self) -> dict[str, Any]:
        """Log-safe dump: RedactedString fields rendered masked."""
        return cast(dict[str, Any], _mask_for_log(self.model_dump()))

    def to_log_dict(self) -> dict[str, Any]:
        """Alias of safe_dump() for logging call sites."""
        return self.safe_dump()


class ErrorData(ContractModel):
    """Structured ``data`` payload of PhonebridgeError / JSON-RPC errors.

    - ``reason``: typed operational sub-code (ErrorReason) carrying
      detail the seven wire codes cannot express.
    - ``detail``: human-readable context; MUST be PII-free (rule 10) -
      never embed caller numbers or tokens.
    - ``free_lines``: FREE alternatives returned with
      LINE_EXCLUSIVE_NOT_OWNER (contract "Intent / allocation").
    """

    reason: ErrorReason | None = None
    detail: str | None = None
    free_lines: list[LineId] = Field(default_factory=list)


class PhonebridgeError(Exception):
    """Single domain error type.

    ``code`` is a verbatim ErrorCode from docs/api-contract.md;
    ``data`` carries operational detail through the typed reason
    sub-code convention (ErrorReason) plus optional context fields.
    """

    def __init__(self, code: ErrorCode, data: ErrorData | None = None) -> None:
        self.code = code
        self.data = data if data is not None else ErrorData()
        super().__init__(code.value)

    def __str__(self) -> str:
        detail = f" ({self.data.detail})" if self.data.detail else ""
        return f"{self.code.value}{detail}"


# ---------------------------------------------------------------------------
# Fallback policy (design §5.3, ADR-01)
#
# Typed discriminated union on `policy`. The wire spellings
# "forward_via_line:<line_id>" / "record_and_telegram:<channel>" are
# single strings - splitting "<policy>:<param>" is the config adapter's
# job; the domain sees typed fields only. No forward reference to
# LineAssignment.
# ---------------------------------------------------------------------------


class NoPickupFallback(ContractModel):
    """Core policy: let the carrier handle it (carrier voicemail /
    missed call on the phone itself). Default per contract."""

    policy: Literal["no_pickup"] = "no_pickup"


class RejectFallback(ContractModel):
    """Core policy: actively reject the call inside LineManager."""

    policy: Literal["reject"] = "reject"


class ForwardViaLineFallback(ContractModel):
    """Worker policy: dial OUT on another line and bridge the call."""

    policy: Literal["forward_via_line"] = "forward_via_line"
    line_id: LineId


class RecordAndTelegramFallback(ContractModel):
    """Worker policy: answer, record, post to a Telegram channel."""

    policy: Literal["record_and_telegram"] = "record_and_telegram"
    channel: str


class AnswerAndAnnounceFallback(ContractModel):
    """Worker policy: answer and play an announcement. Valid only while
    an agent is registered for inbound on the line (params via admin)."""

    policy: Literal["answer_and_announce"] = "answer_and_announce"


FallbackPolicy = Annotated[
    NoPickupFallback
    | RejectFallback
    | ForwardViaLineFallback
    | RecordAndTelegramFallback
    | AnswerAndAnnounceFallback,
    Field(discriminator="policy"),
]
"""Discriminated union over the five documented fallback policies."""


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


class LineSummary(ContractModel):
    """Agent-facing line listing row (wire: call.get_lines)."""

    line_id: LineId
    caps: list[Intent]
    state: LineState
    profile: str | None = None


class LineAssignment(ContractModel):
    """Durable line<->owner/provider binding (design §4.2).

    Survives agent sleep/disconnect and provider reconnects (no
    heartbeat). ``idle_revoke_after`` is ALERT-ONLY (round 10c):
    expiry triggers escalation notifications, not silent reassignment.
    """

    assignment_id: AssignmentId
    line_id: LineId
    owner_agent_id: str
    mode: AssignmentMode
    claimable: bool
    accept_window_sec: float = Field(default=30.0, gt=0)
    fallback_policy: FallbackPolicy = Field(default_factory=NoPickupFallback)
    allowed_number_types: frozenset[str] = Field(
        default_factory=lambda: frozenset(
            {"FIXED_LINE", "MOBILE", "FIXED_LINE_OR_MOBILE", "VOIP", "TOLL_FREE"}
        )
    )
    """Per-line phonenumbers-type allowlist (issue #25, round 15):
    `number_type()` results outside this set are rejected with
    DIAL_REJECTED/UNSUPPORTED_NUMBER_TYPE. Premium/shared-cost types
    are opt-in via admin config. ``FIXED_LINE_OR_MOBILE`` is REQUIRED
    in the default - phonenumbers returns this composite type for
    NANP numbers, so a plain US/CA mobile would otherwise be rejected
    as UNSUPPORTED_NUMBER_TYPE (review round 15)."""
    last_seen_at: AwareDatetime
    idle_revoke_after: timedelta | None = None


class CallLease(ContractModel):
    """Ephemeral call/line-hold record (design §4.2).

    ``lease_id`` is the SOLE identifier of the active hold and call -
    no call_id exists anywhere (round 11). Socket/heartbeat-bound:
    a dead agent loses the lease and the call is hung up (dead-man
    switch, §5.4).

    ``answered_at`` / ``answer_confidence`` carry the provisional-
    answer semantic: CallPhase.ACTIVE reflects oFono State=active,
    which may not equal remote answer (§5.5 open spike). The lease
    therefore records WHEN activity was first seen (answered_at) and
    HOW confident the bridge is that a human answered
    (answer_confidence, default "provisional"). A later PLAN_DELTA
    may redefine this once the spike resolves.
    """

    lease_id: LeaseId
    line_id: LineId
    owner_agent_id: str
    intent: Intent
    phase: CallPhase = CallPhase.DIALING
    created_at: AwareDatetime
    answered_at: AwareDatetime | None = None
    answer_confidence: AnswerConfidence = "provisional"


class QueueTicket(ContractModel):
    """FIFO queue entry for a queued acquire (§3.4).

    FIFO guarantee: ``seq`` is a strictly monotonic sequence number
    issued per queue scope; tickets order by ``seq`` (``__lt__`` below),
    which IS the mandatory FIFO ordering of docs/api-contract.md
    "Queue semantics". Tickets are socket-bound - disconnect kills them;
    the queue itself must be bounded (rule 7: queue overflow reports
    STALE_TOKEN/QUEUE_FULL reason detail, never an unbounded park).
    """

    ticket_id: TicketId
    seq: int = Field(ge=0)
    session_id: SessionId
    intent: Intent
    preferred_line_id: LineId | None = None
    enqueued_at: AwareDatetime

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, QueueTicket):
            return NotImplemented
        return self.seq < other.seq


# ---------------------------------------------------------------------------
# Acquire result union (call.acquire; NO_FREE_LINE was removed, round 9)
# ---------------------------------------------------------------------------


class AcquireGranted(ContractModel):
    """Immediate grant: line + lease issued at once."""

    granted: Literal[True] = True
    line_id: LineId
    lease_id: LeaseId


class AcquireQueued(ContractModel):
    """Queued acquire: FIFO ticket issued; grant arrives later as a
    QueueReadyEvent which the agent must consume within grant TTL."""

    granted: Literal[False] = False
    queued: Literal[True] = True
    ticket_id: TicketId
    position: int = Field(ge=1)


class AcquireRefused(ContractModel):
    """Clean refusal: nothing available right now. This is a ROUTINE
    result, not an exception - the client retries itself (no
    server-side parked requests, round 8)."""

    granted: Literal[False] = False
    free_lines: list[LineId] = Field(default_factory=list)


def _acquire_discriminator(value: Any) -> str:
    """Discriminate the three acquire outcomes on (granted, queued):
    granted:true -> granted; else queued:true -> queued; else refused."""

    def _get(key: str) -> Any:
        if isinstance(value, dict):
            return value.get(key)
        return getattr(value, key, None)

    if _get("granted") is True:
        return "granted"
    if _get("queued") is True:
        return "queued"
    return "refused"


AcquireResult = Annotated[
    Annotated[AcquireGranted, Tag("granted")]
    | Annotated[AcquireQueued, Tag("queued")]
    | Annotated[AcquireRefused, Tag("refused")],
    Discriminator(_acquire_discriminator),
]
"""Tagged union of the three call.acquire outcomes."""


# ---------------------------------------------------------------------------
# Call results (server-issued media attachment, ADR-02)
# ---------------------------------------------------------------------------


class DialResult(ContractModel):
    """call.dial result: server-issued media attachment (ADR-02)."""

    lease_id: LeaseId
    media_endpoint: str
    media_token: MediaToken


class AcceptResult(ContractModel):
    """call.accept result: atomic lease + answer + media attach."""

    lease_id: LeaseId
    line_id: LineId
    media_endpoint: str
    media_token: MediaToken


class ClaimResult(ContractModel):
    """call.claim_exclusive result: claim is a REQUEST - moderator
    approval in the admin surface grants it; never self-granted (§4.2)."""

    claim_id: str
    status: Literal["pending_approval"] = "pending_approval"


# ---------------------------------------------------------------------------
# Domain events + typed envelope
# ---------------------------------------------------------------------------

PayloadT = TypeVar("PayloadT", bound=ContractModel)


class EventEnvelope(ContractModel, Generic[PayloadT]):
    """Typed envelope carrying a domain event payload to a subscriber.

    ``name`` is the domain EventName; the adapter maps to ``event.*``
    wire spellings and subscriber-scoped fields (e.g. session_id).
    """

    name: EventName
    emitted_at: AwareDatetime
    payload: PayloadT


class IncomingCallEvent(ContractModel):
    """incoming_call payload: first valid call.accept(call_token) wins
    (wake-and-grant, §4.3/§5.2). ``caller`` is redacted PII; ``expires_at``
    is the accept-window deadline (default 30 s, admin-configurable)."""

    line_id: LineId
    call_token: CallToken
    caller: RedactedString | None = None
    expires_at: AwareDatetime


class CallStateChangedEvent(ContractModel):
    """call_state_changed payload. ``phase`` is the domain CallPhase;
    the wire spelling "disconnected" maps to ENDED at the adapter
    boundary."""

    lease_id: LeaseId
    line_id: LineId
    phase: CallPhase
    reason: str | None = None


class QueueReadyEvent(ContractModel):
    """queue_ready payload: grant for a queued ticket. The agent must
    act (dial/release) within ``expires_at`` or the next ticket is
    served (§3.4 grant TTL)."""

    ticket_id: TicketId
    lease_id: LeaseId
    line_id: LineId
    expires_at: AwareDatetime


class AssignmentRevokedEvent(ContractModel):
    """assignment_revoked payload: durable binding ended (release,
    admin line.revoke, or idle escalation)."""

    assignment_id: AssignmentId
    line_id: LineId
    owner_agent_id: str
    reason: str | None = None


class AudioReadyEvent(ContractModel):
    """audio_ready payload (round 14): the provider's local BT SCO
    path is up - early media flows, uplink accepted without first-word
    clipping. Derived from ``provider.audio_ready`` ingress, NOT from
    remote answer - see ``answer_confidence`` for that distinction."""

    lease_id: LeaseId
    line_id: LineId


class FallbackTriggeredEvent(ContractModel):
    """fallback_triggered payload (round 14): delivered to the
    ``system:fallback_worker`` session only. ``call_token`` is the
    internal fallback_call_token re-issued after accept-window expiry -
    single-use, TTL-bound, bypasses the exclusive-owner check."""

    line_id: LineId
    call_token: CallToken
    caller: RedactedString | None = None
    policy: FallbackPolicy


class LineStateChangedEvent(ContractModel):
    """line_state_changed payload: LineState transition fact."""

    line_id: LineId
    old_state: LineState
    new_state: LineState
    reason: str | None = None


class MissedCallEvent(ContractModel):
    """missed_call payload: an inbound ring ended unanswered
    (accept-window expiry, caller hangup, or reject-policy outcome)."""

    line_id: LineId
    caller: RedactedString | None = None
    reason: str
    voicemail_ref: str | None = None


# ---------------------------------------------------------------------------
# Provider ingress payloads (narrowed surface: distilled FACTS, not
# raw WSS register/ping transport)
# ---------------------------------------------------------------------------


class ProviderRegistration(ContractModel):
    """Distilled registration fact (provider.register at the boundary).

    ``token`` is the per-line shared secret - RedactedString so a
    registration record can never leak it to logs (rule 10)."""

    line_id: LineId
    token: RedactedString
    caps: list[Intent]
    profile: str | None = None
    meta: dict[str, str] = Field(default_factory=dict)


class ProviderLiveness(ContractModel):
    """Provider liveness fact: reachable/unreachable + timestamp.

    Derived from heartbeat health (missed pings -> DEGRADED -> DOWN);
    the ping transport itself stays in the adapter."""

    line_id: LineId
    assignment_id: AssignmentId | None = None
    state: LivenessState
    checked_at: AwareDatetime


class ProviderCallStateUpdate(ContractModel):
    """provider.call_state ingress fact (bounded ProviderCallState).

    ``call_token`` identifies the inbound ring; ``lease_id`` (round 14)
    identifies an established call - without it the gateway cannot
    attribute an update to a lease."""

    line_id: LineId
    assignment_id: AssignmentId
    call_token: CallToken | None = None
    lease_id: LeaseId | None = None
    state: ProviderCallState
    reason: str | None = None
    observed_at: AwareDatetime


class IncomingRingIngress(ContractModel):
    """Ring ingress fact: a physical call is ringing on the line.

    Distilled from the provider's RINGING report; carries the issued
    call_token and the redacted caller id for wake-and-grant routing."""

    line_id: LineId
    assignment_id: AssignmentId
    call_token: CallToken
    caller: RedactedString | None = None
    observed_at: AwareDatetime


class FallbackDecision(ContractModel):
    """Result of the fallback policy decision point (§5.3).

    LineManager calls decide_fallback() when the accept window expires
    (or no subscribed/owner agent exists); the returned policy is then
    executed - core policies inside LineManager, worker policies by the
    internal FallbackWorker via the standard agent API (ADR-01)."""

    line_id: LineId
    call_token: CallToken
    policy: FallbackPolicy
    reason: str | None = None
    fallback_call_token: CallToken | None = None
    """Re-issued ring token for WORKER policies (round 14): bound to
    ``system:fallback_worker``, single-use, TTL-bound, bypasses the
    exclusive-owner check so FallbackWorker can call accept() after
    the original call_token went stale. None for CORE policies."""


# ---------------------------------------------------------------------------
# Abstract kernel interface
# ---------------------------------------------------------------------------


class LineManager(ABC):
    """Abstract domain kernel: single owner of line/lease/queue truth.

    Global behavioral contract - applies to EVERY method:

    - Idempotent-or-duplicate-reject: a retried request is either
      idempotent (same outcome, no second side effect - e.g.
      provider.reregister during the grace window) or rejected with a
      duplicate/stale error. Never double-applied.
    - Atomic state transitions: the implementation is a
      single-event-loop component. Each method's state read-modify-write
      is atomic w.r.t. all other methods AND ingress calls; the
      reentrancy invariant is "no await inside a state transition
      critical section" (rule 4).
    - Bounded waits only (rule 2): every call resolves, fails, or
      queues; nothing parks server-side except the bounded FIFO queue.
    - Ownership checks on every id-keyed access (rule 9): session/lease/
      token/assignment ownership is verified per call, not assumed.
    - ``now``/timestamps are aware UTC only (rule 6).
    """

    # --- agent-facing surface (APICallService; agent.*/call.*) ---

    @abstractmethod
    async def register_intent(
        self,
        agent_id: str,
        intents: Collection[Intent],
        *,
        profile: str | None = None,
    ) -> SessionId:
        """agent.register_intent: declare intents -> session_id.

        Resolves declaratively into LineAssignment records via the
        intent-resolver (§4.1). Token authentication happens at the
        transport boundary; the domain sees an already-authenticated
        agent_id.
        """
        ...

    @abstractmethod
    async def acquire(
        self,
        session_id: SessionId,
        intent: Intent,
        *,
        line_id: LineId | None = None,
        profile: str | None = None,
        enqueue: bool = False,
    ) -> AcquireResult:
        """call.acquire: immediate grant, clean refusal, or FIFO ticket.

        ``intent`` is restricted to ``Intent.DIAL_OUT`` (round 14):
        inbound rings are consumed atomically by ``accept(call_token)``
        and durable exclusive ownership goes through
        ``claim_exclusive`` - anything else is INVALID_INTENT.
        INVALID_INTENT when the intent is not in the line's caps;
        LINE_EXCLUSIVE_NOT_OWNER (+ ErrorData.free_lines) on exclusive
        conflict; LINE_BUSY_INCOMING on a RINGING_LOCKED line. A busy
        pool returns AcquireRefused - routine result, never an error.
        Retry semantics: identical retry while queued returns the SAME
        ticket (idempotent), not a duplicate queue entry.
        """
        ...

    @abstractmethod
    async def cancel_acquire(self, session_id: SessionId, ticket_id: TicketId) -> None:
        """call.cancel_acquire: withdraw a queued ticket. Idempotent:
        cancelling a dead/served ticket is a no-op, not an error."""
        ...

    @abstractmethod
    async def release(self, session_id: SessionId, lease_id: LeaseId) -> None:
        """call.release: end the lease (future calls only; an in-flight
        call finishes, §4.2). OWNER_MISMATCH on a foreign lease.
        Idempotent: releasing an already-released lease is a no-op."""
        ...

    @abstractmethod
    async def dial(
        self,
        session_id: SessionId,
        lease_id: LeaseId,
        number: RedactedString,
        country: str,
    ) -> DialResult:
        """call.dial: issue line.dial to the owning provider ->
        server-issued media attachment. ``number`` is PII: it arrives
        wrapped in RedactedString and must be unwrapped ONLY at the
        provider boundary. ``country`` (ISO 3166-1 alpha-2, round 12)
        is the caller's assertion cross-checked against the number via
        phonenumbers BEFORE the line is touched (DIAL_REJECTED with a
        typed reason); per-line ``allowed_number_types`` on the
        LineAssignment gates UNSUPPORTED_NUMBER_TYPE - only LineManager
        knows which line backs the lease, so this check cannot live in
        the transport adapter (round 15). OWNER_MISMATCH /
        PROVIDER_UNREACHABLE / DIAL_REJECTED apply; a duplicate dial on
        one lease is rejected (INVALID_CALL_STATE reason under
        STALE_TOKEN is the convention)."""
        ...

    @abstractmethod
    async def accept(self, session_id: SessionId, call_token: CallToken) -> AcceptResult:
        """call.accept: ATOMIC lease issue + ring answer + media attach
        (§5.2 step 4). First valid accept wins; a second accept on the
        consumed token gets STALE_TOKEN; owner-scoped lines reject
        non-owners with OWNER_MISMATCH."""
        ...

    @abstractmethod
    async def hangup(self, session_id: SessionId, lease_id: LeaseId) -> None:
        """call.hangup: end the call; lease teardown follows §5.4
        (dead-man switch / orphan grace are impl concerns). Idempotent
        on an already-ended call; OWNER_MISMATCH on foreign lease."""
        ...

    @abstractmethod
    async def send_dtmf(self, session_id: SessionId, lease_id: LeaseId, digits: str) -> None:
        """call.send_dtmf: IVR navigation tones; mandatory from v1.
        INVALID_CALL_STATE reason when the lease has no live call."""
        ...

    @abstractmethod
    async def claim_exclusive(self, session_id: SessionId, line_id: LineId) -> ClaimResult:
        """call.claim_exclusive: file a CLAIM REQUEST. Returns
        pending_approval - never self-granted; a moderator approves in
        the admin surface (§4.2 round 10d). Conflict ->
        LINE_EXCLUSIVE_NOT_OWNER / LINE_NOT_CLAIMABLE reason."""
        ...

    @abstractmethod
    async def get_lines(
        self, session_id: SessionId, intent: Intent | None = None
    ) -> list[LineSummary]:
        """call.get_lines: pool view filtered by intent. Always bounded
        (rule 7): the line set is finite config, not a stream."""
        ...

    # --- provider ingress (narrowed: facts and decisions, not transport) ---

    @abstractmethod
    async def register_provider(self, registration: ProviderRegistration) -> LineAssignment:
        """Provider attach fact -> durable LineAssignment.

        This is the DISTILLED domain fact of a provider.register, not
        the WSS transport method - credential checks and socket
        lifecycle stay in the adapter. Reregister during the grace
        window is idempotent and preserves in-flight leases (wire:
        provider.reregister, 10 s orphan grace, §5.4).
        """
        ...

    @abstractmethod
    async def note_provider_liveness(self, liveness: ProviderLiveness) -> None:
        """Ingest a provider liveness fact (reachable/unreachable +
        timestamp). Drives DEGRADED -> DOWN line-state transitions and
        PROVIDER_UNREACHABLE outcomes. Idempotent: replaying a stale
        fact is harmless; ordering is by checked_at."""
        ...

    @abstractmethod
    async def ingest_ring(self, ring: IncomingRingIngress) -> IncomingCallEvent:
        """Ring ingress: physical call ringing -> RINGING_LOCKED +
        wake-and-grant routing (§4.3). Returns the incoming_call event
        the implementation must emit. Atomic w.r.t. concurrent acquires
        on the same line (single-event-loop invariant)."""
        ...

    @abstractmethod
    async def ingest_call_state(self, update: ProviderCallStateUpdate) -> None:
        """Call-state ingress: bounded ProviderCallState update mapped
        into lease phase transitions. Stale/out-of-order updates are
        deduped by observed_at; DISCONNECTED triggers lease teardown
        (§5.4)."""
        ...

    @abstractmethod
    async def ingest_audio_ready(
        self, line_id: LineId, assignment_id: AssignmentId, lease_id: LeaseId
    ) -> None:
        """provider.audio_ready ingress (round 14): the provider's local
        BT SCO path is up for ``lease_id`` -> emit AudioReadyEvent to
        the lease owner. One-shot per lease; a duplicate or unknown-
        lease report is dropped (idempotent). NOT a call state - it
        says nothing about remote answer."""
        ...

    @abstractmethod
    async def decide_fallback(self, line_id: LineId, call_token: CallToken) -> FallbackDecision:
        """Fallback POLICY DECISION point (§5.3): on accept-window
        expiry or no subscribed/owner agent, resolve the line's
        fallback_policy into a concrete decision. Policy selection is
        domain concern; EXECUTION is internal (core ops inline, worker
        policies via FallbackWorker over the agent API - ADR-01)."""
        ...

    # --- agent session lifecycle ---

    @abstractmethod
    async def drop_session(self, session_id: SessionId) -> None:
        """Agent socket teardown fact (round 15): the transport layer
        reports a disconnected session so the kernel can atomically
        (a) kill every QueueTicket owned by the session - tickets are
        socket-bound (§3.4) - and (b) run the dead-man switch on every
        CallLease owned by it: hangup + lease teardown (§5.4).
        Idempotent: a repeated drop on an already-gone session is a
        no-op. This is the ONLY way sessions end; there is no
        per-ticket/per-lease sweep API."""
        ...

    @abstractmethod
    async def tick(self, now: AwareDatetime) -> None:
        """Single generic lifecycle hook for time-driven housekeeping:
        accept-window expiry, queue grant-TTL advancement, heartbeat
        DEGRADED->DOWN escalation.

        Justification for a hook at all: the narrowed ingress surface
        forbids per-timer public methods, yet all of the above are
        time-driven state transitions that must be atomic w.r.t. the
        other calls (single-event-loop invariant). One explicit tick
        lets the runtime drive them deterministically - including in
        unit tests, which inject time instead of sleeping. This is the
        ONLY timer entry point; implementations may no-op if they own
        internal timers.

        ``now`` MUST be a timezone-aware UTC datetime (rule 6). The
        ``AwareDatetime`` annotation is DOCUMENTARY on method
        parameters: ``AfterValidator`` runs only under pydantic model
        validation, so the aware-UTC requirement is a caller/
        implementation contract, not a runtime check. Implementations
        SHOULD apply the same rule as :data:`AwareDatetime` (reject
        naive, normalize to UTC) when tick is driven from an untrusted
        entry point.
        """
        ...
