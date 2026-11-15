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

import re
from abc import ABC, abstractmethod
from collections.abc import Collection
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Annotated, Any, Generic, Literal, NewType, Protocol, TypeVar, cast

import phonenumbers
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    GetCoreSchemaHandler,
    Tag,
    field_validator,
    model_validator,
)
from pydantic_core import core_schema

__all__ = [
    "CALL_PHASE_WIRE_STATES",
    "DTMF_PATTERN",
    "E164_PATTERN",
    "EVENT_WIRE_NAMES",
    "MAX_DTMF_DIGITS",
    "MAX_IDEMPOTENCY_KEY_LEN",
    "MAX_PENDING_CLAIMS_PER_AGENT",
    "MAX_PENDING_CLAIMS_TOTAL",
    "MAX_PROVIDER_META_ENTRIES",
    "MAX_PROVIDER_META_KEY_LEN",
    "MAX_PROVIDER_META_VALUE_LEN",
    "PROVIDER_COMMAND_WIRE_NAMES",
    "AcceptResult",
    "AcquireGranted",
    "AcquireQueued",
    "AcquireRefused",
    "AcquireResult",
    "AdminAudience",
    "AgentAudience",
    "AnswerAndAnnounceFallback",
    "AnswerConfidence",
    "AssignmentId",
    "AssignmentMode",
    "AssignmentRevokedEvent",
    "AudioReadyEvent",
    "AwareDatetime",
    "BroadcastAudience",
    "CallEndReason",
    "CallLease",
    "CallPhase",
    "CallStateChangedEvent",
    "CallToken",
    "ClaimId",
    "ClaimResult",
    "Clock",
    "ContractModel",
    "DialResult",
    "ErrorCode",
    "ErrorData",
    "ErrorReason",
    "EventAudience",
    "EventEnvelope",
    "EventName",
    "FallbackDecision",
    "FallbackPolicy",
    "FallbackTriggeredEvent",
    "ForwardViaLineFallback",
    "IdleAlertEvent",
    "IncomingCallEvent",
    "IncomingRingIngress",
    "Intent",
    "LeaseId",
    "LineAdminKernel",
    "LineAssignment",
    "LineId",
    "LineManager",
    "LineState",
    "LineStateChangedEvent",
    "LineSummary",
    "LineTimeouts",
    "LivenessState",
    "MediaAllocator",
    "MediaCredentials",
    "MediaToken",
    "MissedCallEvent",
    "NoPickupFallback",
    "PendingClaim",
    "PhonebridgeError",
    "ProviderActiveCall",
    "ProviderAnswerCommand",
    "ProviderBinding",
    "ProviderCallState",
    "ProviderCallStateUpdate",
    "ProviderCapabilityUpdate",
    "ProviderCommand",
    "ProviderDialCommand",
    "ProviderDisconnectReason",
    "ProviderHangupCommand",
    "ProviderLiveness",
    "ProviderRegistration",
    "ProviderRevokeCommand",
    "ProviderSendDtmfCommand",
    "QueueReadyEvent",
    "QueueTicket",
    "QueueUpdatedEvent",
    "RecordAndTelegramFallback",
    "RedactedString",
    "RejectFallback",
    "SessionAudience",
    "SessionId",
    "TicketId",
    "TimeoutConfig",
    "WireCallState",
    "ensure_aware_utc",
    "validate_e164_country",
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

ClaimId = NewType("ClaimId", str)
"""Pending exclusive-line claim request id (call.claim_exclusive);
resolved by the admin approve_claim/reject_claim pair (round 21)."""

LeaseId = NewType("LeaseId", str)
"""Ephemeral single id of the active line hold AND the call.
Replaces any call_id everywhere - no call_id exists (round 11)."""

MediaToken = NewType("MediaToken", str)
"""Authorizes the binary media socket handshake; bound to a
(lease_id, role) pair - every lease carries TWO independent one-time
tokens (agent vs provider) so media.auth role claims are
cryptographically backed (round 21, CRITICAL-2)."""

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

    ACQUIRED = "acquired"
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


WireCallState = Literal["dialing", "alerting", "active", "disconnected"]
"""The four ``state`` values legal on the wire event
``event.call_state_changed`` (docs/api-contract.md). Internal
phases that have no wire spelling map to ``None`` in
``CALL_PHASE_WIRE_STATES`` (round 20)."""


CALL_PHASE_WIRE_STATES: dict[CallPhase, WireCallState | None] = {
    CallPhase.ACQUIRED: None,
    CallPhase.DIALING: "dialing",
    CallPhase.RINGING: None,
    CallPhase.ALERTING: "alerting",
    CallPhase.ACTIVE: "active",
    CallPhase.TERMINATING: None,
    CallPhase.ENDED: "disconnected",
}
"""Authoritative CallPhase → wire-state map (round 20).

``None`` means "kernel-internal - never emitted as
``event.call_state_changed.state``" (ACQUIRED is pre-dial, RINGING
surfaces as ``event.incoming_call``, TERMINATING is transient).
Drift against the doc's state vocabulary is caught by
``test_call_phase_wire_state_parity``."""


class AssignmentMode(str, Enum):
    """LineAssignment.mode (design §4.2)."""

    EXCLUSIVE = "exclusive"
    SHARED_POOL = "shared_pool"


class LivenessState(str, Enum):
    """Provider liveness fact reported via note_provider_liveness.

    WS_DISCONNECTED (round 21, CRITICAL-3): the provider's WS control
    channel dropped while the telephony path may still be alive -
    starts the 10 s orphan-grace window
    (``ProviderBinding.orphan_deadline_at``), after which a
    ``provider.reregister`` carrying a ``state_snapshot`` decides
    re-bind vs teardown. UNREACHABLE instead means heartbeat timeout
    (DEGRADED -> DOWN) and tears leases down IMMEDIATELY - no grace
    (design §5.4)."""

    REACHABLE = "reachable"
    WS_DISCONNECTED = "ws_disconnected"
    UNREACHABLE = "unreachable"


class ProviderDisconnectReason(str, Enum):
    """CLOSED provider-reported disconnect-reason vocabulary
    (wire: ``provider.call_state{state:"disconnected", reason}``,
    round 21, MEDIUM-9).

    These are facts the provider itself reports. ``provider_lost``
    is deliberately absent: it is minted by the gateway on
    orphan-grace expiry or heartbeat timeout, never by a provider -
    it lives in :class:`CallEndReason` instead."""

    NORMAL = "normal"
    BUSY = "busy"
    NO_ANSWER = "no_answer"
    REJECTED = "rejected"
    NETWORK_ERROR = "network_error"
    DONGLE_LOST = "dongle_lost"
    STACK_RESTART = "stack_restart"


class CallEndReason(str, Enum):
    """CLOSED reason vocabulary for the ``reason`` fields of
    ``event.call_state_changed``, ``event.missed_call`` and
    ``event.line_state_changed`` plus lease-teardown paths
    (round 21, MEDIUM-9; wired into the event payloads in the
    round-21 review fix).

    Superset of :class:`ProviderDisconnectReason` plus the reasons
    only the gateway can mint: agent-initiated hangup, inbound
    preemption of an undialed grant, grant/accept window expiry,
    dead-man teardowns, admin revokes and the ``external_call`` line
    fact (a human-initiated call on the physical phone surfaces as
    ``event.line_state_changed{new_state:"BUSY", reason:"external_call"}``,
    design §9 - no lease is ever minted for it). Wire spellings are
    lowercase snake_case - the uppercase ErrorReason members that
    mirror some of the same facts serve the error-channel sub-code
    convention, a different field, and the two vocabularies are
    intentionally not merged. ``ProviderCallStateUpdate.reason``
    deliberately stays on the narrower provider-reported
    :class:`ProviderDisconnectReason` - gateway-minted members of
    this enum can never arrive on that ingress."""

    NORMAL = "normal"
    BUSY = "busy"
    NO_ANSWER = "no_answer"
    REJECTED = "rejected"
    NETWORK_ERROR = "network_error"
    DONGLE_LOST = "dongle_lost"
    STACK_RESTART = "stack_restart"
    AGENT_HANGUP = "agent_hangup"
    PREEMPTED_BY_INCOMING = "preempted_by_incoming"
    GRANT_TTL_EXPIRED = "grant_ttl_expired"
    ACCEPT_WINDOW_EXPIRED = "accept_window_expired"
    AGENT_DISCONNECTED = "agent_disconnected"
    PROVIDER_LOST = "provider_lost"
    ADMIN_REVOKED = "admin_revoked"
    EXTERNAL_CALL = "external_call"


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
    INVALID_STATE = "INVALID_STATE"
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    RESOURCE_EXHAUSTED = "RESOURCE_EXHAUSTED"


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
    # Round 20 additions (paired with the new INVALID_STATE /
    # INVALID_ARGUMENT / RESOURCE_EXHAUSTED wire codes).
    GRANT_TTL_EXPIRED = "GRANT_TTL_EXPIRED"
    SESSION_LIMIT_EXCEEDED = "SESSION_LIMIT_EXCEEDED"
    CLAIM_NOT_FOUND = "CLAIM_NOT_FOUND"
    INVALID_DTMF = "INVALID_DTMF"


class EventName(str, Enum):
    """Domain event names carried on EventEnvelope.

    Wire spellings under ``event.*`` are an adapter-boundary mapping
    concern - the authoritative table is ``EVENT_WIRE_NAMES`` below.
    Any new member MUST get a wire entry in the same commit (drift
    test ``test_event_wire_name_parity`` enforces this).
    """

    INCOMING_CALL = "incoming_call"
    CALL_STATE_CHANGED = "call_state_changed"
    QUEUE_READY = "queue_ready"
    QUEUE_UPDATED = "queue_updated"
    ASSIGNMENT_REVOKED = "assignment_revoked"
    AUDIO_READY = "audio_ready"
    FALLBACK_TRIGGERED = "fallback_triggered"
    LINE_STATE_CHANGED = "line_state_changed"
    MISSED_CALL = "missed_call"
    IDLE_ALERT = "idle_alert"


EVENT_WIRE_NAMES: dict[EventName, str] = {
    EventName.INCOMING_CALL: "event.incoming_call",
    EventName.CALL_STATE_CHANGED: "event.call_state_changed",
    EventName.QUEUE_READY: "event.line_acquired",
    EventName.QUEUE_UPDATED: "event.queue_updated",
    EventName.ASSIGNMENT_REVOKED: "event.assignment_revoked",
    EventName.AUDIO_READY: "event.audio_ready",
    EventName.FALLBACK_TRIGGERED: "event.fallback_triggered",
    EventName.LINE_STATE_CHANGED: "event.line_state_changed",
    EventName.MISSED_CALL: "event.missed_call",
    EventName.IDLE_ALERT: "event.idle_alert",
}
"""Authoritative domain→wire event mapping (round 19b).

Every EventName resolves to the ``event.<name>`` notification an
agent session receives. Names are intentionally not always equal:
``queue_ready`` describes the queue-side grant while the wire
``line_acquired`` describes what the agent got. Drift between this
table and docs/api-contract.md's ``event.*`` table is caught by
``test_event_wire_name_parity``."""


# ---------------------------------------------------------------------------
# Scalars
# ---------------------------------------------------------------------------


def ensure_aware_utc(value: datetime) -> datetime:
    """Enforce CODING_HAZARDS rule 6: reject naive datetimes; normalize
    aware values to canonical UTC. Public (round 20) so that runtime
    entry points - e.g. every ``LineManager.tick(now)`` implementation
    and any adapter ingress bypassing pydantic validation - can apply
    the same check the ``AwareDatetime`` annotation only documents."""
    if not isinstance(value, datetime):
        raise TypeError(f"expected datetime, got {type(value).__name__}")
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError("naive datetime rejected: contract timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


AwareDatetime = Annotated[datetime, AfterValidator(ensure_aware_utc)]
"""Annotated datetime that REJECTS naive input and normalizes to UTC."""

AnswerConfidence = Literal["provisional", "confirmed"]
"""Confidence that the remote party actually answered (see CallPhase.ACTIVE)."""


MAX_DTMF_DIGITS: int = 32
"""Upper bound for a single call.send_dtmf `digits` payload (round
20, rule 7): an unbounded digit string could park synthesized tones
in the provider queue for minutes."""

DTMF_PATTERN: re.Pattern[str] = re.compile(rf"^[0-9A-D*#,wW]{{1,{MAX_DTMF_DIGITS}}}$")
"""Legal DTMF alphabet (round 20): digits 0-9, letters A-D, `*` and
`#`, plus pause commands `,`/`w`/`W` (~500 ms pause each); length
bound derives from MAX_DTMF_DIGITS. Violation -> INVALID_ARGUMENT +
ErrorReason.INVALID_DTMF."""

MAX_IDEMPOTENCY_KEY_LEN: int = 128
"""Upper bound on an ``idempotency_key`` (round 23, review-12 M2;
CODING_HAZARDS rule 7 on an external agent input): when present the
key is a non-empty string of at most 128 chars - an empty or
overlong key is INVALID_ARGUMENT at the boundary."""

MAX_PENDING_CLAIMS_PER_AGENT: int = 8
"""Bounded pending ``call.claim_exclusive`` requests per agent
(round 22, review-10 MEDIUM-4; CODING_HAZARDS rule 7): a buggy agent
spamming claims cannot exhaust kernel memory - overflow is
RESOURCE_EXHAUSTED."""

MAX_PENDING_CLAIMS_TOTAL: int = 256
"""Global bound on pending claims across all agents (round 22,
review-10 MEDIUM-4): the per-agent cap alone is not enough when
many agents connect; overflow is RESOURCE_EXHAUSTED."""

MAX_PROVIDER_META_ENTRIES: int = 16
"""Upper bound on ``ProviderRegistration.meta`` entries (round 22,
review-10 MEDIUM-5; rule 7 on an external JSON-RPC ingress field)."""

MAX_PROVIDER_META_KEY_LEN: int = 64
"""Upper bound on a ``ProviderRegistration.meta`` key length."""

MAX_PROVIDER_META_VALUE_LEN: int = 256
"""Upper bound on a ``ProviderRegistration.meta`` value length."""

_MASK = "****"
_MASK_PREFIX_LEN = 4
_MASK_SUFFIX_LEN = 2
_MASK_FULL_LEN = 4
"""Values up to this length mask completely - a 4-char secret exposes
nothing. Above it the visible frame is capped at half the length, so a
masked rendering never reveals more than half the raw characters."""


class RedactedString:
    """PII-safe string container (api-contract.md "Common types";
    CODING_HAZARDS rule 10). NOT a str subclass (round 20 rewrite).

    Inheriting ``str`` was fail-open: CPython's ``json.dumps`` and
    ``+``-concatenation read the inner ``PyUnicodeObject`` buffer
    directly, bypassing the masked ``__str__`` - structured loggers
    would emit the raw secret. This class is fail-CLOSED instead: the
    raw value is unreachable except through the explicit
    ``.expose()`` / ``.to_wire()`` accessors, while
    ``json.dumps(redacted)``, ``"x" + redacted`` and implicit
    ``isinstance(x, str)`` paths all raise/behave safely.

    - ``.expose()`` / ``.to_wire()`` return the raw value - for
      deliberate in-memory / wire use only, never for logs.
    - ``__str__`` / ``__repr__`` / ``__format__`` render only the
      masked form ("+373****45"); any format spec applies to the mask.
    - ``model_dump_json()`` / ``model_dump(mode="json")`` emit the raw
      value via the explicit serializer (socket contract unchanged).
    - Equality is RedactedString-to-RedactedString only; comparing
      with a plain ``str`` is ``False`` so a raw string can never
      silently stand in for a secret.
    """

    __slots__ = ("_value",)

    _value: str

    def __init__(self, value: str | RedactedString) -> None:
        if isinstance(value, RedactedString):
            self._value = value._value
        elif isinstance(value, str):
            self._value = value
        else:
            raise TypeError(f"RedactedString expects str, got {type(value).__name__}")

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
        return self._value

    def to_wire(self) -> str:
        """Raw value (plain str) for explicit wire serialization."""
        return self._value

    def __len__(self) -> int:
        return len(self._value)

    def __bool__(self) -> bool:
        return bool(self._value)

    def __hash__(self) -> int:
        return hash(self._value)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, RedactedString):
            return False
        return self._value == other._value

    def __repr__(self) -> str:
        return f"RedactedString({self._mask(self._value)!r})"

    def __str__(self) -> str:
        return self._mask(self._value)

    def __format__(self, format_spec: str) -> str:
        """Apply the spec to the MASKED value: ``f"{s:>20}"`` aligns the
        mask, so no format spec can ever emit the raw string."""
        return self._mask(self._value).__format__(format_spec)

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source_type: Any, handler: GetCoreSchemaHandler
    ) -> core_schema.CoreSchema:
        """Accept an existing RedactedString or wrap a raw ``str``;
        JSON mode uses an explicit serializer so
        ``model_dump_json()`` always emits ``.to_wire()`` (raw value
        for the socket) - Python-mode dumps keep the container with
        its masked repr (rounds 15/20)."""
        return core_schema.union_schema(
            [
                core_schema.is_instance_schema(cls),
                core_schema.no_info_after_validator_function(cls, core_schema.str_schema()),
            ],
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
    """Recursively replace RedactedString with its masked string form
    and normalize every other value to a JSON-serializable scalar
    (round 23, review-12 H1).

    ``safe_dump()``/``to_log_dict()`` feed JSON loggers directly:
    ``json.dumps(model.to_log_dict())`` MUST always succeed. A
    ``model_dump()`` in ``"python"`` mode leaves ``datetime``,
    ``timedelta`` and ``Enum`` members as live Python objects that
    ``json.dumps`` cannot serialize (str-valued enums happen to
    survive; datetime/timedelta crash it outright) - they are
    converted here instead of relying on the ``"json"`` mode
    serializer, which would emit the RAW ``RedactedString`` wire
    value and leak PII into logs. Conversions:

    - ``datetime`` -> ``.isoformat()`` (already normalized to aware
      UTC by ``AwareDatetime`` validation);
    - ``timedelta`` -> ``.total_seconds()``;
    - ``Enum`` -> ``.value``;
    - ``set``/``frozenset`` -> deterministically sorted list.
    """
    if isinstance(value, RedactedString):
        return str(value)
    if isinstance(value, dict):
        return {k: _mask_for_log(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_mask_for_log(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return [_mask_for_log(v) for v in sorted(value, key=str)]
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, timedelta):
        return value.total_seconds()
    return value


class ContractModel(BaseModel):
    """Base for every domain contract model.

    - ``hide_input_in_errors=True``: a ValidationError must never echo
      raw caller input - a rejected ``{"caller": "+373..."}`` payload
      must not write the number into the error string (rule 10).
    - ``extra="forbid"``: the contract is closed; unknown keys are a
      defect, not silently dropped.
    - ``frozen=True`` (round 21, MEDIUM-8): every DTO is immutable.
      LineManager, EventBus and the adapters share one process, so a
      stray mutation in one subscriber would corrupt kernel state or
      a sibling's view; collection fields are ``tuple``/``frozenset``
      (JSON serialization to arrays is unchanged). NOTE: ``frozen``
      covers FIELD REASSIGNMENT only - a ``dict`` field kept for wire
      compat (``ProviderRegistration.meta``) remains mutable through
      its contents; treat such fields as immutable by convention
      (round-21 review).
    - ``safe_dump()``/``to_log_dict()`` are the ONLY allowed log dumps:
      raw ``model_dump()``/``model_dump_json()`` emit full PII wire
      values and are PROHIBITED for logging.
    """

    model_config = ConfigDict(hide_input_in_errors=True, extra="forbid", frozen=True)

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
    free_lines: tuple[LineId, ...] = ()


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
# Number validation (docs/api-contract.md "Dial validation", round 12)
# ---------------------------------------------------------------------------

E164_PATTERN: re.Pattern[str] = re.compile(r"^\+[1-9]\d{6,14}$")
"""Strict E.164 shape gate (api-contract.md "Dial validation"):
``+`` followed by a non-zero country-code digit and 7-15 digits
total. Local/national forms, missing ``+``, bare-``+`` hybrids,
spaces, punctuation and extension markers never pass - the check
runs BEFORE phonenumbers parsing so malformed input cannot borrow
a region."""

_ISO_ALPHA2_PATTERN: re.Pattern[str] = re.compile(r"^[A-Z]{2}$")


def validate_e164_country(number: RedactedString, country: str) -> str:
    """Pure E.164 + ISO alpha-2 cross-check (round 24, review-13 M1).

    Single domain helper for the "who may this number be" question so
    the check cannot drift between call sites: strict
    :data:`E164_PATTERN` shape gate first, then
    ``phonenumbers.parse`` + ``is_valid_number()`` +
    ``is_valid_number_for_region(parsed, country)`` (the documented
    ``call.dial`` pipeline - region check via
    ``is_valid_number_for_region``, NOT
    ``region_code_for_number() == country``, so shared calling codes
    like NANP ``+1`` never false-reject). ``country`` is a cross-check
    ASSERTION only - never a parse region and never a source of
    prefixes.

    Returns the validated raw E.164 string. Raises ``ValueError`` on
    any rejection - under pydantic model validation it surfaces as a
    ``ValidationError`` (INVALID_ARGUMENT at the adapter), on the
    ``call.dial`` path the kernel maps the same rejections to
    ``DIAL_REJECTED`` with the typed ``data.reason`` sub-codes."""
    raw = number.expose()
    if not E164_PATTERN.fullmatch(raw):
        raise ValueError("number is not strict E.164")
    if not _ISO_ALPHA2_PATTERN.fullmatch(country):
        raise ValueError("country is not ISO 3166-1 alpha-2")
    try:
        parsed = phonenumbers.parse(raw, None)
    except phonenumbers.NumberParseException as exc:
        raise ValueError("number does not parse") from exc
    if not phonenumbers.is_valid_number(parsed):
        raise ValueError("number is not a valid subscriber number")
    if not phonenumbers.is_valid_number_for_region(parsed, country):
        raise ValueError("number is not valid for the declared country")
    return raw


def _provider_caps_check(caps: tuple[Intent, ...]) -> tuple[Intent, ...]:
    """Shared ``caps`` invariant for provider-facing models (round 24,
    review-13 M3): a provider declares TELECOM capabilities only -
    ``caps`` must be non-empty and MUST NOT contain
    ``Intent.EXCLUSIVE_LINE``: durable exclusivity is a property of
    ``LineAssignment.claimable``/``mode`` granted by the admin claim
    flow, never of the hardware attachment."""
    if not caps:
        raise ValueError("caps must declare at least one intent")
    if Intent.EXCLUSIVE_LINE in caps:
        raise ValueError(
            "exclusive_line is not a provider capability - "
            "exclusivity lives on LineAssignment.claimable/mode"
        )
    return caps


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
    """Worker policy: dial OUT on another line and bridge the call.

    ``line_id`` selects the line the FallbackWorker dials THROUGH;
    ``target_number``/``target_country`` (round 23, review-12 C2)
    carry WHERE the call is forwarded - the FallbackWorker connects
    via the standard agent API only (ADR-01) and has no admin scope
    to query line config, so the policy model itself MUST carry the
    full dial target (same ``number`` + ``country`` pair semantics
    as ``call.dial``: ``target_number`` is a strict-E.164
    RedactedString, ``target_country`` the ISO 3166-1 alpha-2
    cross-check assertion). Round 24 (review-13 M1) moves the check
    INTO the model: ``_valid_dial_target`` runs
    :func:`validate_e164_country` at construction, so a corrupt
    SQLite/config row can never hydrate an invalid policy that would
    only explode inside the FallbackWorker at ring time;
    ``configure_line`` still applies the dial-time
    ``allowed_number_types`` gate against the TARGET line
    (``forward.line_id``) on top.

    A self-loop is banned at two levels: this model cannot know the
    configured line, so ``LineAssignment`` rejects
    ``fallback_policy.line_id == self.line_id`` (round 24, review-13
    M1) and ``configure_line`` rejects the same pair on the wire.
    Wire config spelling:
    ``forward_via_line:<line_id>:<e164_number>:<country>``."""

    policy: Literal["forward_via_line"] = "forward_via_line"
    line_id: LineId
    target_number: RedactedString
    target_country: str

    @model_validator(mode="after")
    def _valid_dial_target(self) -> ForwardViaLineFallback:
        """The policy carries a DIAL TARGET, not a label - validate it
        the way ``call.dial`` would (round 24, review-13 M1) so an
        invalid ``target_number``/``target_country`` pair can never
        exist inside a frozen model (including rows hydrated from
        SQLite, which bypass ``configure_line`` checks entirely)."""
        validate_e164_country(self.target_number, self.target_country)
        return self


class RecordAndTelegramFallback(ContractModel):
    """Worker policy: answer, record, post to a Telegram channel."""

    policy: Literal["record_and_telegram"] = "record_and_telegram"
    channel: str


class AnswerAndAnnounceFallback(ContractModel):
    """Worker policy: answer and play an announcement.

    Eligibility predicate inside ``decide_fallback`` (round 24,
    review-13 M4 - the earlier "only if an agent is registered for
    inbound" was ambiguous for an EXCLUSIVE line with a sleeping
    owner and named no default): the policy is eligible when the
    line's ``assignment.mode == AssignmentMode.EXCLUSIVE`` OR at
    least one live session holds ``Intent.RECEIVE_INBOUND`` on it
    (the ``system:fallback_worker`` session itself is EXCLUDED from
    that count - the worker never satisfies its own precondition);
    when neither holds, ``decide_fallback`` MUST degrade to
    ``NoPickupFallback`` instead of dispatching an announcement the
    worker could never execute.

    ``announcement_ref`` (round 22, review-10 MEDIUM-2) is the
    admin-provisioned announcement reference - a WAV asset id or text
    key the FallbackWorker resolves at execution time. The
    FallbackWorker connects via the standard agent API only (ADR-01),
    so the policy model itself MUST carry the announcement selector:
    the worker has no admin scope to query line config. Wire config
    spelling: ``answer_and_announce:<announcement_ref>``."""

    policy: Literal["answer_and_announce"] = "answer_and_announce"
    announcement_ref: str = Field(min_length=1)


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


class LineTimeouts(ContractModel):
    """Effective per-line timeout values (round 24, review-13 C2 /
    ADR-24.2): the CONCRETIZED counterpart of the ``TimeoutConfig``
    patch model - every field carries its resolved value, never
    ``None``.

    Why a second model exists: adapters need effective values the
    patch model cannot express - the manager adapter answers
    ``provider.register`` with ``heartbeat_interval_ms`` and the
    admin adapter write-through-saves the full resolved set into
    ``lines.timeouts_json`` (design §4.5); ``TimeoutConfig`` holds
    ``None`` for unpatched knobs and would silently persist holes.
    ``LineAssignment.timeouts`` therefore carries this model, and
    ``ProviderBinding.heartbeat_interval_ms`` stamps
    ``heartbeat_interval_sec`` (as ms) at register/reregister time so
    a runtime ``configure_line`` patch never retro-applies to a live
    provider session."""

    grant_ttl_sec: float = Field(default=5.0, gt=0)
    """Deadline to consume a grant (``AcquireGranted.expires_at`` /
    ``QueueReadyEvent.expires_at``) before the lease ends
    ``grant_ttl_expired``."""
    orphan_grace_sec: float = Field(default=10.0, gt=0)
    """WS-drop re-bind window - feeds
    ``ProviderBinding.orphan_deadline_at``."""
    heartbeat_interval_sec: float = Field(default=5.0, gt=0)
    """Required ``provider.ping`` cadence for the line's binding;
    surfaced to the provider as ``heartbeat_interval_ms`` inside the
    ``provider.register``/``provider.reregister`` response."""
    heartbeat_degraded_after: int = Field(default=2, ge=1)
    """Missed pings before the line goes DEGRADED."""
    heartbeat_down_after: int = Field(default=3, ge=1)
    """Missed pings before the line goes DOWN - MUST strictly exceed
    ``heartbeat_degraded_after`` (DOWN is the terminal escalation of
    DEGRADED, not a parallel state)."""
    fallback_accept_ttl_sec: float = Field(default=10.0, gt=0)
    """Deadline for the FallbackWorker to ``call.accept`` a re-issued
    ``fallback_call_token`` - bounds
    ``FallbackTriggeredEvent.expires_at``."""

    @model_validator(mode="after")
    def _heartbeat_thresholds_ordered(self) -> LineTimeouts:
        """Enforce ``heartbeat_down_after > heartbeat_degraded_after``:
        an inverted pair would mark the line DOWN before it could
        ever appear DEGRADED."""
        if self.heartbeat_down_after <= self.heartbeat_degraded_after:
            raise ValueError("heartbeat_down_after must exceed heartbeat_degraded_after")
        return self


class LineSummary(ContractModel):
    """Agent-facing line listing row (wire: call.get_lines)."""

    line_id: LineId
    caps: tuple[Intent, ...]
    state: LineState
    profile: str | None = None


class LineAssignment(ContractModel):
    """Durable line<->owner/provider binding (design §4.2).

    Survives agent sleep/disconnect and provider reconnects (no
    heartbeat). ``idle_revoke_after`` is ALERT-ONLY (round 10c):
    expiry triggers escalation notifications, not silent reassignment.

    ``owner_agent_id`` is ``None`` for a SHARED_POOL line (round 19):
    ``register_provider`` produces the assignment when the provider
    attaches - before any agent has claimed or been granted anything.
    A non-None value means durable exclusive ownership
    (``exclusive_line`` mode). This avoids magic placeholder strings
    ("", "__pool__") in implementations.

    ``assignment_id`` ROTATES (round 24, review-13 M2): every
    owner/mode change of the durable binding (``assign_line``,
    ``approve_claim``, owner ``release_assignment`` reverting to
    SHARED_POOL, ``revoke_assignment``) MINTS a new ``AssignmentId``
    rather than reusing the old one - reusing it let a stale retry
    from the previous owner collide with the new binding
    (OWNER_MISMATCH where idempotent no-op was promised, or a
    foreign ``{ok}``). The superseded id is retained in a bounded
    ``_closed_assignments`` LRU ring (cap 1024, same shape as
    ``_closed_leases``) so a late retry resolves as an idempotent
    success for its past owner instead.
    """

    assignment_id: AssignmentId
    line_id: LineId
    owner_agent_id: str | None = None
    mode: AssignmentMode
    claimable: bool
    accept_window_sec: float = Field(default=30.0, gt=0)
    fallback_policy: FallbackPolicy = Field(default_factory=NoPickupFallback)
    timeouts: LineTimeouts = Field(default_factory=LineTimeouts)
    """Effective per-line timeout values (round 24, review-13 C2):
    the resolved counterpart of the ``TimeoutConfig`` patch -
    hydrated into kernel state on ``configure_line`` and persisted
    verbatim to ``lines.timeouts_json`` by the admin adapter's
    write-through save (design §4.5)."""
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
    """Activity stamp for the ``idle_revoke_after`` window (round 24,
    review-13 H4): refreshed to ``now`` on EVERY line operation AND
    on ``drop_session`` of the owner's LAST open session. A line
    whose ``owner_agent_id`` still holds at least one open session is
    NEVER idle - a secretary agent sitting 25 h on a live socket
    waiting for inbound is ACTIVE and must not trigger a false
    ``event.idle_alert``; the idle clock only runs against a
    session-less owner."""
    idle_revoke_after: timedelta | None = None

    @model_validator(mode="after")
    def _validate_assignment_invariants(self) -> LineAssignment:
        """Enforce the model-boundary invariants (round 19 owner⇔mode;
        round 24 review-13 M1 self-loop): ``exclusive`` requires an
        owner, ``shared_pool`` forbids one - contradictory states
        cannot be constructed - and a ``forward_via_line`` policy can
        never name its own line as the dial-through target (that
        would bridge the ring onto itself; the check used to live
        only in ``configure_line``, which DB hydration bypasses)."""
        if self.mode is AssignmentMode.EXCLUSIVE and self.owner_agent_id is None:
            raise ValueError("exclusive assignment requires owner_agent_id")
        if self.mode is AssignmentMode.SHARED_POOL and self.owner_agent_id is not None:
            raise ValueError("shared_pool assignment cannot carry owner_agent_id")
        if (
            isinstance(self.fallback_policy, ForwardViaLineFallback)
            and self.fallback_policy.line_id == self.line_id
        ):
            raise ValueError("forward_via_line cannot target the same line_id")
        return self


class ProviderBinding(ContractModel):
    """Provider attachment record - a distinct object space from
    LineAssignment (round 20, fixes the ≤0.0.12 conflation where
    ``register_provider`` returned the AGENT-ownership record).

    Identity card of the registered provider: which line it serves,
    its caps/profile and liveness bookkeeping. ``assignment_id`` here
    is a *provider-binding id* - it shares the ``AssignmentId`` type
    tag but lives in a separate object space from
    ``LineAssignment.assignment_id`` (api-contract.md Identifier
    taxonomy); admin operations disambiguate by which object the id
    resolves to - the kernel MUST mint ids unique across BOTH spaces
    (a collision is a defect: the operation rejects the id as
    ambiguous rather than guessing). ``orphan_deadline_at`` is set
    only while a disconnected provider is inside the wire's 10 s
    orphan-grace window.
    """

    assignment_id: AssignmentId
    line_id: LineId
    caps: tuple[Intent, ...]
    """Provider-declared TELECOM capabilities - ``dial_out`` /
    ``receive_inbound`` only (round 24, review-13 M3): availability
    of durable exclusivity is a ``LineAssignment.claimable``/``mode``
    property, never a hardware capability, so
    ``Intent.EXCLUSIVE_LINE`` is rejected here the same way it is on
    the ``provider.register``/``provider.capability_update``
    ingress."""
    profile: str | None = None
    liveness: LivenessState = LivenessState.REACHABLE
    heartbeat_interval_ms: int = Field(default=5000, gt=0)
    """Ping cadence negotiated for THIS provider session (round 24,
    review-13 C2): stamped from the line's effective
    ``LineTimeouts.heartbeat_interval_sec`` (as ms) inside
    ``register_provider`` and returned to the adapter so the
    ``provider.register``/``provider.reregister`` response can carry
    the wire ``heartbeat_interval_ms``. A ``configure_line`` patch to
    ``heartbeat_interval_sec`` takes effect on the NEXT
    ``provider.reregister`` - it is never enforced retroactively
    against the interval stamped here (round 23, review-12 H6:
    shrinking it mid-session would kill a healthy provider on a
    false ping timeout)."""
    connected_at: AwareDatetime
    last_seen_at: AwareDatetime
    orphan_deadline_at: AwareDatetime | None = None

    @field_validator("caps")
    @classmethod
    def _caps_are_telecom_only(cls, caps: tuple[Intent, ...]) -> tuple[Intent, ...]:
        return _provider_caps_check(caps)


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
    session_id: SessionId
    """Owning agent session (round 22, review-10 CRITICAL-2): one
    ``agent_id`` may hold several concurrent sessions (parallel
    workers, a Control MCP session next to an audio-worker session),
    so lease ownership is authenticated on the ``(session_id,
    lease_id)`` pair for ``dial``/``hangup``/``release``/``send_dtmf``;
    ``drop_session`` tears down ONLY this session's leases without
    killing sibling sessions' calls, and ``SessionAudience`` events
    (``audio_ready``, ``call_state_changed``) route on it.
    ``owner_agent_id`` stays for admin views and durable-binding
    correlation."""
    intent: Intent
    phase: CallPhase = CallPhase.ACQUIRED
    created_at: AwareDatetime
    answered_at: AwareDatetime | None = None
    answer_confidence: AnswerConfidence = "provisional"
    grant_expires_at: AwareDatetime | None = None
    """Grant deadline copied from AcquireGranted/QueueReadyEvent
    (round 20): a lease still in CallPhase.ACQUIRED whose
    grant_expires_at lapses is torn down as
    call_state_changed{reason: "grant_ttl_expired"} by tick(). None
    means "no grant TTL pending" (dial already issued, or the lease
    came from accept() - inbound rings carry their own call_token
    expiry)."""


class QueueTicket(ContractModel):
    """FIFO queue entry for a queued acquire (§3.4).

    FIFO guarantee: ``seq`` is a strictly monotonic sequence number
    issued from ONE global counter across all queue scopes (round 19
    fairness rule): when line L frees, the grant goes to the
    lowest-seq eligible ticket regardless of scope (per-line and pool
    tickets compete in a single order - no starvation). Tickets order
    by ``seq`` (``__lt__`` below), which IS the mandatory FIFO ordering
    of docs/api-contract.md "Queue semantics". Tickets are
    socket-bound - disconnect kills them;
    the queue itself must be bounded (rule 7: queue overflow reports
    RESOURCE_EXHAUSTED/QUEUE_FULL reason detail - round 20 - never an
    unbounded park). ``idempotency_key`` (round 20) scopes the
    retry-dedup: identical retries carrying the SAME non-empty
    ``(session_id, idempotency_key)`` collapse into one ticket;
    calls without a key always create separate tickets so a
    multi-call agent can hold N parallel acquires.
    """

    ticket_id: TicketId
    seq: int = Field(ge=0)
    session_id: SessionId
    intent: Intent
    preferred_line_id: LineId | None = None
    enqueued_at: AwareDatetime
    idempotency_key: str | None = Field(
        default=None, min_length=1, max_length=MAX_IDEMPOTENCY_KEY_LEN
    )
    """Bounded when present (round 23, review-12 M2): a key is either
    absent (no dedup - every call is independent) or a non-empty
    string of at most ``MAX_IDEMPOTENCY_KEY_LEN`` chars; an empty or
    overlong key is rejected as INVALID_ARGUMENT at the boundary."""

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, QueueTicket):
            return NotImplemented
        return self.seq < other.seq


# ---------------------------------------------------------------------------
# Acquire result union (call.acquire; NO_FREE_LINE was removed, round 9)
# ---------------------------------------------------------------------------


class AcquireGranted(ContractModel):
    """Immediate grant: line + lease issued at once. ``expires_at``
    (round 20) is the grant TTL - symmetric with the queued path's
    QueueReadyEvent; an agent that never calls ``call.dial`` before
    the deadline loses the lease to
    ``event.call_state_changed{reason: "grant_ttl_expired"}`` and the
    line goes to the next eligible ticket (rule 7)."""

    granted: Literal[True] = True
    line_id: LineId
    lease_id: LeaseId
    expires_at: AwareDatetime


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
    free_lines: tuple[LineId, ...] = ()


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


class PendingClaim(ContractModel):
    """A pending ``call.claim_exclusive`` request awaiting moderator
    approval (round 21, HIGH-7): the admin surface lists these via
    ``LineAdminKernel.list_pending_claims``.

    Bounded (round 22, review-10 MEDIUM-4; rule 7): at most
    ``MAX_PENDING_CLAIMS_PER_AGENT`` per ``requester_agent_id`` and
    ``MAX_PENDING_CLAIMS_TOTAL`` globally - overflow rejects
    ``claim_exclusive`` with RESOURCE_EXHAUSTED. Claims are
    SOCKET-BOUND like queue tickets: ``drop_session(session_id)``
    cancels every pending claim filed by the dead session (the
    approval-notification routing would otherwise target a dead
    socket), and approving a cancelled claim id is
    ``CLAIM_NOT_FOUND``."""

    claim_id: ClaimId
    line_id: LineId
    requester_agent_id: str
    session_id: SessionId
    created_at: AwareDatetime


class ClaimResult(ContractModel):
    """call.claim_exclusive result: claim is a REQUEST - moderator
    approval in the admin surface grants it; never self-granted (§4.2)."""

    claim_id: ClaimId
    status: Literal["pending_approval"] = "pending_approval"


# ---------------------------------------------------------------------------
# ProviderCommand union (round 20 outbox)
# ---------------------------------------------------------------------------
#
# Typed provider-side effects produced inside LineManager mutations and
# stored in the transactional outbox (Rule 4: no network await inside a
# mutation critical section). The adapter drains them via
# ``drain_provider_commands()`` after the mutation commits and performs
# the actual D-Bus/provider call. Every command model carries
# ``RedactedString`` fields where PII occurs so ``safe_dump()``
# redaction - the path taken for metrics/logs - is verified by tests.


class ProviderDialCommand(ContractModel):
    """Initiate an outbound call on a bound provider (wire: line.dial,
    oFono Dial).

    ``media_endpoint``/``media_token`` (round 21, CRITICAL-1/2) are
    the provider-role credentials of the lease's token pair - the
    agent holds a DIFFERENT token for the same lease
    (:class:`MediaCredentials`), so a stolen agent token can never
    claim ``role: "provider"`` in ``media.auth``."""

    command: Literal["dial"] = "dial"
    line_id: LineId
    assignment_id: AssignmentId
    lease_id: LeaseId
    number: RedactedString
    media_endpoint: str
    media_token: MediaToken


class ProviderAnswerCommand(ContractModel):
    """Answer an inbound ring on a bound provider (wire: line.answer,
    oFono Answer).

    ``lease_id`` (round 21, CRITICAL-1) is minted by the kernel at
    accept time: without it the provider could not attribute its own
    ``provider.audio_ready`` / ``provider.call_state{active}``
    reports nor pass ``media.auth``. ``media_endpoint``/
    ``media_token`` are the provider-role credentials - same pair
    semantics as :class:`ProviderDialCommand`."""

    command: Literal["answer"] = "answer"
    line_id: LineId
    assignment_id: AssignmentId
    call_token: CallToken
    lease_id: LeaseId
    media_endpoint: str
    media_token: MediaToken


class ProviderHangupCommand(ContractModel):
    """Tear down a call on a bound provider (oFono Hangup). Reaching a
    provider that already tore the call down ("method doesn't exist"
    racing remote teardown) MUST be treated as idempotent success,
    verified via GetCalls/HangupAll (§5.4).

    Exactly ONE target (round 23, review-12 C1): ``lease_id`` tears
    down an established lease-bound call; ``call_token`` REJECTS a
    still-unanswered inbound ring - no ``lease_id`` exists before
    ``call.accept``/``line.answer``, so a ``RejectFallback`` on
    accept-window expiry could otherwise never reach the wire.
    oFono covers both shapes with the same ``VoiceCall.Hangup`` on
    the ringing/active call object, so one wire method serves both."""

    command: Literal["hangup"] = "hangup"
    line_id: LineId
    assignment_id: AssignmentId
    lease_id: LeaseId | None = None
    call_token: CallToken | None = None

    @model_validator(mode="after")
    def _exactly_one_target(self) -> ProviderHangupCommand:
        """XOR: exactly one of ``lease_id``/``call_token`` - zero
        leaves the provider nothing to correlate, two is ambiguous."""
        if (self.lease_id is None) == (self.call_token is None):
            raise ValueError("exactly one of lease_id / call_token is required")
        return self


class ProviderSendDtmfCommand(ContractModel):
    """In-band DTMF tones on a bound provider - NOT oFono SendTones,
    which is BROKEN on PipeWire Telephony (live probe §5.5). The
    LineProvider owns the whole segment (round 22, review-10
    MEDIUM-1, §6.4): it synthesizes PCM tones and mixes them into
    the per-line uplink endpoint ``phonebridge_uplink_<line_id>``
    it owns, while muting the agent's uplink PCM for the tone
    duration so tones never race the agent's audio stream.

    Coordination with the MediaRouter: the ``0x02`` flush path
    terminates in the MediaRouter's bounded per-lease uplink buffer
    (gateway process) - the LineProvider emits the ``0x02`` frame on
    its provider media socket after a DTMF segment, and the router
    drops every still-queued older uplink frame for the lease, so
    buffered agent speech is neither replayed delayed after the
    tones nor swallowed during the mute.

    `digits` MUST match DTMF_PATTERN."""

    command: Literal["send_dtmf"] = "send_dtmf"
    line_id: LineId
    assignment_id: AssignmentId
    lease_id: LeaseId
    digits: str = Field(pattern=DTMF_PATTERN.pattern)


class ProviderRevokeCommand(ContractModel):
    """Detach a provider binding (revocation or dead-man teardown).
    After issuing, the line drops to DOWN and all its leases follow
    the teardown path."""

    command: Literal["revoke"] = "revoke"
    line_id: LineId
    assignment_id: AssignmentId
    reason: str | None = None
    """Free-form admin note relayed to ``line.revoke{reason?}`` (round-21
    review audit: the wire table documents no closed vocabulary for
    this param, so it does NOT ride CallEndReason)."""


ProviderCommand = Annotated[
    ProviderDialCommand
    | ProviderAnswerCommand
    | ProviderHangupCommand
    | ProviderSendDtmfCommand
    | ProviderRevokeCommand,
    Field(discriminator="command"),
]
"""Discriminated union of provider-side outbox commands (round 20).
`TypeAdapter(ProviderCommand)` round-trips every variant; the adapter
executes them idempotently in drain order."""


PROVIDER_COMMAND_WIRE_NAMES: dict[str, str] = {
    "dial": "line.dial",
    "answer": "line.answer",
    "hangup": "line.hangup",
    "send_dtmf": "line.send_dtmf",
    "revoke": "line.revoke",
}
"""Authoritative discriminator→wire-name map for ProviderCommand
(round 21, CRITICAL-1), the command-side counterpart of
``EVENT_WIRE_NAMES``: every ``command`` literal resolves to the
``line.*`` method the provider surface receives. ``line_id`` is not
a wire parameter - the commands ride the provider's own inbound
connection, which is already bound to exactly one line. Drift
against the ``line.*`` table in docs/api-contract.md is caught by
``test_provider_command_wire_parity``."""


class MediaCredentials(ContractModel):
    """The per-lease media credential pair (round 21, CRITICAL-2).

    One ``lease_id`` gets TWO independent one-time tokens: the agent
    receives ``agent_media_token`` inside DialResult/AcceptResult,
    the provider receives ``provider_media_token`` inside
    ProviderDialCommand/ProviderAnswerCommand. On the wire both
    travel as ``media_token``; the MediaRouter binds each token to
    ``(lease_id, expected_role)``, so a compromised or misused
    ``role: "agent"`` token can never open a socket as
    ``role: "provider"`` and hijack the phone-side stream."""

    media_endpoint: str
    agent_media_token: MediaToken
    provider_media_token: MediaToken

    @model_validator(mode="after")
    def _roles_get_distinct_tokens(self) -> MediaCredentials:
        """The two roles MUST hold different tokens - equal tokens
        would recreate the CRITICAL-2 role-spoofing hole."""
        if self.agent_media_token == self.provider_media_token:
            raise ValueError("agent_media_token and provider_media_token must differ")
        return self


# ---------------------------------------------------------------------------
# Domain events + typed envelope
# ---------------------------------------------------------------------------

PayloadT = TypeVar("PayloadT", bound=ContractModel)


class BroadcastAudience(ContractModel):
    """Every live agent session sees the event. Round 22 (review-10
    HIGH-4) narrows the true-broadcast set to ``line_state_changed``
    only: shared-pool ``incoming_call``/``missed_call`` no longer
    broadcast raw caller_id PII and a one-shot call_token to
    dial_out-only sessions - they route as one ``SessionAudience``
    envelope per live session holding ``Intent.RECEIVE_INBOUND``."""

    scope: Literal["broadcast"] = "broadcast"


class AgentAudience(ContractModel):
    """The event is scoped to a single ``agent_id``: the exclusive
    line owner (``incoming_call``/``missed_call``/``assignment_revoked``
    on owned lines) or the system worker ``system:fallback_worker``
    (``fallback_triggered``). PII or a one-shot ``call_token`` does
    NOT by itself force this scope - but on a shared-pool line
    ``incoming_call``/``missed_call`` reach only the sessions that
    registered ``Intent.RECEIVE_INBOUND`` (one ``SessionAudience``
    envelope each, round 22, review-10 HIGH-4)."""

    scope: Literal["agent"] = "agent"
    agent_id: str


class SessionAudience(ContractModel):
    """Exactly one session sees the event: ``queue_ready``,
    ``queue_updated``, ``audio_ready`` and ``call_state_changed``
    carry session-scoped lease facts that must not leak to other
    sessions (round 21, HIGH-4). One logical event MAY be emitted as
    several envelopes with distinct session ids - e.g. a shared-pool
    ``incoming_call`` fans out as one ``SessionAudience`` envelope
    per live ``Intent.RECEIVE_INBOUND`` session (round 22, review-10
    HIGH-4); the EventBus stays a stateless router and needs no
    session-intent table."""

    scope: Literal["session"] = "session"
    session_id: SessionId


class AdminAudience(ContractModel):
    """Admin-observable events only (round 23, review-12 H6/M6):
    ``idle_alert`` escalations are routed to admin-scope subscribers
    and the configured escalation channels (e.g. Telegram), never to
    ordinary agent sessions - an idle exclusive line is an
    operational signal, not an agent fact. Used ONLY for
    alert-only/administrative events; nothing about a call or a
    lease routes here."""

    scope: Literal["admin"] = "admin"


EventAudience = Annotated[
    BroadcastAudience | AgentAudience | SessionAudience | AdminAudience,
    Field(discriminator="scope"),
]
"""Discriminated union of event routing targets (round 21, HIGH-4).
The audience is stamped by the kernel inside the mutation
transaction - the EventBus is a stateless router over it and never
re-enters kernel tables after the fact (a torn-down lease/session
must not strand its queued event)."""


class EventEnvelope(ContractModel, Generic[PayloadT]):
    """Typed envelope carrying a domain event payload to a subscriber.

    ``name`` is the domain EventName; the adapter maps to ``event.*``
    wire spellings and subscriber-scoped fields (e.g. session_id).
    ``audience`` (round 21) is the mandatory routing target computed
    at emission time - see :data:`EventAudience` for the per-event
    rules."""

    name: EventName
    audience: EventAudience
    emitted_at: AwareDatetime
    payload: PayloadT


class IncomingCallEvent(ContractModel):
    """incoming_call payload: first valid call.accept(call_token) wins
    (wake-and-grant, §4.3/§5.2). ``caller_id`` is redacted PII
    (round-22 wire-name fix: the wire field is ``caller_id``, not
    ``caller``); ``expires_at`` is the absolute accept-window
    deadline in RFC-3339 UTC - the earlier ``expires_in_ms`` relative
    form would skew by queue delay unless recomputed against
    ``emitted_at`` at outbox drain (round 22, review-10 HIGH-1)."""

    line_id: LineId
    call_token: CallToken
    caller_id: RedactedString | None = None
    expires_at: AwareDatetime


class CallStateChangedEvent(ContractModel):
    """call_state_changed payload. ``state`` rides the WIRE vocabulary
    (:data:`WireCallState`, round 22, review-10 HIGH-1): the earlier
    ``phase: CallPhase`` field could hold internal phases that have no
    wire spelling (ACQUIRED/RINGING/TERMINATING), so a naive
    ``model_dump(mode="json")`` could emit a ``state`` value the wire
    contract forbids. The kernel maps CallPhase -> WireCallState via
    :data:`CALL_PHASE_WIRE_STATES` at emission time, so every
    constructible payload is wire-sendable.
    ``answer_confidence`` mirrors CallLease's hedge: how
    sure the bridge is that ACTIVE means a human answered (§5.5).
    ``reason`` rides the CLOSED :class:`CallEndReason` vocabulary
    (round-21 review): it carries BOTH provider-reported teardowns
    (relayed from ``provider.call_state.reason``) and gateway-minted
    ones (``provider_lost``, ``grant_ttl_expired``,
    ``preempted_by_incoming``, ...).
    ``reissued_ticket_id`` (round 23, review-12 M2) is set only on a
    ``preempted_by_incoming`` teardown of an IMMEDIATE grant that was
    taken with ``enqueue=True``: the kernel materializes a NEW ticket
    at the queue head and reports its ``ticket_id`` here, so an agent
    holding several parallel acquires can correlate which preempted
    ``lease_id`` became which ticket (the matching
    ``event.queue_updated{position: 1}`` names the same id). ``None``
    on every other path."""

    lease_id: LeaseId
    line_id: LineId
    state: WireCallState
    reason: CallEndReason | None = None
    answer_confidence: AnswerConfidence | None = None
    reissued_ticket_id: TicketId | None = None


class QueueReadyEvent(ContractModel):
    """queue_ready payload: grant for a queued ticket (wire:
    ``event.line_acquired``). The agent must act (dial/release) within
    ``expires_at`` or the next ticket is served (§3.4 grant TTL).
    ``session_id`` and ``intent`` ride along so the transport adapter
    needs no ticket→owner lookup to address the event (round 17).
    ``ticket_id`` is on the wire (round 22, review-10 HIGH-1): the
    ticket STAYS REFERENCED until its lease ends, so a preempted
    queue-origin grant is re-notified as
    ``event.queue_updated{position: 1}`` on the SAME ``ticket_id``
    (api-contract.md "Grant vs incoming-ring race", round-22
    clarification)."""

    ticket_id: TicketId
    session_id: SessionId
    lease_id: LeaseId
    line_id: LineId
    intent: Intent
    expires_at: AwareDatetime


class QueueUpdatedEvent(ContractModel):
    """queue_updated payload (wire: ``event.queue_updated``): FIFO
    position advance for a pending ticket - emitted on
    cancel_acquire, drop_session, grant-TTL expiry in tick(), or a
    line freeing up (round 17)."""

    session_id: SessionId
    ticket_id: TicketId
    position: int = Field(ge=1)


class AssignmentRevokedEvent(ContractModel):
    """assignment_revoked payload: a durable AGENT-owned
    ``LineAssignment`` ended (owner ``call.release{assignment_id}``
    or admin ``revoke_assignment``). Idle ``idle_revoke_after``
    expiry NEVER emits this event - it is alert-only and surfaces as
    :class:`IdleAlertEvent` instead (round 23, review-12 H6: the
    earlier "idle escalation" wording implied an auto-revoke the
    alert-only rule forbids). Round 22
    (review-10 HIGH-6) makes ``owner_agent_id`` REQUIRED: this event
    is emitted ONLY for an ``EXCLUSIVE`` agent assignment and is
    delivered to that owner's session(s) via ``AgentAudience`` -
    there is always an agent to notify. Revoking a
    ``ProviderBinding`` has no agent audience and emits NO
    ``assignment_revoked``: it produces ``ProviderRevokeCommand`` +
    ``LineStateChangedEvent(new_state=DOWN,
    reason=CallEndReason.ADMIN_REVOKED)`` instead, so an audience
    can never be stamped ``None``. ``reason`` stays free-form
    ``str``: the wire table documents no closed vocabulary for it -
    an admin-supplied revoke note is legal payload (round-21 review
    audit)."""

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
    ``system:fallback_worker`` session only. This event is THE
    fallback delivery channel (round 22, review-10 MEDIUM-2 - the
    earlier "or ``event.incoming_call``" hedge is removed: the worker
    needs the ``policy`` field, which ``incoming_call`` cannot
    carry). ``call_token`` is the internal fallback_call_token
    re-issued after accept-window expiry - single-use, TTL-bound,
    bypasses the exclusive-owner check. ``expires_at`` (round 23,
    review-12 M6) is the absolute deadline the worker has to
    ``call.accept`` the re-issued token - bounded by the line's
    ``fallback_accept_ttl_sec`` knob (~10 s), so a dead or wedged
    FallbackWorker can never hold a ring open indefinitely. The
    deadline is enforced by ``tick(now)`` (round 24, review-13 H2):
    on lapse without a worker accept the kernel burns the
    ``fallback_call_token``, releases the line
    ``RINGING_LOCKED -> FREE`` and emits
    ``MissedCallEvent(reason=CallEndReason.ACCEPT_WINDOW_EXPIRED)`` -
    the no-pickup outcome of last resort."""

    line_id: LineId
    call_token: CallToken
    caller_id: RedactedString | None = None
    policy: FallbackPolicy
    expires_at: AwareDatetime


class LineStateChangedEvent(ContractModel):
    """line_state_changed payload: LineState transition fact.
    ``reason`` rides the CLOSED :class:`CallEndReason` vocabulary
    (round-21 review) - this is the channel that surfaces
    ``external_call`` when a human-initiated call takes the line
    BUSY (design §9)."""

    line_id: LineId
    old_state: LineState
    new_state: LineState
    reason: CallEndReason | None = None


class MissedCallEvent(ContractModel):
    """missed_call payload: an inbound ring ended unanswered
    (accept-window expiry, caller hangup, or reject-policy outcome).
    ``reason`` is REQUIRED on the wire (api-contract.md event table)
    and rides the CLOSED :class:`CallEndReason` vocabulary
    (round-21 review)."""

    line_id: LineId
    caller_id: RedactedString | None = None
    reason: CallEndReason
    voicemail_ref: str | None = None


class IdleAlertEvent(ContractModel):
    """idle_alert payload (round 23, review-12 H6/M6): an EXCLUSIVE
    ``LineAssignment`` has been idle past ``idle_revoke_after``.

    STRICTLY alert-only: the event is an escalation signal routed to
    admin-scope subscribers and configured escalation channels via
    :class:`AdminAudience` - it NEVER implies a revoke (an exclusive
    line is never silently reassigned by the machine, design §12
    resolved question). The earlier "idle escalation" wording on
    ``assignment_revoked`` was removed for the same reason:
    ``AssignmentRevokedEvent`` fires ONLY on explicit revoke paths
    (owner release, admin revoke). Emitted at most once per
    continuous idle period - ``tick(now)`` records the alert so a
    still-idle line does not re-alert on every tick until activity
    resets the window.

    Activity rule (round 24, review-13 H4): a line whose
    ``owner_agent_id`` holds at least one OPEN session is ACTIVE -
    the idle clock never runs against it, so a secretary agent
    waiting days on a live socket for inbound rings cannot trigger
    a false alert. ``last_seen_at`` refreshes on every line
    operation and when ``drop_session`` closes the owner's LAST
    session - that is the moment the idle window starts."""

    assignment_id: AssignmentId
    line_id: LineId
    owner_agent_id: str
    idle_since: AwareDatetime


# ---------------------------------------------------------------------------
# Provider ingress payloads (narrowed surface: distilled FACTS, not
# raw WSS register/ping transport)
# ---------------------------------------------------------------------------


class ProviderActiveCall(ContractModel):
    """One live call as the provider sees it on the D-Bus (round 21).

    Element of ``ProviderRegistration.state_snapshot`` - the
    orphan-grace reconciliation input. ``lease_id`` is ``None`` for a
    call the kernel never issued, e.g. a human-initiated call on the
    physical phone (design §9 external-call rule)."""

    lease_id: LeaseId | None = None
    state: ProviderCallState
    caller_id: RedactedString | None = None


class ProviderRegistration(ContractModel):
    """Distilled registration fact (provider.register at the boundary).

    ``token`` is the per-line shared secret - RedactedString so a
    registration record can never leak it to logs (rule 10). The
    kernel MUST NOT retain the raw token in its in-memory state once
    the binding exists (review-13 security audit): the shared secret
    authenticates the register handshake itself, not the session.
    ``caps`` is non-empty and telecom-only - see
    :func:`_provider_caps_check` (round 24, review-13 M3)."""

    line_id: LineId
    token: RedactedString
    caps: tuple[Intent, ...]
    profile: str | None = None
    meta: dict[str, str] = Field(default_factory=dict)
    """Opaque provider metadata (wire: ``provider.register{meta?}``
    object). Bounded (round 22, review-10 MEDIUM-5; CODING_HAZARDS
    rule 7 on an external JSON-RPC ingress field): at most
    ``MAX_PROVIDER_META_ENTRIES`` keys, each key non-empty and at
    most ``MAX_PROVIDER_META_KEY_LEN`` chars, each value at most
    ``MAX_PROVIDER_META_VALUE_LEN`` chars - a violation is a
    ValidationError (INVALID_ARGUMENT at the adapter). Kept as
    ``dict`` for wire compat: ``frozen=True`` covers
    field reassignment only, so the mapping itself stays mutable -
    treat contents as immutable by convention (round-21 review; a
    MappingProxyType wrapper would add a custom validator+serializer
    for no wire-shape gain)."""
    state_snapshot: tuple[ProviderActiveCall, ...] | None = None
    """The provider's view of live calls at registration time
    (round 21, CRITICAL-3): carried on ``provider.reregister``
    inside the 10 s orphan-grace window so the kernel can verify a
    surviving call still exists on the D-Bus before re-binding its
    lease (design §5.4). ``None`` on a first-time register."""

    @field_validator("caps")
    @classmethod
    def _caps_are_telecom_only(cls, caps: tuple[Intent, ...]) -> tuple[Intent, ...]:
        """A provider declares TELECOM capabilities only (round 24,
        review-13 M3)."""
        return _provider_caps_check(caps)

    @field_validator("meta")
    @classmethod
    def _meta_bounds(cls, meta: dict[str, str]) -> dict[str, str]:
        """Bound the external ``meta`` map (round 22, review-10
        MEDIUM-5): an unbounded provider-controlled dict on a
        wire-facing model is a memory/log hazard."""
        if len(meta) > MAX_PROVIDER_META_ENTRIES:
            raise ValueError(f"meta has {len(meta)} entries, max {MAX_PROVIDER_META_ENTRIES}")
        for key, value in meta.items():
            if not key or len(key) > MAX_PROVIDER_META_KEY_LEN:
                raise ValueError(f"meta key must be 1..{MAX_PROVIDER_META_KEY_LEN} chars")
            if len(value) > MAX_PROVIDER_META_VALUE_LEN:
                raise ValueError(f"meta[{key!r}] value exceeds {MAX_PROVIDER_META_VALUE_LEN} chars")
        return meta


class ProviderLiveness(ContractModel):
    """Provider liveness fact: reachable / ws_disconnected /
    unreachable + timestamp.

    Distilled from the ``provider.ping`` heartbeat path (missed pings
    -> DEGRADED -> DOWN, WS drop -> ws_disconnected/orphan-grace);
    the ping transport itself stays in the adapter."""

    line_id: LineId
    assignment_id: AssignmentId | None = None
    state: LivenessState
    checked_at: AwareDatetime
    state_snapshot: tuple[ProviderActiveCall, ...] | None = None
    """Optional ``provider.ping{state_snapshot?}`` carrier (round-21
    review): same element shape as
    :attr:`ProviderRegistration.state_snapshot` - the calls the
    provider still sees on the D-Bus - so a ping can feed ongoing
    reconciliation the way reregister does inside the orphan-grace
    window. ``None`` when the ping carries no snapshot."""


class ProviderCallStateUpdate(ContractModel):
    """provider.call_state ingress fact (bounded ProviderCallState).

    ``call_token`` identifies the inbound ring; ``lease_id`` (round 14)
    identifies an established call - without it the gateway cannot
    attribute an update to a lease. ``caller_id`` (round 22,
    review-10 MEDIUM-3) carries the inbound caller's E.164 on
    ``ringing`` reports - the wire ``provider.call_state`` param was
    previously missing, so a provider could not report WHO rings.

    Routing rule for adapters (round 22): a ``state="ringing"``
    report is fed to :meth:`LineManager.ingest_ring`, NOT to this
    ingress - the ring path mints the ``call_token`` (see
    :class:`IncomingRingIngress`). This model carries the
    post-ring/established-call updates correlated by
    ``call_token``/``lease_id``.

    Correlating an early caller hangup (round 22): the provider
    learns the minted ``call_token`` from the
    ``provider.call_state{state:"ringing"}`` RESPONSE
    (``{ok, call_token}``) and echoes it on every later update for
    that ring, including a pre-answer ``disconnected``. As a second
    correlation leg - covering rings the gateway discovered via a
    ``state_snapshot`` where the provider never received the token -
    a ``disconnected`` update carrying NEITHER ``call_token`` nor
    ``lease_id`` on a line in ``RINGING_LOCKED`` resolves to that
    line's one open ring (at most one ring exists per line).

    External calls (round 21, HIGH-6): an update carrying NEITHER
    ``call_token`` nor ``lease_id`` on a line NOT in
    ``RINGING_LOCKED`` reports a human-initiated call on the
    physical phone (dialed or answered on the handset itself).
    The kernel marks the line BUSY with reason ``"external_call"`` -
    no agent lease is minted, no PipeWire bridge is attached (audio
    stays on the phone) and NO Hangup is ever sent; the state clears
    when the provider reports DISCONNECTED (design §9).

    ``reason`` is the CLOSED provider vocabulary
    (:class:`ProviderDisconnectReason`, round 21); gateway-minted
    reasons such as ``provider_lost`` never arrive on this ingress."""

    line_id: LineId
    assignment_id: AssignmentId
    call_token: CallToken | None = None
    lease_id: LeaseId | None = None
    caller_id: RedactedString | None = None
    state: ProviderCallState
    reason: ProviderDisconnectReason | None = None
    observed_at: AwareDatetime


class IncomingRingIngress(ContractModel):
    """Ring ingress fact: a physical call is ringing on the line.

    Distilled from the provider's ``provider.call_state{state:
    "ringing", caller_id?}`` report (round 22, review-10 MEDIUM-3).
    The ``call_token`` is NOT an input: the taxonomy in
    api-contract.md reserves ``call_token`` minting to
    ``LineManager``, so the kernel mints it inside the ring
    mutation and hands it back to the caller inside the returned
    :class:`IncomingCallEvent` - the adapter MUST relay it to the
    provider as the ``provider.call_state`` response
    ``{ok, call_token}`` so the provider can correlate a
    pre-answer caller hangup. Re-ingesting a ring on an already
    ``RINGING_LOCKED`` line is idempotent and returns the existing
    event (same ``call_token``) - e.g. a ring re-discovered through
    the ``GetCalls()``/``state_snapshot`` reconciliation (design
    §9)."""

    line_id: LineId
    assignment_id: AssignmentId
    caller_id: RedactedString | None = None
    observed_at: AwareDatetime


class ProviderCapabilityUpdate(ContractModel):
    """provider.capability_update ingress fact (round 22, review-10
    CRITICAL-1): a caps/profile refresh on an ACTIVE
    ``ProviderBinding`` - wire params ``{line_id, assignment_id,
    caps, profile?} -> {ok}``.

    No ``token`` field: unlike ``provider.register``/
    ``provider.reregister`` this RPC carries no shared secret - the
    already-established binding id authenticates the peer on its own
    connection. That is exactly why the kernel needs a dedicated
    method: the M2 adapter cannot abuse ``register_provider`` (which
    requires the token ``capability_update`` does not carry) and
    cannot mutate the frozen ``ProviderBinding`` DTO directly."""

    line_id: LineId
    assignment_id: AssignmentId
    caps: tuple[Intent, ...]
    """Replacement capabilities - same non-empty telecom-only
    invariant as ``ProviderRegistration.caps`` (round 24, review-13
    M3)."""
    profile: str | None = None

    @field_validator("caps")
    @classmethod
    def _caps_are_telecom_only(cls, caps: tuple[Intent, ...]) -> tuple[Intent, ...]:
        return _provider_caps_check(caps)


class FallbackDecision(ContractModel):
    """Result of the fallback policy decision point (§5.3).

    LineManager calls decide_fallback() when the accept window
    expires; the returned policy is then executed - core policies
    inside LineManager, worker policies by the internal
    FallbackWorker via the standard agent API (ADR-01). A line with
    ZERO live subscribed/owner sessions still waits the full
    ``accept_window_sec`` first (round 23, review-12 H6): wake-and-
    grant hooks (webhook/exec) exist precisely to wake a sleeping
    agent inside that window, so "no live session right now" is NOT
    an immediate-fallback trigger.

    The eligibility predicate for ``answer_and_announce``
    (round 24, review-13 M4): the policy is eligible when the
    line's ``assignment.mode == AssignmentMode.EXCLUSIVE`` OR at
    least one live session holds ``Intent.RECEIVE_INBOUND`` on the
    line - the ``system:fallback_worker`` session is EXCLUDED from
    that count; when neither holds the decision degrades to
    ``NoPickupFallback``."""

    line_id: LineId
    call_token: CallToken
    policy: FallbackPolicy
    reason: str | None = None
    """Free-form decision context for logs/telemetry (round-21 review
    audit: no closed vocabulary is documented for this internal field,
    so it does NOT ride CallEndReason)."""
    fallback_call_token: CallToken | None = None
    """Re-issued ring token for WORKER policies (round 14): bound to
    ``system:fallback_worker``, single-use, TTL-bound, bypasses the
    exclusive-owner check so FallbackWorker can call accept() after
    the original call_token went stale. None for CORE policies.
    Delivered to the worker inside ``event.fallback_triggered``
    ONLY - the sole fallback delivery channel (round 22, review-10
    MEDIUM-2)."""


# ---------------------------------------------------------------------------
# Injected pure ports (round 21, MEDIUM-9)
#
# ``InMemoryLineManager`` (M1) takes these through its constructor so
# the kernel stays deterministic in tests - a scripted ``Clock``
# instead of ``datetime.now``/``sleep``, a canned ``MediaAllocator``
# instead of a socket allocator. The ports are one-way inputs and
# hold NO kernel reference.
# ---------------------------------------------------------------------------


class Clock(Protocol):
    """Monotonic aware-UTC time source injected into the kernel.

    The kernel calls it inside mutations; every returned datetime
    MUST satisfy ``ensure_aware_utc`` and MUST NOT move backwards
    relative to the previous call - tick-order bookkeeping
    (orphan deadlines, grant TTLs) depends on monotonicity."""

    def __call__(self) -> datetime: ...


class MediaAllocator(Protocol):
    """Owns the per-lease media port lifecycle (round 21, CRITICAL-2;
    lifecycle corrected round 23, review-12 C3).

    ``allocate`` issues the endpoint plus the two independent
    one-time tokens of :class:`MediaCredentials`. The kernel calls
    ``allocate`` strictly inside ``dial()`` and ``accept()`` - NOT
    at grant time: the ``media_token`` pair TTL (~5 s) is comparable
    to ``grant_ttl_sec``, so allocating at ``AcquireGranted`` /
    ``queue_ready`` would let tokens expire before ``call.dial``
    fires, and a preempted or TTL-expired grant would strand live
    tokens in the router. The kernel splits the pair - agent token
    into the agent result, provider token into the outbox command.

    ``revoke`` closes the port: no socket may outlive its
    ``lease_id``, so the kernel calls it on EVERY
    ``CallLease -> ENDED`` transition (hangup, release,
    ``disconnected``, ``drop_session`` teardown, dongle/provider
    loss, ``tick()`` expiry, assignment revoke) - the outbox events
    alone cannot close the MediaRouter's sockets because the router
    sees no event stream. Idempotent and synchronous: revoking an
    already-closed or never-allocated ``lease_id`` is a no-op, and
    the call performs no network await (rule 4).

    (The port-like name ``MediaPort`` was considered in round 23;
    ``MediaAllocator`` is kept to avoid a public-surface rename -
    the lifecycle contract above is what M3 implements.)"""

    def allocate(self, lease_id: LeaseId, line_id: LineId) -> MediaCredentials: ...

    def revoke(self, lease_id: LeaseId) -> None:
        """Close every media socket/credential bound to ``lease_id``.
        Called on every ``CallLease -> ENDED``; idempotent - a
        second revoke (or a lease that never allocated) is a no-op."""
        ...


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
        agent_id. An EMPTY ``intents`` set is rejected with
        INVALID_ARGUMENT, and ``Intent.EXCLUSIVE_LINE`` is
        INVALID_INTENT (round 21): durable exclusivity is requested
        ONLY through ``claim_exclusive`` + moderator approval
        (round 10d/14) - a self-declared intent can never grant it.
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
        idempotency_key: str | None = None,
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
        Retry/dedup semantics (round 20): retry dedup is scoped to the
        SAME non-empty ``(session_id, idempotency_key)`` pair - an
        identical retry while queued returns the SAME ticket. Calls
        WITHOUT a key always create independent tickets/grants
        (subject to the per-session limit, RESOURCE_EXHAUSTED /
        SESSION_LIMIT_EXCEEDED on overflow), so a multi-call agent
        can run N parallel acquires.

        ``system:fallback_worker`` (round 22, review-10 MEDIUM-2): a
        session owned by that agent id BYPASSES
        LINE_EXCLUSIVE_NOT_OWNER on this path - the worker executes
        admin-configured ``forward_via_line`` policy whose target may
        legitimately be an EXCLUSIVE line owned by a human/secretary;
        the admin who configured the policy accepted that override.
        It receives no other privileges (still intent-checked,
        still lease-scoped).

        Preemption of an undialed grant (round 22, review-10 HIGH-2):
        when a physical ring preempts a granted-but-undialed lease,
        the lease ends with ``preempted_by_incoming``. If the grant
        came from a queue ticket, that SAME ``ticket_id`` is reissued
        at the head (``seq`` retained) and the agent learns via
        ``event.queue_updated{position: 1}`` - a granted ticket stays
        referenced until its lease ends. If an IMMEDIATE grant was
        taken with ``enqueue=True``, a NEW ticket materializes at
        the head and the session learns its ``ticket_id`` inside the
        ``event.queue_updated`` notification (the caller's enqueue
        wish is honored). ``enqueue=False`` grants are torn down
        without reissue.
        """
        ...

    @abstractmethod
    async def cancel_acquire(self, session_id: SessionId, ticket_id: TicketId) -> None:
        """call.cancel_acquire: withdraw a queued ticket. Idempotent:
        cancelling a dead/served ticket is a no-op, not an error."""
        ...

    @abstractmethod
    async def release(self, session_id: SessionId, lease_id: LeaseId) -> None:
        """call.release{lease_id}: end the EPHEMERAL CallLease (round 22,
        review-10 HIGH-3 lifecycle fix).

        The lease NEVER outlives the call and the call NEVER outlives
        the lease: ``release`` on an ``ACQUIRED`` (granted-but-undialed)
        lease simply frees the line; ``release`` during an in-flight
        call is hangup-intent - the kernel enqueues a
        ``ProviderHangupCommand`` and ends the lease, so a call can
        never stay up with no owning lease (the earlier "an in-flight
        call finishes" wording of §4.2 applied to the DURABLE
        assignment, not to a lease). ``hangup``/provider
        ``disconnected`` already end the lease implicitly, so a
        trailing ``release`` after them is an idempotent no-op.
        OWNER_MISMATCH on a foreign ``(session_id, lease_id)`` pair.

        The wire ``call.release`` carries exactly ONE target
        (``lease_id`` XOR ``assignment_id``, round 22); a request
        carrying ZERO or BOTH targets is ``INVALID_ARGUMENT`` at the
        adapter boundary (round 24, review-13 L1) and never reaches
        this method or :meth:`release_assignment`.

        The DURABLE ``LineAssignment`` has its own voluntary-release
        path - :meth:`release_assignment`."""
        ...

    @abstractmethod
    async def release_assignment(self, session_id: SessionId, assignment_id: AssignmentId) -> None:
        """call.release{assignment_id}: voluntary release of a durable
        EXCLUSIVE ``LineAssignment`` by its owner (round 22, review-10
        HIGH-3): the line reverts to the claimable shared pool and the
        owner receives ``event.assignment_revoked``. This is the
        "release" in the §4.2 LineAssignment "Ends by" row - and
        THERE an in-flight call finishes (the durable binding ends,
        the ephemeral lease completes its call and cannot renew).

        The wire ``call.release`` carries exactly ONE target
        (``lease_id`` XOR ``assignment_id``); the adapter dispatches
        to :meth:`release` or this method, and a request carrying
        ZERO or BOTH targets is ``INVALID_ARGUMENT`` (round 24,
        review-13 L1). OWNER_MISMATCH when the session's
        ``agent_id`` is not the assignment owner;
        ``PhonebridgeError(ErrorCode.STALE_TOKEN)`` on an id unknown
        to the LineAssignment object space - the durable-binding
        counterpart of a consumed ``call_token`` (round 24,
        review-13 L1). Idempotent on an already-released binding:
        the superseded id stays resolvable via the bounded
        ``_closed_assignments`` ring after the rotation documented
        on :class:`LineAssignment` (round 24, review-13 M2) - a
        retried release by the previous owner is a no-op, not an
        ``OWNER_MISMATCH``."""
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
        one lease is rejected with INVALID_STATE (round 20 - the
        INVALID_CALL_STATE case no longer rides STALE_TOKEN).

        Provider-side compensation (Saga, round 23, review-12 H3):
        ``dial()`` itself performs no network await - it transitions
        the lease to DIALING, enqueues ``ProviderDialCommand`` and
        returns ``DialResult`` immediately (the agent answer does NOT
        wait for the provider). The adapter then sends ``line.dial``
        and MUST await the provider's ``{accepted}`` reply with a
        bounded deadline (~2 s). On ``{accepted: false}``, a timeout,
        or a transport error the adapter MUST compensate by calling
        :meth:`ingest_call_state` with ``state=DISCONNECTED,
        reason=NETWORK_ERROR`` so the lease ends atomically and the
        line frees - a lease can never wedge in DIALING forever."""
        ...

    @abstractmethod
    async def accept(self, session_id: SessionId, call_token: CallToken) -> AcceptResult:
        """call.accept: ATOMIC lease issue + ring answer + media attach
        (§5.2 step 4). First valid accept wins; a second accept on the
        consumed token gets STALE_TOKEN; owner-scoped lines reject
        non-owners with OWNER_MISMATCH.

        Inbound-intent gate (round 22, review-10 HIGH-4): the
        accepting session MUST have registered
        ``Intent.RECEIVE_INBOUND`` - a ``dial_out``-only agent can
        neither see the ring nor hijack it on a shared line
        (INVALID_INTENT otherwise). The ``system:fallback_worker``
        session is exempt: it accepts with the single-use
        ``fallback_call_token`` minted for it at accept-window
        expiry (itself bounded by ``fallback_accept_ttl_sec``,
        round 23).

        Provider-side compensation (Saga, round 23, review-12 H3):
        same rule as :meth:`dial` - ``accept()`` mints the lease and
        enqueues ``ProviderAnswerCommand`` without a network await;
        the adapter awaits the provider's ``{accepted}`` on
        ``line.answer`` with a ~2 s deadline and on
        ``{accepted: false}`` / timeout / transport error MUST feed
        :meth:`ingest_call_state` ``(DISCONNECTED, NETWORK_ERROR)``
        to close the lease and free the line."""
        ...

    @abstractmethod
    async def hangup(self, session_id: SessionId, lease_id: LeaseId) -> None:
        """call.hangup: end the call AND the ephemeral lease atomically
        (auto-release, round 22, review-10 HIGH-3): no separate
        ``call.release`` is needed afterwards - the lease ends with
        the call, the queue advances, and a trailing ``release`` is
        an idempotent no-op (the earlier §5.1 "hangup + release"
        phrasing is superseded: a line can never stay BUSY because an
        agent forgot to release). A provider ``disconnected`` report
        ends the lease the same way. Dead-man switch / orphan grace
        follow §5.4. OWNER_MISMATCH on a foreign
        ``(session_id, lease_id)`` pair."""
        ...

    @abstractmethod
    async def send_dtmf(self, session_id: SessionId, lease_id: LeaseId, digits: str) -> None:
        """call.send_dtmf: IVR navigation tones; mandatory from v1.
        ``digits`` MUST match ``DTMF_PATTERN`` (<= MAX_DTMF_DIGITS
        chars) - violation is INVALID_ARGUMENT with reason
        INVALID_DTMF (round 20). INVALID_STATE when the lease has no
        ACTIVE call - tones are synthesized in-band by the
        LineProvider (see :class:`ProviderSendDtmfCommand`), so they
        only make sense mid-call."""
        ...

    @abstractmethod
    async def claim_exclusive(self, session_id: SessionId, line_id: LineId) -> ClaimResult:
        """call.claim_exclusive: file a CLAIM REQUEST. Returns
        pending_approval - never self-granted; a moderator approves in
        the admin surface (§4.2 round 10d). Conflict ->
        LINE_EXCLUSIVE_NOT_OWNER / LINE_NOT_CLAIMABLE reason.
        Bounded (round 22, review-10 MEDIUM-4): at most
        ``MAX_PENDING_CLAIMS_PER_AGENT`` pending claims per agent and
        ``MAX_PENDING_CLAIMS_TOTAL`` globally - overflow is
        RESOURCE_EXHAUSTED. Pending claims are socket-bound:
        ``drop_session`` cancels them."""
        ...

    @abstractmethod
    async def get_lines(
        self, session_id: SessionId, intent: Intent | None = None
    ) -> list[LineSummary]:
        """call.get_lines: pool view filtered by intent. Always bounded
        (rule 7): the line set is finite config, not a stream.

        ``intent=Intent.EXCLUSIVE_LINE`` filter semantics (round 24,
        review-13 M3): returns the lines whose
        ``assignment.claimable is True and assignment.mode ==
        AssignmentMode.SHARED_POOL`` - exclusivity AVAILABILITY is a
        ``LineAssignment`` property, never a provider capability;
        ``ProviderBinding.caps`` carries only the telecom intents
        ``dial_out``/``receive_inbound``."""
        ...

    # --- provider ingress (narrowed: facts and decisions, not transport) ---

    @abstractmethod
    async def register_provider(self, registration: ProviderRegistration) -> ProviderBinding:
        """Provider attach fact -> ProviderBinding (round 20; the
        ≤0.0.12 signature returned the agent-side LineAssignment,
        conflating two object spaces).

        This is the DISTILLED domain fact of a provider.register, not
        the WSS transport method - credential checks and socket
        lifecycle stay in the adapter. Reregister during the grace
        window is idempotent and preserves in-flight leases (wire:
        provider.reregister, 10 s orphan grace, §5.4). On the first
        registration of a new ``line_id`` the kernel ALSO initializes
        the line's default ``LineAssignment`` (SHARED_POOL,
        owner_agent_id=None) if none exists - the binding itself is
        the returned record.

        The returned :class:`ProviderBinding` stamps
        ``heartbeat_interval_ms`` from the line's EFFECTIVE
        ``LineTimeouts.heartbeat_interval_sec`` (round 24,
        review-13 C2) so the adapter can answer
        ``provider.register``/``provider.reregister`` with the wire
        ``heartbeat_interval_ms`` - a ``configure_line`` patch to the
        interval applies on the NEXT reregister, never
        retroactively to the stamped session value.
        """
        ...

    @abstractmethod
    async def update_provider_capabilities(
        self, update: ProviderCapabilityUpdate
    ) -> ProviderBinding:
        """provider.capability_update ingress (round 22, review-10
        CRITICAL-1): replace ``caps``/``profile`` on the ACTIVE
        ``ProviderBinding`` identified by ``(line_id,
        assignment_id)`` and return the updated binding record.

        No shared-secret ``token`` rides this RPC - the binding id
        authenticates the already-registered peer - so this path can
        NEVER be folded into ``register_provider``. Unknown/stale
        binding id -> PhonebridgeError; an update identical to the
        current caps/profile is an idempotent no-op; a capability
        shrink does NOT tear down live leases that relied on the old
        caps (their facts were granted already) but no new grants
        are made against removed intents."""
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
        wake-and-grant routing (§4.3). The kernel MINTS the
        ``call_token`` here (round 22, review-10 MEDIUM-3 - the
        taxonomy reserves it to LineManager) and returns it inside
        the ``IncomingCallEvent``; the adapter MUST relay that token
        to the provider as the ``provider.call_state`` response
        ``{ok, call_token}`` so a pre-answer caller hangup can be
        correlated. Audience (round 22, HIGH-4): on an EXCLUSIVE
        line one ``AgentAudience`` envelope to the owner; on a
        SHARED_POOL line one ``SessionAudience`` envelope per live
        session holding ``Intent.RECEIVE_INBOUND`` - never a
        broadcast to dial_out-only sessions.

        Returns the incoming_call event - the same instance is ALSO
        appended to the outbox inside the mutation transaction; the
        EventBus publishes exclusively from ``drain_events()``, the
        return value exists for caller convenience (round 20).
        Atomic w.r.t. concurrent acquires on the same line
        (single-event-loop invariant). Idempotent: re-ingesting a
        ring on an already RINGING_LOCKED line returns the existing
        event with the SAME ``call_token``.

        Line-state contract (round 24, review-13 H3 - the glare
        matrix is defined for EVERY state, including the classic
        race where the agent's ``call.dial`` is still in flight to
        the provider while the cellular network lands a genuinely
        first inbound call on the handset - Call Waiting filtering
        cannot see it):
        - ``FREE`` -> transition to ``RINGING_LOCKED``, mint the
          ``call_token``;
        - ``BUSY`` holding a lease still in ``CallPhase.ACQUIRED``
          (granted-but-undialed) WITH ``Intent.RECEIVE_INBOUND`` in
          the binding's ``caps`` -> preempt the grant
          (``preempted_by_incoming``) and lock the ring;
        - ``RINGING_LOCKED`` -> idempotent return of the open ring's
          event;
        - ``BUSY`` in every other phase (the glare case in
          ``DIALING``/``ALERTING``/``ACTIVE`` or an ``external_call``
          hold), ``DOWN``, or a binding whose ``caps`` lack
          ``RECEIVE_INBOUND`` -> ``PhonebridgeError`` with
          ``ErrorCode.INVALID_STATE``. The LineProvider maps that
          error (or a ``{ok: false}`` wire result) to a
          ``VoiceCall.Hangup`` on the inbound call object (and
          counts ``glare_inbound_rejected``) - it MUST NOT leave the
          glare call ringing and MUST NOT report the line
          ``RINGING_LOCKED``."""
        ...

    @abstractmethod
    async def ingest_call_state(self, update: ProviderCallStateUpdate) -> None:
        """Call-state ingress: bounded ProviderCallState update mapped
        into lease phase transitions. Stale/out-of-order updates are
        deduped by observed_at; DISCONNECTED ends the lease - an
        ephemeral CallLease NEVER outlives its call (auto-release,
        round 22, review-10 HIGH-3, §5.4)."""
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
        expiry, resolve the line's fallback_policy into a concrete
        decision. Policy selection is domain concern; EXECUTION is
        internal (core ops inline, worker policies via FallbackWorker
        over the agent API - ADR-01). The window ALWAYS elapses first:
        even with zero live subscribed/owner sessions the kernel waits
        the full ``accept_window_sec`` so configured wake-and-grant
        hooks can wake a sleeping agent (round 23, review-12 H6) -
        "no live session" is never an immediate-fallback trigger.

        Timer ownership (round 24, review-13 H2): this method NEVER
        sleeps - ``tick(now)`` is the ONLY timer driver in the kernel
        (rules 2/4 forbid a parked await inside the domain). ``tick``
        runs the fallback decision internally once
        ``now >= ring.expires_at``; a DIRECT ``decide_fallback``
        call on a ring whose ``expires_at`` has NOT yet elapsed
        raises ``PhonebridgeError(ErrorCode.INVALID_STATE)`` - the
        accept window cannot be short-circuited from outside."""
        ...

    # --- agent session lifecycle ---

    @abstractmethod
    async def drop_session(self, session_id: SessionId) -> None:
        """Agent socket teardown fact (round 15): the transport layer
        reports a disconnected session so the kernel can atomically
        (a) kill every QueueTicket owned by the session - tickets are
        socket-bound (§3.4) - (b) run the dead-man switch on every
        CallLease owned BY THAT SESSION - ``CallLease.session_id``
        pins the lease to one session, so a disconnected session
        never kills sibling sessions' calls under the same
        ``agent_id`` (round 22, review-10 CRITICAL-2) - and
        (c) cancel every :class:`PendingClaim` filed by the session -
        pending claims are socket-bound like tickets, an approval
        could never be delivered to a dead socket anyway (round 22,
        review-10 MEDIUM-4).
        Idempotent: a repeated drop on an already-gone session is a
        no-op. This is the ONLY way sessions end; there is no
        per-ticket/per-lease sweep API."""
        ...

    @abstractmethod
    async def tick(self, now: AwareDatetime) -> None:
        """Single generic lifecycle hook for time-driven housekeeping:
        accept-window expiry, queue grant-TTL advancement, heartbeat
        DEGRADED->DOWN escalation.

        Tick OWNS both ring timers (round 24, review-13 H2): at
        ``now >= ring.expires_at`` (``accept_window_sec`` elapsed) it
        performs the :meth:`decide_fallback` policy decision
        internally - including minting the ``fallback_call_token``
        and emitting ``event.fallback_triggered`` for worker
        policies; a second deadline, ``fallback_accept_ttl_sec``
        (surfaced as ``FallbackTriggeredEvent.expires_at``), bounds
        the worker's ``call.accept``. When that second timer lapses
        without an accept, ``tick`` burns the
        ``fallback_call_token``, releases the line
        ``RINGING_LOCKED -> FREE`` and emits
        ``MissedCallEvent(reason=CallEndReason.ACCEPT_WINDOW_EXPIRED)``
        - a dead or wedged FallbackWorker can never hold a ring
        open. ``tick`` also drives idle bookkeeping: an EXCLUSIVE
        line whose ``owner_agent_id`` holds at least one open
        session is never idle; ``last_seen_at`` is stamped by
        ``drop_session`` and by every line operation (round 24,
        review-13 H4).

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
        MUST begin with ``now = ensure_aware_utc(now)`` (round 20 -
        the public helper applies the same reject-naive/normalize-UTC
        rule at runtime).
        """
        ...

    # --- transactional outbox (round 20; CODING_HAZARDS rule 4) ---
    #
    # Every mutating method above appends domain events and provider
    # commands to a bounded, per-manager in-memory outbox inside its
    # mutation transaction - never performing a network await inside
    # the critical section. The runtime/adapter calls the two drains
    # AFTER the mutation commits: envelopes go to the EventBus, typed
    # ProviderCommands to the provider adapter. Ordering is
    # deterministic (mutation order); the bounded capacity turns an
    # overflow into RESOURCE_EXHAUSTED rather than unbounded growth.
    #
    # Overflow model (round 23, review-12 H2 / ADR-C): a two-level
    # limit replaces a single hard cap, because an in-memory kernel
    # has no automatic rollback - a RESOURCE_EXHAUSTED raised after
    # state dicts were already mutated would leave half-applied
    # state, and a teardown event refused at a full outbox would
    # wedge a line BUSY forever.
    #
    # * SOFT_CAP (pre-flight): resource-CREATING operations
    #   (``acquire``, ``dial``, ``accept``, ``claim_exclusive``)
    #   check remaining outbox capacity BEFORE any state mutation;
    #   an outbox at/above the soft cap rejects the call with
    #   RESOURCE_EXHAUSTED while the kernel state is still untouched.
    #   The rejected agent simply retries.
    # * Teardown paths NEVER raise RESOURCE_EXHAUSTED:
    #   ``hangup``, ``release``, ``drop_session``,
    #   ``ingest_call_state`` teardowns, provider-loss and ``tick()``
    #   expiries append their events/commands unconditionally
    #   (reserve-tail capacity - teardown volume is bounded by the
    #   number of live leases, which is itself bounded).
    # * Drain contract: the adapter MUST call ``drain_events()`` and
    #   ``drain_provider_commands()`` after EVERY kernel call before
    #   yielding to the event loop, so the outbox can never grow
    #   across mutations and the soft cap is observed on a nearly
    #   empty buffer in practice.

    @abstractmethod
    def drain_events(self) -> list[EventEnvelope[Any]]:
        """Atomically drain pending domain-event envelopes in emission
        order. The EventBus MUST publish ONLY from this queue - a
        mutation never emits to subscribers directly. MUST be called
        by the adapter after EVERY kernel call (round 23, review-12
        H2): the outbox is not allowed to accumulate across
        mutations."""
        ...

    @abstractmethod
    def drain_provider_commands(self) -> list[ProviderCommand]:
        """Atomically drain pending provider commands in emission
        order. The provider adapter executes them idempotently
        (already-gone teardowns are success, §5.4) and MUST drain
        after EVERY kernel call (round 23, review-12 H2)."""
        ...


class TimeoutConfig(ContractModel):
    """Runtime-configurable kernel timing knobs (round 22, review-10
    HIGH-5; design §10: "ALL timeouts and windows are
    admin-configurable without a daemon restart").

    Passed to :meth:`LineAdminKernel.configure_line` as a PATCH:
    every member is optional, ``None`` means "leave unchanged".
    Values are scoped per line - the line's provider binding and
    queue grants share them."""

    grant_ttl_sec: float | None = Field(default=None, gt=0)
    """Deadline for an agent to consume a grant
    (``AcquireGranted.expires_at``/``QueueReadyEvent.expires_at``)
    before the lease ends ``grant_ttl_expired`` (~5 s illustrative
    default, admin-configured)."""
    orphan_grace_sec: float | None = Field(default=None, gt=0)
    """WS-drop re-bind window - feeds
    ``ProviderBinding.orphan_deadline_at`` (default 10 s, round
    5b/20)."""
    heartbeat_interval_sec: float | None = Field(default=None, gt=0)
    """Required ``provider.ping`` cadence for the line's binding.
    A runtime change takes effect on the provider's NEXT
    ``provider.reregister`` - the live provider learns
    ``heartbeat_interval_ms`` only from the register response, so
    a patched interval MUST NOT be enforced against the current
    session (round 23, review-12 H6: shrinking it mid-session
    would kill a healthy provider on a false ping timeout)."""
    heartbeat_degraded_after: int | None = Field(default=None, ge=1)
    """Missed pings before the line goes DEGRADED."""
    heartbeat_down_after: int | None = Field(default=None, ge=1)
    """Missed pings before the line goes DOWN (must exceed the
    DEGRADED threshold when both are set)."""
    fallback_accept_ttl_sec: float | None = Field(default=None, gt=0)
    """Deadline for the FallbackWorker to ``call.accept`` a re-issued
    ``fallback_call_token`` (round 23, review-12 M6; ~10 s
    illustrative default): bounds
    ``FallbackTriggeredEvent.expires_at``, so a dead worker never
    leaves a ring pending past its TTL."""

    @model_validator(mode="after")
    def _heartbeat_thresholds_ordered(self) -> TimeoutConfig:
        """Same invariant as :class:`LineTimeouts` (round 24,
        review-13 C2): when BOTH thresholds are patched at once,
        ``heartbeat_down_after`` must exceed
        ``heartbeat_degraded_after`` - catching the inverted pair at
        patch time instead of after hydration into the effective
        values."""
        if (
            self.heartbeat_down_after is not None
            and self.heartbeat_degraded_after is not None
            and self.heartbeat_down_after <= self.heartbeat_degraded_after
        ):
            raise ValueError("heartbeat_down_after must exceed heartbeat_degraded_after")
        return self


class LineAdminKernel(ABC):
    """Narrow admin-surface kernel interface (round 19).

    The agent-facing ``LineManager`` deliberately has NO mutation
    methods for claims, revokes, or runtime line config - the wire
    admin namespace (admin.*, scope ``admin``) maps onto this separate
    interface so an agent-scope token can never reach a privileged
    path by accident. ``InMemoryLineManager`` (M1) implements BOTH
    interfaces; the admin adapter alone holds a reference to this
    surface.
    """

    @abstractmethod
    async def assign_line(self, line_id: LineId, owner_agent_id: str) -> LineAssignment:
        """Admin override (round 22, review-10 HIGH-5; design §4.2
        "exclusive ownership is granted via claim + moderator approval
        OR admin assign"): grant a durable EXCLUSIVE
        ``LineAssignment`` to ``owner_agent_id`` WITHOUT a pending
        claim - the provisioning path used to pre-assign a line
        before the agent connects and files a claim.

        ``line_id`` must exist (LINE_NOT_FOUND) and must not already
        be held exclusive by another agent
        (LINE_EXCLUSIVE_NOT_OWNER); an existing SHARED_POOL
        assignment is converted in place (``mode=EXCLUSIVE``,
        ``owner_agent_id`` set) and mints a FRESH ``assignment_id``
        for the new owner/mode pair (rotation, round 24, review-13
        M2 - see :class:`LineAssignment`)."""
        ...

    @abstractmethod
    async def approve_claim(self, claim_id: ClaimId) -> LineAssignment:
        """Approve a pending ``call.claim_exclusive`` request: grants
        the durable ``exclusive_line`` assignment (moderator action,
        §4.2) and mints a FRESH ``assignment_id`` for it (rotation,
        round 24, review-13 M2). Unknown or already-resolved
        ``claim_id`` ->
        ``PhonebridgeError(ErrorCode.INVALID_STATE,
        ErrorData(reason=ErrorReason.CLAIM_NOT_FOUND))`` (round 24,
        review-13 L1 - a claim is pending STATE, not a token, so the
        missing id reports as INVALID_STATE with the typed
        sub-code)."""
        ...

    @abstractmethod
    async def reject_claim(self, claim_id: ClaimId, reason: str | None = None) -> None:
        """Reject a pending claim request with an optional PII-free
        reason string. Unknown or already-resolved ``claim_id`` ->
        ``PhonebridgeError(ErrorCode.INVALID_STATE,
        ErrorData(reason=ErrorReason.CLAIM_NOT_FOUND))`` - same
        mapping as :meth:`approve_claim` (round 24, review-13 L1)."""
        ...

    @abstractmethod
    async def list_pending_claims(self) -> list[PendingClaim]:
        """Admin read (round 21, HIGH-7): every pending claim request.
        The admin surface needs this to show moderators what awaits
        approval - without it the pending set lives only in private
        kernel state."""
        ...

    @abstractmethod
    async def list_assignments(self) -> list[LineAssignment]:
        """Admin read (round 21, HIGH-7): every durable
        ``LineAssignment`` (agent-ownership object space), so the
        admin surface can resolve valid ``assignment_id`` targets for
        ``revoke_assignment`` without reaching into kernel internals."""
        ...

    @abstractmethod
    async def list_provider_bindings(self) -> list[ProviderBinding]:
        """Admin read (round 21, HIGH-7): every ``ProviderBinding``
        (provider-attachment object space) - the second half of the
        ``assignment_id`` disambiguation documented on
        ``revoke_assignment``."""
        ...

    @abstractmethod
    async def revoke_assignment(
        self, assignment_id: AssignmentId, reason: str | None = None
    ) -> None:
        """Admin ``line.revoke``: end a durable binding.

        ``assignment_id`` disambiguates by which object space it
        resolves to (round 20 - two identifier spaces share the
        AssignmentId tag, api-contract.md Identifier taxonomy): a
        ``LineAssignment`` (agent ownership) -> revert the line to
        SHARED_POOL and emit ``AssignmentRevokedEvent`` to the
        revoked owner; a ``ProviderBinding`` -> enqueue a
        ``ProviderRevokeCommand`` in the outbox, drop the line to
        DOWN and tear down its leases, emitting
        ``LineStateChangedEvent(new_state=DOWN,
        reason=CallEndReason.ADMIN_REVOKED)`` - NEVER an
        ``AssignmentRevokedEvent``, because a provider attachment
        has no agent audience (round 22, review-10 HIGH-6).
        Unknown id in BOTH spaces ->
        ``PhonebridgeError(ErrorCode.STALE_TOKEN)`` (round 24,
        review-13 L1 - an unresolvable durable id is a stale
        reference, the counterpart of a consumed ``call_token``).
        Revoking an agent assignment reverts the line to
        SHARED_POOL and mints a FRESH ``assignment_id`` for the
        reverted binding (rotation, round 24, review-13 M2 - see
        :class:`LineAssignment`); the revoked id resolves through
        the bounded ``_closed_assignments`` ring so a retried
        revoke/release is idempotent rather than
        ``OWNER_MISMATCH``."""
        ...

    @abstractmethod
    async def configure_line(
        self,
        line_id: LineId,
        *,
        fallback_policy: FallbackPolicy | None = None,
        accept_window_sec: float | None = None,
        allowed_number_types: frozenset[str] | None = None,
        claimable: bool | None = None,
        idle_revoke_after: timedelta | None = None,
        timeouts: TimeoutConfig | None = None,
    ) -> LineAssignment:
        """Runtime line-config patch (admin.configure_line): apply only
        the non-None fields, return the updated LineAssignment - the
        returned record carries the line's EFFECTIVE
        :class:`LineTimeouts` in ``timeouts`` (round 24, review-13
        C2) so the admin adapter can write it through to
        ``lines.timeouts_json`` verbatim (design §4.5). All
        windows are admin-configurable without a daemon restart
        (round 10a). ``idle_revoke_after`` (round 22, review-10
        HIGH-5) patches the ALERT-ONLY idle-escalation deadline
        already carried on ``LineAssignment``; ``timeouts`` carries
        the remaining runtime knobs (grant TTL, orphan grace,
        heartbeat DEGRADED/DOWN thresholds, fallback accept TTL) as
        a partial :class:`TimeoutConfig` patch.

        Fallback-policy validation (round 23, review-12 C2; refined
        round 24, review-13 M1): a ``forward_via_line`` policy is
        pre-validated HERE, at config time -
        ``target_number``/``target_country`` go through the same
        strict-E.164 + ISO alpha-2 checks ``call.dial`` applies -
        and are additionally enforced INSIDE
        :class:`ForwardViaLineFallback` itself via
        :func:`validate_e164_country`, so a corrupt hydrated row can
        never carry an invalid policy past the model boundary. The
        ``allowed_number_types`` gate is evaluated against the
        TARGET line ``forward.line_id`` (when that line is already
        registered in the store - checking the CONFIGURED line's
        allowlist would gate the wrong policy, review-13 M1) and
        re-checked at dial time by the FallbackWorker against the
        target's effective allowlist. A SELF-LOOP is rejected
        outright: ``forward.line_id == line_id`` is
        INVALID_ARGUMENT - the line can never be its own forward
        target (the same invariant is model-enforced inside
        :class:`LineAssignment`). A patched
        ``heartbeat_interval_sec`` takes effect on the provider's
        next reregister, never mid-session (see
        :class:`TimeoutConfig`)."""
        ...
