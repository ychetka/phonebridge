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
- rule 10 (no secrets/PII): caller numbers, provider tokens AND the
  call/media tokens travel as :class:`RedactedString` (round 25,
  review-15 item 3 - the token NewTypes wrap the container, so
  ``safe_dump()``/``to_log_dict()`` emit only the masked form);
  ``hide_input_in_errors`` keeps raw input
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

import hmac
import re
from abc import ABC, abstractmethod
from collections.abc import Collection
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import (
    Annotated,
    Any,
    Final,
    Generic,
    Literal,
    NewType,
    Protocol,
    TypeAlias,
    TypeVar,
    cast,
    runtime_checkable,
)

import phonenumbers
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    GetCoreSchemaHandler,
    Tag,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_core import core_schema

__all__ = [
    "CALL_PHASE_FORWARD_TRANSITIONS",
    "CALL_PHASE_WIRE_STATES",
    "DTMF_PATTERN",
    "E164_PATTERN",
    "ERROR_CODE_WIRE",
    "EVENT_AUDIENCE_SCOPES",
    "EVENT_PAYLOAD_TYPES",
    "EVENT_WIRE_NAMES",
    "MAX_CALLER_ID_LEN",
    "MAX_CLOCK_SKEW_SEC",
    "MAX_CLOSED_ASSIGNMENTS",
    "MAX_CLOSED_LEASES",
    "MAX_CTRL_FRAME_LEN",
    "MAX_DETAIL_LEN",
    "MAX_DTMF_DIGITS",
    "MAX_ENDPOINT_LEN",
    "MAX_FORWARD_HOPS",
    "MAX_IDEMPOTENCY_KEYS_PER_SESSION",
    "MAX_IDEMPOTENCY_KEY_LEN",
    "MAX_LEASES_PER_SESSION",
    "MAX_LINES_TOTAL",
    "MAX_LIST_PAGE",
    "MAX_PENDING_CLAIMS_PER_AGENT",
    "MAX_PENDING_CLAIMS_TOTAL",
    "MAX_PROFILE_LEN",
    "MAX_PROVIDER_META_ENTRIES",
    "MAX_PROVIDER_META_KEY_LEN",
    "MAX_PROVIDER_META_VALUE_LEN",
    "MAX_QUEUE_TICKETS_TOTAL",
    "MAX_REASON_LEN",
    "MAX_SESSIONS_PER_AGENT",
    "MAX_SESSIONS_TOTAL",
    "MAX_TICKETS_PER_SESSION",
    "MAX_TOKEN_LEN",
    "MEDIA_AUTH_TIMEOUT_SEC",
    "MEDIA_PAIR_TTL_SEC",
    "OUTBOX_SOFT_CAP",
    "PROVIDER_COMMAND_WIRE_NAMES",
    "SNAPSHOT_RECONCILED_PHASES",
    "SYSTEM_AGENT_PREFIX",
    "SYSTEM_FALLBACK_WORKER",
    "AcceptResult",
    "AcquireGranted",
    "AcquireQueued",
    "AcquireRefused",
    "AcquireResult",
    "AdminAudience",
    "AdminLineView",
    "AgentAudience",
    "AnswerAndAnnounceFallback",
    "AnswerConfidence",
    "AssignmentGrantedEvent",
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
    "ClaimResolvedEvent",
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
    "LineHealthChangedEvent",
    "LineId",
    "LineManager",
    "LineState",
    "LineStateChangedEvent",
    "LineSummary",
    "LineTimeouts",
    "LivenessState",
    "MediaAllocator",
    "MediaCredentials",
    "MediaReattachResult",
    "MediaRole",
    "MediaToken",
    "MediaTransport",
    "MissedCallEvent",
    "NoPickupFallback",
    "PairWindowRequest",
    "PairWindowResponse",
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
    "ProviderMediaReattachRequest",
    "ProviderMediaReattachResult",
    "ProviderReattachMediaCommand",
    "ProviderRegistration",
    "ProviderRevokeCommand",
    "ProviderSendDtmfCommand",
    "QueueCancelReason",
    "QueueCancelledEvent",
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
    "clamp_provider_stamp",
    "ensure_aware_utc",
    "heartbeat_interval_ms_from_sec",
    "is_system_agent_id",
    "validate_e164_country",
]


# ---------------------------------------------------------------------------
# PII-safe string container (api-contract.md "Common types";
# docs/CODING_HAZARDS.md rule 10). Declared BEFORE the identifier
# taxonomy: the token NewTypes (CallToken/MediaToken) wrap this
# container (round 25, review-15 item 3).
# ---------------------------------------------------------------------------

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
        """ "+37362112345" -> "+373****45"; never exposes more than half
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
        # Constant-time secret compare (round 25, review-15 item 39):
        # ``==`` on the raw str short-circuits on the first differing
        # byte - a timing oracle for a media/call token checked inside
        # ``media.auth``/``call.accept``. ``hmac.compare_digest`` makes
        # the comparison length-tolerant but time-flat.
        return hmac.compare_digest(self._value.encode(), other._value.encode())

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

CallToken = NewType("CallToken", RedactedString)
"""One-shot inbound-ring token; TTL = accept_window_sec (default 30 s).
Burns on call.accept or timeout.

Round 25 (review-15 item 3; CODING_HAZARDS rule 10): the token wraps
:class:`RedactedString`, so ``safe_dump()``/``to_log_dict()`` emit the
masked form and never the raw secret. The raw value is reachable only
through ``.expose()``/``.to_wire()``; equality and hash behave exactly
like :class:`RedactedString` (constant-time compare)."""

ClaimId = NewType("ClaimId", str)
"""Pending exclusive-line claim request id (call.claim_exclusive);
resolved by the admin approve_claim/reject_claim pair (round 21)."""

LeaseId = NewType("LeaseId", str)
"""Ephemeral single id of the active line hold AND the call.
Replaces any call_id everywhere - no call_id exists (round 11)."""

MediaToken = NewType("MediaToken", RedactedString)
"""Authorizes the binary media socket handshake; bound to a
(lease_id, role) pair - every lease carries TWO independent one-time
tokens (agent vs provider) so media.auth role claims are
cryptographically backed (round 21, CRITICAL-2).

Round 25 (review-15 item 3; CODING_HAZARDS rule 10): the token wraps
:class:`RedactedString`, so ``safe_dump()``/``to_log_dict()`` emit the
masked form and never the raw secret. The raw value is reachable only
through ``.expose()``/``.to_wire()``; equality and hash behave exactly
like :class:`RedactedString` (constant-time compare)."""

LineId = NewType("LineId", str)
"""Canonical line identifier. The agent-side alias "default" is resolved
at the API boundary and never appears inside the domain (round 11)."""

TicketId = NewType("TicketId", str)
"""FIFO queue ticket id; pairs with QueueTicket.seq for ordering."""

SessionId = NewType("SessionId", str)
"""Agent session id returned by register_intent (wire: agent.register_intent).
Tickets and leases are scoped to it; disconnect kills tickets (socket-bound)."""


# ---------------------------------------------------------------------------
# Reserved system identity + identifier hygiene
# (round 25, review-15 items 21/37)
# ---------------------------------------------------------------------------

SYSTEM_AGENT_PREFIX: str = "system:"
"""Reserved prefix for kernel-internal service identities (round 25,
review-15 item 21). These identities are provisioned out-of-band and
MUST never be issuable to an external socket identity - see the
:meth:`LineManager.register_intent` contract note. The colon is also
why external agent ids may never contain ``":"`` (see
:func:`_check_id`)."""

SYSTEM_FALLBACK_WORKER: str = f"{SYSTEM_AGENT_PREFIX}fallback_worker"
"""The FallbackWorker's reserved agent identity - the only current
member of the ``system:*`` namespace. It accepts re-issued
``fallback_call_token`` rings and executes worker fallback policies
over the standard agent API (ADR-01)."""


def is_system_agent_id(agent_id: str) -> bool:
    """True iff ``agent_id`` carries the reserved
    :data:`SYSTEM_AGENT_PREFIX` - a kernel-internal service identity
    (round 25, review-15 item 21)."""
    return agent_id.startswith(SYSTEM_AGENT_PREFIX)


_ID_MAX_LEN: int = 128
"""Upper bound on any domain identifier (round 25, review-15 item 37;
CODING_HAZARDS rule 7 on external id-shaped inputs)."""

_ID_FORBIDDEN: str = ":"
"""The colon is the field separator of single-string wire spellings
(``forward_via_line:<line_id>:<number>:<country>``,
``record_and_telegram:<channel>``, ``answer_and_announce:<ref>``) -
a ``":"`` inside an identifier would corrupt the adapter's split and
could smuggle a second parameter (round 25, review-15 item 37)."""

_ID_CHARSET: re.Pattern[str] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
"""Strict identifier grammar (round 27, review-17 item 36):
identifiers are interpolated into PipeWire node names
(``phonebridge_uplink_<line_id>``) and into the state-file paths the
LineProvider keeps under ``/run``, so the alphabet is restricted to
``[A-Za-z0-9._-]`` with a strictly alphanumeric first character - no
path separators, whitespace, glob metacharacters or ``:`` can ever
ride an id (the colon is outside the charset, so the
``_ID_FORBIDDEN`` check below is now redundant-but-explicit
documentation of the wire-separator rule)."""

_IdT = TypeVar("_IdT", bound=str)


def _check_id(value: _IdT) -> _IdT:
    """Domain-identifier hygiene (round 25, review-15 item 37;
    charset added round 27, review-17 item 36): an id is a non-empty
    opaque string of at most ``_ID_MAX_LEN`` chars matching
    :data:`_ID_CHARSET` and MUST NOT contain ``_ID_FORBIDDEN``
    (``:``) - the field separator of the single-string wire
    spellings. Generic over the identifier NewTypes so each checked
    field keeps its declared type."""
    if not value:
        raise ValueError("identifier must be non-empty")
    if len(value) > _ID_MAX_LEN:
        raise ValueError(f"identifier exceeds {_ID_MAX_LEN} chars")
    if _ID_FORBIDDEN in value:
        raise ValueError(f"identifier must not contain {_ID_FORBIDDEN!r}")
    if not _ID_CHARSET.fullmatch(value):
        raise ValueError(
            "identifier must match [A-Za-z0-9][A-Za-z0-9._-]* "
            "(PipeWire node names and state-file paths embed it)"
        )
    return value


def _check_line_id(value: LineId) -> LineId:
    """:func:`_check_id` plus the ``"default"`` alias ban (round 25,
    review-15 item 37): ``"default"`` is an API-boundary alias the
    adapter resolves before calling the kernel - a literal ``default``
    inside domain records would collide with a genuinely-named line
    and bypass per-line ownership."""
    _check_id(value)
    if value == "default":
        raise ValueError('"default" is an API-boundary alias, not a domain line_id')
    return value


def _check_agent_id(value: str) -> str:
    """:func:`_check_id` for agent identities (round 25, review-15
    item 37): the colon ban applies to EXTERNAL agent ids, but the
    reserved ``system:*`` namespace stays legal on domain records -
    :data:`SYSTEM_FALLBACK_WORKER` is a kernel-internal agent that
    legitimately appears on ``AgentAudience.agent_id`` /
    ``owner_agent_id`` (item 21). A bare ``system:`` with an empty
    or whitespace-only suffix is NOT a legal member of that
    namespace - the prefix alone names nothing - and the suffix
    itself obeys the FULL external-id hygiene (round 27, review-17
    item 38): :func:`_check_id` on the suffix, so ``system: x`` or
    ``system:x y`` are rejected by the strict charset, not merely by
    the colon/length checks."""
    if is_system_agent_id(value):
        suffix = value[len(SYSTEM_AGENT_PREFIX) :]
        if not suffix.strip():
            raise ValueError(
                f"system agent id must carry a non-empty suffix after {SYSTEM_AGENT_PREFIX!r}"
            )
        _check_id(suffix)
        return value
    return _check_id(value)


# Checked-identifier field aliases (round 25, review-15 item 37):
# same ``Annotated[..., AfterValidator]`` style as ``AwareDatetime`` -
# the annotation stays the declared identifier type while model
# validation applies the hygiene check at construction and hydration.

_CheckedId = Annotated[str, AfterValidator(_check_id)]
_CheckedLineId = Annotated[LineId, AfterValidator(_check_line_id)]
_CheckedAgentId = Annotated[str, AfterValidator(_check_agent_id)]
_CheckedLeaseId = Annotated[LeaseId, AfterValidator(_check_id)]
_CheckedSessionId = Annotated[SessionId, AfterValidator(_check_id)]
_CheckedTicketId = Annotated[TicketId, AfterValidator(_check_id)]
_CheckedClaimId = Annotated[ClaimId, AfterValidator(_check_id)]
_CheckedAssignmentId = Annotated[AssignmentId, AfterValidator(_check_id)]

_RedactedT = TypeVar("_RedactedT", bound=RedactedString)


def _check_token(value: _RedactedT) -> _RedactedT:
    """Bearer-token hygiene (round 28, review-18 items 16/17): a
    ``CallToken``/``MediaToken`` is a non-empty opaque string of at
    most ``MAX_TOKEN_LEN`` chars - an empty token can never
    authorize anything and an unbounded one is a wire/log bloat
    hazard. Generic over the token NewTypes so each checked field
    keeps its declared type."""
    raw = value.expose()
    if not raw:
        raise ValueError("token must be non-empty")
    if len(raw) > MAX_TOKEN_LEN:
        raise ValueError(f"token exceeds {MAX_TOKEN_LEN} chars")
    return value


def _check_caller_number(value: RedactedString) -> RedactedString:
    """Caller-number field bound (round 28, review-18 items 16/17):
    ``caller_id``/``number`` fields are non-empty when present and
    at most ``MAX_CALLER_ID_LEN`` chars - a witheld identity is
    modeled by the absent field (``None``), never by ``""``."""
    raw = value.expose()
    if not raw:
        raise ValueError("caller number must be non-empty")
    if len(raw) > MAX_CALLER_ID_LEN:
        raise ValueError(f"caller number exceeds {MAX_CALLER_ID_LEN} chars")
    return value


_CheckedCallToken = Annotated[CallToken, AfterValidator(_check_token)]
_CheckedMediaToken = Annotated[MediaToken, AfterValidator(_check_token)]
_CheckedCallerNumber = Annotated[RedactedString, AfterValidator(_check_caller_number)]


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class Intent(str, Enum):
    """Agent intent vocabulary (docs/api-contract.md "Common types")."""

    DIAL_OUT = "dial_out"
    RECEIVE_INBOUND = "receive_inbound"
    EXCLUSIVE_LINE = "exclusive_line"


class LineState(str, Enum):
    """Line CALL-OCCUPANCY state machine (design §4.2; round 25,
    review-15 item 15 - the pure call-occupancy vocabulary).

    RINGING_LOCKED: inbound ring in flight; outbound acquire fails with
    LINE_BUSY_INCOMING until accept-window expiry or accept.

    Transport/provider health is a SEPARATE axis, NOT a LineState: it
    is carried by ``ProviderBinding.liveness``/``LineSummary.liveness``
    (:class:`LivenessState`) and surfaced via
    ``event.line_health_changed`` (:class:`LineHealthChangedEvent`).
    The former RESERVED/DEGRADED/DOWN members were removed in round 25
    - they conflated "a call occupies the line" with "the provider is
    healthy", and the split lets a liveness change and a BUSY fact
    coexist truthfully (a degraded provider can still hold a call)."""

    FREE = "FREE"
    BUSY = "BUSY"
    RINGING_LOCKED = "RINGING_LOCKED"


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


CALL_PHASE_FORWARD_TRANSITIONS: dict[CallPhase, frozenset[CallPhase]] = {
    CallPhase.ACQUIRED: frozenset({CallPhase.DIALING, CallPhase.RINGING, CallPhase.ENDED}),
    CallPhase.DIALING: frozenset({CallPhase.ALERTING, CallPhase.ACTIVE, CallPhase.ENDED}),
    CallPhase.RINGING: frozenset({CallPhase.ACTIVE, CallPhase.ENDED}),
    CallPhase.ALERTING: frozenset({CallPhase.ACTIVE, CallPhase.ENDED}),
    CallPhase.ACTIVE: frozenset({CallPhase.TERMINATING, CallPhase.ENDED}),
    CallPhase.TERMINATING: frozenset({CallPhase.ENDED}),
    CallPhase.ENDED: frozenset(),
}
"""Authoritative forward-only :class:`CallPhase` transition matrix
(round 28, review-18 item 16; ``DIALING -> ACTIVE`` added in the
round-28 fix, PR #72 review - oFono/provider may report an
alerting-less answer when the network answers before an
``alerting`` leg propagates, and the matrix must not reject a
legal provider fact). The kernel MUST apply ONLY the transitions
listed here in :meth:`LineManager.ingest_call_state`: a regression
(an ``ACTIVE`` lease returning to ``DIALING``/``ALERTING``) is a
defect fact dropped like an out-of-order update, ``ENDED`` is
terminal, and phase can never skip backwards over ``ENDED``.
``ACQUIRED -> RINGING`` covers the preemption of an undialed
grant by an inbound ring. Drift is pinned by
``test_call_phase_forward_matrix``."""


SNAPSHOT_RECONCILED_PHASES: frozenset[CallPhase] = frozenset(
    {
        CallPhase.DIALING,
        CallPhase.ALERTING,
        CallPhase.ACTIVE,
        CallPhase.TERMINATING,
    }
)
"""The formal definition of "dialing-or-later" for
``state_snapshot`` reconciliation (round 29, SA_quant_review_1
slice-A item 7): a ``provider.reregister``/``provider.ping``
snapshot is authoritative ONLY for leases in one of these phases -
the phases a lease can reach only AFTER the kernel delivered a
``line.dial``/``line.answer`` command, so the provider could
actually see the call on the D-Bus. ``ACQUIRED`` is excluded
because a granted-but-undialed lease is pre-dial (the provider can
never observe it), and ``RINGING`` is excluded too: an
``ACQUIRED -> RINGING`` preemption grant likewise never produced a
dial command. ``CallPhase`` is a ``str``-valued enum with no
ordering - reconciliation MUST test membership in this set, never
a ``>=`` comparison that the type does not support (a ``RINGING``
lease reached via preemption sits outside any linear
``ACQUIRED -> DIALING`` ladder anyway). Pinned verbatim by
``test_snapshot_reconciled_phases``."""


class AssignmentMode(str, Enum):
    """LineAssignment.mode (design §4.2)."""

    EXCLUSIVE = "exclusive"
    SHARED_POOL = "shared_pool"


class LivenessState(str, Enum):
    """Provider liveness fact reported via note_provider_liveness.

    DEGRADED (round 28, review-18 item 1) is the KERNEL-COMPUTED
    middle rung of the heartbeat ladder ``reachable -> degraded ->
    unreachable``: a binding that has been silent for
    ``heartbeat_degraded_after * heartbeat_interval`` is degraded
    (still tolerated - a warning, not a teardown), and escalating
    past ``heartbeat_down_after * heartbeat_interval`` goes
    unreachable. Like UNBOUND it is NEVER a reported fact -
    :class:`ProviderLiveness` rejects it because the provider only
    reports ``reachable``/``ws_disconnected``/``unreachable``
    observations and the kernel derives degradation itself. A
    REACHABLE fact on the live channel clears it, so
    ``degraded -> reachable`` is legal. A control-socket drop on a
    DEGRADED binding resolves ``degraded -> ws_disconnected``
    (round-28 fix, PR #72 review): the socket-loss axis overrides
    the elapsed-time rung, so a degraded binding that loses its
    socket still reaches the orphan-grace path.

    WS_DISCONNECTED (round 21, CRITICAL-3): the provider's WS control
    channel dropped while the telephony path may still be alive -
    starts the 10 s orphan-grace window
    (``ProviderBinding.orphan_deadline_at``), after which a
    ``provider.reregister`` carrying a ``state_snapshot`` decides
    re-bind vs teardown. UNREACHABLE instead means heartbeat timeout
    (the ``heartbeat_degraded_after``/``heartbeat_down_after``
    escalation ladder) and tears leases down IMMEDIATELY - no grace
    (design §5.4).

    This is the provider-HEALTH axis (round 25, review-15 item 15):
    it lives on ``ProviderBinding.liveness``/``LineSummary.liveness``
    and its transitions surface as ``event.line_health_changed`` -
    never as ``event.line_state_changed``, whose
    :class:`LineState` vocabulary is call-occupancy only.

    UNBOUND (round 26, review-16 item 13) is the DERIVED listing
    state for a line with no provider attached - ``LineSummary``
    reports it because REACHABLE was misleading (nothing exists to
    be healthy or sick). It is never a REPORTED fact:
    :class:`ProviderLiveness` always comes from an attached binding
    and :class:`LineHealthChangedEvent` transitions never produce
    it (binding revocation surfaces UNREACHABLE)."""

    REACHABLE = "reachable"
    DEGRADED = "degraded"
    WS_DISCONNECTED = "ws_disconnected"
    UNREACHABLE = "unreachable"
    UNBOUND = "unbound"


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
    this enum can never arrive on that ingress.

    ``MAX_CALL_DURATION`` (round 28, review-18 item 7) is the
    gateway-minted teardown reason of the ``max_call_duration_sec``
    timeout: :meth:`LineManager.tick` ends a DIALING/ALERTING/ACTIVE
    lease whose wall-clock length crosses the cap with
    ``reason:"max_call_duration"`` (and bounds the
    ``record_and_telegram`` recording by the same wall-clock rule).
    It is INADMISSIBLE on ``event.missed_call`` (round-28 fix,
    PR #72 review - the exclusion is enforced on
    :class:`MissedCallEvent`): a missed call is an unanswered ring
    that never held a lease, so it can never hit the cap - the
    teardown surfaces as ``call_state_changed`` + ``ended_lease``
    instead."""

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
    MAX_CALL_DURATION = "max_call_duration"


class QueueCancelReason(str, Enum):
    """CLOSED reason vocabulary for ``event.queue_cancelled``
    (:class:`QueueCancelledEvent`, round 25, review-15 item 23).

    A pending ticket is CANCELLED - never silently parked - when its
    target stops being able to serve it:

    - ``LINE_EXCLUSIVE``: the target line became EXCLUSIVE via
      ``assign_line``/``approve_claim`` - the ticket can never be
      granted to a non-owner. A ``claimable`` flip to False under
      ``configure_line`` is NOT this reason (round 28, review-18
      item 5): ``claimable=False`` only makes the line
      non-claimable, it still serves the ``SHARED_POOL``, so the
      line's pool tickets stay queued - only pending CLAIMS on it
      are auto-rejected via ``claim_resolved{status:"rejected"}``;
    - ``PROVIDER_LOST``: the line's provider binding was revoked or
      declared UNREACHABLE - no provider means no grant;
    - ``CAPABILITY_REMOVED``: a ``provider.capability_update`` removed
      the intent the ticket asked for;
    - ``CLAIM_RESOLVED``: the ticket was invalidated by a claim
      lifecycle resolution on the same line (companion of
      :class:`ClaimResolvedEvent`).

    Overlap rule (round 27, review-17 item 50): an ``approve_claim``
    cancels BOTH the new owner's own queued pool tickets (moot -
    the owner no longer competes for its own line) and every OTHER
    session's tickets targeting the line. The reasons differ: the
    CLAIMING session's tickets die as ``CLAIM_RESOLVED`` (their fate
    follows the claim, not ownership exclusion), while foreign
    tickets die as ``LINE_EXCLUSIVE`` - a single approval never
    reports the same reason for both audiences and ``claim_resolved``
    is never used for a non-claimant's ticket.

    Cancelling keeps the queue honest: the wire table documents no
    "ticket silently expires" mechanism, so every non-grant fate
    MUST reach the session explicitly (socket-bound tickets dying
    with their session stay the implicit ``drop_session`` case)."""

    LINE_EXCLUSIVE = "line_exclusive"
    PROVIDER_LOST = "provider_lost"
    CAPABILITY_REMOVED = "capability_removed"
    CLAIM_RESOLVED = "claim_resolved"


class ErrorCode(str, Enum):
    """Wire error codes - VERBATIM from docs/api-contract.md,
    including the round-25 (review-15 item 4) transport/auth trio
    ``UNAUTHORIZED``/``FORBIDDEN``/``SESSION_NOT_FOUND``.

    The wire JSON-RPC error object carries TWO layers (round 25):
    ``error.code`` is the INTEGER from :data:`ERROR_CODE_WIRE`
    (JSON-RPC application-reserved range -32099..-32000),
    ``error.message`` is the ErrorCode string, and ``error.data`` is
    the :class:`ErrorData` JSON (reason/detail/free_lines).

    No additional codes may be introduced in the domain: new error
    semantics travel as typed reason sub-codes in ErrorData.reason
    until the wire document itself is amended. (tests/test_domain_
    contract.py parses the doc error-code table and asserts this enum
    matches it exactly.)
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
    # Round 25 (review-15 item 4): transport/auth-layer codes -
    # reserved system identity on an external socket (UNAUTHORIZED),
    # an authenticated identity reaching a scope it does not hold
    # (FORBIDDEN), and an unknown/disconnected session id
    # (SESSION_NOT_FOUND).
    UNAUTHORIZED = "UNAUTHORIZED"
    FORBIDDEN = "FORBIDDEN"
    SESSION_NOT_FOUND = "SESSION_NOT_FOUND"


ERROR_CODE_WIRE: dict[ErrorCode, int] = {
    ErrorCode.INVALID_ARGUMENT: -32001,
    ErrorCode.INVALID_STATE: -32002,
    ErrorCode.INVALID_INTENT: -32003,
    ErrorCode.OWNER_MISMATCH: -32011,
    ErrorCode.LINE_EXCLUSIVE_NOT_OWNER: -32012,
    ErrorCode.LINE_BUSY_INCOMING: -32013,
    ErrorCode.LINE_NOT_FOUND: -32014,
    ErrorCode.STALE_TOKEN: -32020,
    ErrorCode.PROVIDER_UNREACHABLE: -32021,
    ErrorCode.DIAL_REJECTED: -32022,
    ErrorCode.RESOURCE_EXHAUSTED: -32030,
    ErrorCode.UNAUTHORIZED: -32040,
    ErrorCode.FORBIDDEN: -32041,
    ErrorCode.SESSION_NOT_FOUND: -32042,
}
"""Authoritative ErrorCode -> JSON-RPC ``error.code`` integer map
(round 25, review-15 item 4). The wire error object carries the
INTEGER from this table as ``error.code`` - the application-reserved
band -32099..-32000 (JSON-RPC proper owns -32768..-32000 for the
protocol and implementation-defined server errors; this table keeps
the application's own codes inside the server-error block without
colliding with the documented protocol codes). ``error.message`` is
the ErrorCode string and ``error.data`` is the :class:`ErrorData`
JSON (reason/detail/free_lines). Completeness, uniqueness and range
are pinned by ``test_error_code_wire_map``."""


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
    LINE_HEALTH_CHANGED = "line_health_changed"
    MISSED_CALL = "missed_call"
    IDLE_ALERT = "idle_alert"
    CLAIM_RESOLVED = "claim_resolved"
    ASSIGNMENT_GRANTED = "assignment_granted"
    QUEUE_CANCELLED = "queue_cancelled"


EVENT_WIRE_NAMES: dict[EventName, str] = {
    EventName.INCOMING_CALL: "event.incoming_call",
    EventName.CALL_STATE_CHANGED: "event.call_state_changed",
    EventName.QUEUE_READY: "event.line_acquired",
    EventName.QUEUE_UPDATED: "event.queue_updated",
    EventName.ASSIGNMENT_REVOKED: "event.assignment_revoked",
    EventName.AUDIO_READY: "event.audio_ready",
    EventName.FALLBACK_TRIGGERED: "event.fallback_triggered",
    EventName.LINE_STATE_CHANGED: "event.line_state_changed",
    EventName.LINE_HEALTH_CHANGED: "event.line_health_changed",
    EventName.MISSED_CALL: "event.missed_call",
    EventName.IDLE_ALERT: "event.idle_alert",
    EventName.CLAIM_RESOLVED: "event.claim_resolved",
    EventName.ASSIGNMENT_GRANTED: "event.assignment_granted",
    EventName.QUEUE_CANCELLED: "event.queue_cancelled",
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

MediaTransport: TypeAlias = Literal["ws_pcm"]
"""Vocabulary of media transports (round 27, review-17 item 78):
the single unified transport - framed PCM over WebSocket per
docs/api-contract.md "Binary media protocol" (``unix://`` sockets
and ``wss://`` listeners carry identical framing). A second
transport may only be added by a wire-document amendment - same
closed-vocabulary rule as the event/command tables."""

MediaRole: TypeAlias = Literal["agent", "provider"]
"""The two independent credential halves of one lease's media pair
(round 28, review-18 item 2): ``MediaToken`` values are bound to a
``(lease_id, role)`` pair since round 21, so a media-channel
reissue MUST address exactly one role - see
:meth:`MediaAllocator.reissue`."""


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

MAX_IDEMPOTENCY_KEYS_PER_SESSION: int = 256
"""Bound on the per-session ``call.acquire`` dedup table (round 29,
SA_quant_review_1 slice-B item 6; rule 7): the
``(session_id, idempotency_key)`` outcome entries live only inside
the LIVE session, but a long-lived session minting a fresh key on
every call would otherwise grow the table without bound. Once a
session already holds this many entries, a NEW key is rejected
``RESOURCE_EXHAUSTED`` fail-closed - the table is never
LRU-trimmed, because evicting a recorded outcome would let a
same-key retry re-execute an acquire the dedup table was built to
absorb."""

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

MAX_PROFILE_LEN: Final[int] = 128
"""Upper bound on a ``profile`` label (round 26, review-16 item 4;
CODING_HAZARDS rule 7 on an external agent/provider input): when
present the ``profile`` fields of :class:`ProviderRegistration`,
:class:`ProviderCapabilityUpdate` and the :class:`LineSummary`
mirror are a non-empty string of at most 128 chars - an empty or
overlong label is INVALID_ARGUMENT at the boundary. The
``LineSummary`` listing mirror shares the same bound so a stored
value can never become unlistable."""

MAX_SESSIONS_PER_AGENT: int = 32
"""Concurrent open sessions per ``agent_id`` in ``register_intent``
(round 25, review-15 item 30; CODING_HAZARDS rule 7 on an external
socket count): parallel workers and a Control MCP session next to an
audio-worker session are legitimate, but an unbounded session count
per identity is a kernel-memory DoS. Overflow ->
RESOURCE_EXHAUSTED / ErrorReason.SESSION_LIMIT_EXCEEDED."""

MAX_TICKETS_PER_SESSION: int = 16
"""Pending queue tickets per session (round 25, review-15 item 30;
rule 7): a multi-call agent may hold N parallel acquires, but the N
is bounded. Overflow -> RESOURCE_EXHAUSTED / ErrorReason.QUEUE_FULL."""

MAX_QUEUE_TICKETS_TOTAL: int = 1024
"""Global bound on pending :class:`QueueTicket` entries across all
sessions (round 25, review-15 item 30; rule 7): the per-session cap
alone is not enough when many sessions connect. Overflow ->
RESOURCE_EXHAUSTED / ErrorReason.QUEUE_FULL."""

MAX_LEASES_PER_SESSION: int = 8
"""Live :class:`CallLease` records per session (round 25, review-15
item 30; rule 7): every lease carries a real call plus a media
credential pair - an unbounded count would park unbounded PipeWire
bridges and provider commands."""

MAX_FORWARD_HOPS: Final[int] = 8
"""Bound on the transitive ``forward_via_line`` chain walk (round
26, review-16 item 2; docs/specs/line-manager.md):
``configure_line`` MUST reject a policy that would create a
forwarding CYCLE (``A -> B -> ... -> A``) by walking the stored
policy chain of the target lines; the walk is bounded at this many
hops so a pathological config can never park the kernel in an
unbounded traversal (rule 7). Reaching the bound on an OPEN chain
is fail-closed too (round 27, review-17 item 19): a chain deeper
than this bound is rejected INVALID_ARGUMENT rather than accepted
unverified. The two failure cases share ``INVALID_ARGUMENT`` but
MUST carry distinguishing ``ErrorData.detail`` text (round 29,
SA_quant_review_1 slice-A item 12 - no dedicated
``ErrorReason`` member exists and the closed sub-code vocabulary
is not extended for it): "forward cycle" when the walk closes a
loop vs "forward chain exceeds MAX_FORWARD_HOPS" when it runs
out of hops on an open chain, so diagnostics can tell a config
loop from an over-deep chain. A direct self-loop is already
model-enforced on :class:`LineAssignment` - this bound covers the
multi-line case the model cannot see."""

MEDIA_PAIR_TTL_SEC: int = 30
"""Half-connected media-pair teardown window (round 25, review-15
item 30; rule 7 - a socket pair that never completes its handshake
must not linger). Replaces the earlier documented ~5 s pair TTL:
MCP LLM agents routinely take >5 s between tool calls, so the
MediaRouter tears down a half-connected pair after THIS window, not
after the old 5 s."""

MEDIA_AUTH_TIMEOUT_SEC: int = 5
"""First-frame deadline on a freshly accepted media socket (round
25, review-15 item 30; rules 2/7): a socket that never sends
``media.auth`` inside this window is closed - an unauthenticated
connection may not hold a router slot indefinitely."""

OUTBOX_SOFT_CAP: int = 256
"""Soft depth cap of the transactional outbox (round 27, review-17
item 12): the pre-flight check documented on ``drain_events()`` /
``drain_provider_commands()`` rejects resource-CREATING mutations at
or above this many queued event/command entries while kernel state
is still untouched. The HARD ceiling is this soft cap plus a
reserve tail sized by the number of live leases - teardown facts
(hangup/release/drop_session/provider-loss/tick expiries) append
unconditionally so a wedged outbox can never strand a teardown."""

MAX_CLOSED_LEASES: int = 1024
"""Bound on the ``_closed_leases`` LRU ring of superseded lease ids
(round 27, review-17 item 12): the kernel retains a bounded ring of
recently-ended ``lease_id`` values so a late retry resolves as an
idempotent success instead of STALE_TOKEN confusion - the same
ring shape as :data:`MAX_CLOSED_ASSIGNMENTS` on the durable-binding
side."""

MAX_CLOSED_ASSIGNMENTS: int = 1024
"""Bound on the ``_closed_assignments`` LRU ring of superseded
binding ids (round 27, review-17 item 12; rotation documented on
:class:`LineAssignment`): a late ``release``/``revoke`` retry by the
previous owner resolves through this ring as an idempotent success
rather than OWNER_MISMATCH. ONE ring is shared by both
assignment-id object spaces - ``LineAssignment`` and
``ProviderBinding`` (round 29, SA_quant_review_1 slice-A item 5):
mint-uniqueness across the two spaces makes a shared ring safe
because a stored id can only ever have named one object type."""

MAX_SESSIONS_TOTAL: int = 512
"""Global bound on open agent sessions across all ``agent_id``
values (round 27, review-17 item 73; rule 7): complements
:data:`MAX_SESSIONS_PER_AGENT` - the per-agent cap alone is not
enough when many distinct agents connect; overflow is
RESOURCE_EXHAUSTED / ErrorReason.SESSION_LIMIT_EXCEEDED."""

MAX_LINES_TOTAL: int = 256
"""Global bound on configured lines (round 29, SA_quant_review_1
slice-A item 10; rule 7): every other kernel collection carries an
explicit cap, but the configured-line set itself was bounded only
by "config is finite" - an unbounded line count would scale the
bounded ``forward_via_line`` chain walk, the admin listings and
every queue/liveness sweep into unbounded work.
:meth:`LineAdminKernel.provision_line` MUST reject a provisioning
attempt beyond this count with ``RESOURCE_EXHAUSTED``."""

MAX_LIST_PAGE: int = 256
"""Page bound for the shared ``{offset?, limit?}`` window on the
listing calls (round 29, SA_quant_review_1 slice-B item 1; PR #74
review): ``call.get_lines``, ``admin.list_lines``,
``admin.list_leases`` and ``admin.list_tickets`` accept an optional
``limit`` inside ``1..MAX_LIST_PAGE`` (default ``MAX_LIST_PAGE``)
and return a stable-ordered slice, so an unbounded listing can
never overrun the wire's 64 KiB WS-TEXT frame cap at scale. The
bound is named once here so the wire tables, the kernel signatures
and the store implementations pin the same number."""

MAX_DETAIL_LEN: int = 256
"""Upper bound on ``ErrorData.detail`` (round 27, review-17 item
63; rule 7): the human-readable error context is an external
input-sized field that lands in logs and wire replies, so it is
bounded like every other free-form string."""

MAX_REASON_LEN: int = 128
"""Upper bound on free-form ``reason``/``detail`` strings that do
NOT ride a closed enum vocabulary (round 27, review-17 items
27/52/63): admin revoke notes, claim-reject reasons and fallback
decision context are bounded - enum-typed ``reason`` fields are
closed by construction and need no length cap."""

MAX_CTRL_FRAME_LEN: int = 4096
"""Upper bound on one control-frame line of the hci-agent control
protocol (round 27, review-17 item 77): the constant lived only in
docs/hci-agent.md, so the feeder and the agent would have had to
keep the limit in sync by hand - it is exported here so both ends
import the same value."""

MAX_CLOCK_SKEW_SEC: float = 30.0
"""Advisory-skew ceiling for provider-side stamps (round 28,
review-18 items 8/17): ``ProviderLiveness.checked_at`` and
``ProviderCallStateUpdate.observed_at`` travel on provider-clock
time and are used ONLY as dedup/ordering keys - deadline
arithmetic (orphan grace, heartbeat ladder) ALWAYS uses kernel
receipt time. The ceiling is one-directional (round-28 fix,
PR #72 review): ONLY a stamp deeper into the future than
``MAX_CLOCK_SKEW_SEC`` past kernel ``now`` is normalized to
``now`` at ingestion so an NTP-skewed peer can neither arm a
permanent dedup barrier nor suspend the liveness detector (and a
``timedelta.max``-magnitude stamp can never overflow a
``stamp - last_seen`` subtraction); a stamp in the PAST is
retained VERBATIM as a dedupe/ordering key and never feeds
elapsed-time or liveness math."""

MAX_TOKEN_LEN: int = 256
"""Upper bound on a ``CallToken``/``MediaToken`` value (round 28,
review-18 item 17): the one-shot bearer credentials are opaque
server-minted strings - bounded like every other external field
that lands in wire payloads (CODING_HAZARDS rule 7)."""

MAX_ENDPOINT_LEN: int = 512
"""Upper bound on a ``media_endpoint`` URI (round 28, review-18
item 17): comfortably covers ``unix://`` paths and ``wss://``
authority+path while keeping the wire payload bounded."""

MAX_CALLER_ID_LEN: int = 64
"""Upper bound on ``caller_id``/``number`` carrier fields (round
28, review-18 item 17): E.164 tops out at 16 chars but a provider
may report a non-strict identity - the bound keeps the value
bounded without cutting a legal number."""


def clamp_provider_stamp(stamp: datetime, now: datetime) -> datetime:
    """Canonical advisory-stamp clamp for provider-side timestamps
    (round 29, SA_quant_review_1 slice-A item 4): the ONE place the
    skew rule lives, shared by every provider-fact ingestion path
    (:meth:`LineManager.note_provider_liveness`,
    :meth:`LineManager.ingest_call_state` and the
    :attr:`ProviderLiveness.checked_at` /
    :attr:`ProviderCallStateUpdate.observed_at` fields they
    consume) so the two ingress paths can never diverge.

    Rule (round 28, review-18 items 8/17): ONLY a ``stamp`` deeper
    than :data:`MAX_CLOCK_SKEW_SEC` into the FUTURE relative to
    kernel receipt time ``now`` is normalized to ``now`` - an
    NTP-skewed peer can neither arm a permanent dedup barrier nor
    suspend the liveness detector; a stamp inside the skew window
    or anywhere in the PAST is returned VERBATIM as the
    dedupe/ordering key and never feeds elapsed-time math. Both
    arguments pass through :func:`ensure_aware_utc` first - the
    same reject-naive/normalize-UTC convention every runtime
    entry point applies."""
    stamp_utc = ensure_aware_utc(stamp)
    now_utc = ensure_aware_utc(now)
    if stamp_utc - now_utc > timedelta(seconds=MAX_CLOCK_SKEW_SEC):
        return now_utc
    return stamp_utc


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
    - ``set``/``frozenset`` -> deterministically sorted list (round
      25, review-15 item 40: each element is masked FIRST, then the
      masked results are sorted by their rendered string - sorting
      the live objects would make the order depend on the raw secret
      via ``__hash__``-adjacent comparisons and could still crash on
      mixed types);
    - ``dict`` -> same mapping with KEYS masked as well (round 25,
      review-15 item 39: a ``RedactedString`` or ``Enum`` key would
      otherwise leak its raw value or crash ``json.dumps``).
    """
    if isinstance(value, RedactedString):
        return str(value)
    if isinstance(value, dict):
        return {_mask_for_log(k): _mask_for_log(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_mask_for_log(v) for v in value]
    if isinstance(value, (set, frozenset)):
        masked = [_mask_for_log(v) for v in value]
        return sorted(masked, key=lambda x: x if isinstance(x, str) else str(x))
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

    model_config = ConfigDict(
        hide_input_in_errors=True,
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
    )
    """``allow_inf_nan=False`` (round 25, review-15 item 22):
    ``float("inf")``/``float("nan")`` fail validation on EVERY model -
    an infinite or NaN ``*_sec`` knob would otherwise sail through
    ``gt=0`` comparisons and crash ``datetime + timedelta(...)``
    arithmetic downstream (rule 7 on numeric config inputs)."""

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
      never embed caller numbers or tokens. Bounded at
      ``MAX_DETAIL_LEN`` (round 27, review-17 item 63).
    - ``free_lines``: FREE alternatives returned with
      LINE_EXCLUSIVE_NOT_OWNER (contract "Intent / allocation").
    """

    reason: ErrorReason | None = None
    detail: str | None = Field(default=None, max_length=MAX_DETAIL_LEN)
    free_lines: tuple[_CheckedLineId, ...] = ()
    """Each entry goes through :func:`_check_line_id` (round 26,
    review-16 item 5): a colon-bearing/empty/overlong/``"default"``
    id can never be echoed back to the wire as a suggested FREE
    alternative."""


class PhonebridgeError(Exception):
    """Single domain error type.

    ``code`` is a verbatim ErrorCode from docs/api-contract.md;
    ``data`` carries operational detail through the typed reason
    sub-code convention (ErrorReason) plus optional context fields.
    """

    def __init__(self, code: ErrorCode, data: ErrorData | None = None) -> None:
        self.code = code
        self.data = data if data is not None else ErrorData()
        # Round 25 (review-15 item 42): ``args`` must carry the __init__
        # parameters themselves so ``copy.deepcopy``/``pickle``
        # round-trips through ``__init__`` - the old ``code.value``
        # arg left ``args`` incompatible with the signature.
        super().__init__(code, self.data)

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

_NON_GEO_REGION: str = "001"
"""phonenumbers' pseudo-region code for non-geographic numbering
plans (round 25, review-15 item 36): ``+800`` global toll-free,
``+808`` shared-cost, ``+870`` Inmarsat and friends carry no ISO
3166-1 region - ``region_code_for_number`` reports ``"001"`` for
them. The ``country`` cross-check accepts this value ONLY when the
parsed number's own region is also ``"001"``."""

_ISO_REGION_CODES: frozenset[str] = frozenset().union(
    *phonenumbers.COUNTRY_CODE_TO_REGION_CODE.values()
)
"""Every region code phonenumbers knows (round 25, review-15 item
36): this phonenumbers release exposes no ``is_valid_region_code``
helper, so the known-region set is derived as the union of
``COUNTRY_CODE_TO_REGION_CODE`` values - the same derivation the
helper itself performs upstream. A syntactically-alpha-2 code like
``"ZZ"`` that is not in this set is an UNKNOWN_COUNTRY_CODE, not a
COUNTRY_MISMATCH - keeping the two failure branches distinct lets
the kernel map them to different typed ``ErrorReason`` sub-codes."""

_VALID_NUMBER_TYPES: frozenset[str] = frozenset(
    name
    for name, value in vars(phonenumbers.PhoneNumberType).items()
    if name.isupper() and isinstance(value, int)
)
"""The phonenumbers number-type vocabulary (round 25, review-15 item
36): FIXED_LINE, MOBILE, FIXED_LINE_OR_MOBILE, VOIP, TOLL_FREE,
PREMIUM_RATE, SHARED_COST, PERSONAL_NUMBER, PAGER, UAN, VOICEMAIL,
UNKNOWN. In this phonenumbers release ``PhoneNumberType`` is a
Java-ported holder of int constants - not a Python ``Enum`` - so
the names are read off the class dict; a member outside this set
can never be produced by ``number_type()`` and is an
``allowed_number_types`` config bug."""


def validate_e164_country(number: RedactedString, country: str) -> str:
    """Pure E.164 + ISO alpha-2 cross-check (round 24, review-13 M1;
    region-membership and ``"001"`` handling added in round 25,
    review-15 item 36).

    Single domain helper for the "who may this number be" question so
    the check cannot drift between call sites: strict
    :data:`E164_PATTERN` shape gate first, then
    ``phonenumbers.parse`` + ``is_valid_number()`` + region
    cross-check. ``country`` is a cross-check ASSERTION only - never
    a parse region and never a source of prefixes.

    Rejection branches raise TYPED :class:`PhonebridgeError`
    directly (round 27, review-17 item 33 - replaces the
    string-message contract where the kernel mapped English
    ``ValueError`` text to sub-codes): each branch carries its
    ``ErrorReason`` sub-code under ``ErrorCode.INVALID_ARGUMENT``,
    so no call site can ever mis-map a branch by matching prose:

    - number not strict E.164 / unparseable ->
      ``INVALID_FORMAT_NOT_E164``;
    - ``country`` not ISO alpha-2 in shape, or alpha-2 but not a
      KNOWN ISO 3166-1 region (e.g. ``"ZZ"``) ->
      ``UNKNOWN_COUNTRY_CODE``;
    - parse succeeds but the number is not a valid subscriber
      number -> ``INVALID_NUMBER``;
    - valid number, valid region, wrong pairing -> ``COUNTRY_MISMATCH``
      (region check via ``is_valid_number_for_region``, NOT
      ``region_code_for_number() == country``, so shared calling
      codes like NANP ``+1`` never false-reject).

    Non-geographic plans (round 25 item 36): ``country == "001"`` is
    legal ONLY when ``region_code_for_number(parsed) == "001"``
    (``+800`` global toll-free and friends, otherwise unreachable
    through the alpha-2 gate). Both mixed pairings - a geographic
    number declared ``"001"`` and a ``"001"``-region number declared
    with a geographic country - are COUNTRY_MISMATCH, never
    silently coerced.

    Returns the validated raw E.164 string. Raises
    :class:`PhonebridgeError` on any rejection - because
    ``PhonebridgeError`` is NOT a ``ValueError`` subclass, pydantic
    lets it propagate out of field/model validators UNWRAPPED
    (round 27, review-17 item 33): model construction fails with the
    typed domain error itself, never a ``ValidationError`` whose
    ``errors()[]`` context would have to be re-parsed. On the
    ``call.dial`` path the kernel MAY re-code the same rejections to
    ``DIAL_REJECTED`` while preserving the typed ``data.reason``
    sub-code. ``detail`` text never embeds the number - caller
    identity is PII (rule 10)."""
    raw = number.expose()
    if not E164_PATTERN.fullmatch(raw):
        raise PhonebridgeError(
            ErrorCode.INVALID_ARGUMENT,
            ErrorData(
                reason=ErrorReason.INVALID_FORMAT_NOT_E164,
                detail="number is not strict E.164",
            ),
        )
    if country != _NON_GEO_REGION and not _ISO_ALPHA2_PATTERN.fullmatch(country):
        raise PhonebridgeError(
            ErrorCode.INVALID_ARGUMENT,
            ErrorData(
                reason=ErrorReason.UNKNOWN_COUNTRY_CODE,
                detail="country is not ISO 3166-1 alpha-2",
            ),
        )
    if country != _NON_GEO_REGION and country not in _ISO_REGION_CODES:
        raise PhonebridgeError(
            ErrorCode.INVALID_ARGUMENT,
            ErrorData(
                reason=ErrorReason.UNKNOWN_COUNTRY_CODE,
                detail="country is not a known ISO 3166-1 alpha-2 region",
            ),
        )
    try:
        parsed = phonenumbers.parse(raw, None)
    except phonenumbers.NumberParseException as exc:
        raise PhonebridgeError(
            ErrorCode.INVALID_ARGUMENT,
            ErrorData(
                reason=ErrorReason.INVALID_FORMAT_NOT_E164,
                detail="number does not parse",
            ),
        ) from exc
    if not phonenumbers.is_valid_number(parsed):
        raise PhonebridgeError(
            ErrorCode.INVALID_ARGUMENT,
            ErrorData(
                reason=ErrorReason.INVALID_NUMBER,
                detail="number is not a valid subscriber number",
            ),
        )
    region = phonenumbers.region_code_for_number(parsed)
    if country == _NON_GEO_REGION:
        # Only a genuinely non-geographic number may be declared "001".
        if region != _NON_GEO_REGION:
            raise PhonebridgeError(
                ErrorCode.INVALID_ARGUMENT,
                ErrorData(
                    reason=ErrorReason.COUNTRY_MISMATCH,
                    detail="number is not valid for the declared country",
                ),
            )
    elif region == _NON_GEO_REGION:
        # A non-geographic number may not be declared with a
        # geographic ISO country - the pairing is a mismatch.
        raise PhonebridgeError(
            ErrorCode.INVALID_ARGUMENT,
            ErrorData(
                reason=ErrorReason.COUNTRY_MISMATCH,
                detail="number is not valid for the declared country",
            ),
        )
    elif not phonenumbers.is_valid_number_for_region(parsed, country):
        raise PhonebridgeError(
            ErrorCode.INVALID_ARGUMENT,
            ErrorData(
                reason=ErrorReason.COUNTRY_MISMATCH,
                detail="number is not valid for the declared country",
            ),
        )
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


def _bounded_meta(meta: dict[str, str]) -> dict[str, str]:
    """Shared bound for provider ``meta`` maps (round 22, review-10
    MEDIUM-5; extracted for reuse on :class:`ProviderBinding` in
    round 27, review-17 item 27): an unbounded provider-controlled
    dict on a wire-facing or kernel-state model is a memory/log
    hazard - at most ``MAX_PROVIDER_META_ENTRIES`` keys, each key
    non-empty and at most ``MAX_PROVIDER_META_KEY_LEN`` chars, each
    value at most ``MAX_PROVIDER_META_VALUE_LEN`` chars."""
    if len(meta) > MAX_PROVIDER_META_ENTRIES:
        raise ValueError(f"meta has {len(meta)} entries, max {MAX_PROVIDER_META_ENTRIES}")
    for key, value in meta.items():
        if not key or len(key) > MAX_PROVIDER_META_KEY_LEN:
            raise ValueError(f"meta key must be 1..{MAX_PROVIDER_META_KEY_LEN} chars")
        if len(value) > MAX_PROVIDER_META_VALUE_LEN:
            # Round 25 (review-15 item 39): the key is external
            # input and must NOT be echoed - hide_input_in_errors
            # does not cover validator-authored messages.
            raise ValueError(f"meta value exceeds {MAX_PROVIDER_META_VALUE_LEN} chars")
    # Round 28 (review-18 item 16): return a COPY, not the inbound
    # mapping - the model then owns its map outright, so an adapter
    # mutating the dict it distilled from the wire afterwards can
    # never corrupt the stored kernel fact.
    return dict(meta)


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
    A TRANSITIVE cycle (``A -> B -> ... -> A``) is banned at the
    ``configure_line`` kernel layer (round 26, review-16 item 2;
    docs/specs/line-manager.md): the kernel MUST walk the stored
    policy chain of the target lines, bounded at
    :data:`MAX_FORWARD_HOPS` hops, and reject a chain that closes
    back onto the configured line - a model-level check is
    impossible here because the cycle spans several assignments.
    Wire config spelling:
    ``forward_via_line:<line_id>:<e164_number>:<country>``."""

    policy: Literal["forward_via_line"] = "forward_via_line"
    line_id: _CheckedLineId
    target_number: RedactedString
    target_country: str

    @model_validator(mode="after")
    def _valid_dial_target(self) -> ForwardViaLineFallback:
        """The policy carries a DIAL TARGET, not a label - validate it
        the way ``call.dial`` would (round 24, review-13 M1) so an
        invalid ``target_number``/``target_country`` pair can never
        exist inside a frozen model (including rows hydrated from
        SQLite, which bypass ``configure_line`` checks entirely).

        Uniform error shape (round 28, review-18 item 16): errors
        inside model validators surface as ``ValueError`` (wrapped
        into ``ValidationError`` by pydantic), never as
        ``PhonebridgeError`` - error-code mapping is the ADAPTER's
        job. The typed check keeps its kernel semantics via
        ``__cause__``: the raised ``ValueError`` chains the
        ``PhonebridgeError`` from :func:`validate_e164_country`,
        so an adapter that wants the sub-code (e.g.
        ``INVALID_FORMAT_NOT_E164``) can still read it off the
        cause while the generic mapping stays ``INVALID_ARGUMENT``."""
        try:
            validate_e164_country(self.target_number, self.target_country)
        except PhonebridgeError as exc:
            raise ValueError(f"forward_via_line dial target invalid: {exc.code.value}") from exc
        return self


class RecordAndTelegramFallback(ContractModel):
    """Worker policy: answer, record, post to a Telegram channel."""

    policy: Literal["record_and_telegram"] = "record_and_telegram"
    channel: _CheckedId
    """Opaque channel selector for the FallbackWorker (the
    ``record_and_telegram:<channel>`` wire spelling), constrained by
    the strict :func:`_check_id` grammar
    (``[A-Za-z0-9][A-Za-z0-9._-]*``, alphanumeric first char): a
    Telegram ``@handle`` or a numeric chat-id (``-100...``) can
    never appear on the wire - administrative config MUST use a safe
    alias the worker resolves locally (round 27, review-17 item 13
    side effect, PR #68 review)."""


class AnswerAndAnnounceFallback(ContractModel):
    """Worker policy: answer and play an announcement.

    Eligibility predicate inside ``decide_fallback`` (round 24,
    review-13 M4 - the earlier "only if an agent is registered for
    inbound" was ambiguous for an EXCLUSIVE line with a sleeping
    owner and named no default; pinned normatively round 29,
    SA_quant_review_1 slice-A item 3 -
    docs/specs/line-manager.md "Fallback eligibility is evaluated
    at decision time (round 29)"): the predicate is evaluated at
    DECISION time on live kernel state, not at ``configure_line``
    time - it cannot be model-enforced because the model cannot
    see sessions. The policy is eligible when the
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
    spelling: ``answer_and_announce:<announcement_ref>`` - the ref is
    a checked identifier (``_CheckedId``) because a ``":"`` inside it
    would corrupt the adapter's ``<policy>:<param>`` split."""

    policy: Literal["answer_and_announce"] = "answer_and_announce"
    announcement_ref: _CheckedId


FallbackPolicy = Annotated[
    NoPickupFallback
    | RejectFallback
    | ForwardViaLineFallback
    | RecordAndTelegramFallback
    | AnswerAndAnnounceFallback,
    Field(discriminator="policy"),
]
"""Discriminated union over the five documented fallback policies."""

_WORKER_FALLBACK_TYPES: tuple[type[ContractModel], ...] = (
    ForwardViaLineFallback,
    RecordAndTelegramFallback,
    AnswerAndAnnounceFallback,
)
"""Policy variants dispatched to the FallbackWorker (round 25,
review-15 item 31): they execute over the standard agent API
(ADR-01), so the kernel hands the worker a re-issued
``fallback_call_token`` inside ``event.fallback_triggered`` - the
token IS the delivery. The two CORE policies
(:class:`NoPickupFallback` / :class:`RejectFallback`) run inline
inside LineManager and never reach the worker."""


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


def heartbeat_interval_ms_from_sec(interval_sec: float) -> int:
    """Canonical ``heartbeat_interval_sec`` -> ``heartbeat_interval_ms``
    conversion (round 24c, reviewer finding on the round-24 C2 model).

    The rule is "nearest millisecond, halves up" - implemented as
    ``int(interval_sec * 1000 + 0.5)`` because Python's ``round()``
    is banker's rounding (round-half-to-even: ``round(2.5) == 2``),
    which would silently stamp 1 ms less than documented at .5-ms
    boundaries (round 27, review-17 item 37). Clamped to ``>= 1``.
    The interval itself is floored at millisecond
    granularity (``ge=0.001`` s, the wire unit) on
    :class:`LineTimeouts` and :class:`TimeoutConfig`, so the clamp only
    guards a caller that bypasses model validation; the pair together
    guarantees a stamped ``heartbeat_interval_ms >= 1`` for ANY config
    that passes validation - a truncation to zero (e.g.
    ``int(0.0005 * 1000) == 0``) can never reach
    :class:`ProviderBinding` and break its ``gt=0`` construction."""
    return max(1, int(interval_sec * 1000 + 0.5))


class LineTimeouts(ContractModel):
    """Effective per-line timeout values (round 24, review-13 C2 /
    ADR-24.2): the CONCRETIZED counterpart of the ``TimeoutConfig``
    patch model - every field carries its resolved value, never
    ``None``. The single exception is ``max_call_duration_sec``
    (round 25, review-15 item 24): ``None`` there IS the resolved
    value - "no wall-clock cap configured" - not an unpatched knob.

    Why a second model exists: adapters need effective values the
    patch model cannot express - the manager adapter answers
    ``provider.register`` with ``heartbeat_interval_ms`` and the
    admin adapter write-through-saves the full resolved set into
    ``lines.timeouts_json`` (design §4.5); ``TimeoutConfig`` holds
    ``None`` for unpatched knobs and would silently persist holes.
    ``LineAssignment.timeouts`` therefore carries this model, and
    ``ProviderBinding.heartbeat_interval_ms`` stamps
    ``heartbeat_interval_sec`` (as ms, via
    :func:`heartbeat_interval_ms_from_sec` - nearest ms, halves up,
    clamped to ``>= 1`` ms, rounds 24c/27) at register/reregister time so
    a runtime ``configure_line`` patch never retro-applies to a live
    provider session.

    Upper bound (round 25, review-15 item 22): every float knob is
    additionally capped at ``le=86400.0`` s - one day. A larger value
    is a config bug, not a knob: an effectively-infinite window would
    defer teardown bookkeeping indefinitely (rule 7), and a
    ``1e20``-style input would explode ``timedelta`` arithmetic even
    with ``allow_inf_nan`` off.

    Two heartbeat ladders (round 27, review-17 item 55): the
    hci-agent control channel carries its own transport-level
    heartbeat (``interval_ms``/``missed_limit`` in
    docs/hci-agent.md) which feeds liveness FACTS into the
    LineProvider - the binding ladder here is the KERNEL-side
    escalation of ``provider.ping`` facts. The two are deliberately
    independent: the feeder ladder detects transport death faster
    than the kernel ladder, and the kernel ladder is authoritative
    for ``event.line_health_changed`` - the feeder-side values are
    tuned per-link, not derived from these fields."""

    grant_ttl_sec: float = Field(default=5.0, gt=0, le=86400.0)
    """Deadline to consume a grant (``AcquireGranted.expires_at`` /
    ``QueueReadyEvent.expires_at``) before the lease ends
    ``grant_ttl_expired``."""
    orphan_grace_sec: float = Field(default=10.0, gt=0, le=86400.0)
    """WS-drop re-bind window - feeds
    ``ProviderBinding.orphan_deadline_at``."""
    heartbeat_interval_sec: float = Field(default=5.0, ge=0.001, le=86400.0)
    """Required ``provider.ping`` cadence for the line's binding;
    surfaced to the provider as ``heartbeat_interval_ms`` inside the
    ``provider.register``/``provider.reregister`` response. Floored
    at millisecond granularity (``ge=0.001`` s - the wire unit is
    ms, round 24c) and converted via
    :func:`heartbeat_interval_ms_from_sec`, so no valid value can
    truncate to a zero-cadence ``heartbeat_interval_ms``."""
    heartbeat_degraded_after: int = Field(default=2, ge=1)
    """Missed pings before the binding's health reports degraded -
    the first rung of the liveness escalation ladder, surfaced via
    ``event.line_health_changed`` (round 25, review-15 item 15)."""
    heartbeat_down_after: int = Field(default=3, ge=1)
    """Missed pings before the binding is declared UNREACHABLE -
    MUST strictly exceed ``heartbeat_degraded_after`` (unreachable
    is the terminal escalation of degraded, not a parallel state;
    the teardown follows design §5.4)."""
    fallback_accept_ttl_sec: float = Field(default=10.0, gt=0, le=86400.0)
    """Deadline for the FallbackWorker to ``call.accept`` a re-issued
    ``fallback_call_token`` - bounds
    ``FallbackTriggeredEvent.expires_at``."""
    ring_drain_ttl_sec: float = Field(default=60.0, gt=0, le=86400.0)
    """Bound on the ring-drain wait (round 25, review-15 item 16;
    design §5.2 step 6 "ring-drain rule"): after the accept
    window/fallback decision emits ``event.missed_call`` the line
    stays ``RINGING_LOCKED`` while the physical call still rings on
    the modem - until the provider reports the ring's
    ``disconnected`` or this TTL lapses, whichever first (~60 s
    default). Without it the FIFO queue could hand a still-ringing
    line to the next ``call.dial``, and a provider
    ``state_snapshot`` would re-register the same physical ring as
    a NEW ``incoming_call``."""
    max_call_duration_sec: float | None = Field(default=None, gt=0, le=86400.0)
    """Optional wall-clock cap on a single call (round 25,
    review-15 item 24; design §10, §5.3): ``None`` is a legitimate
    EFFECTIVE value meaning "no cap configured" - the one field
    where ``None`` is the resolved state, not an unpatched knob.
    When set it bounds every call's duration on the line and caps
    the ``record_and_telegram`` fallback's recording length."""

    @model_validator(mode="after")
    def _heartbeat_thresholds_ordered(self) -> LineTimeouts:
        """Enforce ``heartbeat_down_after > heartbeat_degraded_after``:
        an inverted pair would mark the binding UNREACHABLE before it
        could ever appear degraded on the liveness axis."""
        if self.heartbeat_down_after <= self.heartbeat_degraded_after:
            raise ValueError("heartbeat_down_after must exceed heartbeat_degraded_after")
        return self

    def apply_patch(self, patch: TimeoutConfig) -> LineTimeouts:
        """Atomically merge a :class:`TimeoutConfig` PATCH into these
        effective values (round 24b, residual review finding on the
        round-24 C2 model).

        The merge is a full ``model_validate`` of the merged dict -
        every ``None`` patch field keeps the stored value, every
        non-``None`` field replaces it - so ALL invariants are
        re-checked against the RESULT, not against the patch alone.
        This closes the single-field hole: a lone
        ``heartbeat_degraded_after=4`` is a legal
        :class:`TimeoutConfig` in isolation yet inverts the
        heartbeat ladder against a stored ``heartbeat_down_after=3``.
        ``LineAdminKernel.configure_line`` MUST merge through this
        method so such a violation surfaces as a mapped
        ``PhonebridgeError(ErrorCode.INVALID_ARGUMENT)`` instead of
        silently hydrating invalid effective values or leaking a raw
        ``ValidationError`` at the adapter.

        The operation is ATOMIC: either a fully-validated
        :class:`LineTimeouts` is returned or the error is raised -
        the stored values are never half-patched. An all-``None``
        patch re-validates and returns the same effective values.
        On failure ``ErrorData.detail`` joins ALL validation
        failures with ``"; "`` in ``"field_name: msg"`` form (round
        24c for the join-all rule - first-error-only would hide a
        second broken knob; round 25, review-15 item 42 for the
        field-path prefix - a bare msg does not name the broken knob).
        Only the pydantic ``loc`` FIELD NAME and ``msg`` strings are
        echoed - ``loc`` is a schema path, never raw input, so this
        stays PII-free under ``hide_input_in_errors`` (rule 10). A
        model-level failure carries no loc and renders as the bare
        msg.

        ``clear_max_call_duration`` (round 25, review-15 item 24) is
        a CONTROL flag, not a stored knob: it is consumed HERE and
        never reaches the merged dict - ``True`` writes an explicit
        ``max_call_duration_sec=None`` (uncapped) into the merge,
        the wire-level "disable the cap" the ``None``-means-
        unchanged convention cannot express (same shape as the
        ``idle_revoke_after``/``clear_idle_revoke_after`` pair on
        ``configure_line``)."""
        patch_values = patch.model_dump(exclude_none=True)
        if patch_values.pop("clear_max_call_duration", None):
            patch_values["max_call_duration_sec"] = None
        merged = {**self.model_dump(), **patch_values}
        try:
            return LineTimeouts.model_validate(merged)
        except ValidationError as exc:
            detail = (
                "; ".join(
                    f"{'.'.join(str(part) for part in err['loc'])}: {err['msg']}"
                    if err.get("loc")
                    else err["msg"]
                    for err in exc.errors()
                )
                or "invalid timeouts patch"
            )
            if len(detail) > MAX_DETAIL_LEN:
                # Round 27 (review-17 item 63): ``ErrorData.detail``
                # is bounded, so a many-field failure is truncated to
                # the cap instead of re-raising a ValidationError -
                # ``apply_patch`` must always surface the typed
                # INVALID_ARGUMENT error.
                detail = detail[: MAX_DETAIL_LEN - 3] + "..."
            raise PhonebridgeError(
                ErrorCode.INVALID_ARGUMENT,
                ErrorData(detail=detail),
            ) from exc


class LineSummary(ContractModel):
    """Agent-facing line listing row (wire: call.get_lines).

    ``state`` is the call-occupancy axis; ``liveness`` (round 25,
    review-15 item 15) is the SEPARATE provider-health axis - the
    line's provider health as last reported via
    ``event.line_health_changed``/``note_provider_liveness``.
    ``UNBOUND`` is the default for unbound/pool lines (round 26,
    review-16 item 13): no provider attached means there is nothing
    to be healthy or sick - REACHABLE was misleading on a
    provider-less row.

    ``provider_meta`` was removed from this agent-facing row (round
    28, review-18 item 13): provider hardware metadata is a
    privileged fact about the physical dongle - it lives on the
    admin-scoped :class:`AdminLineView` only, so a plain
    ``call.get_lines`` can never leak the provider's declared
    model/IMEI-family details to an unrelated agent."""

    line_id: _CheckedLineId
    caps: tuple[Intent, ...]
    """The line's TELECOM capability set as the agent sees it - while
    a provider is attached it mirrors the live ``ProviderBinding.caps``
    (round 25, review-15 item 41); an unbound/pool line has NO binding
    and legitimately reports an empty tuple - its claimability is a
    ``LineAssignment.claimable``/``mode`` fact, not a cap.
    ``Intent.EXCLUSIVE_LINE`` can never ride the listing either way
    (exclusivity AVAILABILITY is what the
    ``get_lines(intent=EXCLUSIVE_LINE)`` filter answers separately)."""
    state: LineState
    liveness: LivenessState = LivenessState.UNBOUND
    profile: str | None = Field(default=None, min_length=1, max_length=MAX_PROFILE_LEN)
    """Bounded when present (round 26, review-16 item 4): a non-empty
    label of at most :data:`MAX_PROFILE_LEN` chars - the listing
    mirror shares the ingress bound so a stored profile can never
    become unlistable.

    Resolution rule (round 27, review-17 items 58/80): the row
    reports ``binding.profile`` while a live provider binding is
    attached, and falls back to ``assignment.profile`` - the
    administrative line label from the stored config - when the line
    is UNBOUND (no binding exists to override the stored label)."""
    assignment_id: _CheckedAssignmentId | None = None
    """Owner-scoped self-identification (round 27, review-17 item 2):
    the kernel fills this in ONLY on rows for lines the calling
    agent owns EXCLUSIVELY - it is the contractual way to learn
    one's own ``assignment_id`` when ``event.assignment_granted``
    was emitted before the session connected (or was missed). It is
    ``None`` on SHARED_POOL lines and on other agents' lines - a
    listing must never leak a foreign durable-binding id."""

    @field_validator("caps")
    @classmethod
    def _caps_are_telecom_only(cls, caps: tuple[Intent, ...]) -> tuple[Intent, ...]:
        """The EXCLUSIVE_LINE ban of ``_provider_caps_check`` WITHOUT
        its non-empty rule (round 25, review-15 item 41): the caps
        mirror the live ``ProviderBinding`` while attached, and an
        unbound/pool line has no binding - it reports ``()``, because
        claimability is a ``LineAssignment`` fact, not a capability."""
        if Intent.EXCLUSIVE_LINE in caps:
            raise ValueError(
                "exclusive_line is not a provider capability - "
                "exclusivity lives on LineAssignment.claimable/mode"
            )
        return caps

    @model_validator(mode="after")
    def _caps_iff_bound(self) -> LineSummary:
        """``caps`` is non-empty IFF ``liveness`` differs from
        UNBOUND (round 26, review-16 item 13): a bound provider
        always contributes non-empty caps, and a provider-less line
        reports neither caps nor health - the two columns can never
        contradict each other on the listing.

        UNBOUND does NOT force ``state == FREE`` (round 27, review-17
        item 9 - replaces the round-26 same-item follow-up): the
        teardown window is real on the listing. When a binding dies
        mid-call the provider facts disappear instantly
        (``liveness=UNBOUND``, ``caps=()``) while call occupancy is
        still draining - a lease still in BUSY, or a ring held
        ``RINGING_LOCKED`` until ``ring_drain_ttl_sec`` expiry.
        Forbidding the pair made the legal transitional row
        unconstructible; the row may report the occupancy truth
        until teardown completes."""
        if (self.caps == ()) != (self.liveness is LivenessState.UNBOUND):
            raise ValueError("caps is non-empty iff liveness is not unbound")
        return self


class AdminLineView(ContractModel):
    """Admin-scoped line row (round 27, review-17 item 3): the
    ``admin.list_lines`` result - a deliberate superset
    of :class:`LineSummary` that exposes the durable and call ids
    the agent listing hides by design.

    ``assignment_id``/``owner_agent_id`` are admin-visible here
    (the admin surface needs them to target ``revoke_assignment``),
    unlike ``LineSummary.assignment_id``, which is filled ONLY on
    the calling agent's own rows. ``lease_id`` names the live call
    lease when the line is BUSY; ``queue_position`` is the global
    FIFO rank of the HEAD ticket queued for this line (``None``
    when nothing waits) - same single-ordering rule as
    :attr:`AcquireQueued.position` (item 48). The same
    ``caps``<->``liveness`` invariant as :class:`LineSummary`
    applies, and UNBOUND may still coexist with a non-FREE
    ``state`` inside the teardown window (item 9)."""

    line_id: _CheckedLineId
    caps: tuple[Intent, ...]
    state: LineState
    liveness: LivenessState = LivenessState.UNBOUND
    assignment_id: _CheckedAssignmentId | None = None
    """The DURABLE ``LineAssignment`` id when one exists - the
    provider ``ProviderBinding`` id is reported separately on
    ``admin.list_leases``/``list_provider_bindings`` (two object
    spaces share the ``AssignmentId`` tag, round 20)."""
    owner_agent_id: _CheckedAgentId | None = None
    lease_id: _CheckedLeaseId | None = None
    queue_position: int | None = Field(default=None, ge=1)
    profile: str | None = Field(default=None, min_length=1, max_length=MAX_PROFILE_LEN)
    """Same profile resolution rule as :class:`LineSummary`
    (round 28, review-18 item 13): ``binding.profile`` while a live
    provider binding is attached, else ``assignment.profile`` - the
    admin listing shows what the stored label actually resolved to,
    bounded by :data:`MAX_PROFILE_LEN`."""
    provider_meta: dict[str, str] = Field(default_factory=dict)
    """Live binding metadata mirror - the admin-scoped home of the
    field removed from :class:`LineSummary` (round 28, review-18
    item 13): filled with ``binding.meta`` while a live provider
    binding is attached, empty for UNBOUND lines. The
    ``_bounded_meta`` invariant holds, so a row can never carry
    more than the register ingress allowed."""

    @field_validator("provider_meta")
    @classmethod
    def _provider_meta_bounds(cls, meta: dict[str, str]) -> dict[str, str]:
        """Same ``_bounded_meta`` invariant as the register ingress
        (round 28, review-18 item 13) - the admin listing mirror
        never exceeds what ``provider.register`` accepted."""
        return _bounded_meta(meta)

    @field_validator("caps")
    @classmethod
    def _caps_are_telecom_only(cls, caps: tuple[Intent, ...]) -> tuple[Intent, ...]:
        """Same telecom-only rule as :class:`LineSummary` (round 27,
        review-17 item 3): ``exclusive_line`` never rides a
        listing row."""
        if Intent.EXCLUSIVE_LINE in caps:
            raise ValueError(
                "exclusive_line is not a provider capability - "
                "exclusivity lives on LineAssignment.claimable/mode"
            )
        return caps

    @model_validator(mode="after")
    def _caps_iff_bound(self) -> AdminLineView:
        """Same caps<->liveness pairing as :class:`LineSummary`
        (round 27, review-17 item 3), without the unbound->FREE
        corollary (item 9)."""
        if (self.caps == ()) != (self.liveness is LivenessState.UNBOUND):
            raise ValueError("caps is non-empty iff liveness is not unbound")
        return self


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
    ``_closed_assignments`` LRU ring (cap
    :data:`MAX_CLOSED_ASSIGNMENTS`, same shape as the
    ``_closed_leases`` ring capped at :data:`MAX_CLOSED_LEASES`,
    round 27, review-17 item 12) so a late retry resolves as an
    idempotent success for its past owner instead.

    Shared-ring rationale (round 29, SA_quant_review_1 slice-A item
    5): ``_closed_assignments`` is ONE bounded ring shared by BOTH
    assignment-id object spaces (``LineAssignment`` agent-ownership
    records AND ``ProviderBinding`` provider attachments) - the
    same ring the :meth:`LineAdminKernel.revoke_assignment`
    docstring resolves superseded provider-binding ids through.
    Sharing is safe precisely because the kernel MUST mint
    ``assignment_id`` values unique ACROSS both object spaces
    (round 20, review rule - a collision is a defect): an id found
    in the ring can only ever have belonged to one object type, so
    the ring can never mis-resolve a superseded
    ``LineAssignment`` id as a ``ProviderBinding`` fact or vice
    versa. The mint-uniqueness invariant is what makes the single
    ring correct - an implementation that minted the two spaces
    independently would break it.
    """

    assignment_id: _CheckedAssignmentId
    line_id: _CheckedLineId
    owner_agent_id: _CheckedAgentId | None = None
    profile: str | None = Field(default=None, min_length=1, max_length=MAX_PROFILE_LEN)
    """Administrative line label from the stored config (round 27,
    review-17 items 58/80): provisioning-side metadata - NOT the
    provider-declared profile. ``LineSummary.profile`` resolves
    ``binding.profile`` while a live binding is attached and falls
    back to this stored label otherwise."""
    mode: AssignmentMode
    claimable: bool
    accept_window_sec: float = Field(default=30.0, gt=0, le=86400.0)
    """Ring accept window (``IncomingCallEvent.expires_at``); one-day
    ceiling like every float time knob (round 25, review-15 item
    22)."""
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
    """ALERT-ONLY idle deadline (round 10c); strictly positive when
    set - ``None`` means "no idle alert configured". Zero/negative
    values are rejected below (round 25, review-15 item 22): a
    non-positive window would fire ``event.idle_alert`` instantly and
    on every ``tick`` thereafter. Disabling a previously set window
    goes through ``configure_line(clear_idle_revoke_after=True)``,
    not through a zero patch. The value is also capped at
    ``timedelta(seconds=86400)`` (round 28, review-18 item 11): a
    day is the longest meaningful alert-only window, and a
    ``timedelta.max``-scale value would overflow the
    ``last_activity_at + idle_revoke_after`` arithmetic at
    hydration time."""

    @field_validator("allowed_number_types")
    @classmethod
    def _allowed_number_types_known(cls, types: frozenset[str]) -> frozenset[str]:
        """Validate members against the phonenumbers type vocabulary
        :data:`_VALID_NUMBER_TYPES` (round 25, review-15 item 36): a
        typo'd or invented name can never match a ``number_type()``
        result - it would silently make the allowlist dead config -
        and an EMPTY allowlist would reject every dial as
        UNSUPPORTED_NUMBER_TYPE. The model is authoritative;
        ``configure_line`` applies the same check on the admin
        ingress."""
        if not types:
            raise ValueError("allowed_number_types must not be empty")
        unknown = types - _VALID_NUMBER_TYPES
        if unknown:
            # Count, never the names (rule 10): the set is external
            # config input and validator-authored messages are not
            # covered by hide_input_in_errors.
            raise ValueError(
                f"allowed_number_types carries {len(unknown)} "
                "name(s) outside the phonenumbers type vocabulary"
            )
        return types

    @field_validator("owner_agent_id")
    @classmethod
    def _owner_is_never_system(cls, owner: str | None) -> str | None:
        """A durable owner can never be a kernel-internal identity
        (round 27, review-17 item 14): ``system:*`` names service
        roles such as :data:`SYSTEM_FALLBACK_WORKER` that hold no
        agent session, so an assignment granted to one could never
        be exercised or released - and a hydrated row must be
        rejected just like an ``assign_line`` request."""
        if owner is not None and is_system_agent_id(owner):
            raise ValueError("owner_agent_id must not be a system:* identity")
        return owner

    @model_validator(mode="after")
    def _validate_assignment_invariants(self) -> LineAssignment:
        """Enforce the model-boundary invariants (round 19 owner⇔mode;
        round 24 review-13 M1 self-loop; round 25 review-15 item 22
        idle window): ``exclusive`` requires an owner, ``shared_pool``
        forbids one - contradictory states cannot be constructed - a
        ``forward_via_line`` policy can never name its own line as
        the dial-through target (that would bridge the ring onto
        itself; the check used to live only in ``configure_line``,
        which DB hydration bypasses), and a set
        ``idle_revoke_after`` must be strictly positive (``None`` is
        the disabled state; ``<= timedelta(0)`` would fire the
        alert-only escalation instantly and continuously) and bounded
        at 86400 s (round 28, review-18 item 11 - the day cap)."""
        if self.mode is AssignmentMode.EXCLUSIVE and self.owner_agent_id is None:
            raise ValueError("exclusive assignment requires owner_agent_id")
        if self.mode is AssignmentMode.SHARED_POOL and self.owner_agent_id is not None:
            raise ValueError("shared_pool assignment cannot carry owner_agent_id")
        if (
            isinstance(self.fallback_policy, ForwardViaLineFallback)
            and self.fallback_policy.line_id == self.line_id
        ):
            raise ValueError("forward_via_line cannot target the same line_id")
        if self.idle_revoke_after is not None and self.idle_revoke_after <= timedelta(0):
            raise ValueError("idle_revoke_after must be positive or None")
        if self.idle_revoke_after is not None and self.idle_revoke_after > timedelta(seconds=86400):
            raise ValueError("idle_revoke_after must not exceed 86400 s")
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

    ``liveness`` walks the heartbeat ladder ``reachable -> degraded
    -> unreachable`` (round 28, review-18 item 1): ``tick`` compares
    kernel-receipt elapsed silence against the stamped
    ``heartbeat_interval_ms`` scaled by ``heartbeat_degraded_after``
    and ``heartbeat_down_after`` - the thresholds are frozen on the
    binding at register time so a ``configure_line`` patch can never
    move a deadline under a live session. ``UNBOUND`` can never be
    stored here: a binding existing at all means a provider is
    attached - UNBOUND is the listing-only absence of this record."""

    assignment_id: _CheckedAssignmentId
    line_id: _CheckedLineId
    caps: tuple[Intent, ...]
    """Provider-declared TELECOM capabilities - ``dial_out`` /
    ``receive_inbound`` only (round 24, review-13 M3): availability
    of durable exclusivity is a ``LineAssignment.claimable``/``mode``
    property, never a hardware capability, so
    ``Intent.EXCLUSIVE_LINE`` is rejected here the same way it is on
    the ``provider.register``/``provider.capability_update``
    ingress."""
    profile: str | None = Field(default=None, min_length=1, max_length=MAX_PROFILE_LEN)
    """Bounded when present (round 27, review-17 items 27/46): the
    same ``1..MAX_PROFILE_LEN`` rule as
    :attr:`ProviderRegistration.profile` - the binding carries the
    label the provider declared so the ``LineSummary`` mirror can
    never hold an unlistable value."""
    meta: dict[str, str] = Field(default_factory=dict)
    """Opaque provider metadata carried over from the registration
    (round 27, review-17 item 27): ``register_provider`` MUST copy
    the incoming ``ProviderRegistration.meta`` verbatim so admin
    diagnostics can inspect it; the same ``_bounded_meta``
    invariant applies. Kept as ``dict`` for wire parity - treat
    contents as immutable by convention.

    Ownership rule (round 28, review-18 item 16): the binding holds
    the kernel's OWN copy - ``_bounded_meta`` returns a fresh dict
    at validation, so a caller mutating the dict it passed to the
    constructor afterwards cannot corrupt the stored mirror (and
    an adapter MUST NOT keep a mutable handle into this map)."""
    liveness: LivenessState = LivenessState.REACHABLE
    """Provider-health fact on the heartbeat ladder (round 28,
    review-18 item 1): the kernel computes ``reachable ->
    degraded -> unreachable`` from missed-ping elapsed time; the
    reported-fact states ``ws_disconnected`` (orphan grace) and the
    derived listing state ``unbound`` can never ride a stored
    binding's ladder position by provider report alone - a binding
    itself proves the provider is attached."""
    heartbeat_interval_ms: int = Field(default=5000, gt=0, le=86_400_000)
    """Ping cadence negotiated for THIS provider session (round 24,
    review-13 C2): stamped from the line's effective
    ``LineTimeouts.heartbeat_interval_sec`` inside
    ``register_provider`` via
    :func:`heartbeat_interval_ms_from_sec` - nearest ms, halves up,
    clamped to ``>= 1`` (rounds 24c/27; ``heartbeat_interval_sec`` is
    floored at ``ge=0.001`` s so a valid config can never stamp 0) -
    and returned to the adapter so the
    ``provider.register``/``provider.reregister`` response can carry
    the wire ``heartbeat_interval_ms``. A ``configure_line`` patch to
    ``heartbeat_interval_sec`` takes effect on the NEXT
    ``provider.reregister`` - it is never enforced retroactively
    against the interval stamped here (round 23, review-12 H6:
    shrinking it mid-session would kill a healthy provider on a
    false ping timeout). Upper bound ``le=86_400_000`` ms (round 29,
    SA_quant_review_1 slice-A item 8): the exact millisecond
    counterpart of ``heartbeat_interval_sec <= 86400`` on
    :class:`LineTimeouts`/``TimeoutConfig`` - a hydrated
    ``ProviderBinding`` bypassing
    :func:`heartbeat_interval_ms_from_sec` can no longer carry an
    interval the config model would have rejected."""
    heartbeat_degraded_after: int = Field(ge=1)
    """Missed-ping threshold before the binding's health reports
    degraded - STAMPED at register/reregister from the line's
    effective ``LineTimeouts.heartbeat_degraded_after`` (round 25,
    review-15 item 32), exactly like ``heartbeat_interval_ms``: the
    value is frozen for the life of this binding and a
    ``configure_line`` patch never retro-applies mid-session. No
    default: the kernel MUST stamp the line's effective value
    explicitly."""
    heartbeat_down_after: int = Field(ge=1)
    """Missed-ping threshold before the binding is declared
    UNREACHABLE - same stamping rule as
    ``heartbeat_degraded_after``; MUST strictly exceed it (the
    escalation ladder has no reachable rung above unreachable)."""
    connected_at: AwareDatetime
    last_seen_at: AwareDatetime
    orphan_deadline_at: AwareDatetime | None = None
    """Orphan-grace deadline - in-memory only (round 27, review-17
    item 76): bindings are NOT durable state, so a kernel restart
    discards every deadline together with the sockets they guarded
    - the single-process kernel owns the provider control sockets,
    and a restart kills them, so no zombie binding can survive
    into the next process anyway."""

    @field_validator("caps")
    @classmethod
    def _caps_are_telecom_only(cls, caps: tuple[Intent, ...]) -> tuple[Intent, ...]:
        return _provider_caps_check(caps)

    @field_validator("meta")
    @classmethod
    def _meta_bounds(cls, meta: dict[str, str]) -> dict[str, str]:
        """Same ``_bounded_meta`` invariant as the register ingress
        (round 27, review-17 item 27) - enforced on the stored
        binding so a carried-over map can never be unbounded."""
        return _bounded_meta(meta)

    @model_validator(mode="after")
    def _binding_invariants(self) -> ProviderBinding:
        """Round 25 (review-15 items 32/41): the stamped heartbeat
        ladder keeps ``down > degraded`` - the same invariant as
        :class:`LineTimeouts`, enforced AGAIN on the binding because
        the stamped pair is what ``tick(now)`` compares missed-ping
        counts against (an inverted stamp would declare the binding
        UNREACHABLE before it could ever appear degraded); and
        ``orphan_deadline_at`` may exist ONLY while
        ``liveness == WS_DISCONNECTED`` - a REACHABLE or UNREACHABLE
        binding carries no orphan-grace window."""
        if self.heartbeat_down_after <= self.heartbeat_degraded_after:
            raise ValueError("heartbeat_down_after must exceed heartbeat_degraded_after")
        if (
            self.orphan_deadline_at is not None
            and self.liveness is not LivenessState.WS_DISCONNECTED
        ):
            raise ValueError("orphan_deadline_at requires liveness ws_disconnected")
        return self


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

    lease_id: _CheckedLeaseId
    line_id: _CheckedLineId
    owner_agent_id: _CheckedAgentId
    session_id: _CheckedSessionId
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
    audio_ready_received: bool = False
    """Set by the kernel inside ``ingest_audio_ready`` (round 27,
    review-17 item 53): records that the provider's local BT SCO
    path was reported up for this lease. Consumed by the
    ``send_dtmf`` gate - in-band tones before the SCO channel is up
    are silently lost - and by diagnostics distinguishing "no media
    yet" from "media up"."""
    profile: str | None = Field(default=None, min_length=1, max_length=MAX_PROFILE_LEN)
    """The ``acquire(profile=...)`` label the agent asked for, saved
    on the lease (round 28, review-18 item 13): the acquire contract
    says the chosen profile rides the grant to the dial command and
    the admin listing, so it MUST be recorded here rather than
    recomputed - ``admin.list_leases`` reports it directly off the
    stored field. On a QUEUED acquire the lease inherits the value
    verbatim from :attr:`QueueTicket.profile` (round-28 fix,
    PR #72 review) - the label survives queueing unchanged. Bounded
    like every profile carrier by :data:`MAX_PROFILE_LEN`."""

    @model_validator(mode="after")
    def _lease_invariants(self) -> CallLease:
        """Round 25 (review-15 item 41): a lease is an EPHEMERAL
        dial/receive hold - ``Intent.EXCLUSIVE_LINE`` can never ride
        it (durable exclusivity lives on ``LineAssignment`` only,
        granted via claim+approval or admin assign; a lease holding
        it would smuggle a durable claim into a socket-bound
        record). And ``answered_at`` is an activity stamp - it can
        never precede the lease's own ``created_at``."""
        if self.intent is Intent.EXCLUSIVE_LINE:
            raise ValueError("a CallLease can never hold exclusive_line intent")
        if self.answered_at is not None and self.answered_at < self.created_at:
            raise ValueError("answered_at must not precede created_at")
        return self


class QueueTicket(ContractModel):
    """FIFO queue entry for a queued acquire (§3.4).

    FIFO guarantee: ``seq`` is a strictly monotonic sequence number
    issued from ONE global counter across all queue scopes (round 19
    fairness rule): when line L frees, the grant goes to the
    lowest-seq eligible ticket regardless of scope (per-line and pool
    tickets compete in a single order - no starvation). The counter
    is minted at acquire-INGRESS for EVERY ``call.acquire``
    (round 25, review-15 item 7): immediate grants consume a seq too,
    so a preempted immediate grant can re-materialize a ticket that
    reuses its ORIGINAL ingress seq - by construction lower than any
    later-queued ticket, which is why head insertion never needs a
    negative or fractional seq. Tickets order
    by ``seq`` (``__lt__`` below), which IS the mandatory FIFO ordering
    of docs/api-contract.md "Queue semantics". The counter is a
    monotonic ACCOUNTING sequence, not a gapless one (round 27,
    review-17 item 49): a fresh ``seq`` is consumed by every
    ``call.acquire`` ingress call - including immediate grants,
    refusals and deduplicated retries - so gaps between the seq
    values of live tickets are expected and carry no meaning.

    ``position``/``QueueUpdatedEvent.position`` is the ticket's
    GLOBAL FIFO rank among ALL eligible tickets (round 27,
    review-17 item 48): it is computed over the single shared
    ordering across per-line and pool scopes - never a per-line
    rank - so two tickets on different lines never share a
    position.

    Tickets are
    socket-bound - disconnect kills them;
    the queue itself must be bounded (rule 7: queue overflow reports
    RESOURCE_EXHAUSTED/QUEUE_FULL reason detail - round 20 - never an
    unbounded park). The bounds are the contract constants
    :data:`MAX_TICKETS_PER_SESSION` and
    :data:`MAX_QUEUE_TICKETS_TOTAL` (round 25, review-15 item 30) -
    per-session and global respectively; sibling bound
    :data:`MAX_LEASES_PER_SESSION` caps the live leases the queue
    can grant into. ``idempotency_key`` (round 20) scopes the
    retry-dedup: identical retries carrying the SAME non-empty
    ``(session_id, idempotency_key)`` collapse into one ticket;
    calls without a key always create separate tickets so a
    multi-call agent can hold N parallel acquires. The dedup table
    lives strictly inside the LIVE session (round 27, review-17
    item 22): ``drop_session`` discards the session's
    ``(session_id, idempotency_key)`` entries together with its
    tickets, so a key can never dedup across a reconnect.
    """

    ticket_id: _CheckedTicketId
    seq: int = Field(ge=0)
    session_id: _CheckedSessionId
    intent: Intent
    preferred_line_id: _CheckedLineId | None = None
    enqueued_at: AwareDatetime
    idempotency_key: str | None = Field(
        default=None, min_length=1, max_length=MAX_IDEMPOTENCY_KEY_LEN
    )
    """Bounded when present (round 23, review-12 M2): a key is either
    absent (no dedup - every call is independent) or a non-empty
    string of at most ``MAX_IDEMPOTENCY_KEY_LEN`` chars; an empty or
    overlong key is rejected as INVALID_ARGUMENT at the boundary."""
    profile: str | None = Field(default=None, min_length=1, max_length=MAX_PROFILE_LEN)
    """The ``acquire(profile=...)`` label recorded on the ticket
    (round-28 fix, PR #72 review - audit item 20 named BOTH
    :class:`CallLease` and QueueTicket, and the queued path was the
    gap): the per-call label MUST survive queueing, so the kernel
    saves the accepted value here and the :class:`CallLease` minted
    when the ticket is granted INHERITS it verbatim. Same
    ``1..MAX_PROFILE_LEN`` bound as :attr:`CallLease.profile` -
    ``None`` means the acquire carried no label."""

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
    line_id: _CheckedLineId
    lease_id: _CheckedLeaseId
    expires_at: AwareDatetime


class AcquireQueued(ContractModel):
    """Queued acquire: FIFO ticket issued; grant arrives later as a
    QueueReadyEvent which the agent must consume within grant TTL."""

    granted: Literal[False] = False
    queued: Literal[True] = True
    ticket_id: _CheckedTicketId
    position: int = Field(ge=1)
    """Global FIFO rank among ALL eligible tickets (round 27,
    review-17 item 48): the rank is computed over the single shared
    ordering across per-line and pool scopes, never per-line - see
    :class:`QueueTicket`."""


class AcquireRefused(ContractModel):
    """Clean refusal: nothing available right now. This is a ROUTINE
    result, not an exception - the client retries itself (no
    server-side parked requests, round 8)."""

    granted: Literal[False] = False
    free_lines: tuple[_CheckedLineId, ...] = ()
    """Suggested FREE alternatives - each entry goes through
    :func:`_check_line_id` (round 26, review-16 item 5), same rule
    as :attr:`ErrorData.free_lines`."""


def _acquire_discriminator(value: Any) -> str:
    """Discriminate the three acquire outcomes on (granted, queued)
    - STRICT (round 25, review-15 item 38): ``granted`` must be an
    explicit bool. ``granted is True`` -> ``"granted"``;
    ``granted is False and queued is True`` -> ``"queued"``;
    ``granted is False`` -> ``"refused"``. ANY other shape - a
    missing ``granted`` key, a non-bool value - raises ``ValueError``:
    the previous unconditional ``refused`` fallback let an empty
    ``{}`` silently validate as ``AcquireRefused``, turning a
    malformed payload into a routine "nothing free" answer."""

    def _get(key: str) -> Any:
        if isinstance(value, dict):
            return value.get(key)
        return getattr(value, key, None)

    granted = _get("granted")
    if granted is True:
        return "granted"
    if granted is False:
        if _get("queued") is True:
            return "queued"
        return "refused"
    raise ValueError("acquire result requires an explicit granted: bool")


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

    lease_id: _CheckedLeaseId
    media_endpoint: str = Field(min_length=1, max_length=MAX_ENDPOINT_LEN)
    """The AGENT-side media endpoint (round 25, review-15 item 6):
    the address the calling agent connects its media socket to -
    the ``agent_media_endpoint`` half of the lease's
    :class:`MediaCredentials` pair. The provider receives a
    DIFFERENT endpoint inside its outbox command."""
    media_token: _CheckedMediaToken


class AcceptResult(ContractModel):
    """call.accept result: atomic lease + answer + media attach."""

    lease_id: _CheckedLeaseId
    line_id: _CheckedLineId
    media_endpoint: str = Field(min_length=1, max_length=MAX_ENDPOINT_LEN)
    """The AGENT-side media endpoint (round 25, review-15 item 6) -
    same split as :attr:`DialResult.media_endpoint`."""
    media_token: _CheckedMediaToken


class MediaReattachResult(ContractModel):
    """call.reattach_media result (round 25, review-15 item 9):
    fresh AGENT-side media credentials for a LIVE lease after the
    agent's media socket dropped.

    Media tokens are one-shot: a dropped agent media socket burns
    its token and the lease itself stays alive (the call continues -
    only the audio path detached). The kernel mints a NEW
    ``(agent_media_endpoint, media_token)`` pair entry for the same
    ``lease_id`` so the agent can re-authenticate on
    ``media.auth{role: "agent"}``; the provider-side pair entry is
    untouched (its own detach is signalled provider-side - see
    :class:`ProviderReattachMediaCommand`). The reissued pair
    SUPERSEDES the prior agent-side pair (round 26, review-16 item
    8): the old ``media_token`` becomes invalid at issue time - a
    lease holds a single active pair per role - bounding the
    replay window to the reissue instant rather than the full pair
    TTL."""

    lease_id: _CheckedLeaseId
    media_endpoint: str = Field(min_length=1, max_length=MAX_ENDPOINT_LEN)
    """The AGENT-side media endpoint - same split semantics as
    :attr:`DialResult.media_endpoint`."""
    media_token: _CheckedMediaToken


class ProviderMediaReattachResult(ContractModel):
    """provider.reattach_media result (round 27, review-17 item 4):
    the provider-side mirror of :class:`MediaReattachResult` - the
    fresh PROVIDER-role media credentials minted by
    :meth:`LineManager.reattach_provider_media` when the provider
    asks for a re-pair on a live lease (e.g. its media socket
    dropped while the control channel stayed up, outside the
    register/reregister reissue path)."""

    media_endpoint: str = Field(min_length=1, max_length=MAX_ENDPOINT_LEN)
    """The PROVIDER-side media endpoint - same split semantics as
    :attr:`ProviderDialCommand.media_endpoint`."""
    media_token: _CheckedMediaToken


class ProviderMediaReattachRequest(ContractModel):
    """provider.reattach_media ingress model (round 27, review-17
    item 4): the provider's request for fresh media credentials on
    a live lease, wire params ``{line_id, assignment_id,
    lease_id}``. The ``assignment_id`` names the binding the
    request rides on - same binding-id discipline as
    :class:`ProviderLiveness` - and ``lease_id`` must resolve to a
    live lease owned by that binding."""

    line_id: _CheckedLineId
    assignment_id: _CheckedAssignmentId
    lease_id: _CheckedLeaseId


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
    ``CLAIM_NOT_FOUND``. Pending claims never linger silently
    (round 25, review-15 item 23): entries on a line that becomes
    owned or ``claimable=False`` auto-resolve as rejected ->
    :class:`ClaimResolvedEvent` ``{status: "rejected"}`` to the
    claiming session. Provider LOSS resolves them the same way
    (round 28, review-18 item 14): the line's binding turning
    ``UNREACHABLE`` or being revoked makes the line unclaimable in
    fact, so pending claims on it auto-reject with
    ``reason: "provider_lost"`` - a durable assignment to a
    providerless line could serve no one."""

    claim_id: _CheckedClaimId
    line_id: _CheckedLineId
    requester_agent_id: _CheckedAgentId
    session_id: _CheckedSessionId
    created_at: AwareDatetime


class ClaimResult(ContractModel):
    """call.claim_exclusive result: claim is a REQUEST - moderator
    approval in the admin surface grants it; never self-granted (§4.2)."""

    claim_id: _CheckedClaimId
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
    line_id: _CheckedLineId
    assignment_id: _CheckedAssignmentId
    lease_id: _CheckedLeaseId
    number: _CheckedCallerNumber
    media_endpoint: str = Field(min_length=1, max_length=MAX_ENDPOINT_LEN)
    """The PROVIDER-side media endpoint (round 25, review-15 item 6):
    the ``provider_media_endpoint`` half of the lease's
    :class:`MediaCredentials` pair - the address the LineProvider
    connects ITS media socket to. The agent receives a different
    endpoint inside DialResult/AcceptResult."""
    media_token: _CheckedMediaToken


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
    line_id: _CheckedLineId
    assignment_id: _CheckedAssignmentId
    call_token: _CheckedCallToken
    """Ring correlation is BY LINE, not by token (round 25, review-15
    item 10): a line never holds two rings, so the provider resolves
    ``line.answer{call_token}`` against the line's ONE open ring.
    The field carries whichever token the kernel currently tracks -
    the original ``call_token`` OR a re-issued
    ``fallback_call_token`` - and the provider MUST answer the open
    ring regardless of which value arrives (no provider-side token
    table change)."""
    lease_id: _CheckedLeaseId
    media_endpoint: str = Field(min_length=1, max_length=MAX_ENDPOINT_LEN)
    """The PROVIDER-side media endpoint (round 25, review-15 item 6) -
    same split as :attr:`ProviderDialCommand.media_endpoint`."""
    media_token: _CheckedMediaToken


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
    line_id: _CheckedLineId
    assignment_id: _CheckedAssignmentId
    lease_id: _CheckedLeaseId | None = None
    call_token: _CheckedCallToken | None = None
    """Ring-reject target - same BY-LINE correlation rule as
    :attr:`ProviderAnswerCommand.call_token` (round 25, review-15
    item 10): the field carries whichever token the kernel tracks
    (original ``call_token`` or re-issued ``fallback_call_token``)
    and the provider tears down the line's ONE open ring regardless
    of which value arrives."""

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
    line_id: _CheckedLineId
    assignment_id: _CheckedAssignmentId
    lease_id: _CheckedLeaseId
    digits: str = Field(pattern=DTMF_PATTERN.pattern)


class ProviderRevokeCommand(ContractModel):
    """Detach a provider binding (revocation or dead-man teardown).
    After issuing, the binding's liveness is torn down - the
    ``event.line_health_changed{new_state: "unreachable"}`` fact of
    the provider-health axis (round 25, review-15 item 15) - and all
    its leases follow the teardown path."""

    command: Literal["revoke"] = "revoke"
    line_id: _CheckedLineId
    assignment_id: _CheckedAssignmentId
    reason: str | None = Field(default=None, max_length=MAX_REASON_LEN)
    """Free-form admin note relayed to ``line.revoke{reason?}`` (round-21
    review audit: the wire table documents no closed vocabulary for
    this param, so it does NOT ride CallEndReason). Bounded at
    :data:`MAX_REASON_LEN` (round 27, review-17 item 52)."""


class ProviderReattachMediaCommand(ContractModel):
    """Re-issue provider media credentials on a LIVE lease (wire:
    line.reattach_media; round 25, review-15 item 9).

    Delivered on the provider's own channel when a re-bound lease
    needs fresh provider media credentials - e.g. a successful
    ``provider.reregister`` that preserved in-flight leases inside
    the orphan-grace window. Media tokens are one-shot and do NOT
    survive a socket drop: the burned ``provider_media_token`` from
    the dead session can never pass ``media.auth`` again, so the
    kernel mints a NEW provider-role pair entry per preserved lease
    and hands it here. The provider treats it like the
    ``media_endpoint``/``media_token`` pair of
    :class:`ProviderDialCommand`: reconnect the media socket,
    re-authenticate, resume the stream. The reissued pair
    SUPERSEDES the prior provider-side pair (round 26, review-16
    item 8): the old ``media_token`` becomes invalid at issue
    time - a lease holds a single active pair per role - bounding
    the replay window to the reissue instant rather than the full
    pair TTL.

    Re-auth deadline (round 27, review-17 item 30 - replaces the
    wire doc's undefined "before its old media deadline"): the
    re-authentication window is a FRESH
    :data:`MEDIA_PAIR_TTL_SEC` counted from the reissue instant -
    the dead socket's original deadline is meaningless (its pair is
    already burned), so the provider MUST complete the new
    ``media.auth`` inside 30 s of this command's minting or the
    reissued pair expires half-connected and is torn down like any
    unauthenticated pair."""

    command: Literal["reattach_media"] = "reattach_media"
    line_id: _CheckedLineId
    assignment_id: _CheckedAssignmentId
    lease_id: _CheckedLeaseId
    media_endpoint: str = Field(min_length=1, max_length=MAX_ENDPOINT_LEN)
    """The PROVIDER-side media endpoint - same split as
    :attr:`ProviderDialCommand.media_endpoint` (round 25, item 6)."""
    media_token: _CheckedMediaToken


ProviderCommand = Annotated[
    ProviderDialCommand
    | ProviderAnswerCommand
    | ProviderHangupCommand
    | ProviderSendDtmfCommand
    | ProviderRevokeCommand
    | ProviderReattachMediaCommand,
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
    "reattach_media": "line.reattach_media",
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
    """The per-lease media credential pair (round 21, CRITICAL-2;
    endpoint split round 25, review-15 item 6).

    One ``lease_id`` gets TWO independent one-time tokens AND two
    independent endpoints: the agent receives
    ``agent_media_endpoint``/``agent_media_token`` inside
    DialResult/AcceptResult, the provider receives
    ``provider_media_endpoint``/``provider_media_token`` inside
    ProviderDialCommand/ProviderAnswerCommand (and the re-issue
    pair inside :class:`ProviderReattachMediaCommand` /
    :class:`MediaReattachResult`, item 9). On the wire both travel
    as ``media_endpoint``/``media_token``; the MediaRouter binds
    each token to ``(lease_id, expected_role)``, so a compromised
    or misused ``role: "agent"`` token can never open a socket as
    ``role: "provider"`` and hijack the phone-side stream.

    ``media.auth`` validation (round 27, review-17 item 69): the
    TEXT ``media.auth`` frame's ``role`` field is client-declared -
    the ONLY defense is that the router validates the COMPLETE
    ``(token -> lease_id, expected_role)`` pair and rejects a
    token/role mismatch (an agent token claiming
    ``role: "provider"`` fails even though both frames parse). The
    token inside the auth frame MUST be masked in access logs -
    ``MediaToken`` renders only the masked form through
    ``__str__``/``__repr__``/``to_log_dict()``; a WS-server access
    log that dumps raw TEXT payloads would print the bearer
    credential, so adapters MUST log the frame through the model's
    masked serialization, never via raw payload dumps.

    Agent-side ``0x02`` semantics (round 27, review-17 item 68): a
    ``0x02`` flush frame from the AGENT direction drains only the
    kernel's uplink ring - it is consumed at the router and NEVER
    bridged to the provider, so it cannot recall or stop downlink
    audio already transmitted toward the agent (no barge-in on
    already-sent audio); agents must treat it as a local
    buffer-reset, not a remote mute.

    The endpoint split exists because the two roles need different
    transports: a LOCAL agent is served on the ``unix://`` media
    socket while a REMOTE agent needs the ``wss://`` listener - the
    :class:`MediaAllocator` picks ``agent_media_endpoint`` from the
    acquiring session's transport profile (see
    ``MediaAllocator.allocate``); the transport vocabulary itself
    is the closed :data:`MediaTransport` literal (round 27,
    review-17 items 59/78).

    Two ``provider_media_endpoint`` levels (round 27, review-17
    item 26): the ``provider.register`` response's
    ``provider_media_endpoint`` names the provider-role STATIC
    listener address (a default the provider may advertise before
    any lease exists), while the per-lease
    ``provider_media_endpoint`` minted inside ``line.dial``/
    ``line.answer``/``line.reattach_media`` is AUTHORITATIVE for
    that lease - it is what ``media.auth`` binds the token to, and
    a divergence between the two resolves in favour of the
    per-lease value (the static address is only the socket the
    per-lease endpoint is minted from)."""

    agent_media_endpoint: str = Field(min_length=1, max_length=MAX_ENDPOINT_LEN)
    provider_media_endpoint: str = Field(min_length=1, max_length=MAX_ENDPOINT_LEN)
    agent_media_token: _CheckedMediaToken
    provider_media_token: _CheckedMediaToken

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
    only - round 25 (review-15 item 15) adds the second true-
    broadcast event ``line_health_changed``, a PII-free operational
    provider-health fact: shared-pool
    ``incoming_call``/``missed_call`` no longer
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
    agent_id: _CheckedAgentId


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
    session_id: _CheckedSessionId


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


EVENT_AUDIENCE_SCOPES: dict[EventName, frozenset[str]] = {
    EventName.INCOMING_CALL: frozenset({"agent", "session"}),
    EventName.MISSED_CALL: frozenset({"agent", "session"}),
    EventName.FALLBACK_TRIGGERED: frozenset({"agent"}),
    EventName.ASSIGNMENT_REVOKED: frozenset({"agent"}),
    EventName.LINE_STATE_CHANGED: frozenset({"broadcast", "agent", "admin"}),
    EventName.LINE_HEALTH_CHANGED: frozenset({"broadcast", "admin", "agent"}),
    EventName.QUEUE_READY: frozenset({"session"}),
    EventName.QUEUE_UPDATED: frozenset({"session"}),
    EventName.QUEUE_CANCELLED: frozenset({"session"}),
    EventName.AUDIO_READY: frozenset({"session"}),
    EventName.CALL_STATE_CHANGED: frozenset({"session"}),
    EventName.IDLE_ALERT: frozenset({"admin"}),
    EventName.CLAIM_RESOLVED: frozenset({"session"}),
    EventName.ASSIGNMENT_GRANTED: frozenset({"agent"}),
}
"""Authoritative event -> legal ``audience.scope`` set (round 25,
review-15 item 31) - the audience-union literal each event name may
be stamped with, enforced by :class:`EventEnvelope` at construction:

- ``incoming_call``/``missed_call`` carry caller-number PII and a
  one-shot ``call_token``: ``agent`` (exclusive owner) or
  ``session`` (one envelope per live RECEIVE_INBOUND session,
  round 22 HIGH-4) - NEVER ``broadcast``;
- ``fallback_triggered`` carries the worker's
  ``fallback_call_token``: ``agent`` only (the reserved
  :data:`SYSTEM_FALLBACK_WORKER` identity);
- ``assignment_revoked`` names a specific owner: ``agent`` only;
- ``line_state_changed`` / ``line_health_changed`` are broadcast
  only for SHARED_POOL lines (round 27, review-17 items 6/7): on
  an agent-owned EXCLUSIVE line both events route ONLY to the
  owner's ``agent`` audience plus an ``admin`` copy - a broadcast
  copy, even PII-redacted, would still leak the private line's
  activity. ``line_state_changed`` may additionally carry an
  ``agent``-scoped copy with the unmasked ``reason`` (item 44,
  batch 2);
- ``queue_ready``/``queue_updated``/``queue_cancelled``/
  ``audio_ready``/``call_state_changed`` carry session-scoped
  lease/ticket facts: ``session`` only;
- ``claim_resolved`` carries the claiming session's own claim
  outcome (item 2, batch 2): ``session`` only - an approval leaks
  the fresh ``assignment_id`` and must reach only the claimant;
- ``assignment_granted`` delivers the rotated ``assignment_id`` to
  the line's owner (item 2, batch 2): ``agent`` only - the agent
  needs it to call ``release{assignment_id}`` later;
- ``idle_alert`` is an operational escalation: ``admin`` only.
  """


class EventEnvelope(ContractModel, Generic[PayloadT]):
    """Typed envelope carrying a domain event payload to a subscriber.

    ``name`` is the domain EventName; the adapter maps to ``event.*``
    wire spellings and subscriber-scoped fields (e.g. session_id).
    ``audience`` (round 21) is the mandatory routing target computed
    at emission time - see :data:`EventAudience` for the per-event
    rules and :data:`EVENT_AUDIENCE_SCOPES` for the enforced
    ``(name, scope)`` legality table (round 25). The second enforced
    leg is ``(name, payload-type)`` (round 26, review-16 item 1):
    :data:`EVENT_PAYLOAD_TYPES` pins the payload model each event
    name may carry, so a mis-typed payload can never ride a
    broadcast-legal name."""

    name: EventName
    audience: EventAudience
    emitted_at: AwareDatetime
    payload: PayloadT

    @model_validator(mode="after")
    def _audience_scope_legal_for_event(self) -> EventEnvelope[PayloadT]:
        """Round 25 (review-15 item 31): reject ``(name,
        audience.scope)`` pairs outside :data:`EVENT_AUDIENCE_SCOPES`
        - a PII-bearing event must never be constructible with
        :class:`BroadcastAudience`, and session-scoped lease facts
        must never widen to other sessions."""
        allowed = EVENT_AUDIENCE_SCOPES.get(self.name) or frozenset()
        if self.audience.scope not in allowed:
            raise ValueError(
                f"audience scope {self.audience.scope!r} is not legal for event {self.name.value!r}"
            )
        return self

    @model_validator(mode="after")
    def _payload_type_matches_name(self) -> EventEnvelope[PayloadT]:
        """Round 26 (review-16 item 1): reject a ``(name, payload)``
        pair where the payload is not an instance of the model
        :data:`EVENT_PAYLOAD_TYPES` pins for ``name`` - the lookup
        happens at validation time, so the map's later module
        position (it is defined after the event classes below) is
        fine. A broadcast-audience legal name carrying a PII
        payload, or any other mis-typed pair, is a defect, not a
        routing choice."""
        expected = EVENT_PAYLOAD_TYPES.get(self.name)
        if expected is not None and not isinstance(self.payload, expected):
            raise ValueError(
                f"payload {type(self.payload).__name__} does not match event {self.name.value!r}"
            )
        return self


class IncomingCallEvent(ContractModel):
    """incoming_call payload: first valid call.accept(call_token) wins
    (wake-and-grant, §4.3/§5.2). ``caller_id`` is redacted PII
    (round-22 wire-name fix: the wire field is ``caller_id``, not
    ``caller``); ``expires_at`` is the absolute accept-window
    deadline in RFC-3339 UTC - the earlier ``expires_in_ms`` relative
    form would skew by queue delay unless recomputed against
    ``emitted_at`` at outbox drain (round 22, review-10 HIGH-1)."""

    line_id: _CheckedLineId
    call_token: _CheckedCallToken
    caller_id: _CheckedCallerNumber | None = None
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
    on every other path.

    ``answer_confidence: "confirmed"`` may ride ONLY
    ``"active"``/``"disconnected"`` (round 28, review-18 item 16):
    claiming a confirmed answer while ``dialing``/``alerting``
    reports a human answer before the far end could possibly have
    picked up - a provisional confidence on those phases stays
    legal (the hedge exists precisely because early "active" is
    unreliable, §5.5)."""

    lease_id: _CheckedLeaseId
    line_id: _CheckedLineId
    state: WireCallState
    reason: CallEndReason | None = None
    answer_confidence: AnswerConfidence | None = None
    reissued_ticket_id: _CheckedTicketId | None = None

    @model_validator(mode="after")
    def _reason_and_reissue_scoped(self) -> CallStateChangedEvent:
        """Round 25 (review-15 item 41): ``reason`` is a teardown
        fact - it may ride ONLY ``state == "disconnected"`` (a
        reason on ``active``/``dialing``/``alerting`` would report
        why a still-live call ended); and ``reissued_ticket_id``
        exists ONLY on a ``PREEMPTED_BY_INCOMING`` teardown - it
        names the re-queued ticket of a preempted grant, which no
        other reason produces."""
        if self.reason is not None and self.state != "disconnected":
            raise ValueError("reason is allowed only on state 'disconnected'")
        if (
            self.reissued_ticket_id is not None
            and self.reason is not CallEndReason.PREEMPTED_BY_INCOMING
        ):
            raise ValueError("reissued_ticket_id requires reason 'preempted_by_incoming'")
        if self.answer_confidence == "confirmed" and self.state in ("dialing", "alerting"):
            raise ValueError(
                "answer_confidence 'confirmed' requires state 'active' or 'disconnected'"
            )
        return self


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

    ticket_id: _CheckedTicketId
    session_id: _CheckedSessionId
    lease_id: _CheckedLeaseId
    line_id: _CheckedLineId
    intent: Intent
    expires_at: AwareDatetime


class QueueUpdatedEvent(ContractModel):
    """queue_updated payload (wire: ``event.queue_updated``): FIFO
    position advance for a pending ticket - emitted on
    cancel_acquire, drop_session, grant-TTL expiry in tick(), or a
    line freeing up (round 17)."""

    session_id: _CheckedSessionId
    ticket_id: _CheckedTicketId
    position: int = Field(ge=1)
    """Global FIFO rank among ALL eligible tickets (round 27,
    review-17 item 48) - same single-ordering rule as
    :attr:`AcquireQueued.position`."""


class QueueCancelledEvent(ContractModel):
    """queue_cancelled payload (wire: ``event.queue_cancelled``;
    round 25, review-15 item 23): a pending ticket was CANCELLED -
    never silently parked - because its target stopped being able
    to serve it.

    Fates (see :class:`QueueCancelReason`): the target line became
    EXCLUSIVE via ``assign_line``/``approve_claim``, the binding lost
    the intent capability the ticket asked for
    (``provider.capability_update``), or the provider was
    revoked/declared UNREACHABLE. A ``claimable`` flip to False under
    ``configure_line`` is NOT a fate (round 28, review-18 item 5):
    the line keeps serving the ``SHARED_POOL`` - only pending
    CLAIMS on it auto-reject via ``claim_resolved``, the queued
    tickets live on. Delivered to the ticket's own
    ``session_id`` only. A ticket dying with its socket stays the
    implicit ``drop_session`` case - no event is owed to a dead
    socket."""

    session_id: _CheckedSessionId
    ticket_id: _CheckedTicketId
    reason: QueueCancelReason


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
    ``LineHealthChangedEvent(new_state=LivenessState.UNREACHABLE)``
    plus lease teardowns with ``reason=CallEndReason.ADMIN_REVOKED``
    instead (round 25, review-15 item 15 - provider health is the
    liveness axis, not a LineState), so an audience
    can never be stamped ``None``. ``reason`` stays free-form
    ``str``: the wire table documents no closed vocabulary for it -
    an admin-supplied revoke note is legal payload (round-21 review
    audit), bounded at :data:`MAX_REASON_LEN` (round 27, review-17
    item 52)."""

    assignment_id: _CheckedAssignmentId
    line_id: _CheckedLineId
    owner_agent_id: _CheckedAgentId
    reason: str | None = Field(default=None, max_length=MAX_REASON_LEN)


class ClaimResolvedEvent(ContractModel):
    """claim_resolved payload (wire: ``event.claim_resolved``;
    round 25, review-15 item 2): a ``call.claim_exclusive`` request
    reached its fate - moderator ``approve_claim`` /
    ``reject_claim``, or an auto-reject when the line became
    owned/``claimable=False`` while the claim was still pending
    (item 23: PendingClaim entries never linger silently).

    Delivered to the CLAIMING session only (``SessionAudience``):
    the session that filed the claim learns the outcome without the
    agent ever having to poll the admin surface. ``assignment_id``
    is present IFF ``status == "approved"`` - the rotated fresh id
    the claimant needs to later call ``release{assignment_id}``.
    ``reason`` is a PII-free admin/auto-reject note, free-form
    ``str`` (no closed vocabulary is documented for it), bounded at
    :data:`MAX_REASON_LEN` (round 27, review-17 item 52)."""

    claim_id: _CheckedClaimId
    line_id: _CheckedLineId
    status: Literal["approved", "rejected"]
    assignment_id: _CheckedAssignmentId | None = None
    reason: str | None = Field(default=None, max_length=MAX_REASON_LEN)

    @model_validator(mode="after")
    def _assignment_id_iff_approved(self) -> ClaimResolvedEvent:
        """``assignment_id`` is present IFF the claim was approved
        (round 25, item 2): an approval without the rotated id
        strands the owner (it could never release), and an id on a
        rejection would name a binding that does not exist."""
        if (self.status == "approved") != (self.assignment_id is not None):
            raise ValueError("assignment_id is present iff status is 'approved'")
        return self


class AssignmentGrantedEvent(ContractModel):
    """assignment_granted payload (wire: ``event.assignment_granted``;
    round 25, review-15 item 2): a durable EXCLUSIVE
    ``LineAssignment`` was granted to an agent - on BOTH
    ``approve_claim`` and admin ``assign_line`` paths.

    Delivered to the owner's ``AgentAudience`` so the agent learns
    the rotated ``assignment_id`` it needs to later call
    ``release{assignment_id}`` - the approval path alone cannot
    carry it (``approve_claim`` answers the ADMIN, not the agent),
    and an agent-owned line whose id the agent never learned would
    be unreleasable. Companion of :class:`ClaimResolvedEvent`: the
    same grant may emit both envelopes when claimant == owner.

    Re-delivery (round 27, review-17 item 2): a grant emitted while
    the owner held NO live session would be lost forever -
    ``AgentAudience`` reaches connected sockets only. The durable
    fallback is ``LineSummary.assignment_id``, filled on the
    owner's own ``call.get_lines`` rows, and the kernel
    additionally re-emits this event to an owner session
    registering later (same ``assignment_id``) - same re-delivery
    pattern as ``event.incoming_call`` for a live ring, since the
    assignment is durable too.

    ``mode`` is EXCLUSIVE-only (round 27, review-17 item 47): the
    event exists to deliver the ROTATED binding id of a
    transition to durable exclusive ownership - a SHARED_POOL
    assignment is the absence of a grant and is never
    notified."""

    assignment_id: _CheckedAssignmentId
    line_id: _CheckedLineId
    mode: AssignmentMode

    @model_validator(mode="after")
    def _mode_is_exclusive(self) -> AssignmentGrantedEvent:
        """Grant events carry only the transition to EXCLUSIVE
        ownership (round 27, review-17 item 47): a SHARED_POOL
        ``mode`` here would announce a non-grant."""
        if self.mode is not AssignmentMode.EXCLUSIVE:
            raise ValueError("assignment_granted requires mode 'exclusive'")
        return self


class AudioReadyEvent(ContractModel):
    """audio_ready payload (round 14): the provider's local BT SCO
    path is up - early media flows, uplink accepted without first-word
    clipping. Derived from ``provider.audio_ready`` ingress, NOT from
    remote answer - see ``answer_confidence`` for that distinction."""

    lease_id: _CheckedLeaseId
    line_id: _CheckedLineId


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
    deadline is enforced by ``tick(now)`` (round 24, review-13 H2;
    ring-drain corrected round 27, review-17 item 72): on lapse
    without a worker accept the kernel burns the
    ``fallback_call_token`` and emits
    ``MissedCallEvent(reason=CallEndReason.ACCEPT_WINDOW_EXPIRED)``
    - the no-pickup outcome of last resort - but the line stays
    ``RINGING_LOCKED`` under the ring-drain rule (round 25,
    review-15 item 16; docs/specs/line-manager.md): it frees only
    when the provider reports the ring's ``disconnected`` or
    ``ring_drain_ttl_sec`` lapses, whichever first, never
    immediately on the token burn.

    Provider-side correlation (round 25, review-15 item 10): when the
    worker later accepts, the re-issued ``fallback_call_token``
    flows to the provider through the SAME ``line.answer{call_token}``
    field the original token used - the provider answers the line's
    ONE open ring regardless of which value arrives, so no
    provider-side token table change is needed. Kernel-side the
    ORIGINAL ``call_token`` is burned in the SAME ``tick`` step that
    mints this replacement (round 27, review-17 item 71) - the two
    tokens never coexist valid."""

    line_id: _CheckedLineId
    call_token: _CheckedCallToken
    caller_id: _CheckedCallerNumber | None = None
    policy: FallbackPolicy
    expires_at: AwareDatetime

    @model_validator(mode="after")
    def _policy_is_worker_dispatchable(self) -> FallbackTriggeredEvent:
        """``fallback_triggered`` exists ONLY to dispatch a WORKER
        policy (round 25, review-15 item 31): core policies
        (:class:`NoPickupFallback`/:class:`RejectFallback`) execute
        inside LineManager and never dispatch to the worker -
        allowing one here would emit a delivery event nothing can
        act on and silently drop the ring."""
        if not isinstance(self.policy, _WORKER_FALLBACK_TYPES):
            raise ValueError("fallback_triggered requires a worker fallback policy")
        return self


class LineStateChangedEvent(ContractModel):
    """line_state_changed payload: call-occupancy :class:`LineState`
    transition fact. ``reason`` rides the CLOSED
    :class:`CallEndReason` vocabulary
    (round-21 review) - this is the channel that surfaces
    ``external_call`` when a human-initiated call takes the line
    BUSY (design §9). Provider-health transitions do NOT ride this
    event: they moved to ``event.line_health_changed``
    (:class:`LineHealthChangedEvent`, round 25, review-15 item 15).

    Exclusive-line privacy (round 25, review-15 item 44; tightened
    round 27, review-17 item 6): when the line is agent-owned
    EXCLUSIVE the event routes ONLY to the owner's
    ``AgentAudience`` plus an ``AdminAudience`` copy - broadcast is
    FORBIDDEN, because even a redacted broadcast copy would reveal
    to every pool observer that the private line changed occupancy
    (the earlier ``BroadcastAudience`` + ``reason=None`` scheme
    still leaked the activity itself). For a SHARED_POOL line
    ``BroadcastAudience`` stays legal - pool occupancy is a shared
    operational fact with no owner to protect.

    Transactional coalescing (round 28, review-18 item 16): a
    ``BUSY -> FREE -> BUSY`` replacement that happens inside ONE
    mutation transaction (e.g. a teardown immediately followed by
    the grant of the head queue ticket) emits a SINGLE ``BUSY``
    fact for the NEW lease - the intermediate ``FREE`` is never
    published, because a subscriber seeing the phantom vacancy
    would observe occupancy that never existed outside the
    transaction."""

    line_id: _CheckedLineId
    old_state: LineState
    new_state: LineState
    reason: CallEndReason | None = None

    @model_validator(mode="after")
    def _state_must_change(self) -> LineStateChangedEvent:
        """A no-change "transition" is a defect, not a notification
        (round 25, review-15 item 41 - same rule as
        :class:`LineHealthChangedEvent`)."""
        if self.old_state == self.new_state:
            raise ValueError("old_state and new_state must differ")
        return self


class LineHealthChangedEvent(ContractModel):
    """line_health_changed payload (wire:
    ``event.line_health_changed``; round 25, review-15 item 15): a
    provider-liveness :class:`LivenessState` transition fact on the
    line's binding - the SEPARATE provider-health axis.

    This event replaces the DEGRADED/DOWN transitions that
    previously rode ``event.line_state_changed``: heartbeat
    escalation (reachable -> unreachable), WS drop
    (``ws_disconnected``), reregister recovery and provider revoke
    all surface here, while ``line_state_changed`` stays the pure
    call-occupancy channel. Audience: ``broadcast`` for SHARED_POOL
    lines plus ``admin`` escalation; on an agent-owned EXCLUSIVE
    line the event routes ONLY to the owner's ``AgentAudience`` +
    ``AdminAudience`` - broadcast is forbidden because the health of
    a private line is private activity too (round 27, review-17
    item 6). ``old_state != new_state`` is enforced -
    a no-change event is a defect, not a notification. UNBOUND is
    legal ONLY as ``old_state`` (round 27, review-17 item 8 -
    replaces the round-26 both-ends ban): the FIRST provider attach
    on a previously unbound configured line emits
    ``unbound -> reachable``; UNBOUND is never a ``new_state`` - a
    dying binding surfaces UNREACHABLE, and ``unbound`` remains a
    derived :class:`LineSummary` listing state.

    DEGRADED is legal as ``new_state`` ONLY from ``reachable`` and
    resolves off the rung into ``reachable``, ``unreachable`` or
    ``ws_disconnected`` (round 28, review-18 item 1; the
    ``degraded -> ws_disconnected`` exit was added in the round-28
    fix, PR #72 review): the heartbeat ladder is
    ``reachable -> degraded -> unreachable`` - the first missed-ping
    step degrades, continued silence escalates to unreachable, a
    REACHABLE fact on the live channel restores ``degraded ->
    reachable``, and a control-socket drop while DEGRADED resolves
    ``degraded -> ws_disconnected`` - the socket-loss axis overrides
    the elapsed-time rung, so the 10 s orphan-grace path stays
    reachable from a degraded binding. ``ws_disconnected`` remains
    the ONLY liveness state that arms the orphan grace -
    ``degraded`` and ``unreachable`` never do. Degradation is
    kernel-computed and has no reported-fact entry into the ladder -
    ``ws_disconnected -> degraded`` or ``unreachable -> degraded``
    can never be emitted."""

    line_id: _CheckedLineId
    old_state: LivenessState
    new_state: LivenessState

    @model_validator(mode="after")
    def _state_must_change(self) -> LineHealthChangedEvent:
        if self.old_state == self.new_state:
            raise ValueError("old_state and new_state must differ")
        return self

    @model_validator(mode="after")
    def _unbound_only_as_old_state(self) -> LineHealthChangedEvent:
        """UNBOUND may appear only as ``old_state`` (round 27,
        review-17 item 8): a first provider attach reports
        ``unbound -> reachable``, and that is the only legal
        unbound transition - ``unbound`` as ``new_state`` would
        report a binding that became provider-less, which the
        contract renders as UNREACHABLE instead."""
        if self.new_state is LivenessState.UNBOUND:
            raise ValueError("unbound is a derived listing state, never a new_state")
        if (
            self.old_state is LivenessState.UNBOUND
            and self.new_state is not LivenessState.REACHABLE
        ):
            raise ValueError("the only unbound transition is 'unbound -> reachable'")
        return self

    @model_validator(mode="after")
    def _degraded_ladder_rules(self) -> LineHealthChangedEvent:
        """DEGRADED ladder rules (round 28, review-18 item 1; the
        ``ws_disconnected`` exit was added in the round-28 fix,
        PR #72 review): the kernel-computed middle rung of
        ``reachable -> degraded -> unreachable`` enters only from
        ``reachable`` (the first missed-ping step) and resolves
        into ``reachable`` (a live ping restored health),
        ``unreachable`` (elapsed-time escalation to down) or
        ``ws_disconnected`` (control-socket loss - the socket-loss
        axis overrides the elapsed-time rung, keeping the
        orphan-grace path reachable from a degraded binding)."""
        if (
            self.new_state is LivenessState.DEGRADED
            and self.old_state is not LivenessState.REACHABLE
        ):
            raise ValueError("the only degraded entry is 'reachable -> degraded'")
        if self.old_state is LivenessState.DEGRADED and self.new_state not in (
            LivenessState.REACHABLE,
            LivenessState.UNREACHABLE,
            LivenessState.WS_DISCONNECTED,
        ):
            raise ValueError(
                "degraded resolves only to 'reachable', 'unreachable' or 'ws_disconnected'"
            )
        return self


class MissedCallEvent(ContractModel):
    """missed_call payload: an inbound ring ended unanswered
    (accept-window expiry, caller hangup, or reject-policy outcome).
    ``reason`` is REQUIRED on the wire (api-contract.md event table)
    and rides the CLOSED :class:`CallEndReason` vocabulary
    (round-21 review). One member of that shared vocabulary is
    INADMISSIBLE here (round-28 fix, PR #72 review):
    ``max_call_duration`` can never mint a missed_call - a
    max-duration end is an established-lease teardown that
    produces ``event.call_state_changed{state:"disconnected"}``
    plus the ended lease, not an unanswered ring - so the
    validator below enforces the exclusion rather than leaving it
    to docstring discipline. ``voicemail_ref`` was removed in
    round 27 (review-17 item 24): no producer ever minted it - a
    dead wire member is dropped from the contract rather than
    carried."""

    line_id: _CheckedLineId
    caller_id: _CheckedCallerNumber | None = None
    reason: CallEndReason

    @model_validator(mode="after")
    def _reason_is_a_missed_call_reason(self) -> MissedCallEvent:
        """``max_call_duration`` is inadmissible on ``missed_call``
        (round-28 fix, PR #72 review): a ring that never became a
        lease can never hit the call-duration cap - the reason
        would report a lease-scoped teardown fact on a ring
        fact."""
        if self.reason is CallEndReason.MAX_CALL_DURATION:
            raise ValueError("max_call_duration is not a missed_call reason")
        return self


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
    resets the window. Re-arm rule (round 27, review-17 item 54):
    the "already alerted" latch is cleared by the FIRST line
    activity (any operation refreshing ``last_seen_at``) or by the
    owner opening a session - a later idle period after either
    produces a fresh single alert.

    Activity rule (round 24, review-13 H4): a line whose
    ``owner_agent_id`` holds at least one OPEN session is ACTIVE -
    the idle clock never runs against it, so a secretary agent
    waiting days on a live socket for inbound rings cannot trigger
    a false alert. ``last_seen_at`` refreshes on every line
    operation and when ``drop_session`` closes the owner's LAST
    session - that is the moment the idle window starts."""

    assignment_id: _CheckedAssignmentId
    line_id: _CheckedLineId
    owner_agent_id: _CheckedAgentId
    idle_since: AwareDatetime


EVENT_PAYLOAD_TYPES: dict[EventName, type[ContractModel]] = {
    EventName.INCOMING_CALL: IncomingCallEvent,
    EventName.CALL_STATE_CHANGED: CallStateChangedEvent,
    EventName.QUEUE_READY: QueueReadyEvent,
    EventName.QUEUE_UPDATED: QueueUpdatedEvent,
    EventName.ASSIGNMENT_REVOKED: AssignmentRevokedEvent,
    EventName.AUDIO_READY: AudioReadyEvent,
    EventName.FALLBACK_TRIGGERED: FallbackTriggeredEvent,
    EventName.LINE_STATE_CHANGED: LineStateChangedEvent,
    EventName.LINE_HEALTH_CHANGED: LineHealthChangedEvent,
    EventName.MISSED_CALL: MissedCallEvent,
    EventName.IDLE_ALERT: IdleAlertEvent,
    EventName.CLAIM_RESOLVED: ClaimResolvedEvent,
    EventName.ASSIGNMENT_GRANTED: AssignmentGrantedEvent,
    EventName.QUEUE_CANCELLED: QueueCancelledEvent,
}
"""Authoritative event -> payload-model map (round 26, review-16
item 1): the ``(name, payload-type)`` counterpart of
:data:`EVENT_AUDIENCE_SCOPES`, enforced by
:class:`EventEnvelope` at construction so a mis-typed payload can
never ride a mismatched event name (e.g. caller PII under a
broadcast-legal name). Defined AFTER the event classes because
every value is a class object; :class:`EventEnvelope` looks names
up at validation time, so the forward position is safe.
Completeness is pinned by ``test_event_payload_types_map``."""


# ---------------------------------------------------------------------------
# Provider ingress payloads (narrowed surface: distilled FACTS, not
# raw WSS register/ping transport)
# ---------------------------------------------------------------------------


class ProviderActiveCall(ContractModel):
    """One live call as the provider sees it on the D-Bus (round 21).

    Element of ``ProviderRegistration.state_snapshot`` - the
    orphan-grace reconciliation input. ``lease_id`` is ``None`` for a
    call the kernel never issued, e.g. a human-initiated call on the
    physical phone (design §9 external-call rule).

    ``call_token`` (round 25, review-15 item 26) lets a reregister
    snapshot carry the ring token of a still-ringing call the
    provider discovered on the D-Bus - the same kernel-minted token
    the provider received in the ``provider.call_state`` response,
    echoed back so the kernel can correlate the orphaned ring
    against its own ``call_token`` record instead of minting a
    duplicate.

    Provider DOWN/revoke clears ANY line fact the binding owned
    (round 25, item 26): open rings are burned - the ``call_token``
    is consumed and the ring torn down as
    ``MissedCallEvent(reason=CallEndReason.PROVIDER_LOST)`` - leases
    are torn down, and an ``external_call`` BUSY hold is released:
    the line returns to FREE once a healthy binding confirms via
    ``state_snapshot`` (if the snapshot still shows the external
    call, the BUSY/external_call fact is re-established)."""

    lease_id: _CheckedLeaseId | None = None
    call_token: _CheckedCallToken | None = None
    state: ProviderCallState
    caller_id: _CheckedCallerNumber | None = None

    @model_validator(mode="after")
    def _correlators_are_alternative(self) -> ProviderActiveCall:
        """``call_token`` and ``lease_id`` are ALTERNATIVE
        correlators (round 27, review-17 item 11 - mirrors
        :meth:`ProviderCallStateUpdate._correlators_are_alternative`):
        the token names a still-ringing call, the lease an
        established one - carrying BOTH makes the snapshot element's
        binding ambiguous; NEITHER is the documented external-call
        case. Correlator<->state coherence is deliberately NOT
        checked (round 27, review-17 item 11, PR #68 review):
        ``call_token`` with ``state == ACTIVE`` is legal - the
        snapshot may describe a ring the air side already answered
        before the kernel's ``line.answer`` landed."""
        if self.call_token is not None and self.lease_id is not None:
            raise ValueError("call_token and lease_id are mutually exclusive")
        return self


class ProviderRegistration(ContractModel):
    """Distilled registration fact (provider.register at the boundary).

    Round 27 (review-17 item 32) removed the wire ``token`` field:
    the per-line shared secret is consumed and verified by the
    ADAPTER against the API-key store BEFORE this model is
    constructed - the kernel sees only the distilled fact, so the
    "never retain the raw token" rule is now enforced at type level
    instead of by docstring. ``caps`` is non-empty and telecom-only
    - see :func:`_provider_caps_check` (round 24, review-13 M3).

    ``reregister`` (round 28, review-18 item 3; hardened in the
    round-28 fix, PR #72 review) is another ADAPTER-distilled fact
    in the same style: the wire carries two methods -
    ``provider.register`` and ``provider.reregister`` - and only
    the adapter knows which one arrived. The adapter MUST derive
    this flag from the RPC METHOD NAME that arrived
    (``provider.register`` -> ``False``, ``provider.reregister``
    -> ``True``) and MUST NOT copy a ``reregister`` key out of the
    wire params: a params-supplied value is ignored by the adapter
    BEFORE the fact is distilled - otherwise a provider could
    self-promote a plain register into a supersede. Same
    distilled-fact discipline as the round-27 token removal
    (review-17 item 32): the kernel's register-vs-reregister
    classification (:meth:`LineManager.register_provider`) runs on
    this flag - the wire itself carries no ``reregister`` field."""

    line_id: _CheckedLineId
    caps: tuple[Intent, ...]
    reregister: bool = False
    """Adapter-distilled method flag (round 28, review-18 item 3;
    hardened in the round-28 fix, PR #72 review): ``True`` when the
    request arrived as ``provider.reregister``, ``False`` for
    ``provider.register`` - derived ONLY from the RPC method name,
    never from a ``reregister`` key in the wire params (a
    params-supplied value is dropped before distilling). The kernel
    uses it to tell a fresh attach from a supersede/re-bind of a
    live binding (see :meth:`LineManager.register_provider`)."""
    profile: str | None = Field(default=None, min_length=1, max_length=MAX_PROFILE_LEN)
    """Bounded when present (round 26, review-16 item 4; rule 7 on an
    external ingress field): a non-empty label of at most
    :data:`MAX_PROFILE_LEN` chars - shared with
    :class:`ProviderCapabilityUpdate` and the :class:`LineSummary`
    mirror."""
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
    for no wire-shape gain).

    Ownership rule (round 28, review-18 item 16): the model stores
    its OWN copy of the map - ``_bounded_meta`` returns a fresh
    dict at validation, so the adapter MUST NOT keep mutating the
    dict it distilled from the wire after construction (it could
    no longer corrupt the stored fact anyway, and the kernel
    copies it again into ``ProviderBinding.meta``)."""
    state_snapshot: tuple[ProviderActiveCall, ...] | None = None
    """The provider's view of live calls at registration time
    (round 21, CRITICAL-3): carried on ``provider.reregister``
    inside the 10 s orphan-grace window so the kernel can verify a
    surviving call still exists on the D-Bus before re-binding its
    lease (design §5.4). ``None`` on a first-time register.

    Reconciliation scope (round 28, review-18 item 10; formalized
    round 29, SA_quant_review_1 slice-A item 7): the
    snapshot is authoritative ONLY for leases that had a delivered
    ``line.dial``/``line.answer`` command - i.e. leases whose phase
    is a member of :data:`SNAPSHOT_RECONCILED_PHASES`
    (``DIALING``/``ALERTING``/``ACTIVE``/``TERMINATING`` - the
    formal "dialing-or-later" set; phase is a ``str``-enum with no
    ordering, so membership in this set IS the test, never a
    comparison). An ``ACQUIRED`` lease is
    pre-dial: the provider could never see it on the D-Bus, so it
    is EXCLUDED from reconciliation - an empty snapshot does not
    kill undialed grants; a ``RINGING`` lease reached via
    ``ACQUIRED -> RINGING`` preemption is excluded the same way.
    The 1:1 provider-call to kernel-lease
    rule applies only to the reconciled (dialed) set. A
    ``provider.ping{state_snapshot}`` fact obeys the same scope
    rules as reregister reconciliation (round-21 review; restated
    round 28)."""

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
        wire-facing model is a memory/log hazard. Shared with
        :class:`ProviderBinding` via :func:`_bounded_meta` (round 27,
        review-17 item 27)."""
        return _bounded_meta(meta)


class ProviderLiveness(ContractModel):
    """Provider liveness fact: reachable / ws_disconnected /
    unreachable + timestamp.

    Distilled from the ``provider.ping`` heartbeat path (missed pings
    climb the ``heartbeat_degraded_after``/``heartbeat_down_after``
    ladder -> UNREACHABLE, WS drop -> ws_disconnected/orphan-grace);
    the ping transport itself stays in the adapter. Transitions mint
    ``event.line_health_changed`` on the provider-health axis
    (round 25, review-15 item 15), never ``line_state_changed``."""

    line_id: _CheckedLineId
    assignment_id: _CheckedAssignmentId
    """REQUIRED binding id (round 25, review-15 item 32 - formerly
    optional): a liveness fact without the binding id could
    cross-apply a stale close event to a re-registered binding -
    after a ``provider.reregister`` mints a new ``assignment_id``,
    an untagged UNREACHABLE/WS_DISCONNECTED fact cannot be told
    apart from the dead binding's last gasp. Every fact names its
    binding; a fact that resolves to a superseded id resolves
    through the closed-assignment ring as
    ``PhonebridgeError(ErrorCode.STALE_TOKEN)`` (round 29,
    SA_quant_review_1 slice-B item 2), never applied and never
    silently absorbed."""
    state: LivenessState
    checked_at: AwareDatetime
    """Provider-side observation stamp - ADVISORY ONLY (round 28,
    review-18 items 8/11; skew asymmetry clarified in the round-28
    fix, PR #72 review): the kernel uses it as a dedup/ordering
    key (a same-stamp repeat is a duplicate, not a new fact) but
    NEVER for deadline arithmetic - ``last_seen_at`` on the binding
    is always KERNEL RECEIPT time, so a provider-clock skew can
    neither freeze nor fast-forward the heartbeat ladder. ONLY a
    stamp deeper than :data:`MAX_CLOCK_SKEW_SEC` into the FUTURE
    is normalized to ``now`` at ingestion; a PAST stamp is
    retained VERBATIM as the dedupe key and never feeds
    elapsed-time or liveness math. The normalization itself is the
    canonical :func:`clamp_provider_stamp` helper (round 29,
    SA_quant_review_1 slice-A item 4) - implementations MUST apply
    it verbatim at ingestion rather than re-deriving the rule, so
    this field and
    :attr:`ProviderCallStateUpdate.observed_at` can never diverge
    in their clamping."""
    state_snapshot: tuple[ProviderActiveCall, ...] | None = None
    """Optional ``provider.ping{state_snapshot?}`` carrier (round-21
    review): same element shape as
    :attr:`ProviderRegistration.state_snapshot` - the calls the
    provider still sees on the D-Bus - so a ping can feed ongoing
    reconciliation the way reregister does inside the orphan-grace
    window. ``None`` when the ping carries no snapshot. The
    reconciliation scope is identical to the register-time one
    (round 28, review-18 item 10; formalized round 29): only leases
    whose phase is a member of :data:`SNAPSHOT_RECONCILED_PHASES`
    are reconciled - an ``ACQUIRED`` or ``RINGING`` lease is
    excluded, an empty snapshot never kills an undialed grant."""

    @field_validator("state")
    @classmethod
    def _state_is_a_reported_fact(cls, state: LivenessState) -> LivenessState:
        """UNBOUND is rejected (round 26, review-16 item 13): a
        liveness REPORT always comes from an attached binding -
        ``unbound`` is a derived :class:`LineSummary` listing state,
        never a reported fact a provider could send. DEGRADED is
        rejected for the same reason (round 28, review-18 item 1):
        degradation is a KERNEL-COMPUTED rung of the heartbeat
        ladder, never a provider-reported observation."""
        if state is LivenessState.UNBOUND:
            raise ValueError("unbound is a derived listing state, not a reported liveness fact")
        if state is LivenessState.DEGRADED:
            raise ValueError("degraded is kernel-computed, not a reported liveness fact")
        return state

    @model_validator(mode="after")
    def _snapshot_only_on_reachable(self) -> ProviderLiveness:
        """``state_snapshot`` is legal only on a REACHABLE ping
        (round 27, review-17 item 66): a WS_DISCONNECTED or
        UNREACHABLE fact describes a dead/dying channel whose call
        list the kernel must not reconcile against - the snapshot is
        meaningful only while the provider demonstrably sees the
        D-Bus."""
        if self.state_snapshot is not None and self.state is not LivenessState.REACHABLE:
            raise ValueError("state_snapshot is allowed only on state 'reachable'")
        return self


class ProviderCallStateUpdate(ContractModel):
    """provider.call_state ingress fact (bounded ProviderCallState)
    - POST-RING updates only.

    ``call_token`` identifies the inbound ring; ``lease_id`` (round 14)
    identifies an established call - without it the gateway cannot
    attribute an update to a lease.

    Routing rule for adapters (round 22; hardened to model
    validators in round 27, review-17 items 44/45): a
    ``state="ringing"`` report is fed to
    :meth:`LineManager.ingest_ring`, NOT to this ingress - the ring
    path mints the ``call_token`` (see
    :class:`IncomingRingIngress`), so ``state == RINGING`` is
    REJECTED here. ``caller_id`` lives on the ring ingress only:
    it must be ``None`` on this model - post-ring updates never
    re-report the caller. This model carries the
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
    Equivalently (round 25, review-15 item 10): the provider may
    always report a ring teardown BY LINE ALONE when it never
    learned a token - ring correlation is by line, not by token, so
    the echoed ``call_token`` is a hint, never the identity key.

    External calls (round 21, HIGH-6): an update carrying NEITHER
    ``call_token`` nor ``lease_id`` on a line NOT in
    ``RINGING_LOCKED`` reports a human-initiated call on the
    physical phone (dialed or answered on the handset itself).
    The kernel marks the line BUSY with reason ``"external_call"`` -
    no agent lease is minted, no PipeWire bridge is attached (audio
    stays on the phone) and NO Hangup is ever sent; the state clears
    when the provider reports DISCONNECTED (design §9).

    Handset-answered ring (round 28, review-18 item 9): a
    ``state="active"`` update that DOES carry ``call_token`` while
    the line is still ``RINGING_LOCKED`` means a human answered the
    ringing call on the physical handset before any agent's
    ``call.accept`` burned the token. The ring closes WITHOUT
    minting a lease - the kernel marks the line
    ``BUSY{reason:"external_call"}`` exactly like a dialed external
    call (no agent lease, no PipeWire bridge, NO ``line.hangup`` is
    ever queued for it) and the state clears on the later
    ``disconnected`` report. The best-effort ``line.hangup`` allowed
    inside ring-drain is reserved for the paths where the GATEWAY
    rejects the call (``reject`` fallback policy) - it is never
    queued on a ``no_pickup``/missed outcome or on this
    handset-answered path.

    ``reason`` is the CLOSED provider vocabulary
    (:class:`ProviderDisconnectReason`, round 21); gateway-minted
    reasons such as ``provider_lost`` never arrive on this ingress.
    It is a teardown fact and may ride ONLY
    ``state == DISCONNECTED`` (round 26, review-16 item 3 - the same
    rule :class:`CallStateChangedEvent` enforces: a reason on
    ``ringing``/``dialing``/``alerting``/``active`` would report why
    a still-live call ended)."""

    line_id: _CheckedLineId
    assignment_id: _CheckedAssignmentId
    call_token: _CheckedCallToken | None = None
    lease_id: _CheckedLeaseId | None = None
    caller_id: _CheckedCallerNumber | None = None
    """Retained for wire-shape parity with ``provider.call_state``
    but MUST be ``None`` (round 27, review-17 item 45): the caller's
    E.164 is a RING fact - it arrives on :class:`IncomingRingIngress`
    and is never re-reported on post-ring updates."""
    state: ProviderCallState
    reason: ProviderDisconnectReason | None = None
    observed_at: AwareDatetime
    """Provider-side observation stamp - ADVISORY ONLY (round 28,
    review-18 items 8/11; skew asymmetry clarified in the round-28
    fix, PR #72 review): a dedup/ordering key (a same-stamp repeat
    is a duplicate), never an input to deadline arithmetic - ONLY
    a stamp deeper than :data:`MAX_CLOCK_SKEW_SEC` into the FUTURE
    is normalized to ``now`` at ingestion so an NTP-skewed provider
    cannot suspend call-state ordering; a PAST stamp is retained
    VERBATIM as the dedupe key and never feeds elapsed-time
    math. The normalization itself is the canonical
    :func:`clamp_provider_stamp` helper (round 29,
    SA_quant_review_1 slice-A item 4) - the same clamp
    :attr:`ProviderLiveness.checked_at` mandates, so both
    provider-stamp ingress paths share one rule."""

    @field_validator("state")
    @classmethod
    def _state_is_post_ring(cls, state: ProviderCallState) -> ProviderCallState:
        """``RINGING`` is rejected on this ingress (round 27,
        review-17 item 44): ring facts ride ONLY
        :class:`IncomingRingIngress` - a ringing update here would
        double-report the ring or bypass token minting."""
        if state is ProviderCallState.RINGING:
            raise ValueError("ringing facts ingress via IncomingRingIngress, not call_state")
        return state

    @model_validator(mode="after")
    def _caller_id_ring_only(self) -> ProviderCallStateUpdate:
        """``caller_id`` must be ``None`` (round 27, review-17 item
        45): caller identity is established once, on the ring
        ingress - post-ring updates carry no caller field."""
        if self.caller_id is not None:
            raise ValueError("caller_id is reported on the ring ingress only")
        return self

    @model_validator(mode="after")
    def _correlators_are_alternative(self) -> ProviderCallStateUpdate:
        """``call_token`` and ``lease_id`` are ALTERNATIVE
        correlators (round 25, review-15 item 41): the token names a
        still-open inbound ring, the lease an established call -
        carrying BOTH makes the update's binding ambiguous (which
        record does the state transition apply to?) and carrying
        neither is the documented external-call/open-ring-by-line
        case above."""
        if self.call_token is not None and self.lease_id is not None:
            raise ValueError("call_token and lease_id are mutually exclusive")
        return self

    @model_validator(mode="after")
    def _reason_only_on_disconnected(self) -> ProviderCallStateUpdate:
        """``reason`` is a teardown fact - it may ride ONLY
        ``state == DISCONNECTED`` (round 26, review-16 item 3),
        mirroring the :class:`CallStateChangedEvent` rule: a reason
        on a live provider state would report why a still-live call
        ended."""
        if self.reason is not None and self.state is not ProviderCallState.DISCONNECTED:
            raise ValueError("reason is allowed only on state 'disconnected'")
        return self


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

    line_id: _CheckedLineId
    assignment_id: _CheckedAssignmentId
    caller_id: _CheckedCallerNumber | None = None
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

    line_id: _CheckedLineId
    assignment_id: _CheckedAssignmentId
    caps: tuple[Intent, ...]
    """Replacement capabilities - same non-empty telecom-only
    invariant as ``ProviderRegistration.caps`` (round 24, review-13
    M3)."""
    profile: str | None = Field(default=None, min_length=1, max_length=MAX_PROFILE_LEN)
    """Bounded when present (round 26, review-16 item 4): same
    ``1..MAX_PROFILE_LEN`` rule as :attr:`ProviderRegistration.profile`.
    ``None`` means "leave the stored profile unchanged" (round 27,
    review-17 item 25) - there is no clear-profile operation in v1:
    a provider drops its label by re-registering without one."""

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

    line_id: _CheckedLineId
    call_token: _CheckedCallToken
    policy: FallbackPolicy
    reason: str | None = Field(default=None, max_length=MAX_REASON_LEN)
    """Free-form decision context for logs/telemetry (round-21 review
    audit: no closed vocabulary is documented for this internal field,
    so it does NOT ride CallEndReason). Bounded at
    :data:`MAX_REASON_LEN` like every other free-form reason (round
    27, review-17 item 27)."""
    fallback_call_token: _CheckedCallToken | None = None
    """Re-issued ring token for WORKER policies (round 14): bound to
    ``system:fallback_worker``, single-use, TTL-bound, bypasses the
    exclusive-owner check so FallbackWorker can call accept() after
    the original call_token went stale. None for CORE policies.
    Delivered to the worker inside ``event.fallback_triggered``
    ONLY - the sole fallback delivery channel (round 22, review-10
    MEDIUM-2). On the worker's accept it flows to the provider
    through the SAME ``line.answer{call_token}`` field the original
    token used - ring correlation is BY LINE, so no provider-side
    token table change (round 25, review-15 item 10)."""

    @model_validator(mode="after")
    def _worker_token_matches_policy_kind(self) -> FallbackDecision:
        """The re-issued ``fallback_call_token`` IS the worker
        delivery (round 25, review-15 item 31): a worker policy
        (``_WORKER_FALLBACK_TYPES``) MUST carry it and a core policy
        MUST NOT - a token on a core decision would be an orphaned
        accept credential, and a tokenless worker decision could
        never be delivered via ``event.fallback_triggered``."""
        is_worker = isinstance(self.policy, _WORKER_FALLBACK_TYPES)
        if is_worker != (self.fallback_call_token is not None):
            raise ValueError("fallback_call_token must be set exactly for worker fallback policies")
        return self


# ---------------------------------------------------------------------------
# Injected pure ports (round 21, MEDIUM-9)
#
# ``InMemoryLineManager`` (M1) takes these through its constructor so
# the kernel stays deterministic in tests - a scripted ``Clock``
# instead of ``datetime.now``/``sleep``, a canned ``MediaAllocator``
# instead of a socket allocator. The ports are one-way inputs and
# hold NO kernel reference.
# ---------------------------------------------------------------------------


@runtime_checkable
class Clock(Protocol):
    """Monotonic aware-UTC time source injected into the kernel.

    The kernel calls it inside mutations; every returned datetime
    MUST satisfy ``ensure_aware_utc`` and MUST NOT move backwards
    relative to the previous call - tick-order bookkeeping
    (orphan deadlines, grant TTLs) depends on monotonicity.

    Single timebase (round 27, review-17 item 5): this port is the
    ONLY stamping authority inside the kernel - deadlines minted
    here (``expires_at``, ``orphan_deadline_at``, grant TTLs) are
    evaluated by ``tick(now)``, so the runtime MUST feed ``tick``
    values of the SAME clock (``tick(clock())``). Two independent
    sources would drift every deadline by the offset between
    them.

    Monotonicity requirement (round 25, review-15 item 42): the
    implementation MUST derive its value from a MONOTONIC source -
    e.g. ``time.monotonic()``/``CLOCK_MONOTONIC`` anchored to UTC at
    boot - never from the raw wall clock. A wall-clock jump (NTP
    step, manual date change, suspend/resume skew) must never move
    deadlines backwards (extending a grant TTL or orphan window) nor
    skip them forward arbitrarily (mass-expiring leases) - only
    monotonic elapsed time may drive deadline arithmetic.

    ``@runtime_checkable`` (round 25, review-15 item 41) so fakes
    and adapters can be ``isinstance``-probed at wiring time."""

    def __call__(self) -> datetime: ...


@runtime_checkable
class MediaAllocator(Protocol):
    """Owns the per-lease media port lifecycle (round 21, CRITICAL-2;
    lifecycle corrected round 23, review-12 C3).

    ``allocate`` issues the endpoint plus the two independent
    one-time tokens of :class:`MediaCredentials`. The kernel calls
    ``allocate`` strictly inside ``dial()`` and ``accept()`` - NOT
    at grant time: a preempted or TTL-expired grant would strand
    live tokens in the router for the whole
    :data:`MEDIA_PAIR_TTL_SEC` teardown window (round 25, review-15
    item 30: the half-connected-pair TTL was raised from ~5 s to
    30 s because MCP LLM agents take >5 s between tool calls - the
    longer window makes the stranding cost strictly worse). The
    kernel splits the pair - agent token into the agent result,
    provider token into the outbox command.

    ``reissue`` (round 28, review-18 item 2) replaces the
    credentials of ONE role of a live pair while the pair itself
    survives: the kernel calls it when a single media side needs
    fresh credentials (agent ``call.reattach_media``, provider
    ``provider.reattach_media`` or the reregister supersede that
    re-pairs every preserved lease). The old token of the
    addressed role is invalidated AT ISSUE TIME and the other
    role's token and socket are untouched - the two halves of a
    pair have independent lifetimes since round 21.

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

    def allocate(
        self, lease_id: LeaseId, line_id: LineId, session_id: SessionId | None = None
    ) -> MediaCredentials:
        """Issue the endpoint pair + the two one-time tokens of
        :class:`MediaCredentials` for ``lease_id``.

        ``session_id`` (round 25, review-15 item 6) identifies the
        acquiring agent session: the allocator resolves it against
        the session-transport table the ADAPTER registers at
        session attach (round 27, review-17 item 59 - the formerly
        implicit "session's transport profile" is now typed): each
        session records its :data:`MediaTransport` at connect time
        and the allocator maps transport -> endpoint (``unix://``
        for a local agent socket, ``wss://`` for a remote one) - the
        kernel passes no transport knowledge itself. ``None`` means
        "no agent-side profile known"
        (e.g. a lease minted for the internal FallbackWorker); the
        allocator then falls back to the line's default agent
        endpoint. The ``provider_media_endpoint`` is always the
        provider-role address of the same pair."""
        ...

    def reissue(self, lease_id: LeaseId, role: MediaRole) -> MediaCredentials:
        """Replace the credential half of ONE ``role`` of the live
        pair bound to ``lease_id`` (round 28, review-18 item 2).

        Returns the pair view AFTER the reissue: the addressed
        role's ``*_media_endpoint``/``*_media_token`` fields carry
        the fresh values and the other role's fields are the
        still-valid existing values - the kernel reads only the
        requested role's half (agent half for
        ``call.reattach_media``/``MediaReattachResult``, provider
        half for ``provider.reattach_media``/
        ``ProviderReattachMediaCommand`` and the reregister
        supersede re-pairing).

        Rules: the old token of the addressed role is invalidated
        AT THE ISSUE INSTANT (a lease holds a single live token
        per role - the replay window closes at reissue, same
        supersede rule as round 26 review-16 item 8); the other
        role's token and socket are untouched; and the operation
        is idempotent BY ``lease_id`` ONLY while the pair is live
        - a retried reissue supersedes the retry's own fresh
        token cleanly, but a ``lease_id`` whose pair was
        ``revoke``d or never ``allocate``d MUST NOT mint
        credentials (reject - silently resurrecting a dead pair
        would bypass ``revoke``'s no-socket-outlives-lease
        guarantee)."""
        ...

    def revoke(self, lease_id: LeaseId) -> None:
        """Close every media socket/credential bound to ``lease_id``.
        Called on every ``CallLease -> ENDED``; idempotent - a
        second revoke (or a lease that never allocated) is a no-op.

        Ordering (round 29, SA_quant_review_1 slice-A item 9): the
        revoke completes INSIDE the same kernel mutation that ends
        the lease - before the mutating method returns - so a
        ``drain_events()`` teardown event can never announce a dead
        lease whose media sockets are still open; no socket
        outlives its ``lease_id`` for an instant observable outside
        the mutation."""
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
    - Unknown ``session_id`` (round 27, review-17 item 23): EVERY
      method that takes a ``session_id`` parameter resolves an
      unknown or already-dropped session as
      ``PhonebridgeError(ErrorCode.SESSION_NOT_FOUND)`` raised by
      the KERNEL - the code is owned here, not at the transport
      layer, so an adapter surfaces it verbatim and never needs to
      guess between SESSION_NOT_FOUND and OWNER_MISMATCH. (A
      ``drop_session`` on an already-gone session stays idempotent
      by its own contract - it is the teardown fact, not a lookup.)

    Driver methods are KERNEL-INTERNAL (round 29, SA_quant_review_1
    slice-A item 2 - normative, docs/specs/line-manager.md
    "Driver methods are kernel-internal (round 29)"): ``tick``,
    ``decide_fallback``, ``drain_events`` and
    ``drain_provider_commands`` are lifecycle drains/drivers the
    runtime invokes out-of-band - they are public on the ABC only
    because the single-owner kernel and its adapters need the
    entry points, NOT because they are wire calls. A transport
    adapter MUST NOT route any wire RPC to a driver method: the
    wire surface is EXCLUSIVELY the methods mapped in
    docs/api-contract.md (``agent.*``/``call.*``/``provider.*`` on
    this surface, ``admin.*`` on :class:`LineAdminKernel`). A
    wire-facing route named like a driver method - or an adapter
    dispatch table built by iterating this ABC's methods - is a
    contract violation: the only legal invocation of
    ``decide_fallback`` is the kernel's own deadline path inside
    ``tick`` (see its docstring), ``tick`` is driven by the
    runtime's clock loop, and the two drains are called by the
    adapter AFTER kernel calls, never in response to a wire
    request.
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

        Reserved ``system:*`` identities (round 25, review-15 item
        21): the transport adapter MUST reject an ``agent_id``
        starting with :data:`SYSTEM_AGENT_PREFIX` presented on an
        external socket with ``UNAUTHORIZED`` - the prefix is
        reserved for kernel-internal service identities provisioned
        out-of-band (:data:`SYSTEM_FALLBACK_WORKER`), and API-key
        issuance must never bind a ``system:*`` identity to an
        external scope.

        Session caps (round 25, review-15 item 30; rule 7): at most
        :data:`MAX_SESSIONS_PER_AGENT` concurrent open sessions per
        ``agent_id`` AND at most :data:`MAX_SESSIONS_TOTAL` open
        sessions across ALL agent ids (round 27, review-17 item 73 -
        the per-agent cap alone cannot bound kernel session state
        against an unbounded set of distinct identities) - overflow
        on either is RESOURCE_EXHAUSTED with
        ``ErrorReason.SESSION_LIMIT_EXCEEDED``.

        Re-register semantics (round 27, review-17 item 70): every
        ``agent.register_intent`` call mints a NEW ``session_id`` -
        there is no "update intents" operation. A reconnecting agent
        does NOT resume its old session: the old one's tickets and
        leases were socket-bound and died with it via
        ``drop_session``; the new session starts clean (deliberate -
        session state must never migrate between sockets).

        ``profile`` (round 27, review-17 items 27/80): optional
        agent-side session label - a human-readable tag surfaced in
        observability/diagnostics for THIS session, unrelated to
        :attr:`ProviderBinding.profile` (a provider label) or
        :attr:`LineAssignment.profile` (a stored line label).
        Bounded at :data:`MAX_PROFILE_LEN` - empty or overlong is
        INVALID_ARGUMENT.

        Ring re-delivery (round 25, review-15 item 25; symmetric
        pool rule added round 27, review-17 item 10): when a line
        currently holds a live ring (``RINGING_LOCKED`` with an
        unconsumed ``call_token``), the kernel re-emits
        :class:`IncomingCallEvent` to the newly opened session as a
        fresh ``SessionAudience`` envelope carrying the SAME
        ``call_token`` and the original ``expires_at`` - for an
        EXCLUSIVE line only when the registering agent OWNS it, for
        a SHARED_POOL line to ANY new session holding
        ``Intent.RECEIVE_INBOUND`` (the token is still legally
        acceptable by that session, so hiding it would strand the
        ring until ``accept_window`` expiry even though an eligible
        agent just connected). The ``system:fallback_worker``
        session is excluded from that pool delivery (round 28,
        review-18 item 16): the worker is dispatched via
        ``event.fallback_triggered``, never as an ``incoming_call``
        fan-out subscriber.

        Grant re-delivery (round 27, review-17 item 2): a
        registering agent that OWNS an exclusive line receives a
        replayed :class:`AssignmentGrantedEvent` carrying its
        current ``assignment_id`` - the original emit could have
        predated every live session.
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
        ``Intent.DIAL_OUT`` must additionally be among the SESSION's
        registered intents (round 28, review-18 item 16): a session
        that never declared ``dial_out`` at ``register_intent`` gets
        INVALID_INTENT too - intent capability is per-session, not
        per-key.
        ``profile`` (round 27, review-17 items 27/80): optional
        per-call label carried onto the granted lease for
        observability - same ``1..MAX_PROFILE_LEN`` bound as
        :meth:`register_intent`'s session label, INVALID_ARGUMENT
        beyond. The kernel MUST save the accepted value on
        :attr:`CallLease.profile` (round 28, review-18 item 13) -
        the lease carries the label, it is never recomputed. The
        label MUST also survive queueing (round-28 fix, PR #72
        review): a queued acquire records it on
        :attr:`QueueTicket.profile`, and the lease minted when that
        ticket is granted inherits the ticket's value verbatim -
        same for the head-of-queue reissue of a preempted grant,
        which carries the ORIGINAL ticket's profile forward.
        INVALID_INTENT when the intent is not in the line's caps;
        LINE_EXCLUSIVE_NOT_OWNER (+ ErrorData.free_lines) on exclusive
        conflict; LINE_BUSY_INCOMING on a RINGING_LOCKED line. A busy
        pool returns AcquireRefused - routine result, never an error.
        ``free_lines`` inside a refusal carries only lines that could
        actually serve a retry (round 28, review-18 item 16):
        ``SHARED_POOL`` lines advertising ``DIAL_OUT`` whose binding
        is neither ``UNBOUND`` nor ``UNREACHABLE`` - a foreign
        EXCLUSIVE line or a dead/unbound pool line must never be
        suggested as an alternative.
        Retry/dedup semantics (round 20): retry dedup is scoped to the
        SAME non-empty ``(session_id, idempotency_key)`` pair - an
        identical retry while queued returns the SAME ticket. Calls
        WITHOUT a key always create independent tickets/grants
        (subject to the per-session limit, RESOURCE_EXHAUSTED /
        SESSION_LIMIT_EXCEEDED on overflow), so a multi-call agent
        can run N parallel acquires. Queue bounds (round 25,
        review-15 item 30; rule 7): pending tickets are capped at
        :data:`MAX_TICKETS_PER_SESSION` per session and
        :data:`MAX_QUEUE_TICKETS_TOTAL` globally - overflow is
        RESOURCE_EXHAUSTED with ``ErrorReason.QUEUE_FULL``; sibling
        bound :data:`MAX_LEASES_PER_SESSION` caps the session's live
        leases.

        Queue liveness rules (round 28, review-18 item 12): a
        head-of-queue ticket whose session is AT
        :data:`MAX_LEASES_PER_SESSION` is SKIPPED at grant time -
        it keeps its ``seq`` and position and the next eligible
        ticket is served (no head-of-line blocking); the lease
        count is re-evaluated per grant attempt, so the skip ends
        the moment the session frees a lease. A ticket
        re-materialization (preemption reissue) never re-checks
        :data:`MAX_TICKETS_PER_SESSION`/
        :data:`MAX_QUEUE_TICKETS_TOTAL` - it replaces a live grant
        (net-zero occupancy), so the queue caps guard only NEW
        ingress. A pool ticket (one without a preferred
        ``line_id``) is cancelled ``queue_cancelled{reason:
        "provider_lost"}`` the moment the set of eligible
        ``SHARED_POOL`` lines with ``DIAL_OUT`` caps and a binding
        that is neither ``UNBOUND`` nor ``UNREACHABLE`` becomes
        empty - the check runs on every liveness/caps/assignment
        mutation, so a queued ticket can never wait on a pool that
        can never serve it. The dedup table records the OUTCOME of
        a keyed acquire (the granted ``lease_id`` or the queued
        ``ticket_id``): a same-key retry returns the recorded
        outcome while its object lives, and the same key with
        different parameters is INVALID_ARGUMENT; entries die with
        the session as documented below.

        ``system:fallback_worker`` (round 22, review-10 MEDIUM-2; the
        reserved :data:`SYSTEM_FALLBACK_WORKER` identity, round 25
        item 21): a session owned by that agent id BYPASSES
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
        wish is honored). The reissue notification is two-channel
        (round 26, review-16 item 7): the lease teardown itself
        carries the new ticket id as
        ``event.call_state_changed{state: "disconnected",
        reason: "preempted_by_incoming",
        reissued_ticket_id: <ticket>}`` - see
        :attr:`CallStateChangedEvent.reissued_ticket_id` - and the
        ``event.queue_updated{position: 1}`` names the same id, so
        an agent holding several parallel acquires can correlate
        which preempted ``lease_id`` became which ``ticket_id``.
        ``enqueue=False`` grants are torn down without reissue.

        Ingress ``seq`` semantics (round 25, review-15 item 7): the
        global monotonic ``QueueTicket.seq`` counter is minted at
        acquire-INGRESS for EVERY ``call.acquire`` - immediate
        grants consume a seq too, and so do refusals and
        deduplicated retries, so gaps in the sequence are expected
        (round 27, review-17 item 49 - see :class:`QueueTicket`) -
        so the preempted
        ``enqueue=True`` immediate grant above re-materializes a
        ticket reusing its ORIGINAL ingress seq. That seq is by
        construction lower than any ticket queued after it, so the
        head insertion needs no negative or fractional seq value.
        The dedup table lives only inside the LIVE session (round
        27, review-17 item 22): ``drop_session`` discards a
        session's ``(session_id, idempotency_key)`` entries with
        its tickets, so a key never dedups across a reconnect. It
        is also BOUNDED (round 29, SA_quant_review_1 slice-B item
        6; rule 7): at most :data:`MAX_IDEMPOTENCY_KEYS_PER_SESSION`
        recorded outcomes per session - a long-lived session
        minting a fresh key per call would otherwise grow kernel
        memory without bound. Once the cap is reached a NEW key is
        rejected ``RESOURCE_EXHAUSTED`` fail-closed; the table is
        NEVER LRU-trimmed, because evicting a recorded outcome
        would let a same-key retry re-execute the acquire the dedup
        table exists to absorb.
        ``position`` inside :class:`AcquireQueued` is the GLOBAL
        FIFO rank across all eligible tickets (item 48).
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
        session (:data:`SYSTEM_FALLBACK_WORKER`, round 25 item 21)
        is exempt: it accepts with the single-use
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
        INVALID_DTMF (round 20). INVALID_STATE unless the lease is
        ACTIVE or ALERTING AND its ``audio_ready`` was received
        (round 27, review-17 item 13 - tightens the round-25/26 gate
        which allowed bare ALERTING): in-band tones are synthesized
        by the LineProvider into ``phonebridge_uplink``, so before
        the provider reports SCO-up there is literally no uplink
        for a tone to ride - the ``0.3-0.6 s`` gap the ALERTING
        early-media rule was meant to close would swallow them
        anyway. Early media is precisely "SCO up while ALERTING",
        so ALERTING + ``audio_ready`` remains legal; the kernel
        reads the flag off ``CallLease.audio_ready_received``
        (item 53). Tones ride :class:`ProviderSendDtmfCommand`
        (wire contract: docs/api-contract.md, round 26, review-16
        item 6)."""
        ...

    @abstractmethod
    async def reattach_media(self, session_id: SessionId, lease_id: LeaseId) -> MediaReattachResult:
        """call.reattach_media (round 25, review-15 item 9): mint a
        FRESH agent-side media token pair entry for a LIVE lease
        after the agent's media socket dropped - the agent-driven
        counterpart of :class:`ProviderReattachMediaCommand`.

        Media tokens are one-shot: a dropped media socket burns its
        token while the lease and the call stay alive, so the agent
        needs a new ``(media_endpoint, media_token)`` pair to
        re-authenticate ``media.auth{role: "agent"}``. The reissued
        pair SUPERSEDES the prior agent-side pair for the lease
        (round 26, review-16 item 8): the old ``media_token``
        becomes invalid at issue time - a lease holds a single
        active pair per role - bounding the replay window to the
        reissue instant rather than the full pair TTL. The kernel
        MUST mint it through the per-role
        ``MediaAllocator.reissue(lease_id, role="agent")`` port
        call (round 28, review-18 item 2) - only the agent half is
        replaced, the provider half's token and socket are
        untouched.
        ``OWNER_MISMATCH`` on a foreign ``(session_id, lease_id)``
        pair; ``STALE_TOKEN`` on a dead/ended lease - a reattach can
        never resurrect a torn-down media path."""
        ...

    @abstractmethod
    async def reattach_provider_media(
        self, request: ProviderMediaReattachRequest
    ) -> ProviderMediaReattachResult:
        """provider.reattach_media ingress (round 27, review-17 item
        4): mint FRESH PROVIDER-role media credentials for a LIVE
        lease when the provider's media socket dropped while its
        control channel stayed up - the case
        :class:`ProviderReattachMediaCommand` cannot cover because
        that command rides the ``provider.reregister`` path, which
        requires the CONTROL socket to drop first.

        ``request.assignment_id`` must resolve to the LIVE binding
        and ``request.lease_id`` to a live lease owned by that
        binding - a stale binding id is dropped
        (``PhonebridgeError(ErrorCode.STALE_TOKEN)``), a dead or
        foreign lease is ``STALE_TOKEN``/``OWNER_MISMATCH``. The
        reissued pair SUPERSEDES the prior provider-side pair at
        issue time (round 26, review-16 item 8 - single active pair
        per role) and obeys a FRESH ``MEDIA_PAIR_TTL_SEC`` handshake
        window counted from THIS reissue (round 27, review-17 item
        30 - the dead socket's old deadline is meaningless; the
        provider MUST re-run ``media.auth{role: "provider"}`` with
        the new token inside the new window). The kernel MUST mint
        it through the per-role
        ``MediaAllocator.reissue(lease_id, role="provider")`` port
        call (round 28, review-18 item 2) - only the provider half
        is replaced, the agent half's token and socket are
        untouched."""
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
        ``drop_session`` cancels them.

        Unbound-line rule (round 27, review-17 items 35/60): a
        configured line that never registered a provider is VISIBLE
        in ``call.get_lines`` (``liveness=UNBOUND``) but NOT
        claimable - a durable exclusive assignment to a providerless
        line could serve no one - so a claim on it resolves as
        ``PhonebridgeError(ErrorCode.INVALID_STATE,
        ErrorData(reason=ErrorReason.LINE_NOT_CLAIMABLE))``, NOT
        LINE_NOT_FOUND (the line exists; only provider absence
        blocks the claim). ``LINE_NOT_FOUND`` is reserved for a
        ``line_id`` absent from the configured line set entirely."""
        ...

    @abstractmethod
    async def get_lines(
        self,
        session_id: SessionId,
        intent: Intent | None = None,
        *,
        offset: Annotated[int, Field(ge=0)] = 0,
        limit: Annotated[int, Field(ge=1, le=MAX_LIST_PAGE)] = MAX_LIST_PAGE,
    ) -> list[LineSummary]:
        """call.get_lines: pool view filtered by intent. Always bounded
        (rule 7): the line set is finite config, not a stream.

        Bounded pagination (round 29, SA_quant_review_1 slice-B item
        1; PR #74 review): ``offset``/``limit`` carry the shared wire
        ``{offset?, limit?}`` window - a negative ``offset`` or a
        ``limit`` outside ``1..MAX_LIST_PAGE`` is INVALID_ARGUMENT
        (the ``Field`` bounds reject it at the model boundary). The
        page is a STABLE-ORDERED slice: rows enumerate in
        ``line_id`` order, so offset-paging a quiescent listing
        never skips or repeats a row; a full ``limit``-sized page
        means "advance ``offset`` by ``limit``", a short page ends
        the list.

        ``intent=Intent.EXCLUSIVE_LINE`` filter semantics (round 24,
        review-13 M3): returns the lines whose
        ``assignment.claimable is True and assignment.mode ==
        AssignmentMode.SHARED_POOL`` - exclusivity AVAILABILITY is a
        ``LineAssignment`` property, never a provider capability;
        ``ProviderBinding.caps`` carries only the telecom intents
        ``dial_out``/``receive_inbound``. The filter ADDITIONALLY
        excludes ``UNBOUND`` lines (round 28, review-18 item 14):
        only genuinely claimable lines are listed - a providerless
        line can never serve a new exclusive owner, so an
        availability filter must not advertise it (the line still
        appears under the unfiltered/other-intent listing as the
        configured-fleet row).

        Exclusive-line privacy (round 25, review-15 item 44): for a
        session that does NOT own an EXCLUSIVE line, OTHER agents'
        EXCLUSIVE lines are omitted from the listing entirely - the
        caller sees SHARED_POOL lines plus the lines it owns. The
        ``state`` column stays the call-occupancy axis while
        ``liveness`` carries provider health separately (item 15).

        Visibility of provider-less lines (round 27, review-17 item
        60): a configured line that never registered a provider IS
        listed with ``liveness=UNBOUND``, ``caps=()`` and the stored
        ``LineAssignment.profile`` label - the derived row exists so
        operators see the whole configured fleet; it is not
        claimable (see :meth:`claim_exclusive`). On rows for lines
        the caller owns EXCLUSIVELY the kernel fills
        ``LineSummary.assignment_id`` (item 2) - the durable id the
        owner needs for ``call.release{assignment_id}`` when the
        ``event.assignment_granted`` copy predated its session."""
        ...

    # --- provider ingress (narrowed: facts and decisions, not transport) ---

    @abstractmethod
    async def register_provider(self, registration: ProviderRegistration) -> ProviderBinding:
        """Provider attach fact -> ProviderBinding (round 20; the
        ≤0.0.12 signature returned the agent-side LineAssignment,
        conflating two object spaces).

        This is the DISTILLED domain fact of a provider.register, not
        the WSS transport method - credential checks and socket
        lifecycle stay in the adapter: the wire ``token`` param is
        authenticated against the API-key store BEFORE
        :class:`ProviderRegistration` is even constructed (round 27,
        review-17 item 32 - the model no longer carries it).

        Register vs reregistration (round 28, review-18 item 3):
        ``registration.reregister`` is the adapter-distilled flag of
        WHICH wire method arrived - ``provider.register`` sets
        ``False``, ``provider.reregister`` sets ``True``; it is
        derived from the method name alone, and a ``reregister``
        key in the wire params is ignored, never copied
        (:class:`ProviderRegistration`). A
        ``register`` fact (``reregister=False``) arriving while the
        line holds a LIVE binding is rejected
        ``PhonebridgeError(ErrorCode.INVALID_STATE)`` - a duplicate
        attach is a protocol violation, not a supersede. A
        ``reregister`` fact (``reregister=True``) on a live
        REACHABLE **or DEGRADED** binding is the legal half-open
        supersede (round-28 fix, PR #72 review: DEGRADED is still a
        live-control state - the socket is up, only pings are
        late - so it shares the supersede path, while
        WS_DISCONNECTED keeps the orphan-grace ws-loss reattach
        path and UNREACHABLE falls under the dead-binding
        fresh-register rule below): the provider's new control
        channel arrived before the old
        socket's death was detected, so the kernel marks the OLD
        binding superseded, mints a FRESH ``assignment_id``, and
        preserves/re-binds the live leases per the
        ``state_snapshot`` reconciliation rules below.

        Reregister determinism under a reconnect storm (round 29,
        SA_quant_review_1 slice-A item 6): the supersede applies
        to the line's CURRENT live binding - whatever it is at the
        instant this mutation runs - including a binding minted
        moments earlier by a racing ``provider.reregister``.
        Sequential last-writer-wins: each reregister supersedes
        exactly one live binding, mints a fresh id and re-binds
        the preserved leases; the outcome is fully determined by
        kernel arrival order, never ambiguous, and never produces
        a "reregister rejected on an already-reregistered binding"
        verdict. Facts arriving on the SUPERSEDED binding's id
        afterwards - a ``provider.ping``/liveness fact, a
        ``provider.call_state`` update, a ring fact, a
        capability_update - resolve through the
        ``_closed_assignments`` ring and are rejected
        ``PhonebridgeError(ErrorCode.STALE_TOKEN)`` (round 29,
        SA_quant_review_1 slice-B item 2): the superseded control
        socket is dead for ingress the instant the supersede
        commits, and the ADAPTER MUST close that socket after
        delivering the supersede reply - a lingering socket can
        never carry a valid fact again.

        When
        ``state_snapshot`` is ``None`` the leases are preserved
        WITHOUT reconciliation (round 28 - chosen normative
        answer): the provider declared no view, and the binding
        hand-over alone does not kill calls. When present, the
        snapshot is authoritative ONLY for leases in
        :data:`SNAPSHOT_RECONCILED_PHASES` (round 29,
        SA_quant_review_1 slice-A item 7 - the formal
        "dialing-or-later" set: ``DIALING``/``ALERTING``/
        ``ACTIVE``/``TERMINATING``; an ``ACQUIRED`` or
        ``RINGING`` lease was never dialed, so an empty snapshot
        does not kill it). Reregister inside the
        orphan-grace window stays idempotent and preserves
        in-flight leases (wire: provider.reregister, 10 s orphan
        grace, §5.4). On the first
        registration of a new ``line_id`` the kernel ALSO initializes
        the line's default ``LineAssignment`` (SHARED_POOL,
        owner_agent_id=None) if none exists - the binding itself is
        the returned record.

        The returned :class:`ProviderBinding` stamps
        ``heartbeat_interval_ms`` from the line's EFFECTIVE
        ``LineTimeouts.heartbeat_interval_sec`` (round 24,
        review-13 C2) via :func:`heartbeat_interval_ms_from_sec`
        (nearest ms, halves up, min 1 ms - rounds 24c/27) so the adapter
        can answer
        ``provider.register``/``provider.reregister`` with the wire
        ``heartbeat_interval_ms`` - a ``configure_line`` patch to the
        interval applies on the NEXT reregister, never
        retroactively to the stamped session value. The
        ``heartbeat_degraded_after``/``heartbeat_down_after``
        thresholds stamp the same way from the effective
        ``LineTimeouts`` (round 25, review-15 item 32): ``tick(now)``
        escalates missed pings against the STAMPED pair, so a
        mid-session thresholds patch takes effect on the next
        reregister only.

        Media reissue on reregister (round 25, review-15 item 9): on
        a successful reregister that preserves in-flight leases the
        kernel mints a NEW provider media token per preserved lease
        through the per-role
        ``MediaAllocator.reissue(lease_id, role="provider")`` port
        call (round 28, review-18 item 2 - the provider half alone
        is replaced; the agent half's token and socket are
        untouched) and enqueues :class:`ProviderReattachMediaCommand`
        in the
        outbox - media tokens are one-shot and do NOT survive a
        socket drop, so the dead session's burned
        ``provider_media_token`` can never authenticate the new
        media socket. The reissued pair SUPERSEDES the prior
        provider-side pair for the lease (round 26, review-16
        item 8): the old ``media_token`` becomes invalid at issue
        time - a lease holds a single active pair per role -
        bounding the replay window to the reissue instant rather
        than the full pair TTL.

        Dead-binding outbox rule (round 28, review-18 item 4 -
        moves the round-26 drain-time drop to mutation time):
        commands still queued for the binding the reregister
        replaced are handled INSIDE this mutation, not at
        :meth:`drain_provider_commands` time. Commands addressed to
        leases that survive the re-bind (``line.hangup``,
        ``line.send_dtmf``, ``line.reattach_media``) are RETARGETED
        to the NEW ``assignment_id`` - dropping a queued
        ``line.hangup`` would leave a ghost call on the modem.
        Commands that can never land (the superseded binding's own
        in-flight ``line.dial``/``line.answer`` whose lease did not
        re-bind) are dropped fail-closed in the same transaction,
        and their leases end with the
        ``disconnected``/``network_error`` effect so the teardown
        events reach the SAME cycle's ``drain_events()``.

        Reregister on a DEAD binding (round 27, review-17 item 21):
        once the orphan-grace window expired or the binding was
        declared UNREACHABLE, ``provider.reregister`` is classified
        as a FRESH register - the kernel mints a new
        ``assignment_id``, tears nothing down twice (the dead
        binding's leases already ended), and treats any
        ``state_snapshot`` entries as provider-reported
        external-call facts (their ``lease_id``s cannot re-bind:
        the leases are gone) - there is no "late reregister"
        verdict distinct from either legal path.
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
        authenticates the already-registered peer ONLY in
        combination with arrival on that binding's own live control
        channel (round 27, review-17 item 31): the adapter MUST
        verify the request's ``assignment_id`` equals the binding
        id of the socket it arrived on - bare knowledge of an id
        (e.g. read off ``admin.list_provider_bindings``) is not a
        credential, so the RPC can NEVER be folded into
        ``register_provider``. Unknown/stale binding id ->
        PhonebridgeError (a superseded id resolves through the
        ``_closed_assignments`` ring as
        ``PhonebridgeError(ErrorCode.STALE_TOKEN)`` - the shared
        superseded-binding ingress rule of round 29,
        SA_quant_review_1 slice-B item 2); an update identical to the current
        caps/profile is an idempotent no-op; a capability shrink
        does NOT tear down live leases that relied on the old
        caps (their facts were granted already) but no new grants
        are made against removed intents. A shrink DOES cancel every
        pending queue ticket that asked for the removed intent -
        ``event.queue_cancelled{reason: "capability_removed"}``
        (round 25, review-15 item 23): a ticket that can never be
        granted is cancelled, never silently parked.

        ``profile`` patch semantics (round 27, review-17 item 25):
        ``None`` means "leave unchanged" - the standard
        None-means-unchanged convention. There is deliberately NO
        clear-profile flag in v1: a provider that wants to drop its
        label registers ``profile`` anew on its next
        register/reregister; the wire surface stays free of a
        second, redundant mutation path."""
        ...

    @abstractmethod
    async def note_provider_liveness(self, liveness: ProviderLiveness) -> None:
        """Ingest a provider liveness FACT (reachable/ws_disconnected/
        unreachable + timestamp). Drives the provider-HEALTH axis:
        transitions surface as ``event.line_health_changed``
        (round 25, review-15 item 15 - never ``line_state_changed``),
        an UNREACHABLE binding tears its leases down per §5.4,
        cancels its pending tickets as
        ``event.queue_cancelled{reason: "provider_lost"}`` (item 23),
        rejects its pending claims as
        ``event.claim_resolved{status: "rejected",
        reason: "provider_lost"}`` (round 28, review-18 item 14)
        and feeds PROVIDER_UNREACHABLE outcomes. Idempotent:
        replaying a stale fact is harmless; ordering is by
        checked_at.

        ``checked_at`` is an ADVISORY dedup/ordering stamp on the
        provider's clock (round 28, review-18 items 8/11; skew
        asymmetry clarified in the round-28 fix, PR #72 review): a
        same-stamp repeat is a duplicate, and ONLY a stamp deeper
        than :data:`MAX_CLOCK_SKEW_SEC` into the FUTURE is
        normalized to kernel ``now`` at ingestion - a PAST stamp
        is retained VERBATIM as the dedupe/ordering key and NEVER
        feeds elapsed-time or liveness math. The clamp is applied
        through :func:`clamp_provider_stamp` (round 29,
        SA_quant_review_1 slice-A item 4) - the single shared
        implementation of this rule. The binding's
        ``last_seen_at`` is ALWAYS kernel receipt time and every
        timer stays kernel-clock, so provider-clock skew can
        neither freeze nor fast-forward the heartbeat ladder.
        Dedup scope (round 29, SA_quant_review_1 slice-B item 5):
        the ``checked_at`` dedup/ordering key is scoped to
        ``(assignment_id, accepted_stamp)`` - a stamp replayed by a
        NEW binding can never collide with a superseded binding's
        recorded dedup entry, so a past-skewed stamp stays a
        harmless verbatim key instead of a false duplicate.

        A REACHABLE fact on the live channel clears a
        kernel-computed DEGRADED binding back to REACHABLE
        (round 28, review-18 item 1): ``degraded -> reachable`` is
        the recovery leg of the heartbeat ladder and surfaces as a
        normal ``event.line_health_changed`` transition. A
        WS_DISCONNECTED fact on a DEGRADED binding resolves
        ``degraded -> ws_disconnected`` the same way (round-28
        fix, PR #72 review): the socket-loss axis overrides the
        elapsed-time rung and arms the orphan-grace window.

        An UNREACHABLE fact also cleans the provider-command outbox
        AT THE MUTATION (round 28, review-18 item 4): commands
        still queued for the dying binding are dropped in the same
        transaction, and the in-flight
        ``line.dial``/``line.answer`` leases of dropped commands
        end with the ``disconnected``/``network_error`` effect
        inside the same transaction - their teardown events reach
        the SAME cycle's ``drain_events()`` rather than outliving
        the command drain (the adapters drain events first, so
        teardown events minted during a LATER command drain would
        sit unsent until the next transaction).

        Fact recording vs escalation (round 25, review-15 item 32):
        this method records facts and arms/disarms
        ``ProviderBinding.orphan_deadline_at`` - a WS_DISCONNECTED
        fact SETS the orphan window. A REACHABLE fact clears it ONLY
        when it arrives on the binding's own live control channel
        (round 27, review-17 item 62): inside the grace window the
        authoritative recovery is ``provider.reregister`` - a
        delayed ping stamped ``checked_at`` before the WS drop and
        replayed on a lingering socket MUST NOT disarm the orphan
        deadline, otherwise a dead session's last gasp could keep a
        zombie binding alive.
        The missed-ping DEGRADED->DOWN escalation is owned
        EXCLUSIVELY by ``tick(now)``, which compares the binding's
        missed-ping count against the thresholds STAMPED on the
        binding at register/reregister
        (``heartbeat_degraded_after``/``heartbeat_down_after``) -
        never against the line's current ``LineTimeouts``, so a
        mid-session ``configure_line`` patch cannot silently kill a
        healthy provider. The comparison is ELAPSED-TIME based
        (round 27, review-17 item 28): ``tick`` derives
        ``now - last_seen_at`` and escalates once it crosses each
        stamped threshold - there is no separate "missed count"
        counter and no per-interval jitter tolerance; the
        thresholds themselves ARE the tolerance. A ping arriving at
        ``interval*1.5`` is simply "on time" until
        ``heartbeat_degraded_after`` elapses.
        ``ProviderLiveness.assignment_id`` is
        REQUIRED (item 32): a fact naming a superseded binding id
        is never cross-applied to a re-registered binding - it
        resolves through the ``_closed_assignments`` ring and is
        rejected ``PhonebridgeError(ErrorCode.STALE_TOKEN)``
        (round 29, SA_quant_review_1 slice-B item 2): the same
        superseded-binding rule :meth:`register_provider` pins for
        EVERY ``provider.*`` fact - ping/liveness, call-state,
        ring and snapshot facts alike."""
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
        broadcast to dial_out-only sessions. The
        :data:`SYSTEM_FALLBACK_WORKER` session is excluded from that
        pool fan-out (round 28, review-18 item 16): the worker is
        dispatched via ``event.fallback_triggered`` after the
        accept-window decision, never as an ``incoming_call``
        subscriber; an EXCLUSIVE line's owner still receives the
        event even when the owner is not an ordinary pool
        subscriber.

        Returns the incoming_call event - the same instance is ALSO
        appended to the outbox inside the mutation transaction; the
        EventBus publishes exclusively from ``drain_events()``, the
        return value exists for caller convenience (round 20).
        Atomic w.r.t. concurrent acquires on the same line
        (single-event-loop invariant). Idempotent: re-ingesting a
        ring on an already RINGING_LOCKED line returns the existing
        event with the SAME ``call_token``.

        Line-state contract (round 24, review-13 H3; rewritten for
        the liveness split in round 25, review-15 item 15 - the glare
        matrix is defined for EVERY call-occupancy state, including
        the classic race where the agent's ``call.dial`` is still in
        flight to the provider while the cellular network lands a
        genuinely first inbound call on the handset - Call Waiting
        filtering cannot see it):
        - ``FREE`` -> transition to ``RINGING_LOCKED``, mint the
          ``call_token``;
        - ``RINGING_LOCKED`` -> idempotent return of the open ring's
          event (same ``call_token``) - including a ring whose
          kernel-internal ring-drain sub-phase is already running
          (round 29, SA_quant_review_1 slice-A item 1: the drain
          after ``MissedCallEvent`` is private bookkeeping with no
          wire-visible marker; ``RINGING_LOCKED`` stays the
          reported state throughout, so a re-reported ring during
          the drain resolves to the same open ring, not a new
          ``incoming_call``);
        - ``BUSY`` holding a lease still in ``CallPhase.ACQUIRED``
          (granted-but-undialed) on a binding whose ``caps`` include
          ``Intent.RECEIVE_INBOUND`` -> preempt the grant
          (``preempted_by_incoming``) and lock the ring;
        - every other case - ``BUSY`` in ``DIALING``/``ALERTING``/
          ``ACTIVE``, ``BUSY`` under an ``external_call`` hold, or a
          binding whose ``caps`` lack ``RECEIVE_INBOUND`` ->
          ``PhonebridgeError`` with ``ErrorCode.INVALID_STATE`` and
          the provider MUST hang up the glare call: the LineProvider
          maps that error (or a ``{ok: false}`` wire result) to a
          ``VoiceCall.Hangup`` on the inbound call object (and
          counts ``glare_inbound_rejected``) - it MUST NOT leave the
          glare call ringing and MUST NOT report the line
          ``RINGING_LOCKED``.

        Provider health is NOT a :class:`LineState` (round 25, item
        15): a ring report arriving on a binding marked
        ``LivenessState.UNREACHABLE`` is rejected
        ``INVALID_STATE`` - a dead provider cannot ring - while a
        ``WS_DISCONNECTED`` binding still accepts rings inside the
        orphan-grace window (the telephony path may be alive even
        though the control socket dropped).

        Stale-binding rings (round 27, review-17 item 67): a ring
        fact whose ``assignment_id`` names a superseded binding -
        delivered on a lingering socket after ``provider.reregister``
        rotated the id - is NOT idempotently absorbed by the
        RINGING_LOCKED line-correlation path: the kernel raises
        ``PhonebridgeError(ErrorCode.STALE_TOKEN)`` so the provider
        learns its facts ride a dead binding - the superseded id
        resolves through the ``_closed_assignments`` ring like
        every other ``provider.*`` fact (round 29,
        SA_quant_review_1 slice-B item 2). Idempotent same-token
        return applies only to a ring on the LIVE binding id."""
        ...

    @abstractmethod
    async def ingest_call_state(self, update: ProviderCallStateUpdate) -> None:
        """Call-state ingress: bounded ProviderCallState update mapped
        into lease phase transitions. Stale/out-of-order updates are
        deduped by observed_at; DISCONNECTED ends the lease - an
        ephemeral CallLease NEVER outlives its call (auto-release,
        round 22, review-10 HIGH-3, §5.4).

        ``observed_at`` is an ADVISORY dedup/ordering stamp on the
        provider's clock (round 28, review-18 items 8/11; skew
        asymmetry clarified in the round-28 fix, PR #72 review): a
        same-stamp repeat is a duplicate, and ONLY a stamp deeper
        than :data:`MAX_CLOCK_SKEW_SEC` into the FUTURE is
        normalized to kernel ``now`` at ingestion - a PAST stamp
        is retained VERBATIM as the dedupe/ordering key and NEVER
        feeds elapsed-time or liveness math, so deadline
        arithmetic never reads the provider's clock. The clamp is
        applied through :func:`clamp_provider_stamp` (round 29,
        SA_quant_review_1 slice-A item 4) - the single shared
        implementation of this rule.

        Legal :class:`CallPhase` transitions (round 28, review-18
        item 16 - the forward-only matrix, pinned verbatim by
        :data:`CALL_PHASE_FORWARD_TRANSITIONS`): ``ACQUIRED`` may
        advance to ``DIALING``/``RINGING`` (preemption)/``ENDED``;
        ``DIALING`` -> ``ALERTING``/``ACTIVE`` (an alerting-less
        answer - the network may report ``active`` before an
        ``alerting`` leg propagates or without one, a legal
        provider fact the matrix must not reject; added in the
        round-28 fix, PR #72 review)/``ENDED``; ``RINGING`` ->
        ``ACTIVE`` (handset/agent answer)/``ENDED``;
        ``ALERTING`` -> ``ACTIVE``/``ENDED``; ``ACTIVE`` ->
        ``TERMINATING``/``ENDED``; ``TERMINATING`` -> ``ENDED``;
        ``ENDED`` is terminal. A REGRESSION is never legal - an
        ``ACTIVE`` lease can never return to ``DIALING``/``ALERTING``
        (a provider reporting its own state machine restart is a
        defect fact, dropped like an out-of-order update), and
        phase can never skip backwards over ``ENDED``.

        Instant ``BUSY -> FREE -> BUSY`` coalescing (round 28,
        review-18 item 16): when a DISCONNECTED teardown is
        immediately followed inside the same transaction by the
        grant of the head queue ticket, the outbox carries a single
        ``line_state_changed{new_state:"BUSY"}`` for the NEW lease
        - the intermediate ``FREE`` is never published (see
        :class:`LineStateChangedEvent`).

        Binding-level reasons (round 27, review-17 item 18):
        ``reason=dongle_lost`` on a ``disconnected`` update is NOT a
        per-call detail - it reports tunnel/USB death of the whole
        attachment, so the kernel MUST additionally mark the
        binding UNREACHABLE (``event.line_health_changed``), tear
        down the binding's REMAINING leases with
        ``CallEndReason.PROVIDER_LOST`` and cancel its pending
        tickets as ``queue_cancelled{reason: "provider_lost"}`` -
        the same consequences an UNREACHABLE liveness fact carries,
        triggered here by the provider's own report. ``stack_restart``
        stays call-scoped (the call vanished; the bus may be fine).

        Stale-binding drops (round 27, review-17 item 67): an
        update whose ``assignment_id`` names a superseded or dead
        binding is NOT applied - the kernel raises
        ``PhonebridgeError(ErrorCode.STALE_TOKEN)`` so the wire
        reply is a typed error, never a silent ``{ok}`` that would
        hide the desync. The rule covers EVERY provider-side fact
        carrying the dead id (round 29, SA_quant_review_1 slice-B
        item 2): the superseded id resolves through the
        ``_closed_assignments`` ring, and the adapter closes the
        superseded control socket after the supersede reply so a
        lingering socket can never deliver a fact again.

        Dedup scope (round 29, SA_quant_review_1 slice-B item 5):
        the ``observed_at`` dedup/ordering key is always evaluated
        scoped to ``(assignment_id, lease_or_call,
        accepted_stamp)`` - the binding id is part of the key, so
        a past-skewed stamp replayed by a NEW binding can never
        collide with a superseded binding's recorded dedup entry
        and have a fresh fact falsely dropped as a duplicate."""
        ...

    @abstractmethod
    async def ingest_audio_ready(
        self, line_id: LineId, assignment_id: AssignmentId, lease_id: LeaseId
    ) -> None:
        """provider.audio_ready ingress (round 14): the provider's local
        BT SCO path is up for ``lease_id`` -> emit AudioReadyEvent to
        the lease owner AND set ``CallLease.audio_ready_received``
        (round 27, review-17 item 53 - the flag
        :meth:`send_dtmf` gates on). One-shot per lease; a duplicate
        or unknown-lease report is dropped (idempotent) -
        implementations SHOULD count such drops
        (``audio_ready_late_dropped``-style telemetry, item 79): a
        late fact about SCO arriving after lease end is a
        provider/kernel desync symptom worth observing, unlike the
        ``SCO_*`` counter series. NOT a call state - it
        says nothing about remote answer.

        Parameter hygiene (round 28, review-18 item 16): the
        signature parameters are plain ids, so the implementation
        MUST apply the same model-boundary checks the checked field
        aliases enforce - ``_check_line_id`` on ``line_id`` (the
        ``"default"``-alias ban included) and ``_check_id`` on
        ``lease_id``/``assignment_id`` - a malformed id is
        INVALID_ARGUMENT, and
        an ``assignment_id`` naming a superseded binding resolves
        as ``PhonebridgeError(ErrorCode.STALE_TOKEN)`` (the same
        stale-binding rule as :meth:`ingest_ring`/
        :meth:`ingest_call_state`), never a silent accept."""
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
        accept window cannot be short-circuited from outside.

        KERNEL-INTERNAL (round 27, review-17 item 51; hardened
        round 29, SA_quant_review_1 slice-A item 2): the method is
        public on the ABC only because ``tick`` dispatches through
        it - adapters MUST NOT call it and MUST NOT expose any
        wire route to it (the class-level driver-methods rule);
        the only legal invocation is the kernel's own deadline
        path described above.

        Token ordering (round 27, review-17 item 71): when the
        fallback path mints ``fallback_call_token`` for the SAME
        physical ring, the ORIGINAL ``call_token`` is burned in the
        SAME ``tick`` step that mints the replacement - the two
        tokens never coexist valid; ``call.accept`` on the original
        token after that tick resolves as STALE_TOKEN."""
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
        per-ticket/per-lease sweep API.

        Media teardown ordering (round 29, SA_quant_review_1
        slice-A item 9): the dead-man lease teardown calls
        ``MediaAllocator.revoke(lease_id)`` INSIDE this same
        mutation - the allocator side effects complete before
        ``drop_session`` returns, so a ``call_state_changed{
        disconnected}`` event drained afterwards can never precede
        the media teardown it announces; no socket outlives its
        ``lease_id`` even for an instant observable outside the
        mutation."""
        ...

    @abstractmethod
    async def tick(self, now: AwareDatetime) -> None:
        """Single generic lifecycle hook for time-driven housekeeping:
        accept-window expiry, queue grant-TTL advancement, heartbeat
        liveness escalation (the ``heartbeat_degraded_after`` /
        ``heartbeat_down_after`` ladder minting
        ``event.line_health_changed``, round 25, review-15 item 15),
        and the ``max_call_duration_sec`` teardown (round 28,
        review-18 item 7).

        Heartbeat ladder (round 28, review-18 item 1): elapsed
        silence is measured on KERNEL time as
        ``now - binding.last_seen_at`` (``last_seen_at`` is receipt
        time - provider-side stamps never feed this arithmetic).
        The binding walks ``reachable -> degraded -> unreachable``:
        ``>= heartbeat_degraded_after * heartbeat_interval`` (the
        stamped ms interval converted to seconds) escalates to
        ``degraded``, and ``>= heartbeat_down_after *
        heartbeat_interval`` escalates to ``unreachable`` with the
        full §5.4 teardown. The comparison is INCLUSIVE (``>=``):
        with ``heartbeat_degraded_after == 1`` the first miss is
        counted once at least one full interval has elapsed since
        the last fact - a ping arriving exactly AT the interval
        boundary already counts as missed (the stamped interval is
        the expected arrival, so "at the boundary" means "not on
        time"). A heartbeat-driven ``unreachable`` also drops the
        dead binding's queued provider commands AT THE MUTATION
        (round 28, review-18 item 4), with the in-flight
        ``line.dial``/``line.answer`` teardown effects minted in
        the same transaction so their events reach the same
        cycle's ``drain_events()``.

        ``max_call_duration_sec`` (round 28, review-18 item 7):
        when the line's effective cap is set, a lease in
        ``DIALING``/``ALERTING``/``ACTIVE`` crossing
        ``now >= lease.created_at + max_call_duration_sec`` is torn
        down with ``reason: "max_call_duration"`` (the new
        :class:`CallEndReason` member) - the cap bounds one call's
        wall-clock length, including a ``record_and_telegram``
        worker recording. ``ACQUIRED`` is exempt (its
        ``grant_expires_at`` already bounds the undialed grant).
        Ring-drain best-effort ``line.hangup`` restriction (item
        9): the drain MAY queue ``line.hangup`` only on paths where
        the GATEWAY rejected the call (``reject`` fallback
        policy) - never on ``no_pickup``/missed outcomes, where
        the phone's own carrier-side end clears the ring.

        Tick OWNS both ring timers (round 24, review-13 H2): at
        ``now >= ring.expires_at`` (``accept_window_sec`` elapsed) it
        performs the :meth:`decide_fallback` policy decision
        internally - including minting the ``fallback_call_token``
        and emitting ``event.fallback_triggered`` for worker
        policies; a second deadline, ``fallback_accept_ttl_sec``
        (surfaced as ``FallbackTriggeredEvent.expires_at``), bounds
        the worker's ``call.accept``. When that second timer lapses
        without an accept, ``tick`` burns the
        ``fallback_call_token`` and emits
        ``MissedCallEvent(reason=CallEndReason.ACCEPT_WINDOW_EXPIRED)``
        - BUT the line does NOT return to FREE yet (round 27,
        review-17 item 72 - resolves the collision between this
        docstring and docs/specs/line-manager.md §ring-drain): it
        stays ``RINGING_LOCKED`` while the physical call still rings
        on the modem, until the provider reports
        ``disconnected`` or ``ring_drain_ttl_sec`` elapses in a
        later ``tick`` - only then is the line released FREE.
        ``tick`` also drives idle bookkeeping: an EXCLUSIVE
        line whose ``owner_agent_id`` holds at least one open
        session is never idle; ``last_seen_at`` is stamped by
        ``drop_session`` and by every line operation (round 24,
        review-13 H4).

        Ring-drain sub-phase (round 29, SA_quant_review_1 slice-A
        item 1 - normative, docs/specs/line-manager.md "Ring drain
        after a fallback decision"): once ``MissedCallEvent`` has
        been emitted for a ring, the interval during which the
        kernel awaits the provider's ``disconnected`` report or
        the ``ring_drain_ttl_sec`` lapse is a KERNEL-INTERNAL
        bookkeeping sub-phase - it carries NO wire-visible marker.
        ``LineState.RINGING_LOCKED`` remains the reported
        occupancy state for the whole interval (an
        ``acquire``/``ingest_ring`` on a draining line sees the
        same ``RINGING_LOCKED`` as on a still-answerable one), so
        implementations MUST track the drain sub-phase in private
        kernel state; the contract deliberately adds no public
        enum rung or field for it.

        Justification for a hook at all: the narrowed ingress surface
        forbids per-timer public methods, yet all of the above are
        time-driven state transitions that must be atomic w.r.t. the
        other calls (single-event-loop invariant). One explicit tick
        lets the runtime drive them deterministically - including in
        unit tests, which inject time instead of sleeping. This is the
        ONLY timer entry point (round 27, review-17 item 29 - the
        earlier "implementations may no-op if they own internal
        timers" clause is REMOVED: an implementation that keeps its
        own timers would drift away from the injected clock and
        silently skip every deadline above).

        Single timebase (round 27, review-17 item 5): the injected
        ``Clock`` port is the ONLY stamping source inside kernel
        mutations (``expires_at``, ``last_seen_at``, ``*_at``), and
        the caller MUST drive ``tick`` with values of the SAME clock
        (``tick(clock.now())``) so stamped deadlines and their
        evaluation can never come from diverging sources. ``now``
        MUST also be non-decreasing across calls - the kernel MUST
        reject a backwards ``now`` with
        ``PhonebridgeError(ErrorCode.INVALID_ARGUMENT)`` (round 29,
        SA_quant_review_1 slice-A item 11 - hardened from the
        earlier "MAY reject ... as INVALID_STATE"): monotonicity is
        a hard invariant, so an implementation can never silently
        re-arm elapsed deadlines or diverge from another conforming
        one on the same input.

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
        after EVERY kernel call (round 23, review-12 H2).

        Fail-closed cleanup ownership (round 28, review-18 item 4 -
        replaces the round-26 drain-time drop of review-16 item
        9): a command whose ``assignment_id`` names a binding that
        died or was superseded is removed from the outbox AT THE
        MUTATION that killed/replaced the binding
        (:meth:`note_provider_liveness` UNREACHABLE,
        :meth:`register_provider` supersede,
        :meth:`LineAdminKernel.revoke_assignment`,
        :meth:`tick` heartbeat-down) - never inside this drain.
        The in-flight ``line.dial``/``line.answer`` leases of
        dropped commands end with the
        ``disconnected``/``network_error`` effect inside the same
        transaction, so their lease-end events reach the SAME
        cycle's ``drain_events()`` instead of outliving the
        command drain (the adapters drain events first - events
        minted inside a command-drain teardown would sit unsent
        until the next transaction). The drain itself keeps only a
        DEFENSIVE drop for a command addressed to a binding that
        vanished with no successor marker - a safety net for an
        inconsistent outbox, never the mechanism.
        The single exception is :class:`ProviderRevokeCommand`
        (item 34): it bypasses the drop while the binding's
        control channel is still open - an UNREACHABLE-by-heartbeat
        binding can still hold a live socket the revoke must
        reach; it drops only with the physical channel."""
        ...


class PairWindowRequest(ContractModel):
    """admin.pair_window ingress model (round 27, review-17 item 61):
    the typed params of the ``admin.pair_window {line_id,
    duration_sec}`` wire op (api-contract.md "Non-kernel admin
    operations").

    NON-KERNEL: the request is routed by the admin adapter to the
    line-bundle supervisor, which drives the container's BlueZ
    ``Agent1`` on the named line's adapter only (hci-agent.md §5) -
    ``LineAdminKernel`` owns call/line state, not bus plumbing, so
    no kernel method consumes this model. ``duration_sec`` carries
    the documented <= 300 s ceiling at TYPE level - an overlong
    window is rejected at the model boundary instead of being
    silently clamped. During the window SSP confirmation is
    auto-accepted ONLY for the line's configured
    ``paired_phone_mac``, never for an arbitrary neighboring
    Bluetooth device."""

    line_id: _CheckedLineId
    duration_sec: float = Field(gt=0, le=300.0)
    """Pairing window length in seconds (round 27, review-17 item
    61): strictly positive and bounded at the documented 300 s
    ceiling - the wire-level clamp is now a hard model bound."""


class PairWindowResponse(ContractModel):
    """admin.pair_window result (round 27, review-17 item 61): the
    supervisor's reply carrying the absolute ``expires_at`` at which
    the pairing window closes (RFC 3339 UTC - same deadline-style
    convention as :attr:`IncomingCallEvent.expires_at`; a relative
    duration would skew by supervisor scheduling delay)."""

    line_id: _CheckedLineId
    expires_at: AwareDatetime


class TimeoutConfig(ContractModel):
    """Runtime-configurable kernel timing knobs (round 22, review-10
    HIGH-5; design §10: "ALL timeouts and windows are
    admin-configurable without a daemon restart").

    Passed to :meth:`LineAdminKernel.configure_line` as a PATCH:
    every member is optional, ``None`` means "leave unchanged".
    The merge into the line's stored :class:`LineTimeouts` is
    ATOMIC and MUST go through :meth:`LineTimeouts.apply_patch`
    (round 24b): the merged dict is re-validated in full, so a
    single-field patch that passes THIS model's own checks can
    still be rejected against the stored effective values -
    failure maps to ``PhonebridgeError(ErrorCode.INVALID_ARGUMENT)``,
    never a raw ``ValidationError``, and leaves no partial state.
    Values are scoped per line - the line's provider binding and
    queue grants share them. Every float knob carries the same
    one-day ``le=86400.0`` ceiling as :class:`LineTimeouts` (round
    25, review-15 item 22): a larger value is a config bug, not a
    knob. ``clear_max_call_duration`` is the one member that is not
    a knob at all but a CONTROL flag (round 25, review-15 item 24):
    ``None`` "no-op" / ``True`` "reset the effective
    ``max_call_duration_sec`` to uncapped" - the
    ``None``-means-unchanged convention alone cannot express
    "disable a set cap"."""

    grant_ttl_sec: float | None = Field(default=None, gt=0, le=86400.0)
    """Deadline for an agent to consume a grant
    (``AcquireGranted.expires_at``/``QueueReadyEvent.expires_at``)
    before the lease ends ``grant_ttl_expired`` (~5 s illustrative
    default, admin-configured)."""
    orphan_grace_sec: float | None = Field(default=None, gt=0, le=86400.0)
    """WS-drop re-bind window - feeds
    ``ProviderBinding.orphan_deadline_at`` (default 10 s, round
    5b/20)."""
    heartbeat_interval_sec: float | None = Field(default=None, ge=0.001, le=86400.0)
    """Required ``provider.ping`` cadence for the line's binding,
    floored at millisecond granularity (``ge=0.001`` s - the wire
    unit is ms, round 24c; the sec -> ms conversion is pinned by
    :func:`heartbeat_interval_ms_from_sec` - nearest ms, halves up,
    min 1 (round 27, review-17 item 37), so a valid value can never
    stamp a zero cadence).
    A runtime change takes effect on the provider's NEXT
    ``provider.reregister`` - the live provider learns
    ``heartbeat_interval_ms`` only from the register response, so
    a patched interval MUST NOT be enforced against the current
    session (round 23, review-12 H6: shrinking it mid-session
    would kill a healthy provider on a false ping timeout)."""
    heartbeat_degraded_after: int | None = Field(default=None, ge=1)
    """Missed pings before the binding's health reports degraded
    (liveness axis, round 25, review-15 item 15)."""
    heartbeat_down_after: int | None = Field(default=None, ge=1)
    """Missed pings before the binding is declared UNREACHABLE (must
    exceed the degraded threshold when both are set)."""
    fallback_accept_ttl_sec: float | None = Field(default=None, gt=0, le=86400.0)
    """Deadline for the FallbackWorker to ``call.accept`` a re-issued
    ``fallback_call_token`` (round 23, review-12 M6; ~10 s
    illustrative default): bounds
    ``FallbackTriggeredEvent.expires_at``, so a dead worker never
    leaves a ring pending past its TTL."""
    ring_drain_ttl_sec: float | None = Field(default=None, gt=0, le=86400.0)
    """Bound on the post-decision ring-drain wait (round 25,
    review-15 item 16; design §5.2 step 6, ~60 s default): how long
    the line stays ``RINGING_LOCKED`` while the physical call still
    rings on the modem after ``event.missed_call`` was emitted."""
    max_call_duration_sec: float | None = Field(default=None, gt=0, le=86400.0)
    """Optional wall-clock cap on a single call (round 25,
    review-15 item 24; design §10 - also bounds the
    ``record_and_telegram`` recording length). ``None`` is PATCH
    "leave unchanged" here, NOT "unlimited": the wire-level off
    switch for a previously set cap is
    ``clear_max_call_duration``."""
    clear_max_call_duration: bool | None = Field(default=None)
    """Explicit off switch for ``max_call_duration_sec`` (round 25,
    review-15 item 24), mirroring the
    ``idle_revoke_after``/``clear_idle_revoke_after`` pair on
    :meth:`LineAdminKernel.configure_line`: ``True`` resets the
    effective cap to ``None`` (uncapped) at merge time - the
    ``None``-means-unchanged convention alone could never disable
    a set cap. A CONTROL flag, not a stored knob: it is consumed
    inside :meth:`LineTimeouts.apply_patch` and never lands on the
    effective model. ``True`` together with
    ``max_call_duration_sec`` is rejected below - one patch sets
    or clears, never both."""

    @model_validator(mode="after")
    def _heartbeat_thresholds_ordered(self) -> TimeoutConfig:
        """Same invariant as :class:`LineTimeouts` (round 24,
        review-13 C2): when BOTH thresholds are patched at once,
        ``heartbeat_down_after`` must exceed
        ``heartbeat_degraded_after`` - catching the inverted pair at
        patch time instead of after hydration into the effective
        values. A LONE threshold passes here and is re-checked
        against the stored effective values by
        :meth:`LineTimeouts.apply_patch` at merge time (round
        24b)."""
        if (
            self.heartbeat_down_after is not None
            and self.heartbeat_degraded_after is not None
            and self.heartbeat_down_after <= self.heartbeat_degraded_after
        ):
            raise ValueError("heartbeat_down_after must exceed heartbeat_degraded_after")
        return self

    @model_validator(mode="after")
    def _max_call_duration_set_xor_clear(self) -> TimeoutConfig:
        """Set-XOR-clear rule for the call-duration cap (round 25,
        review-15 item 24), same contract as
        ``idle_revoke_after`` + ``clear_idle_revoke_after`` on
        :meth:`LineAdminKernel.configure_line`:
        ``clear_max_call_duration=True`` combined with a patched
        ``max_call_duration_sec`` is contradictory - one PATCH sets
        the cap or removes it, never both."""
        if self.clear_max_call_duration and self.max_call_duration_sec is not None:
            raise ValueError(
                "max_call_duration_sec and clear_max_call_duration are mutually exclusive"
            )
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

    Driver methods are KERNEL-INTERNAL here too (round 29,
    SA_quant_review_1 slice-A item 2 - same normative rule as
    :class:`LineManager`, docs/specs/line-manager.md "Driver
    methods are kernel-internal (round 29)"): the two drains
    ``drain_events``/``drain_provider_commands`` are outbox drivers
    the adapter calls AFTER kernel mutations, not wire calls. A
    transport adapter MUST NOT route any wire RPC to a driver
    method - the wire surface is exclusively the ``admin.*``
    methods mapped in docs/api-contract.md, and a route named like
    a driver method is a contract violation.
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
        M2 - see :class:`LineAssignment`).

        ``owner_agent_id`` (round 26, review-16 item 14) MUST
        satisfy the agent-identifier grammar of
        :func:`_check_agent_id` AND MUST NOT be a ``system:*``
        identity (:data:`SYSTEM_AGENT_PREFIX`): ``system:*`` names
        kernel-internal service roles such as
        :data:`SYSTEM_FALLBACK_WORKER`, never a durable line
        owner - a system role holds no agent session, so an
        assignment granted to one could never be exercised or
        released. Violations are INVALID_ARGUMENT.

        Outbox effects (round 25, review-15 batch 2): the kernel
        emits :class:`AssignmentGrantedEvent` to the new owner's
        ``AgentAudience`` so the agent learns the rotated
        ``assignment_id`` it needs for ``release{assignment_id}``
        (item 2 - the admin reply cannot carry it to the agent);
        cancels every pending queue ticket targeting the line as
        ``event.queue_cancelled{reason: "line_exclusive"}`` and
        auto-resolves its pending claims as rejected
        :class:`ClaimResolvedEvent` (item 23 - a non-owner can
        never be granted or approved on the now-exclusive line).

        Assign on a non-idle line (round 28, review-18 item 14):
        ``assign_line`` is legal on a ``BUSY``/``RINGING_LOCKED``
        line - ownership and call occupancy are different axes.
        Live leases and a held ring stay with the sessions that
        own them: the new owner does NOT inherit a foreign lease
        (no ``(session_id, lease_id)`` ownership migrates) and a
        delivered ``incoming_call`` is not re-routed - the
        exclusivity grant changes who may acquire NEXT, not who
        owns what already runs."""
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
        sub-code).

        Outbox effects (round 25, review-15 batch 2):
        :class:`ClaimResolvedEvent` ``{status: "approved",
        assignment_id}`` to the claiming session, plus
        :class:`AssignmentGrantedEvent` to the new owner's
        ``AgentAudience`` (item 2 - the agent learns the rotated id
        it needs for ``release{assignment_id}``). The now-exclusive
        line cancels pending tickets (round 27, review-17 item 50
        reason split): FOREIGN sessions' tickets as
        ``queue_cancelled{reason: "line_exclusive"}``, the claiming
        session's OWN queued tickets on the line as
        ``queue_cancelled{reason: "claim_resolved"}`` - moot now
        that the claimant owns the line - and auto-rejects other
        pending claims on it (item 23).

        Approve on a non-idle line (round 28, review-18 item 14):
        same isolation rule as :meth:`assign_line` - the grant is
        legal on a ``BUSY``/``RINGING_LOCKED`` line, live leases
        and the held ring stay with their sessions, the new owner
        inherits nothing already running (no foreign lease
        migrates and no delivered ``incoming_call`` is
        re-routed)."""
        ...

    @abstractmethod
    async def reject_claim(self, claim_id: ClaimId, reason: str | None = None) -> None:
        """Reject a pending claim request with an optional PII-free
        reason string, bounded at :data:`MAX_REASON_LEN` (round 27,
        review-17 item 27 - the free-form admin text lands on the
        wire inside :class:`ClaimResolvedEvent`, which enforces the
        same bound; an overlong reason is INVALID_ARGUMENT).
        Unknown or already-resolved ``claim_id`` ->
        ``PhonebridgeError(ErrorCode.INVALID_STATE,
        ErrorData(reason=ErrorReason.CLAIM_NOT_FOUND))`` - same
        mapping as :meth:`approve_claim` (round 24, review-13 L1).

        Outbox effect (round 25, review-15 item 2): emits
        :class:`ClaimResolvedEvent` ``{status: "rejected",
        reason?}`` to the claiming session via the outbox - the
        claimant learns the fate without polling the admin
        surface."""
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
        ``revoke_assignment``.

        Trust note (round 27, review-17 item 31): the listing
        exposes every live binding id - an ``assignment_id`` is a
        locator, not a credential, but a leaked admin listing does
        lower the bar for a forged ``capability_update``; the
        socket-equals-binding check on
        :meth:`LineManager.update_provider_capabilities` is what
        keeps the write path authenticated."""
        ...

    @abstractmethod
    async def list_lines(
        self,
        *,
        offset: Annotated[int, Field(ge=0)] = 0,
        limit: Annotated[int, Field(ge=1, le=MAX_LIST_PAGE)] = MAX_LIST_PAGE,
    ) -> list[AdminLineView]:
        """Admin read ``admin.list_lines`` (round 27, review-17 item
        3): every configured line as an :class:`AdminLineView` -
        the admin-scoped superset of :class:`LineSummary` carrying
        ``assignment_id``/``owner_agent_id`` and, for a live call,
        ``lease_id`` plus the head ticket's ``queue_position``.
        Bounded by :data:`MAX_LINES_TOTAL` (rule 7 - the line set
        is finite and now explicitly capped, round 29). Paged by
        the shared ``{offset?, limit?}`` window (round 29, PR #74
        review - :data:`MAX_LIST_PAGE`); the stable slice order is
        ``line_id``."""
        ...

    @abstractmethod
    async def list_leases(
        self,
        *,
        offset: Annotated[int, Field(ge=0)] = 0,
        limit: Annotated[int, Field(ge=1, le=MAX_LIST_PAGE)] = MAX_LIST_PAGE,
    ) -> list[CallLease]:
        """Admin read ``admin.list_leases`` (round 27, review-17 item
        3): every live :class:`CallLease` - the objects exist only
        in kernel state, so the kernel surface is the ONLY place
        this listing can come from. Bounded by
        :data:`MAX_LEASES_PER_SESSION` x live sessions (rule 7).
        Paged by the shared ``{offset?, limit?}`` window (round 29,
        PR #74 review - :data:`MAX_LIST_PAGE`); the stable slice
        order is ``(created_at, lease_id)`` - :class:`CallLease`
        carries no ``seq``, so the deterministic pair of mint time
        plus the unique lease id is the listing order."""
        ...

    @abstractmethod
    async def list_tickets(
        self,
        *,
        offset: Annotated[int, Field(ge=0)] = 0,
        limit: Annotated[int, Field(ge=1, le=MAX_LIST_PAGE)] = MAX_LIST_PAGE,
    ) -> list[QueueTicket]:
        """Admin read ``admin.list_tickets`` (round 27, review-17
        item 3): every pending :class:`QueueTicket` in the global
        FIFO - same single-ordering rule as
        :attr:`QueueTicket.seq`; bounded by
        :data:`MAX_QUEUE_TICKETS_TOTAL` (rule 7). Paged by the
        shared ``{offset?, limit?}`` window (round 29, PR #74
        review - :data:`MAX_LIST_PAGE`); the stable slice order is
        ``seq`` itself - the mandatory FIFO ordering."""
        ...

    @abstractmethod
    async def remove_line(self, line_id: LineId) -> None:
        """Admin ``admin.remove_line`` (round 27, review-17 item 3):
        delete a line from kernel state so the admin adapter can
        drop its ``lines`` row.

        Preconditions: ``line_id`` must exist (``LINE_NOT_FOUND``)
        and the line MUST be quiescent - ``INVALID_STATE`` while it
        holds a live ``ProviderBinding``, any live ``CallLease``,
        pending ``QueueTicket`` entries or pending claims, and also
        while ANOTHER configured line's ``forward_via_line`` policy
        still targets it (round 28, review-18 item 14): deleting a
        forward target would leave a dangling chain the FallbackWorker
        would only discover at ring time - the operator retargets or
        clears the referencing policy first. The
        operator first revokes/detaches through the existing paths;
        removal then deletes the durable ``LineAssignment`` record
        itself - no tombstone is kept, the id simply stops
        resolving (a late ``call.release`` on it is
        ``STALE_TOKEN``). Emits no ``assignment_revoked`` for a
        SHARED_POOL line - the line is gone, not reassigned. An
        OWNED quiescent line is the exception (round 28, review-18
        item 14): its durable ``EXCLUSIVE`` binding ends, so the
        owner MUST be told - the kernel emits
        :class:`AssignmentRevokedEvent` to the owner's
        ``AgentAudience`` BEFORE deleting the record, otherwise
        the owner would hold an ``assignment_id`` that silently
        stopped resolving.

        Write-through ordering (round 28, review-18 item 6): the
        kernel deletes its record FIRST and the admin adapter drops
        the ``lines`` row SECOND - the kernel is the source of
        truth, so a failed precondition never reaches the store."""
        ...

    @abstractmethod
    async def provision_line(
        self,
        line_id: LineId,
        *,
        profile: str | None = None,
        fallback_policy: FallbackPolicy | None = None,
        accept_window_sec: float | None = None,
        allowed_number_types: frozenset[str] | None = None,
        claimable: bool | None = None,
        idle_revoke_after: timedelta | None = None,
        timeouts: TimeoutConfig | None = None,
    ) -> LineAssignment:
        """Admin ``admin.provision_line`` (round 28, review-18 item 6):
        create a CONFIGURED, QUIESCENT line - the missing "create"
        half of the line lifecycle (``configure_line`` could only
        patch an existing row).

        Creates the durable ``LineAssignment`` in its DEFAULT pool
        shape: ``mode=SHARED_POOL``, ``owner_agent_id=None``,
        ``claimable=True`` (``claimable=None`` resolves to the
        default; ``False`` provisions a non-claimable pool line).
        The optional params share the
        ``None``-means-default convention and the validation rules
        of :meth:`configure_line` (``profile`` bounded at
        :data:`MAX_PROFILE_LEN`; ``idle_revoke_after`` inside
        ``(0, 86400 s]``; ``timeouts`` merged via
        :meth:`LineTimeouts.apply_patch` onto the defaults).

        A ``line_id`` that already exists is
        ``PhonebridgeError(ErrorCode.INVALID_ARGUMENT)`` - the
        method creates, never patches (patching is
        :meth:`configure_line`'s job). The configured-line count is
        bounded (round 29, SA_quant_review_1 slice-A item 10; rule
        7): a provisioning attempt that would push the line set
        past :data:`MAX_LINES_TOTAL` is rejected
        ``PhonebridgeError(ErrorCode.RESOURCE_EXHAUSTED)`` - the
        bound keeps the forward-chain walk, the admin listings and
        the queue/liveness sweeps finite. The new line is visible
        IMMEDIATELY as ``UNBOUND`` on
        :meth:`list_lines`/:meth:`LineManager.get_lines`: it has
        no provider binding yet, so the derived listing row
        reports ``caps=()``, ``liveness=UNBOUND`` and the stored
        ``profile`` label - the configured-fleet visibility rule
        of round 27 review-17 item 60 applies to provisioned
        lines by construction.

        Write-through ordering (round 28, review-18 item 6): the
        kernel creates its record FIRST and the admin adapter
        upserts the new ``lines`` row SECOND - the kernel is the
        source of truth, so a failed precondition never reaches
        the store."""
        ...

    @abstractmethod
    async def revoke_assignment(
        self, assignment_id: AssignmentId, reason: str | None = None
    ) -> LineAssignment | None:
        """Admin ``line.revoke``: end a durable binding -> the NEW
        post-revert ``LineAssignment`` snapshot, or ``None``.

        Return contract (round 25, review-15 item 14): for an
        agent-owned binding the method returns the NEW post-revert
        ``LineAssignment`` - the SHARED_POOL record carrying the
        ROTATED ``assignment_id`` - so the admin surface learns the
        successor id without a second ``list_assignments`` round
        trip. ``None`` is returned when the id resolved to a
        ``ProviderBinding`` (there is no LineAssignment snapshot to
        hand back - the provider object space ends outright).

        ``assignment_id`` disambiguates by which object space it
        resolves to (round 20 - two identifier spaces share the
        AssignmentId tag, api-contract.md Identifier taxonomy): a
        ``LineAssignment`` (agent ownership) -> revert the line to
        SHARED_POOL and emit ``AssignmentRevokedEvent`` to the
        revoked owner; a ``ProviderBinding`` -> enqueue a
        ``ProviderRevokeCommand`` in the outbox, tear the binding's
        liveness down via
        ``LineHealthChangedEvent(new_state=LivenessState.UNREACHABLE)``
        (round 25, review-15 item 15 - provider health rides the
        liveness axis, never ``line_state_changed``) and tear down
        its leases with ``reason=CallEndReason.ADMIN_REVOKED``,
        cancelling pending tickets as
        ``queue_cancelled{reason: "provider_lost"}`` (item 23) -
        NEVER an ``AssignmentRevokedEvent``, because a provider
        attachment has no agent audience (round 22, review-10
        HIGH-6).
        Unknown id in BOTH spaces ->
        ``PhonebridgeError(ErrorCode.STALE_TOKEN)`` (round 24,
        review-13 L1 - an unresolvable durable id is a stale
        reference, the counterpart of a consumed ``call_token``).
        Revoking an agent assignment reverts the line to
        SHARED_POOL and mints a FRESH ``assignment_id`` for the
        reverted binding (rotation, round 24, review-13 M2 - see
        :class:`LineAssignment`); the revoked id resolves through
        the bounded ``_closed_assignments`` ring (capacity
        :data:`MAX_CLOSED_ASSIGNMENTS`, round 27, review-17 item 12)
        so a retried
        revoke/release is idempotent rather than
        ``OWNER_MISMATCH``. The ring is ONE shared structure for
        both object spaces (round 29, SA_quant_review_1 slice-A
        item 5): mint-uniqueness across ``LineAssignment`` and
        ``ProviderBinding`` ids guarantees a ring hit can only
        have belonged to one object type, so a single ring can
        never mis-resolve an id to the wrong space - see
        :class:`LineAssignment` for the invariant. ``reason`` is
        PII-free free-form text
        bounded at :data:`MAX_REASON_LEN` (round 27, review-17 item
        27) - it lands verbatim on ``event.assignment_revoked`` /
        ``line.revoke``, an overlong reason is INVALID_ARGUMENT.

        Edge cases (round 28, review-18 items 4/14): an id resolving
        to a ``SHARED_POOL`` ``LineAssignment`` is rejected
        ``PhonebridgeError(ErrorCode.INVALID_STATE)`` with detail
        "shared_pool assignment has no durable owner to revoke" -
        there is nothing durable to end; the pool record is the
        DEFAULT state of a configured line, not a binding. A
        repeated revoke of a ``ProviderBinding`` id is idempotent:
        superseded/dead provider-binding ids resolve through the
        same bounded closed ring as agent assignments
        (``_closed_assignments``, capacity
        :data:`MAX_CLOSED_ASSIGNMENTS`) and return ``None`` again -
        never ``STALE_TOKEN`` for an id the kernel once owned.
        Revoking a ``ProviderBinding`` also drops its queued
        provider commands AT THE MUTATION (item 4) with the
        in-flight ``line.dial``/``line.answer`` teardown effects
        minted in the same transaction (the
        :class:`ProviderRevokeCommand` exemption of item 34
        applies only to the revoke command itself).

        Dead-binding delivery (round 27, review-17 item 34): the
        enqueued ``ProviderRevokeCommand`` is NOT subject to the
        fail-closed dead-binding drop of
        :meth:`LineManager.drain_provider_commands` while the
        binding's control channel is still open - a binding marked
        UNREACHABLE by missed heartbeats can still hold a live
        socket the revoke must reach. The command drops only when
        the physical channel is already gone - a dead peer cannot be
        informed and the drop is then harmless."""
        ...

    @abstractmethod
    async def configure_line(
        self,
        line_id: LineId,
        *,
        fallback_policy: FallbackPolicy | None = None,
        profile: str | None = None,
        accept_window_sec: float | None = None,
        allowed_number_types: frozenset[str] | None = None,
        claimable: bool | None = None,
        idle_revoke_after: timedelta | None = None,
        clear_idle_revoke_after: bool = False,
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
        heartbeat liveness thresholds, fallback accept TTL, ring
        drain TTL, optional max-call-duration cap) as
        a partial :class:`TimeoutConfig` patch - including the
        ``clear_max_call_duration`` off switch for the cap (round
        25, review-15 items 16/24).

        ``profile`` (round 28, review-18 item 6) patches the
        administrative label carried on
        :attr:`LineAssignment.profile`: ``None`` means "leave
        unchanged", a set value must be non-empty and at most
        :data:`MAX_PROFILE_LEN` chars (INVALID_ARGUMENT beyond) -
        there is deliberately no clear-profile flag, matching the
        provider-label convention on
        :meth:`LineManager.update_provider_capabilities`.

        Write-through ordering (round 28, review-18 item 6): the
        kernel applies the patch FIRST and the adapter upserts the
        ``lines`` row SECOND - the kernel is the source of truth,
        so a config that fails model validation never reaches the
        stored row, and a row that fails to persist never leaves
        kernel state ahead of the store (the adapter's upsert runs
        after the kernel commit, outside the mutation critical
        section).

        ``idle_revoke_after`` vs ``clear_idle_revoke_after`` (round
        25, review-15 item 22): the ``None``-means-unchanged patch
        convention alone could never DISABLE a previously set idle
        window - ``None`` is the no-op marker, not the off switch.
        ``clear_idle_revoke_after=True`` is the explicit off switch:
        it resets ``LineAssignment.idle_revoke_after`` to ``None``
        (the idle alert stops firing). Passing BOTH
        ``idle_revoke_after`` AND ``clear_idle_revoke_after=True``
        is INVALID_ARGUMENT - one call sets or clears, never both.
        A zero/negative ``idle_revoke_after`` is likewise
        INVALID_ARGUMENT at the model boundary.

        ``allowed_number_types`` members are validated against the
        phonenumbers type vocabulary (:data:`_VALID_NUMBER_TYPES`,
        round 25, review-15 item 36): the admin-side check is the
        adapter's job, but the ``LineAssignment`` model is
        authoritative - a hydrated or patched set with unknown or
        zero members can never exist inside a frozen model.

        ``claimable=False`` claim fate (round 28, review-18 item 5 -
        replaces the round-25 queue-cancel wording, which conflated
        claimability with occupancy mode): ``claimable`` is the
        CLAIMABILITY flag, not an occupancy mode - flipping it on a
        ``SHARED_POOL`` line does NOT cancel the line's pending
        queue tickets, because the line keeps serving the pool's
        ``DIAL_OUT`` flow. Only the line's pending CLAIMS
        auto-resolve as rejected :class:`ClaimResolvedEvent` - a
        claim that can never be approved is resolved explicitly,
        never parked. ``queue_cancelled{reason: "line_exclusive"}``
        stays reserved for REAL exclusive transitions
        (:meth:`assign_line`/:meth:`approve_claim`).

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
        target's effective allowlist. ``forward.line_id`` MUST
        resolve to an existing configured line (round 28,
        review-18 item 14): a policy pointing at an absent line is
        ``PhonebridgeError(ErrorCode.LINE_NOT_FOUND)`` - a dead
        forward target is a config defect, not a deferred
        discovery. A SELF-LOOP is rejected
        outright: ``forward.line_id == line_id`` is
        INVALID_ARGUMENT - the line can never be its own forward
        target (the same invariant is model-enforced inside
        :class:`LineAssignment`). A TRANSITIVE cycle is likewise
        rejected (round 26, review-16 item 2;
        docs/specs/line-manager.md): a ``forward_via_line`` policy
        that would close a chain ``A -> B -> ... -> A`` is
        INVALID_ARGUMENT - the kernel MUST walk the stored policy
        chain of the target lines, bounded at
        :data:`MAX_FORWARD_HOPS` hops, so a deeper or closed chain
        can never hang the FallbackWorker in a forwarding loop
        (the multi-line case the model-level self-loop check
        cannot see). The chain check covers the FULL chain through
        the new edge, BOTH directions (round 28, review-18 item
        15): the kernel scans PREDECESSORS - stored policies that
        already forward INTO this line - plus the forward
        traversal of the target chain, so a new edge that would
        close a cycle from either end (``A -> B`` where ``B``
        already reaches ``A`` transitively) or push the combined
        chain depth past :data:`MAX_FORWARD_HOPS` is rejected
        INVALID_ARGUMENT. The two failure cases share the wire
        code but MUST carry distinguishing ``ErrorData.detail``
        text (round 29, SA_quant_review_1 slice-A item 12 - no
        dedicated ``ErrorReason`` sub-code exists): "forward
        cycle" when the walk closes a loop vs "forward chain
        exceeds MAX_FORWARD_HOPS" when it runs out of hops on an
        open chain. On a SUCCESSFUL policy mutation the
        kernel additionally revalidates every line whose stored
        chain passes through the changed one - a
        ``fallback_policy`` patch (or removal) can otherwise leave
        a dangling or over-deep downstream chain. The walk's
        outcome when the bound is reached
        WITHOUT closing a cycle is fail-closed (round 27, review-17
        item 19): a chain deeper than :data:`MAX_FORWARD_HOPS` is
        rejected INVALID_ARGUMENT too - "no cycle found yet" is not
        a verdict, only an unfinished search. The same walk runs at
        HYDRATION time when a ``lines`` row is loaded from SQLite
        (a stored transitive cycle is a config defect the model
        boundary cannot express, so the store-load path performs
        the kernel check rather than trusting the row).
        A patched
        ``heartbeat_interval_sec`` takes effect on the provider's
        next reregister, never mid-session (see
        :class:`TimeoutConfig`).

        ``timeouts`` merge semantics (round 24b): the patch is
        merged into the line's stored :class:`LineTimeouts`
        ATOMICALLY via :meth:`LineTimeouts.apply_patch` - a full
        ``model_validate`` of the merged dict, so the heartbeat
        ladder invariant is checked on the RESULT: a lone
        ``heartbeat_degraded_after`` patch that is legal in
        isolation can still be rejected when the merged
        ``heartbeat_down_after`` no longer exceeds it. A failed
        merge maps to ``PhonebridgeError(ErrorCode.INVALID_ARGUMENT)``
        - never a raw ``ValidationError`` at the adapter - and
        leaves the stored values untouched (no partial state)."""
        ...

    # --- transactional outbox drains (round 25, review-15 item 12) ---
    #
    # The admin mutations above append to the SAME bounded outbox the
    # agent-facing ``LineManager`` drains use - ``InMemoryLineManager``
    # implements both surfaces over one kernel state, so claim
    # resolutions, granted assignments and queue/claim fates ride the
    # identical drain contract documented on ``LineManager`` (soft-cap
    # pre-flight, teardown paths never raise RESOURCE_EXHAUSTED,
    # drain after every call).

    @abstractmethod
    def drain_events(self) -> list[EventEnvelope[Any]]:
        """Same contract as :meth:`LineManager.drain_events`
        (round 25, review-15 item 12): atomically drain pending
        domain-event envelopes in emission order.

        Shared-bus clarification (item 13): the drained envelopes
        feed the SHARED global EventBus - never adapter-local
        connections - so whichever adapter (agent or admin) drains
        after its own mutation still forwards EVERY pending
        envelope, including ones minted by the other surface's
        mutations. MUST be called after EVERY kernel call."""
        ...

    @abstractmethod
    def drain_provider_commands(self) -> list[ProviderCommand]:
        """Same contract as :meth:`LineManager.drain_provider_commands`
        (round 25, review-15 item 12): atomically drain pending
        provider commands in emission order.

        The drained commands feed the SHARED global provider-command
        executor (item 13) - never adapter-local provider
        connections - so an admin ``revoke_assignment`` whose outbox
        ``ProviderRevokeCommand`` is drained by the admin adapter
        still reaches the provider's own channel exactly once. MUST
        be called after EVERY kernel call.

        The same fail-closed cleanup ownership applies here (round
        28, review-18 item 4 - replaces the round-26 drain-time
        drop): dead/superseded-binding commands are removed from the
        outbox AT THE MUTATION that killed/replaced the binding,
        and the in-flight ``line.dial``/``line.answer`` teardown
        effects are minted in the same transaction so their events
        reach the same cycle's ``drain_events()`` - the drain keeps
        only the defensive drop for a command addressed to a
        binding that vanished with no successor. The
        :class:`ProviderRevokeCommand` exemption of item 34 applies
        identically - see
        :meth:`LineManager.drain_provider_commands`."""
        ...
