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
    Generic,
    Literal,
    NewType,
    Protocol,
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
    "CALL_PHASE_WIRE_STATES",
    "DTMF_PATTERN",
    "E164_PATTERN",
    "ERROR_CODE_WIRE",
    "EVENT_AUDIENCE_SCOPES",
    "EVENT_WIRE_NAMES",
    "MAX_DTMF_DIGITS",
    "MAX_IDEMPOTENCY_KEY_LEN",
    "MAX_LEASES_PER_SESSION",
    "MAX_PENDING_CLAIMS_PER_AGENT",
    "MAX_PENDING_CLAIMS_TOTAL",
    "MAX_PROVIDER_META_ENTRIES",
    "MAX_PROVIDER_META_KEY_LEN",
    "MAX_PROVIDER_META_VALUE_LEN",
    "MAX_QUEUE_TICKETS_TOTAL",
    "MAX_SESSIONS_PER_AGENT",
    "MAX_TICKETS_PER_SESSION",
    "MEDIA_AUTH_TIMEOUT_SEC",
    "MEDIA_PAIR_TTL_SEC",
    "PROVIDER_COMMAND_WIRE_NAMES",
    "SYSTEM_AGENT_PREFIX",
    "SYSTEM_FALLBACK_WORKER",
    "AcceptResult",
    "AcquireGranted",
    "AcquireQueued",
    "AcquireRefused",
    "AcquireResult",
    "AdminAudience",
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

_IdT = TypeVar("_IdT", bound=str)


def _check_id(value: _IdT) -> _IdT:
    """Domain-identifier hygiene (round 25, review-15 item 37): an id
    is a non-empty opaque string of at most ``_ID_MAX_LEN`` chars and
    MUST NOT contain ``_ID_FORBIDDEN`` (``:``) - the field separator
    of the single-string wire spellings. Generic over the identifier
    NewTypes so each checked field keeps its declared type."""
    if not value:
        raise ValueError("identifier must be non-empty")
    if len(value) > _ID_MAX_LEN:
        raise ValueError(f"identifier exceeds {_ID_MAX_LEN} chars")
    if _ID_FORBIDDEN in value:
        raise ValueError(f"identifier must not contain {_ID_FORBIDDEN!r}")
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
    itself obeys the same hygiene as any external id (at most
    ``_ID_MAX_LEN`` chars, no ``:``)."""
    if is_system_agent_id(value):
        suffix = value[len(SYSTEM_AGENT_PREFIX):]
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
    (the ``heartbeat_degraded_after``/``heartbeat_down_after``
    escalation ladder) and tears leases down IMMEDIATELY - no grace
    (design §5.4).

    This is the provider-HEALTH axis (round 25, review-15 item 15):
    it lives on ``ProviderBinding.liveness``/``LineSummary.liveness``
    and its transitions surface as ``event.line_health_changed`` -
    never as ``event.line_state_changed``, whose
    :class:`LineState` vocabulary is call-occupancy only."""

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


class QueueCancelReason(str, Enum):
    """CLOSED reason vocabulary for ``event.queue_cancelled``
    (:class:`QueueCancelledEvent`, round 25, review-15 item 23).

    A pending ticket is CANCELLED - never silently parked - when its
    target stops being able to serve it:

    - ``LINE_EXCLUSIVE``: the target line became EXCLUSIVE via
      ``assign_line``/``approve_claim`` (or ``claimable`` flipped
      False under ``configure_line``) - the ticket can never be
      granted to a non-owner;
    - ``PROVIDER_LOST``: the line's provider binding was revoked or
      declared UNREACHABLE - no provider means no grant;
    - ``CAPABILITY_REMOVED``: a ``provider.capability_update`` removed
      the intent the ticket asked for;
    - ``CLAIM_RESOLVED``: the ticket was invalidated by a claim
      lifecycle resolution on the same line (companion of
      :class:`ClaimResolvedEvent`).

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

    Rejection branches map to typed ``ErrorReason`` sub-codes on the
    ``call.dial`` path (the kernel does the string->sub-code mapping;
    the messages are kept deliberately distinct so the branches can
    never collapse into one another):

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

    Returns the validated raw E.164 string. Raises ``ValueError`` on
    any rejection - under pydantic model validation it surfaces as a
    ``ValidationError`` (INVALID_ARGUMENT at the adapter), on the
    ``call.dial`` path the kernel maps the same rejections to
    ``DIAL_REJECTED`` with the typed ``data.reason`` sub-codes."""
    raw = number.expose()
    if not E164_PATTERN.fullmatch(raw):
        raise ValueError("number is not strict E.164")
    if country != _NON_GEO_REGION and not _ISO_ALPHA2_PATTERN.fullmatch(country):
        raise ValueError("country is not ISO 3166-1 alpha-2")
    if country != _NON_GEO_REGION and country not in _ISO_REGION_CODES:
        raise ValueError("country is not a known ISO 3166-1 alpha-2 region")
    try:
        parsed = phonenumbers.parse(raw, None)
    except phonenumbers.NumberParseException as exc:
        raise ValueError("number does not parse") from exc
    if not phonenumbers.is_valid_number(parsed):
        raise ValueError("number is not a valid subscriber number")
    region = phonenumbers.region_code_for_number(parsed)
    if country == _NON_GEO_REGION:
        # Only a genuinely non-geographic number may be declared "001".
        if region != _NON_GEO_REGION:
            raise ValueError("number is not valid for the declared country")
    elif region == _NON_GEO_REGION:
        # A non-geographic number may not be declared with a
        # geographic ISO country - the pairing is a mismatch.
        raise ValueError("number is not valid for the declared country")
    elif not phonenumbers.is_valid_number_for_region(parsed, country):
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
    line_id: _CheckedLineId
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
    channel: _CheckedId


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

    The rule is ``round(interval_sec * 1000)`` - nearest millisecond -
    clamped to ``>= 1``. The interval itself is floored at millisecond
    granularity (``ge=0.001`` s, the wire unit) on
    :class:`LineTimeouts` and :class:`TimeoutConfig`, so the clamp only
    guards a caller that bypasses model validation; the pair together
    guarantees a stamped ``heartbeat_interval_ms >= 1`` for ANY config
    that passes validation - a truncation to zero (e.g.
    ``int(0.0005 * 1000) == 0``) can never reach
    :class:`ProviderBinding` and break its ``gt=0`` construction."""
    return max(1, round(interval_sec * 1000))


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
    :func:`heartbeat_interval_ms_from_sec` - ``round(sec * 1000)``
    clamped to ``>= 1`` ms, round 24c) at register/reregister time so
    a runtime ``configure_line`` patch never retro-applies to a live
    provider session.

    Upper bound (round 25, review-15 item 22): every float knob is
    additionally capped at ``le=86400.0`` s - one day. A larger value
    is a config bug, not a knob: an effectively-infinite window would
    defer teardown bookkeeping indefinitely (rule 7), and a
    ``1e20``-style input would explode ``timedelta`` arithmetic even
    with ``allow_inf_nan`` off."""

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
    ``REACHABLE`` is the default for unbound/pool lines (no provider
    attached means nothing degraded to report)."""

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
    liveness: LivenessState = LivenessState.REACHABLE
    profile: str | None = None

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

    assignment_id: _CheckedAssignmentId
    line_id: _CheckedLineId
    owner_agent_id: _CheckedAgentId | None = None
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
    not through a zero patch."""

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
        alert-only escalation instantly and continuously)."""
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
    profile: str | None = None
    liveness: LivenessState = LivenessState.REACHABLE
    heartbeat_interval_ms: int = Field(default=5000, gt=0)
    """Ping cadence negotiated for THIS provider session (round 24,
    review-13 C2): stamped from the line's effective
    ``LineTimeouts.heartbeat_interval_sec`` inside
    ``register_provider`` via
    :func:`heartbeat_interval_ms_from_sec` - ``round(sec * 1000)``,
    clamped to ``>= 1`` (round 24c; ``heartbeat_interval_sec`` is
    floored at ``ge=0.001`` s so a valid config can never stamp 0) -
    and returned to the adapter so the
    ``provider.register``/``provider.reregister`` response can carry
    the wire ``heartbeat_interval_ms``. A ``configure_line`` patch to
    ``heartbeat_interval_sec`` takes effect on the NEXT
    ``provider.reregister`` - it is never enforced retroactively
    against the interval stamped here (round 23, review-12 H6:
    shrinking it mid-session would kill a healthy provider on a
    false ping timeout)."""
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

    @field_validator("caps")
    @classmethod
    def _caps_are_telecom_only(cls, caps: tuple[Intent, ...]) -> tuple[Intent, ...]:
        return _provider_caps_check(caps)

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
    of docs/api-contract.md "Queue semantics". Tickets are
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
    multi-call agent can hold N parallel acquires.
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


class AcquireRefused(ContractModel):
    """Clean refusal: nothing available right now. This is a ROUTINE
    result, not an exception - the client retries itself (no
    server-side parked requests, round 8)."""

    granted: Literal[False] = False
    free_lines: tuple[LineId, ...] = ()


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
    media_endpoint: str = Field(min_length=1)
    """The AGENT-side media endpoint (round 25, review-15 item 6):
    the address the calling agent connects its media socket to -
    the ``agent_media_endpoint`` half of the lease's
    :class:`MediaCredentials` pair. The provider receives a
    DIFFERENT endpoint inside its outbox command."""
    media_token: MediaToken


class AcceptResult(ContractModel):
    """call.accept result: atomic lease + answer + media attach."""

    lease_id: _CheckedLeaseId
    line_id: _CheckedLineId
    media_endpoint: str = Field(min_length=1)
    """The AGENT-side media endpoint (round 25, review-15 item 6) -
    same split as :attr:`DialResult.media_endpoint`."""
    media_token: MediaToken


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
    :class:`ProviderReattachMediaCommand`)."""

    lease_id: _CheckedLeaseId
    media_endpoint: str = Field(min_length=1)
    """The AGENT-side media endpoint - same split semantics as
    :attr:`DialResult.media_endpoint`."""
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
    ``CLAIM_NOT_FOUND``. Pending claims never linger silently
    (round 25, review-15 item 23): entries on a line that becomes
    owned or ``claimable=False`` auto-resolve as rejected ->
    :class:`ClaimResolvedEvent` ``{status: "rejected"}`` to the
    claiming session."""

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
    number: RedactedString
    media_endpoint: str = Field(min_length=1)
    """The PROVIDER-side media endpoint (round 25, review-15 item 6):
    the ``provider_media_endpoint`` half of the lease's
    :class:`MediaCredentials` pair - the address the LineProvider
    connects ITS media socket to. The agent receives a different
    endpoint inside DialResult/AcceptResult."""
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
    line_id: _CheckedLineId
    assignment_id: _CheckedAssignmentId
    call_token: CallToken
    """Ring correlation is BY LINE, not by token (round 25, review-15
    item 10): a line never holds two rings, so the provider resolves
    ``line.answer{call_token}`` against the line's ONE open ring.
    The field carries whichever token the kernel currently tracks -
    the original ``call_token`` OR a re-issued
    ``fallback_call_token`` - and the provider MUST answer the open
    ring regardless of which value arrives (no provider-side token
    table change)."""
    lease_id: _CheckedLeaseId
    media_endpoint: str = Field(min_length=1)
    """The PROVIDER-side media endpoint (round 25, review-15 item 6) -
    same split as :attr:`ProviderDialCommand.media_endpoint`."""
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
    line_id: _CheckedLineId
    assignment_id: _CheckedAssignmentId
    lease_id: _CheckedLeaseId | None = None
    call_token: CallToken | None = None
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
    reason: str | None = None
    """Free-form admin note relayed to ``line.revoke{reason?}`` (round-21
    review audit: the wire table documents no closed vocabulary for
    this param, so it does NOT ride CallEndReason)."""


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
    re-authenticate, resume the stream."""

    command: Literal["reattach_media"] = "reattach_media"
    line_id: _CheckedLineId
    assignment_id: _CheckedAssignmentId
    lease_id: _CheckedLeaseId
    media_endpoint: str = Field(min_length=1)
    """The PROVIDER-side media endpoint - same split as
    :attr:`ProviderDialCommand.media_endpoint` (round 25, item 6)."""
    media_token: MediaToken


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

    The endpoint split exists because the two roles need different
    transports: a LOCAL agent is served on the ``unix://`` media
    socket while a REMOTE agent needs the ``wss://`` listener - the
    :class:`MediaAllocator` picks ``agent_media_endpoint`` from the
    acquiring session's transport profile (see
    ``MediaAllocator.allocate``)."""

    agent_media_endpoint: str = Field(min_length=1)
    provider_media_endpoint: str = Field(min_length=1)
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
    EventName.LINE_STATE_CHANGED: frozenset({"broadcast", "agent"}),
    EventName.LINE_HEALTH_CHANGED: frozenset({"broadcast", "admin"}),
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
- ``line_state_changed`` is the documented true-broadcast event -
  plus an ``agent``-scoped copy so an exclusive owner can receive
  the unmasked ``reason`` (item 44, batch 2);
- ``line_health_changed`` (item 15, batch 2) is an operational
  provider-health fact: ``broadcast`` for pool observers plus
  ``admin`` escalation - never session/agent-scoped caller PII;
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
    ``(name, scope)`` legality table (round 25)."""

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


class IncomingCallEvent(ContractModel):
    """incoming_call payload: first valid call.accept(call_token) wins
    (wake-and-grant, §4.3/§5.2). ``caller_id`` is redacted PII
    (round-22 wire-name fix: the wire field is ``caller_id``, not
    ``caller``); ``expires_at`` is the absolute accept-window
    deadline in RFC-3339 UTC - the earlier ``expires_in_ms`` relative
    form would skew by queue delay unless recomputed against
    ``emitted_at`` at outbox drain (round 22, review-10 HIGH-1)."""

    line_id: _CheckedLineId
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


class QueueCancelledEvent(ContractModel):
    """queue_cancelled payload (wire: ``event.queue_cancelled``;
    round 25, review-15 item 23): a pending ticket was CANCELLED -
    never silently parked - because its target stopped being able
    to serve it.

    Fates (see :class:`QueueCancelReason`): the target line became
    EXCLUSIVE via ``assign_line``/``approve_claim``, ``claimable``
    flipped False under ``configure_line``, the binding lost the
    intent capability the ticket asked for
    (``provider.capability_update``), or the provider was
    revoked/declared UNREACHABLE. Delivered to the ticket's own
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
    audit)."""

    assignment_id: _CheckedAssignmentId
    line_id: _CheckedLineId
    owner_agent_id: _CheckedAgentId
    reason: str | None = None


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
    ``str`` (no closed vocabulary is documented for it)."""

    claim_id: _CheckedClaimId
    line_id: _CheckedLineId
    status: Literal["approved", "rejected"]
    assignment_id: _CheckedAssignmentId | None = None
    reason: str | None = None

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
    same grant may emit both envelopes when claimant == owner."""

    assignment_id: _CheckedAssignmentId
    line_id: _CheckedLineId
    mode: AssignmentMode


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
    deadline is enforced by ``tick(now)`` (round 24, review-13 H2):
    on lapse without a worker accept the kernel burns the
    ``fallback_call_token``, releases the line
    ``RINGING_LOCKED -> FREE`` and emits
    ``MissedCallEvent(reason=CallEndReason.ACCEPT_WINDOW_EXPIRED)`` -
    the no-pickup outcome of last resort.

    Provider-side correlation (round 25, review-15 item 10): when the
    worker later accepts, the re-issued ``fallback_call_token``
    flows to the provider through the SAME ``line.answer{call_token}``
    field the original token used - the provider answers the line's
    ONE open ring regardless of which value arrives, so no
    provider-side token table change is needed."""

    line_id: _CheckedLineId
    call_token: CallToken
    caller_id: RedactedString | None = None
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

    Exclusive-line privacy (round 25, review-15 item 44): when the
    line is agent-owned EXCLUSIVE and the transition reason is
    sensitive (e.g. ``EXTERNAL_CALL`` revealing private phone use),
    the kernel emits a ``BroadcastAudience`` envelope with
    ``reason=None`` AND an additional ``AgentAudience`` copy
    carrying the real reason to the owner only - the broadcast
    never leaks the owner's private activity while the owner still
    sees the truth."""

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
    call-occupancy channel. Audience is ``broadcast`` + ``admin``:
    a PII-free operational fact pool observers and admin
    subscribers both need. ``old_state != new_state`` is enforced -
    a no-change event is a defect, not a notification."""

    line_id: _CheckedLineId
    old_state: LivenessState
    new_state: LivenessState

    @model_validator(mode="after")
    def _state_must_change(self) -> LineHealthChangedEvent:
        if self.old_state == self.new_state:
            raise ValueError("old_state and new_state must differ")
        return self


class MissedCallEvent(ContractModel):
    """missed_call payload: an inbound ring ended unanswered
    (accept-window expiry, caller hangup, or reject-policy outcome).
    ``reason`` is REQUIRED on the wire (api-contract.md event table)
    and rides the CLOSED :class:`CallEndReason` vocabulary
    (round-21 review)."""

    line_id: _CheckedLineId
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

    assignment_id: _CheckedAssignmentId
    line_id: _CheckedLineId
    owner_agent_id: _CheckedAgentId
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
    call_token: CallToken | None = None
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

    line_id: _CheckedLineId
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
                # Round 25 (review-15 item 39): the key is external
                # input and must NOT be echoed - hide_input_in_errors
                # does not cover validator-authored messages.
                raise ValueError(f"meta value exceeds {MAX_PROVIDER_META_VALUE_LEN} chars")
        return meta


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
    binding; a fact that resolves to a superseded id is dropped by
    the kernel, not applied."""
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

    ``reason`` is the CLOSED provider vocabulary
    (:class:`ProviderDisconnectReason`, round 21); gateway-minted
    reasons such as ``provider_lost`` never arrive on this ingress."""

    line_id: _CheckedLineId
    assignment_id: _CheckedAssignmentId
    call_token: CallToken | None = None
    lease_id: _CheckedLeaseId | None = None
    caller_id: RedactedString | None = None
    state: ProviderCallState
    reason: ProviderDisconnectReason | None = None
    observed_at: AwareDatetime

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

    line_id: _CheckedLineId
    assignment_id: _CheckedAssignmentId
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

    line_id: _CheckedLineId
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
        acquiring agent session: the allocator uses that session's
        transport profile to pick the ``agent_media_endpoint`` -
        ``unix://`` for a local agent socket, ``wss://`` for a
        remote one. ``None`` means "no agent-side profile known"
        (e.g. a lease minted for the internal FallbackWorker); the
        allocator then falls back to the line's default agent
        endpoint. The ``provider_media_endpoint`` is always the
        provider-role address of the same pair."""
        ...

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

        Reserved ``system:*`` identities (round 25, review-15 item
        21): the transport adapter MUST reject an ``agent_id``
        starting with :data:`SYSTEM_AGENT_PREFIX` presented on an
        external socket with ``UNAUTHORIZED`` - the prefix is
        reserved for kernel-internal service identities provisioned
        out-of-band (:data:`SYSTEM_FALLBACK_WORKER`), and API-key
        issuance must never bind a ``system:*`` identity to an
        external scope.

        Session cap (round 25, review-15 item 30; rule 7): at most
        :data:`MAX_SESSIONS_PER_AGENT` concurrent open sessions per
        ``agent_id`` - overflow is RESOURCE_EXHAUSTED with
        ``ErrorReason.SESSION_LIMIT_EXCEEDED``.

        Sleeping-owner ring re-delivery (round 25, review-15 item
        25): when the registering agent OWNS a line that currently
        holds a live ring (``RINGING_LOCKED`` with an unconsumed
        ``call_token``), the kernel re-emits
        :class:`IncomingCallEvent` to the newly opened session as a
        fresh ``SessionAudience`` envelope carrying the SAME
        ``call_token`` and the original ``expires_at`` - a
        reconnecting exclusive owner always sees its pending ring
        and can still ``call.accept`` inside the accept window.
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
        can run N parallel acquires. Queue bounds (round 25,
        review-15 item 30; rule 7): pending tickets are capped at
        :data:`MAX_TICKETS_PER_SESSION` per session and
        :data:`MAX_QUEUE_TICKETS_TOTAL` globally - overflow is
        RESOURCE_EXHAUSTED with ``ErrorReason.QUEUE_FULL``; sibling
        bound :data:`MAX_LEASES_PER_SESSION` caps the session's live
        leases.

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
        wish is honored). ``enqueue=False`` grants are torn down
        without reissue.

        Ingress ``seq`` semantics (round 25, review-15 item 7): the
        global monotonic ``QueueTicket.seq`` counter is minted at
        acquire-INGRESS for EVERY ``call.acquire`` - immediate
        grants consume a seq too - so the preempted
        ``enqueue=True`` immediate grant above re-materializes a
        ticket reusing its ORIGINAL ingress seq. That seq is by
        construction lower than any ticket queued after it, so the
        head insertion needs no negative or fractional seq value.
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
        INVALID_DTMF (round 20). INVALID_STATE when the lease has no
        ACTIVE call - tones are synthesized in-band by the
        LineProvider (see :class:`ProviderSendDtmfCommand`), so they
        only make sense mid-call."""
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
        re-authenticate ``media.auth{role: "agent"}``.
        ``OWNER_MISMATCH`` on a foreign ``(session_id, lease_id)``
        pair; ``STALE_TOKEN`` on a dead/ended lease - a reattach can
        never resurrect a torn-down media path."""
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
        ``dial_out``/``receive_inbound``.

        Exclusive-line privacy (round 25, review-15 item 44): for a
        session that does NOT own an EXCLUSIVE line, OTHER agents'
        EXCLUSIVE lines are omitted from the listing entirely - the
        caller sees SHARED_POOL lines plus the lines it owns. The
        ``state`` column stays the call-occupancy axis while
        ``liveness`` carries provider health separately (item 15)."""
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
        review-13 C2) via :func:`heartbeat_interval_ms_from_sec`
        (``round(sec * 1000)``, min 1 ms - round 24c) so the adapter
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
        and enqueues :class:`ProviderReattachMediaCommand` in the
        outbox - media tokens are one-shot and do NOT survive a
        socket drop, so the dead session's burned
        ``provider_media_token`` can never authenticate the new
        media socket.
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
        are made against removed intents. A shrink DOES cancel every
        pending queue ticket that asked for the removed intent -
        ``event.queue_cancelled{reason: "capability_removed"}``
        (round 25, review-15 item 23): a ticket that can never be
        granted is cancelled, never silently parked."""
        ...

    @abstractmethod
    async def note_provider_liveness(self, liveness: ProviderLiveness) -> None:
        """Ingest a provider liveness FACT (reachable/ws_disconnected/
        unreachable + timestamp). Drives the provider-HEALTH axis:
        transitions surface as ``event.line_health_changed``
        (round 25, review-15 item 15 - never ``line_state_changed``),
        an UNREACHABLE binding tears its leases down per §5.4,
        cancels its pending tickets as
        ``event.queue_cancelled{reason: "provider_lost"}`` (item 23)
        and feeds PROVIDER_UNREACHABLE outcomes. Idempotent:
        replaying a stale fact is harmless; ordering is by
        checked_at.

        Fact recording vs escalation (round 25, review-15 item 32):
        this method records facts and arms/disarms
        ``ProviderBinding.orphan_deadline_at`` - a WS_DISCONNECTED
        fact SETS the orphan window, a REACHABLE fact CLEARS it.
        The missed-ping DEGRADED->DOWN escalation is owned
        EXCLUSIVELY by ``tick(now)``, which compares the binding's
        missed-ping count against the thresholds STAMPED on the
        binding at register/reregister
        (``heartbeat_degraded_after``/``heartbeat_down_after``) -
        never against the line's current ``LineTimeouts``, so a
        mid-session ``configure_line`` patch cannot silently kill a
        healthy provider. ``ProviderLiveness.assignment_id`` is
        REQUIRED (item 32): a fact naming a superseded binding id
        is dropped, never cross-applied to a re-registered
        binding."""
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
          event (same ``call_token``);
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
        though the control socket dropped)."""
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
        liveness escalation (the ``heartbeat_degraded_after`` /
        ``heartbeat_down_after`` ladder minting
        ``event.line_health_changed``, round 25, review-15 item 15).

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
    :func:`heartbeat_interval_ms_from_sec`, ``round(sec * 1000)``
    min 1, so a valid value can never stamp a zero cadence).
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

        Outbox effects (round 25, review-15 batch 2): the kernel
        emits :class:`AssignmentGrantedEvent` to the new owner's
        ``AgentAudience`` so the agent learns the rotated
        ``assignment_id`` it needs for ``release{assignment_id}``
        (item 2 - the admin reply cannot carry it to the agent);
        cancels every pending queue ticket targeting the line as
        ``event.queue_cancelled{reason: "line_exclusive"}`` and
        auto-resolves its pending claims as rejected
        :class:`ClaimResolvedEvent` (item 23 - a non-owner can
        never be granted or approved on the now-exclusive line)."""
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
        line cancels pending tickets
        (``queue_cancelled{reason: "line_exclusive"}``) and
        auto-rejects other pending claims on it (item 23)."""
        ...

    @abstractmethod
    async def reject_claim(self, claim_id: ClaimId, reason: str | None = None) -> None:
        """Reject a pending claim request with an optional PII-free
        reason string. Unknown or already-resolved ``claim_id`` ->
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
        ``revoke_assignment``."""
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

        ``claimable=False`` queue fate (round 25, review-15 item
        23): flipping a line out of the claimable pool cancels every
        pending queue ticket targeting it -
        ``event.queue_cancelled{reason: "line_exclusive"}`` - and
        auto-resolves its pending claims as rejected
        :class:`ClaimResolvedEvent`. A ticket or claim that can
        never be granted is resolved explicitly, never parked.

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
        be called after EVERY kernel call."""
        ...
