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

import copy
import hmac
import math
import re
from abc import ABC, abstractmethod
from collections.abc import Collection, Mapping
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import (
    Annotated,
    Any,
    Final,
    Generic,
    Literal,
    NewType,
    NoReturn,
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
    "CLAIM_VISIBLE_LIVENESS_STATES",
    "DTMF_PATTERN",
    "E164_PATTERN",
    "ERROR_CODE_WIRE",
    "EVENT_AUDIENCE_SCOPES",
    "EVENT_PAYLOAD_TYPES",
    "EVENT_WIRE_NAMES",
    "IDEMPOTENCY_TTL_SEC",
    "MAX_CALLER_ID_LEN",
    "MAX_CALL_STAMP_WATERMARKS_PER_LINE",
    "MAX_CLOCK_SKEW_SEC",
    "MAX_CLOSED_ASSIGNMENTS",
    "MAX_CLOSED_LEASES",
    "MAX_CLOSED_TICKETS",
    "MAX_CTRL_FRAME_LEN",
    "MAX_DETAIL_LEN",
    "MAX_DTMF_DIGITS",
    "MAX_DTMF_STREAM_PAUSE_SEC",
    "MAX_ENDPOINT_LEN",
    "MAX_FORWARD_HOPS",
    "MAX_HEARTBEAT_INTERVAL_MS",
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
    "MAX_TICK_DEADLINES",
    "MAX_TOKEN_LEN",
    "MAX_WAKE_HOOKS_PER_LINE",
    "MEDIA_AUTH_TIMEOUT_SEC",
    "MEDIA_PAIR_TTL_SEC",
    "OUTBOX_HARD_CAP",
    "OUTBOX_SOFT_CAP",
    "PROVIDER_COMMAND_WIRE_NAMES",
    "RING_DEDUP_MIN_GAP_SEC",
    "SERVABLE_LIVENESS_STATES",
    "SNAPSHOT_RECONCILED_PHASES",
    "SYSTEM_AGENT_PREFIX",
    "SYSTEM_FALLBACK_WORKER",
    "UNMATCHED_CALL_SAFETY_SEC",
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
    "CommandAckKind",
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
    "FanoutAudience",
    "ForwardViaLineFallback",
    "IdleAlertEvent",
    "IncomingCallEvent",
    "IncomingCallWakeEvent",
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
    "MediaOverflowEvent",
    "MediaReattachResult",
    "MediaRole",
    "MediaToken",
    "MediaTransport",
    "MissedCallEvent",
    "MissedCallWakeEvent",
    "NoPickupFallback",
    "PairWindowRequest",
    "PairWindowResponse",
    "PendingClaim",
    "PendingRing",
    "PhonebridgeError",
    "ProviderActiveCall",
    "ProviderAnswerCommand",
    "ProviderBinding",
    "ProviderCallState",
    "ProviderCallStateUpdate",
    "ProviderCapabilityUpdate",
    "ProviderCommand",
    "ProviderCommandAck",
    "ProviderDialCommand",
    "ProviderDisconnectReason",
    "ProviderHangupCommand",
    "ProviderLiveness",
    "ProviderMediaReattachRequest",
    "ProviderMediaReattachResult",
    "ProviderReattachMediaCommand",
    "ProviderRegisterResult",
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
    "TransportProfile",
    "WakeAudience",
    "WireCallState",
    "clamp_provider_stamp",
    "ensure_aware_utc",
    "heartbeat_interval_ms_from_sec",
    "is_system_agent_id",
    "secure_compare",
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
    - AUTH decisions MUST use :func:`secure_compare` (round 31,
      SA_swe2_review_1 item 30): ``__eq__``/``__hash__`` are model-
      comparison conveniences whose dict/set lookups route through
      ``str.__hash__`` - a hashing layer an attacker-influenced
      workload could probe. The authentication path rides the
      dedicated constant-time helper, never container membership.
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
        """Model-comparison hash ONLY (round 31, SA_swe2_review_1 item
        30): needed so frozen models containing tokens remain hashable
        and dict/set lookups stay consistent with ``__eq__``. It is
        the ordinary ``str`` hash - NOT a constant-time operation -
        so authentication code MUST compare secrets via
        :func:`secure_compare`, never via ``==`` inside a hash-table
        path or bare ``dict``/``set`` membership checks."""
        return hash(self._value)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, RedactedString):
            return False
        # Constant-time secret compare (round 25, review-15 item 39;
        # length semantics stated honestly round 32,
        # SA_swe2_review_2 item 20): ``==`` on the raw str
        # short-circuits on the first differing byte - a timing
        # oracle for a media/call token checked inside
        # ``media.auth``/``call.accept``. ``hmac.compare_digest``
        # runs in time independent of CONTENT, but it early-outs
        # on a length MISMATCH - the comparison is constant-time
        # only for equal-length inputs, and the length of a
        # presented token is NOT hidden (tokens are fixed-shape
        # credentials, so the leak is documented and accepted -
        # the claim here is content-flatness, never length
        # hiding). Surrogate-
        # safe (round 31, SA_swe2_review_1 item 30; hash/equality
        # consistency fixed round 33): a pathological value carrying
        # an unpaired surrogate fails UTF-8 encoding - the earlier
        # ``NotImplemented`` fallback broke the ``__hash__``/
        # ``__eq__`` contract because ``hash()`` still returns the
        # CONTENT hash while ``==`` degraded to identity, so two
        # equal-content instances compared unequal inside dict/set
        # membership. The unencodable path now falls back to plain
        # ``str`` equality on the raw values instead: it is
        # consistent with the content hash, it never raises, and
        # it only ever applies to non-encodable garbage a real
        # credential path could never produce.
        left = _secret_bytes(self._value)
        right = _secret_bytes(other._value)
        if left is None or right is None:
            return self._value == other._value
        return hmac.compare_digest(left, right)

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


def _secret_bytes(value: str) -> bytes | None:
    """UTF-8 encode a secret for constant-time comparison (round 31,
    SA_swe2_review_1 item 30): returns ``None`` for a value that
    cannot be encoded (an unpaired surrogate), so the caller fails
    closed instead of leaking ``UnicodeEncodeError`` as an
    authentication-path crash."""
    try:
        return value.encode("utf-8")
    except UnicodeEncodeError:
        return None


def secure_compare(left: RedactedString, right: RedactedString) -> bool:
    """Constant-time secret equality for AUTHENTICATION decisions
    (round 31, SA_swe2_review_1 item 30): the helper
    ``media.auth``/``call.accept``/provider-token checks MUST ride.
    :func:`hmac.compare_digest` keeps the comparison flat w.r.t.
    token CONTENT; it early-outs on a length mismatch, so it is
    constant-time ONLY for equal-length inputs and the presented
    token's length is observable (round 32, SA_swe2_review_2 item
    20 - the earlier "length-tolerant" wording implied a length
    hiding the function does not provide; accepted because tokens
    are fixed-shape credentials). Unpaired surrogates return
    ``False`` (fail
    closed) rather than raising ``UnicodeEncodeError``. Container
    ``__eq__``/``__hash__``/dict lookups are model conveniences only -
    a hash-table membership test is never an auth verdict."""
    left_b = _secret_bytes(left.expose())
    right_b = _secret_bytes(right.expose())
    if left_b is None or right_b is None:
        return False
    return hmac.compare_digest(left_b, right_b)


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
    the colon/length checks. Round 30 (SA_carbon_review_19 slice-C
    item 13) additionally bounds the FULL identifier: a ``system:``
    id could otherwise carry an oversized prefix-plus-suffix payload
    through every ``_CheckedAgentId`` field while only its suffix
    was length-checked, so the complete value is capped at
    ``_ID_MAX_LEN`` as well (the prefix is a fixed constant, so a
    full-length bound subsumes the suffix bound)."""
    if len(value) > _ID_MAX_LEN:
        raise ValueError(f"agent id exceeds {_ID_MAX_LEN} chars")
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


def _owner_agent_is_never_system(value: str | None) -> str | None:
    """Durable-ownership agent-id check (round 30,
    SA_carbon_review_19 slice-C item 13): ``system:*`` names
    kernel-internal service roles such as
    :data:`SYSTEM_FALLBACK_WORKER` that hold no external agent
    session - a durable owner / durable-event owner field that
    carried one would grant or attribute ownership to an identity
    that can never exercise or release it. Shared by
    :class:`LineAssignment`, :class:`AdminLineView`,
    :class:`PendingClaim`, :class:`AssignmentRevokedEvent` and
    :class:`IdleAlertEvent`; socket-bound per-session owner fields
    (:class:`CallLease.owner_agent_id`, ``AgentAudience.agent_id``)
    deliberately keep ``_CheckedAgentId`` because the fallback
    worker legitimately owns leases and events."""
    if value is not None and is_system_agent_id(value):
        raise ValueError("owner_agent_id must not be a system:* identity")
    return value


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
    """Bearer-token hygiene (round 28, review-18 items 16/17;
    text gate added round 32, SA_swe2_review_2 item 20): a
    ``CallToken``/``MediaToken`` is a non-empty opaque string of at
    most ``MAX_TOKEN_LEN`` chars - an empty token can never
    authorize anything and an unbounded one is a wire/log bloat
    hazard - and it additionally passes the shared
    :func:`_require_no_control_text` gate: no control bytes, no
    unpaired surrogates, never whitespace-only (a token is
    compared verbatim in ``media.auth``, so an exotic raw value
    is a credential-smuggling surface, not legitimate entropy).
    Generic over the token NewTypes so each checked field
    keeps its declared type."""
    raw = value.expose()
    if not raw:
        raise ValueError("token must be non-empty")
    if len(raw) > MAX_TOKEN_LEN:
        raise ValueError(f"token exceeds {MAX_TOKEN_LEN} chars")
    _require_no_control_text(raw, "token")
    return value


_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
"""Any C0/C1 control byte or DEL (round 31, SA_swe2_review_1 item
31): NUL, CR/LF, tab, ESC and the ANSI/terminal-control range are
banned from every free-form string the contract carries - they
corrupt logs/terminals, smuggle injected content past reviewers,
and a NUL can truncate downstream C-string handling."""

_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")
"""Unpaired UTF-16 surrogate code point (round 32,
SA_swe2_review_2 item 20): JSON decoders admit lone surrogates
into ``str``, but the value can never be UTF-8 encoded - it
crashes serialization on the way back out and trips
``UnicodeEncodeError`` paths the contract's auth helpers had to
special-case. Rejected at the same gate as control text."""


def _require_no_control_text(raw: str, label: str) -> str:
    """Shared control-character/surrogate/whitespace-only gate
    (round 31, SA_swe2_review_1 item 31; unpaired surrogates added
    round 32, SA_swe2_review_2 item 20): a non-empty free-form
    string must contain no C0/C1/DEL bytes, no unpaired UTF-16
    surrogate, and must not be whitespace-only (``"   "`` passes
    ``min_length`` but carries no information -
    it is the whitespace twin of ``""``). Returns ``raw`` for
    chaining."""
    if _CONTROL_CHAR_RE.search(raw):
        raise ValueError(f"{label} must not contain control characters")
    if _SURROGATE_RE.search(raw):
        raise ValueError(f"{label} must not contain unpaired surrogates")
    if not raw.strip():
        raise ValueError(f"{label} must not be whitespace-only")
    return raw


def _check_caller_number(value: RedactedString) -> RedactedString:
    """Caller-number field bound (round 28, review-18 items 16/17;
    hardened round 31, SA_swe2_review_1 item 31):
    ``caller_id``/``number`` fields are non-empty when present, at
    most ``MAX_CALLER_ID_LEN`` chars, whitespace-content only (never
    whitespace-only), and free of control characters - a witheld
    identity is modeled by the absent field (``None``), never by
    ``""`` or ``"\\n"``."""
    raw = value.expose()
    if not raw:
        raise ValueError("caller number must be non-empty")
    if len(raw) > MAX_CALLER_ID_LEN:
        raise ValueError(f"caller number exceeds {MAX_CALLER_ID_LEN} chars")
    _require_no_control_text(raw, "caller number")
    return value


_CheckedCallToken = Annotated[CallToken, AfterValidator(_check_token)]
_CheckedMediaToken = Annotated[MediaToken, AfterValidator(_check_token)]
_CheckedCallerNumber = Annotated[RedactedString, AfterValidator(_check_caller_number)]


_MASKED_CALLER_SHAPE: re.Pattern[str] = re.compile(r".{0,4}\*{4}.{0,2}")
"""Exact geometry of :meth:`RedactedString._mask` output (round 34
fix): at most ``_MASK_PREFIX_LEN`` = 4 visible chars, the
``_MASK`` marker ``"****"``, then at most ``_MASK_SUFFIX_LEN`` = 2
visible chars. A ``fullmatch`` pins the marker into position, so a
raw string that merely CONTAINS ``****`` somewhere - a leading
mask glued to a full number, a trailing mask, or a buried marker -
can never validate as a masked projection."""


def _check_masked_caller(value: str) -> str:
    """Masked-caller projection check (round 34, iteration-2 audit
    item 16): the value a wake-hook payload may carry for the
    caller is the MASKED rendering of the ring's ``caller_id`` -
    the output of ``str(RedactedString)`` (e.g. ``"+373****45"``),
    produced by the kernel when it projects
    :class:`IncomingCallEvent` into :class:`IncomingCallWakeEvent`
    and serializable in ANY dump mode without ever revealing the
    raw number (a ``RedactedString``'s own JSON-mode dump emits
    the raw value, so the wake model deliberately types this a
    plain ``str``). The mask SHAPE is required, not just the
    marker: every ``RedactedString._mask`` output matches
    :data:`_MASKED_CALLER_SHAPE` (``.{0,4}****.{0,2}`` fullmatch -
    up to 4 visible prefix chars, the ``****`` marker, up to 2
    visible suffix chars), so a full raw number, a raw number with
    a leading/trailing ``****`` glued on, or any value whose mask
    sits outside the rendered window is rejected just like a
    markerless string. Bounded by ``MAX_CALLER_ID_LEN`` like the
    raw field it projects and free of control text."""
    if not value:
        raise ValueError("masked caller number must be non-empty")
    if len(value) > MAX_CALLER_ID_LEN:
        raise ValueError(f"masked caller number exceeds {MAX_CALLER_ID_LEN} chars")
    if _MASKED_CALLER_SHAPE.fullmatch(value) is None:
        raise ValueError(
            "masked caller number must match the masked projection shape "
            "(<=4 chars, the **** marker, <=2 chars)"
        )
    _require_no_control_text(value, "masked caller number")
    return value


_CheckedMaskedCaller = Annotated[str, AfterValidator(_check_masked_caller)]


def _check_idempotency_key(value: str | None) -> str | None:
    """``idempotency_key`` text gate (round 32, SA_swe2_review_2
    item 20): same bounded-string rule as every other external
    string - non-empty when present, at most
    :data:`MAX_IDEMPOTENCY_KEY_LEN` chars, free of control bytes
    and unpaired surrogates, never whitespace-only; the key rides
    logs and dedup-table keys verbatim, so the exotic-text ban is
    not cosmetic."""
    if value is None:
        return None
    if len(value) > MAX_IDEMPOTENCY_KEY_LEN:
        raise ValueError(f"idempotency_key exceeds {MAX_IDEMPOTENCY_KEY_LEN} chars")
    return _require_no_control_text(value, "idempotency_key")


_CheckedIdempotencyKey = Annotated[str | None, AfterValidator(_check_idempotency_key)]


def _check_profile(value: str) -> str:
    """Profile/label string hygiene (round 31, SA_swe2_review_1 item
    31): a ``profile``/display label is non-empty, bounded by
    :data:`MAX_PROFILE_LEN` (kept here AND on the ``Field`` so the
    schema keeps publishing the cap), free of control characters
    and never whitespace-only."""
    if not value:
        raise ValueError("profile must be non-empty")
    if len(value) > MAX_PROFILE_LEN:
        raise ValueError(f"profile exceeds {MAX_PROFILE_LEN} chars")
    return _require_no_control_text(value, "profile")


_CheckedProfile = Annotated[str, AfterValidator(_check_profile)]
"""A profile label with the round-31 control-char/whitespace gate
baked into the type alias - shared by every ``profile`` carrier
(registration, binding, assignment, views, lease, ticket)."""


def _check_free_text(value: str | None) -> str | None:
    """Free-form human text hygiene (round 31, SA_swe2_review_1 item
    31) for ``reason``/``detail``-style fields that flow to the
    wire (``event.*`` payloads, ``ErrorData.detail``): control
    characters are banned (NUL/CR/LF/ANSI can hide injected content
    or corrupt a line-oriented log sink), whitespace-only text is
    meaningless, ``None`` stays "field absent". Length bounds stay
    on the individual ``Field`` declarations - this gate owns only
    the character hygiene."""
    if value is None:
        return None
    return _require_no_control_text(value, "text")


_CheckedFreeText = Annotated[str | None, AfterValidator(_check_free_text)]
"""Optional free-form text with the round-31 control-char gate."""


_MAC_PATTERN = re.compile(r"^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$")


def _check_mac_address(value: str | None) -> str | None:
    """Bluetooth MAC field (round 31, SA_swe2_review_1 item 10):
    ``adapter_mac``/``paired_phone_mac`` are optional hardware
    addresses in canonical ``AA:BB:CC:DD:EE:FF`` form - a string
    that is not a MAC is a config defect the model rejects
    (``None`` stays "unconfigured"). The colon bytes are exactly
    why these fields can never ride the ``_CheckedId`` grammar."""
    if value is None:
        return None
    if not _MAC_PATTERN.fullmatch(value):
        raise ValueError("expected a MAC address in AA:BB:CC:DD:EE:FF form")
    return value.upper()


_CheckedMacAddress = Annotated[str | None, AfterValidator(_check_mac_address)]
"""Optional canonical-uppercase Bluetooth MAC address."""


def _check_country_alpha2(value: str | None) -> str | None:
    """Telephony region field (round 31, SA_swe2_review_1
    item 10; region membership added round 33; vocabulary wording
    corrected in the PR #82 review round): ``line_home_country``
    carries exactly two uppercase ASCII letters or ``None``, AND the
    code must be a KNOWN phonenumbers-supported alpha-2 region -
    the ISO 3166-1-style region vocabulary PLUS its telephony
    extensions (e.g. ``"XK"``, ``"AC"``, ``"TA"``), NOT strict ISO
    3166-1 only. An
    alpha-2-shaped but unassigned code (``"ZZ"``, ``"AA"``) would
    silently pass the shape gate and feed a nonsense region into the
    dial-normalization default. Membership is checked against
    :data:`_ISO_REGION_CODES` (the same phonenumbers-derived set
    :func:`validate_e164_country` uses); the runtime module-level
    lookup is safe because validation runs after module load."""
    if value is None:
        return None
    if not re.fullmatch(r"[A-Z]{2}", value):
        raise ValueError("expected an uppercase alpha-2 region code (two uppercase letters)")
    if value not in _ISO_REGION_CODES:
        raise ValueError("expected a known phonenumbers-supported alpha-2 region")
    return value


_CheckedCountryAlpha2 = Annotated[str | None, AfterValidator(_check_country_alpha2)]
"""Optional uppercase alpha-2 telephony region code (phonenumbers
vocabulary - ISO-style regions plus telephony extensions such as
``"XK"``/``"AC"``/``"TA"``)."""


_ANNOUNCEMENT_REF_CHARSET: re.Pattern[str] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")
"""Announcement-reference grammar (round-31 fix round, PR #78
warning 4): a widened :data:`_ID_CHARSET` that additionally
permits ``:`` and ``/`` so provisioned asset selectors like
``builtin:beep`` or a namespaced ``wav/<asset>`` path are legal.
The ``:`` byte is safe HERE because a
:class:`RecordAndTelegramFallback` ref never rides the compact
``<policy>:<param>`` split - the compact spelling carries only
``<channel>`` and the ref arrives via the dict/JSON form or the
default. :attr:`AnswerAndAnnounceFallback.announcement_ref` shares
this widened type since round 32 (SA_swe2_review_2 item 19): its
compact ``answer_and_announce:<ref>`` spelling splits on the
FIRST colon only, so a ``:``-carrying selector stays inside the
param. The alphabet still bans whitespace, control bytes, path
separators other than ``/`` and glob metacharacters, and the
grammar bans a leading ``/`` and ``..`` segments outright."""


def _check_announcement_ref(value: str) -> str:
    """Announcement-asset selector hygiene: non-empty, bounded at
    ``_ID_MAX_LEN``, matching :data:`_ANNOUNCEMENT_REF_CHARSET`,
    and traversal-safe (round 32, SA_swe2_review_2 item 19): no
    ``/``-delimited segment may be ``..`` - a parent-directory
    escape inside an otherwise-legal charset must never reach the
    adapter's file lookup. A leading ``/`` needs no separate
    check: the charset's first-character class already excludes
    it, so the ref is always resolved RELATIVE to the
    announcement-asset root. The kernel check is grammar-only:
    WAV existence/media validation stays ADAPTER-side at
    ``configure_line`` time."""
    if len(value) > _ID_MAX_LEN:
        raise ValueError(f"announcement_ref exceeds {_ID_MAX_LEN} chars")
    if not _ANNOUNCEMENT_REF_CHARSET.fullmatch(value):
        raise ValueError(
            "announcement_ref must match [A-Za-z0-9][A-Za-z0-9._:/-]* "
            "(asset selectors may carry a ':' namespace or '/' path; "
            "the leading-alnum class already forbids a leading '/')"
        )
    if ".." in value.split("/"):
        raise ValueError("announcement_ref must not contain '..' path segments")
    return value


_CheckedAnnouncementRef = Annotated[str, AfterValidator(_check_announcement_ref)]
"""Bounded announcement-asset selector (``:``/``/`` allowed; no
leading ``/``, no ``..`` segments - round 32 item 19)."""


def _check_provider_call_id(value: str | None) -> str | None:
    """Provider-side call handle (round 31, SA_swe2_review_1 item
    20): the adapter-distilled provider call identifier (e.g. an
    oFono voice-call object name like ``call_1``) used for ring/
    call-state dedup. Opaque to the kernel - bounded, non-empty,
    never whitespace-only, control-char free - but deliberately NOT
    constrained to the ``_CheckedId`` charset: provider handle
    shapes (``/hci0/dev_.../call1``) can carry ``/``."""
    if value is None:
        return None
    if len(value) > 128:
        raise ValueError("provider_call_id exceeds 128 chars")
    return _require_no_control_text(value, "provider_call_id")


_CheckedProviderCallId = Annotated[str | None, AfterValidator(_check_provider_call_id)]
"""Optional bounded opaque provider call handle."""


_MEDIA_ENDPOINT_SCHEMES: frozenset[str] = frozenset({"unix://", "wss://"})
"""The only media-endpoint URI schemes the service can emit (round
31, SA_swe2_review_1 item 31): ``unix://`` is the loopback
socket-pair profile, ``wss://`` the tunnel profile - anything else
(``tcp://``, ``javascript:``, a bare path, a scheme-less host)
would send the media adapter chasing a protocol it does not
speak."""


def _check_media_endpoint(value: str) -> str:
    """Media endpoint URI hygiene (round 31, SA_swe2_review_1 item
    31): non-empty, bounded by :data:`MAX_ENDPOINT_LEN` (kept on the
    ``Field`` as well so the schema publishes the cap), control-
    char free, and restricted to the
    :data:`_MEDIA_ENDPOINT_SCHEMES` vocabulary - an arbitrary string
    is not an address the media socket can dial."""
    if not value:
        raise ValueError("media endpoint must be non-empty")
    if len(value) > MAX_ENDPOINT_LEN:
        raise ValueError(f"media endpoint exceeds {MAX_ENDPOINT_LEN} chars")
    _require_no_control_text(value, "media endpoint")
    if not value.startswith(tuple(_MEDIA_ENDPOINT_SCHEMES)):
        raise ValueError("media endpoint must be a unix:// or wss:// URI")
    return value


_CheckedMediaEndpoint = Annotated[str, AfterValidator(_check_media_endpoint)]
"""A scheme-validated bounded media-endpoint URI."""


def _check_free_lines(
    lines: tuple[LineId, ...],
) -> tuple[LineId, ...]:
    """``free_lines`` suggestion-list bound (round 31,
    SA_swe2_review_1 item 27): the hint tuple on ``AcquireRefused``
    and ``ErrorData`` is capped at :data:`MAX_LINES_TOTAL` entries
    and carries no duplicates - a repeated or oversized suggestion
    list is a defect, never a helpful hint."""
    if len(lines) > MAX_LINES_TOTAL:
        raise ValueError(f"free_lines exceeds {MAX_LINES_TOTAL} entries")
    if len(set(lines)) != len(lines):
        raise ValueError("free_lines must not contain duplicate line ids")
    return lines


_CheckedFreeLines = Annotated[
    tuple[Annotated[LineId, AfterValidator(_check_line_id)], ...],
    AfterValidator(_check_free_lines),
]
"""Bounded deduplicated ``free_lines`` tuple (each entry still goes
through :func:`_check_line_id` inside the tuple type)."""


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
    """RESERVED member (round 30, SA_carbon_review_19 slice-A item
    16): kept for wire/schema compatibility only - a ``CallLease``
    can never carry it. Inbound rings are tracked as a ring
    sub-state of the LINE (``LineState.RINGING_LOCKED`` +
    ``call_token``), not as a lease phase: the only former source
    of a ``RINGING`` lease was the ``ACQUIRED -> RINGING``
    preemption transition, which round 30 removed from
    :data:`CALL_PHASE_FORWARD_TRANSITIONS` - a ring preempting an
    undialed grant now ends that lease ``ENDED`` and the ring lives
    on the line's ring slot until accept or drain."""
    ANSWERING = "answering"
    """Inbound-answer in-flight phase (round 31, SA_swe2_review_1
    item 2): ``call.accept`` mints the lease directly into
    ``ANSWERING`` - the ``line.answer`` command has been delivered
    to the provider but the network has not yet confirmed the call
    is up. It is the inbound mirror of ``DIALING``: a real,
    post-command lease phase that provider ``state=active``/
    ``disconnected`` facts reconcile, and it joins
    :data:`SNAPSHOT_RECONCILED_PHASES` for the same reason (the
    provider CAN observe a call whose answer was already sent).
    Like ``DIALING`` it carries no ``grant_expires_at`` - the grant
    was already consumed by the accept - and it is transient on the
    wire: it maps to ``None`` in :data:`CALL_PHASE_WIRE_STATES` so
    it is never emitted as an ``event.call_state_changed.state``."""
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
    CallPhase.ANSWERING: None,
    CallPhase.ALERTING: "alerting",
    CallPhase.ACTIVE: "active",
    CallPhase.TERMINATING: None,
    CallPhase.ENDED: "disconnected",
}
"""Authoritative CallPhase → wire-state map (round 20).

``None`` means "kernel-internal - never emitted as
``event.call_state_changed.state``" (ACQUIRED is pre-dial, RINGING
surfaces as ``event.incoming_call``, ANSWERING and TERMINATING are
transient command-in-flight phases - round 31, SA_swe2_review_1
item 2 - so the four-value wire vocabulary never spells them).
Drift against the doc's state vocabulary is caught by
``test_call_phase_wire_state_parity``."""


CALL_PHASE_FORWARD_TRANSITIONS: dict[CallPhase, frozenset[CallPhase]] = {
    CallPhase.ACQUIRED: frozenset({CallPhase.DIALING, CallPhase.ENDED}),
    CallPhase.DIALING: frozenset(
        {CallPhase.ALERTING, CallPhase.ACTIVE, CallPhase.TERMINATING, CallPhase.ENDED}
    ),
    CallPhase.RINGING: frozenset(),
    CallPhase.ANSWERING: frozenset({CallPhase.ACTIVE, CallPhase.TERMINATING, CallPhase.ENDED}),
    CallPhase.ALERTING: frozenset({CallPhase.ACTIVE, CallPhase.TERMINATING, CallPhase.ENDED}),
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
Round 30 (SA_carbon_review_19 slice-A item 16) removed
``ACQUIRED -> RINGING``: inbound-ring state is a LINE sub-state
(``LineState.RINGING_LOCKED`` + ``call_token``), not a lease phase,
so a ring preempting an undialed grant ends the lease ``ENDED``
while the ring lives on the line's ring slot. Round 31
(SA_swe2_review_1 item 2) finishes that decoupling: the ``RINGING``
row keeps an EMPTY target set - the enum member is reserved/dead,
no lease may carry it and no transition may reach it - while
``ANSWERING`` (``accept()``'s answer-in-flight phase) gets the
real ``ANSWERING -> ACTIVE/TERMINATING/ENDED`` edges provider
facts and hangups drive. Round-31 fix round (PR #78 warning 1):
``TERMINATING`` joins the ``DIALING``/``ANSWERING``/``ALERTING``
target sets - the matrix documented ONLY ``ACTIVE ->
TERMINATING`` while :meth:`hangup`'s contract sends EVERY
in-flight phase through it, and a hangup on a possibly-live
dialed call legitimately parks in ``TERMINATING`` awaiting the
provider's ``disconnected`` (or the hold timeout); ``ACQUIRED``
still reaches ``ENDED`` directly because there is no air-side
call to confirm. Drift is pinned by
``test_call_phase_forward_matrix``."""


SNAPSHOT_RECONCILED_PHASES: frozenset[CallPhase] = frozenset(
    {
        CallPhase.DIALING,
        CallPhase.ANSWERING,
        CallPhase.ALERTING,
        CallPhase.ACTIVE,
        CallPhase.TERMINATING,
    }
)
"""The formal definition of "dialing-or-later" for
``state_snapshot`` reconciliation (round 29, SA_quant_review_1
slice-A item 7; ``ANSWERING`` added round 31, SA_swe2_review_1
item 2): a ``provider.reregister``/``provider.ping`` snapshot is
authoritative ONLY for leases in one of these phases - the phases
a lease can reach only AFTER the kernel delivered a
``line.dial``/``line.answer`` command, so the provider could
actually see the call on the D-Bus. ``ANSWERING`` belongs: the
``line.answer`` command already reached the provider, so an
answer-in-flight lease is exactly as observable as a dialing one.
``ACQUIRED`` is excluded
because a granted-but-undialed lease is pre-dial (the provider can
never observe it), and ``RINGING`` is excluded too: the reserved
member exists only for wire compat - no ``CallLease`` can carry it
since round 30 (SA_carbon_review_19 slice-A item 16), when the
``ACQUIRED -> RINGING`` preemption transition was removed and ring
state moved entirely onto the line's ``RINGING_LOCKED`` ring slot.
``CallPhase`` is a ``str``-valued enum with no
ordering - reconciliation MUST test membership in this set, never
a ``>=`` comparison that the type does not support. Membership is
NECESSARY but not sufficient since round 33: a lease inside this
set that still carries :attr:`CallLease.pending_command` is
EXCLUDED from snapshot reconciliation while its bounded
``provider_command_ack_sec`` window is open - the provider cannot
authoritatively assert a call it has not yet acknowledged, so a
snapshot omission never ends a command-pending lease (a
``disconnected`` fact with matching strong identity still wins;
binding death resolves the marker through the dead-binding
purge). Pinned verbatim by
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


SERVABLE_LIVENESS_STATES: frozenset[LivenessState] = frozenset(
    {LivenessState.REACHABLE, LivenessState.DEGRADED}
)
"""The "attached control channel" rungs (round 34, iteration-2
audit item 4): a binding is SERVABLE - it may satisfy a new
``call.acquire`` grant, receive a ``line.dial``/``line.answer``
command or appear in ``free_lines`` alternatives - exactly while
its liveness is ``reachable`` or ``degraded``. ``ws_disconnected``
is deliberately ABSENT: the orphan-grace rung marks a binding
whose control socket is gone, so nothing can be served through it
until ``provider.reregister`` re-attaches a live channel.
``unreachable``/``unbound`` fail the test outright."""

CLAIM_VISIBLE_LIVENESS_STATES: frozenset[LivenessState] = frozenset(
    {
        LivenessState.REACHABLE,
        LivenessState.DEGRADED,
        LivenessState.WS_DISCONNECTED,
    }
)
"""The "claimable" rungs (round 34, iteration-2 audit item 4 -
supersedes the round-33 single-predicate wording that excluded
``ws_disconnected`` from the claim view): a durable
``call.claim_exclusive`` request and the
``call.get_lines{intent:"exclusive_line"}`` filter admit a line
whose binding is ``ws_disconnected`` inside its orphan grace - the
claim is a moderator-resolved REQUEST, not a call serve, and the
binding may legitimately re-attach before approval ever happens.
Serving still requires :data:`SERVABLE_LIVENESS_STATES`: a
``ws_disconnected`` line is claim-VISIBLE but can never satisfy a
grant until reregister. ``unreachable``/``unbound`` fail both
axes - dead or absent hardware is neither claimable nor servable."""


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
    dead-man teardowns and the ``external_call`` line
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
    # Round 32 (SA_swe2_review_2 item 17): the typed detail on the
    # INVALID_ARGUMENT a keyed retry gets when the SAME
    # idempotency_key re-arrives under DIFFERENT params inside the
    # (method, session_id, key) partition - the stored outcome is
    # never silently merged with a non-matching request.
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    # Round 34 (iteration-2 audit item 21): the typed detail on the
    # INVALID_ARGUMENT a listing call gets when its ``cursor``
    # input was minted under a DIFFERENT scope - a different
    # method, filter tuple, or ordering - so a cursor can never be
    # replayed across listing contexts and silently answer the
    # wrong query.
    CURSOR_MISMATCH = "CURSOR_MISMATCH"


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
    MEDIA_OVERFLOW = "media_overflow"


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
    EventName.MEDIA_OVERFLOW: "event.media_overflow",
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


_MAX_REASONABLE_STAMP_GAP: timedelta = timedelta(days=3650)
"""Far-future safety margin for contract timestamps (round 31,
SA_swe2_review_1 item 18): any stamp whose converted UTC value
lies within this many days of ``datetime.max`` is rejected. Two
failure shapes sit behind it: (a) ``astimezone`` itself can raise
``OverflowError`` on a near-max year with a large negative offset,
leaking a raw non-contract error at the boundary, and (b) a
surviving near-max stamp still explodes the next deadline
addition inside the kernel (``stamp + timedelta`` is not guarded).
Failing closed at validation keeps both defects a contract
``ValueError`` (INVALID_ARGUMENT at the adapter)."""


_MIN_REASONABLE_STAMP: datetime = datetime(2000, 1, 1, tzinfo=timezone.utc)
"""Far-past plausibility floor for contract timestamps (round 32,
SA_swe2_review_2 item 4 - ABSOLUTE bound, deliberately NOT
now-relative): any stamp before the year 2000 is rejected. The
floor exists to catch ``datetime.min``/epoch-zero garbage and
pre-Y2K RTC garbage without re-introducing the round-31
``now - gap`` window - a relative bound made validation depend on
the host clock (the pydantic path has no injected ``now``), so an
RTC-less provider reporting epoch-1970 stamps or a >10-year-old
durable row would wedge at model construction instead of being
handled as advisory input. The only clock-sensitive bound is the
``+MAX_CLOCK_SKEW_SEC`` clamp :func:`clamp_provider_stamp` applies
where elapsed math actually needs it."""


def ensure_aware_utc(value: datetime) -> datetime:
    """Enforce CODING_HAZARDS rule 6: reject naive datetimes; normalize
    aware values to canonical UTC. Public (round 20) so that runtime
    entry points - e.g. every ``LineManager.tick(now)`` implementation
    and any adapter ingress bypassing pydantic validation - can apply
    the same check the ``AwareDatetime`` annotation only documents.

    Pathological-range guard (round 31, SA_swe2_review_1 item 18;
    ABSOLUTE bounds pinned round 32, SA_swe2_review_2 item 4): the
    UTC conversion is wrapped so a near-``datetime.max`` stamp
    (a huge year plus a large negative offset) surfaces as a
    ``ValueError`` - never a raw ``OverflowError`` - and the
    converted result is bounded ABSOLUTELY to
    [:data:`_MIN_REASONABLE_STAMP`,
    ``datetime.max - _MAX_REASONABLE_STAMP_GAP``]. The bound is
    deliberately NOT relative to ``now`` (the round-31 ``now +-
    3650d`` window was itself the defect, SA_swe2_review_2 item 4):
    pydantic validation has no kernel clock, a relative window
    would make every stored/advisory stamp's legality drift with
    wall-clock time, and clock-sensitivity belongs only where
    elapsed math needs it - :func:`clamp_provider_stamp`'s
    ``+MAX_CLOCK_SKEW_SEC`` future clamp on provider stamps. A
    far-future stamp is bounded so downstream
    ``stamp + timedelta`` deadline arithmetic can never overflow;
    a pre-2000 stamp is rejected as garbage either way."""
    if not isinstance(value, datetime):
        raise TypeError(f"expected datetime, got {type(value).__name__}")
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError("naive datetime rejected: contract timestamps must be timezone-aware")
    try:
        converted = value.astimezone(timezone.utc)
    except OverflowError as exc:
        raise ValueError("datetime outside representable UTC range") from exc
    if converted < _MIN_REASONABLE_STAMP:
        raise ValueError("datetime below min reasonable stamp range")
    if converted > datetime.max.replace(tzinfo=timezone.utc) - _MAX_REASONABLE_STAMP_GAP:
        raise ValueError("datetime beyond max reasonable stamp range")
    return converted


AwareDatetime = Annotated[datetime, AfterValidator(ensure_aware_utc)]
"""Annotated datetime that REJECTS naive input and normalizes to UTC."""

AnswerConfidence = Literal["provisional", "confirmed"]
"""Confidence that the remote party actually answered (see CallPhase.ACTIVE)."""

CommandAckKind: TypeAlias = Literal["dial", "answer"]
"""The provider commands carrying a bounded ``{accepted}`` handshake
(round 33): ``line.dial`` and ``line.answer`` are the two
down-channel commands whose reply is a first-class kernel FACT -
the adapter reports the distilled reply through
:class:`ProviderCommandAck` /
:meth:`LineManager.ingest_command_ack` -
``{accepted, provider_call_id?, reason?}`` on ``line.dial``,
``{accepted, reason?}`` on ``line.answer`` (the answer reply has
no provider-call-handle field - ring correlation stays
``call_token``/``line_id``, PR #82 review) - and :attr:`CallLease.pending_command`
records which of the two is still awaiting its bounded reply
(:attr:`LineTimeouts.provider_command_ack_sec`). ``line.hangup``
and ``line.send_dtmf`` are fire-and-forget command surfaces -
their replies are adapter-level transport results, never kernel
facts - and ``line.reattach_media`` rides its own
``provider.reattach_media`` request path."""

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

TransportProfile: TypeAlias = Literal["loopback", "direct_mtls", "wireguard"]
"""Provider-transport profile vocabulary (round 31,
SA_swe2_review_1 item 10; hci-agent.md tunnel profiles):
``loopback`` is the in-process unix-socket pair, ``direct_mtls``
the local mTLS listener and ``wireguard`` the tunneled profile -
carried on :attr:`LineAssignment.transport_profile` so the
provider adapter selects the right connector per line."""


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
"""Bound on the per-session keyed-dedup table (round 29,
SA_quant_review_1 slice-B item 6; overflow policy REVERSED round
32, SA_swe2_review_2 item 17): the
``(method, session_id, idempotency_key)`` outcome entries live
only inside the LIVE session, but a long-lived session minting a
fresh key on every call would otherwise grow the table without
bound. Overflow policy is bounded LRU: once a session already
holds this many entries, the OLDEST entry is evicted to admit
the new key - a NEW key is NEVER rejected, because the
round-29 ``RESOURCE_EXHAUSTED`` fail-closed rule punished a
legitimate long-lived session for its clients' retry hygiene,
while an evicted entry's key replaying later simply re-executes
(a stale retry window, bounded by the TTL below)."""

IDEMPOTENCY_TTL_SEC: float = 3600.0
"""Time-to-live of one keyed-dedup outcome entry (round 32,
SA_swe2_review_2 item 17): an entry recorded for
``(method, session_id, key)`` stops deduping once it is older
than one hour - a retry arriving after the TTL re-executes the
operation fresh (the idempotency contract is a bounded retry
window, not a permanent operation alias). Entries are evicted
earlier only by the :data:`MAX_IDEMPOTENCY_KEYS_PER_SESSION` LRU
bound or by ``drop_session`` tearing the whole session table
down; inside the TTL a same-key replay returns the STORED
result verbatim (``replay-after-completion`` - the recorded
outcome is served, never re-derived), and the same key under
DIFFERENT params inside the live entry's window is
``INVALID_ARGUMENT`` with
``ErrorReason.IDEMPOTENCY_CONFLICT``. In-flight semantics
(round 33, PR #82 review): the adapter creates the
``(method, session_id, key)`` entry BEFORE the kernel
mutation/provider command is dispatched, so a same-key +
identical-params retry arriving while the first request is
still in flight JOINS/WAITS on that operation - never a second
kernel entry or a second ``line.dial``/``line.answer`` - and a
session drop kills the waiter with the table."""

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
bridges and provider commands.

System-session capacity (round 33): the bound applies to EVERY
session uniformly, including the reserved
``system:fallback_worker`` session - there is deliberately NO
separate system-session lease pool. A FallbackWorker juggling
bridged ``forward_via_line`` calls at capacity simply gets
``RESOURCE_EXHAUSTED`` from ``call.acquire`` and reports the
bounded terminal outcome for that leg; the alternative - an
unbounded worker session - is exactly the parked-resource hazard
this constant exists to prevent. A worker fallback is therefore
best-effort by design: it can starve only ITSELF, never the rest
of the fleet."""

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
"""Media-attachment epoch bound (round 25, review-15 item 30; rule
7 - a socket pair that never completes its handshake must not
linger). Replaces the earlier documented ~5 s pair TTL: MCP LLM
agents routinely take >5 s between tool calls, so the kernel
reaps a pair whose role never attached after THIS window, not
after the old 5 s. Round 30 (SA_carbon_review_19 slice-A item 1)
extends the same constant to the re-attach window. Arming point
(round 34, iteration-2 audit item 9 - SUPERSEDES the round-31
"armed at credential mint" pin): for pairs minted inside
:meth:`LineManager.dial`/:meth:`LineManager.accept` the window
does NOT run against an undelivered pair - it is armed when the
command handshake resolves ``{accepted: true}`` through
:meth:`LineManager.ingest_command_ack` (counted from that instant,
and the ``provider_command_ack_sec`` deadline is the effective
bound for the "command dispatched, reply in flight" stretch
before it). A refused/failed handshake ends the lease - the pair
dies with it, so the TTL never burns against credentials nobody
received. Every OTHER issue instant keeps mint-time arming:
``MediaAllocator.reissue()`` (agent/provider ``reattach_media``)
and the ``provider.reregister`` re-pair count a FRESH window from
the reissue instant - those pairs are minted onto an already-live
call with no command handshake to wait on, and a role that never
re-attaches counts exactly like one that detached.
Ack-arming is PER-ROLE and disarm-only (round 35,
iteration-3 audit item 18): the ``{accepted: true}``
handshake arms the window ONLY for roles not yet attached -
a role whose ``media.auth`` already landed before the ack
resolved (a fast connect inside the
``provider_command_ack_sec`` window) is already live, so
arming is a no-op for it and the ack can never start a
countdown against an attached socket. The pair therefore
dies only when a role is still unattached
``MEDIA_PAIR_TTL_SEC`` after its own arming point. ``tick()``
ends the lease with ``CallEndReason.NETWORK_ERROR`` on lapse
(see :meth:`LineManager.ingest_media_state`)."""

MAX_DTMF_STREAM_PAUSE_SEC: float = 30.0
"""Upper bound on a media-stream pause the provider's DTMF pacer
may impose while a tone sequence plays (round 34, iteration-2
audit item 17): synthesizing in-band DTMF on the provider side
interrupts the air-side audio leg for the duration of the tones,
so a bounded pause is legitimate - but an UNBOUNDED one would let
a stuck pacer hold the media pair open while carrying silence,
indefinitely postponing the audio-lost detection the
``MEDIA_PAIR_TTL_SEC``/``audio_lost`` path exists for. This is the
kernel-second spelling of the media-frame ``DTMF_PAUSE_BUDGET_MS``
= 30 000 ms the ``0x05`` DTMF-START pause rides (api-contract.md
media framing; design §6.4): the value is sized so the LARGEST
legal ``call.send_dtmf`` workload - :data:`MAX_DTMF_DIGITS` = 32
digits including ``,``/``w``/``W`` pause chars - fits inside the
budget, and a second ``0x05`` re-arms the budget
rather than stacking pauses. Exhaustion semantic (round
35, iteration-3 audit item 22 - ONE behavior everywhere,
superseding the stale "pacer resumes autonomously"
wording the media docs once carried): a pause observed
LONGER than this window is a media-fault signal - the
MediaRouter reports the provider role detached
(``ingest_media_state`` ``attached:false`` path), the
``MEDIA_PAIR_TTL_SEC`` re-attach window arms, and the
lease ends ``network_error`` if no fresh provider-role
``media.auth`` rebinds inside it. A pause whose ``0x02``
flush lands BEFORE the budget lapses resumes forwarding
and contributes NOTHING to detach accounting - only a
still-armed pause lapse reports the detach, so repeated
legal tone sequences (each ``0x05`` re-arms a fresh
budget per agent ``send_dtmf`` segment, bounded by
:data:`MAX_DTMF_DIGITS`) never accumulate into a
phantom fault; the router never reads a pause as
ordinary in-call silence either."""

UNMATCHED_CALL_SAFETY_SEC: float = 3600.0
"""Safety-net deadline for a kernel record a provider fact left
unmatched (round 34, iteration-2 audit item 13): an
``external_call`` hold minted on an UNQUALIFIED ``active``/
``dialing`` fact - i.e. carrying no ``provider_call_id`` and no
burned ``call_token`` to correlate its lifecycle - gets a private
bounded deadline stamped at ``mint + UNMATCHED_CALL_SAFETY_SEC``
- the flat safety net is the ONLY rule: the line's configured
``max_call_duration_sec`` never applies to an unmatched hold,
because a handset-originated call can legitimately outlive the
configured call cap while remaining a real call the kernel must
keep tracking. ``tick`` releases the line
``FREE`` past it with ``event.line_state_changed`` reason
``network_error`` (a kernel-side lapse fact, never a fabricated
provider ``disconnected``) so a missed teardown fact can never
wedge the line ``BUSY`` forever. A hold minted WITH a
correlation handle (round 35, iteration-3 audit item 9) is
covered too - its ``provider_call_id``/burned ``call_token``
binds the later ``disconnected`` that ends it, AND it arms the
same deadline REFRESHED on every provider fact that binds to
the handle (a live reported call keeps itself alive; a
terminal fact lost in flight is swept within one window of
the last report), plus ``state_snapshot`` reconciliation: a
handle absent from an authoritative snapshot releases the
hold. Only the UNMATCHED hold - no handle to refresh against -
rides the flat mint-time deadline alone."""

MAX_WAKE_HOOKS_PER_LINE: int = 16
"""Bound on :attr:`LineAssignment.wake_hooks` (round 34,
iteration-2 audit item 2): the kernel-visible wake-capability
set of a line - the opaque escalation-hook ids the admin hook
store resolves for this line's rings. Bounded because the set is
hydrated into kernel state and drives the
:class:`WakeAudience` stamp; a configuration naming more hooks
is ``INVALID_ARGUMENT``."""

MAX_CALL_STAMP_WATERMARKS_PER_LINE: int = 64
"""Bound on the per-call-identity ``observed_at`` watermark table
the kernel keeps for a line's :meth:`ingest_call_state` dedup
(round 34, iteration-2 audit item 11): one monotone-max watermark
entry per call identity (``lease_id`` on a qualified update,
``call_token`` on ring-correlated facts, ``provider_call_id``
otherwise, plus a shared ``_unqualified`` bucket for facts
carrying none - whose dedupe unit is the ``(stamp, state)``
PAIR so same-stamp distinct states are a transition, not a
repeat (round 35, iteration-3 audit item 8)). A line's live call
set is small - a lease plus a
ring plus an external hold - so 64 entries is generous headroom
for rapid handsets churning call identities; the table is
FIFO-evicted at the cap (evicting only forfeits dedup of facts
already older than every remaining entry, never correctness of a
live call's ordering)."""

MEDIA_AUTH_TIMEOUT_SEC: int = 5
"""First-frame deadline on a freshly accepted media socket (round
25, review-15 item 30; rules 2/7): a socket that never sends
``media.auth`` inside this window is closed - an unauthenticated
connection may not hold a router slot indefinitely."""

OUTBOX_SOFT_CAP: int = 256
"""Soft depth cap of the transactional outbox (round 27, review-17
item 12; two-level model formalized round 30, SA_carbon_review_19
slice-A item 7): the pre-flight check documented on
``drain_events()`` / ``drain_provider_commands()`` rejects ONLY
agent-initiated resource-CREATING operations -
``acquire``/``dial``/``accept``/``claim_exclusive`` - and ONLY when
the outbox ALREADY holds >= ``OUTBOX_SOFT_CAP`` entries, while
kernel state is still untouched. Provider facts, admin mutation
cascades, event fanout and teardown paths
(hangup/release/release_assignment/drop_session/provider-loss/tick
expiries/admin revoke/remove_line) are EXEMPT from the soft cap -
they are bounded by :data:`OUTBOX_HARD_CAP` instead, so a wedged
outbox can never strand a teardown or deadlock an admin
mutation."""

MAX_CLOSED_LEASES: int = 1024
"""Bound on the ``_closed_leases`` insertion-order ring of
superseded lease ids (round 27, review-17 item 12): the kernel
retains a bounded ring of recently-ended ``lease_id`` values so a
late retry resolves as an idempotent success instead of
STALE_TOKEN confusion - the same ring shape as
:data:`MAX_CLOSED_ASSIGNMENTS` on the durable-binding side.
Insertion order (FIFO), NOT recency-ordered (round 30,
SA_carbon_review_19 slice-A item 12): a ring READ must never
refresh an entry's position - probing stale ids cannot push
younger tombstones out."""

MAX_CLOSED_ASSIGNMENTS: int = 1024
"""Bound on the ``_closed_assignments`` insertion-order ring of
superseded binding ids (round 27, review-17 item 12; rotation
documented on :class:`LineAssignment`): a late
``release``/``revoke`` retry by the previous owner resolves through
this ring as an idempotent success rather than OWNER_MISMATCH. ONE
ring is shared by both assignment-id object spaces -
``LineAssignment`` and ``ProviderBinding`` (round 29,
SA_quant_review_1 slice-A item 5): mint-uniqueness across the two
spaces makes a shared ring safe because a stored id can only ever
have named one object type.

Lookup semantics (round 30, SA_carbon_review_19 slice-A item 12):
entries are tagged by object space (``line`` or ``provider``
assignment id) and carry the previous ``owner_agent_id``. The ring
is insertion-order (FIFO): a READ must never refresh LRU recency,
so probing stale ids cannot push younger tombstones out. A late
``call.release``/``revoke_assignment`` idempotent retry by the
recorded previous owner returns ``None`` - it must NEVER fall
through to the line's NEW current record, which could name a
different owner's binding; provider-side facts on a dead
provider-binding id resolve ``STALE_TOKEN`` through the tagged
space instead. ``remove_line`` records its deleted durable
assignment id into the ring the same way, so removal and revoke
share one tombstone semantic."""

MAX_CLOSED_TICKETS: int = 1024
"""Bound on the ``_closed_tickets`` insertion-order tombstone ring
of resolved queue-ticket ids (round 31, SA_swe2_review_1 item
24): a cancelled, granted or expired ``ticket_id`` is recorded so
a late ``call.cancel_acquire`` retry resolves idempotently.
Semantics (the no-oracle pin of item 24): a LIVE ticket owned by
another session is ``OWNER_MISMATCH``, while EVERY non-live id -
tombstone hit or ring miss alike - is an idempotent ``{ok}``
no-op, so a caller can never learn whether a foreign or
never-issued ticket id once existed. The ring still matters for
a resolved ticket owned by a session that is itself gone: without
it the post-grant ``cancel`` of a served ticket would fall through
to the same unknown-id path as a random id and the bounded log of
"who held which ticket" would be lost. Same shape as the other
two tombstone rings: insertion-order FIFO where a READ never
refreshes position, capacity this bound."""

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

MAX_TICK_DEADLINES: int = 256
"""Bound on deadline items one ``tick`` call processes (round 32,
SA_swe2_review_2 item 2): all deadline classes ``tick`` owns -
accept windows, grant TTLs, ring-drain TTLs, media-pair TTLs,
``hangup_hold_sec`` lapses, ``provider_command_ack_sec`` lapses
(round 33), orphan-grace deadlines, idle alerts -
share ONE aggregate budget: at most this many expired items IN
TOTAL per call across all classes combined, never a per-class
quota; items beyond the bound stay pending and are processed on
the NEXT ``tick`` (continuation, not loss), so a mass-expiry wave
can never wedge one ``tick`` into an over-cap burst or starve the
event loop. Deadline volume can NEVER surface
``RESOURCE_EXHAUSTED`` - expiry is liveness bookkeeping, not a
client request, so backpressure is expressed as deferral, never
rejection."""

OUTBOX_HARD_CAP: int = 8192
"""Final bounded-capacity guard of the outbox (round 30,
SA_carbon_review_19 slice-A item 7; re-derived round 31,
SA_swe2_review_1 item 16; true worst case pinned round 32,
SA_swe2_review_2 item 2): the absolute bound EVERY mutation -
including soft-cap-exempt teardown, admin cascades and ring fanout
- checks BEFORE committing partial state; overflow is
RESOURCE_EXHAUSTED for client-driven mutations only - deadline
volume inside ``tick`` is deferred by
:data:`MAX_TICK_DEADLINES`, never rejected. Sizing: with the
round-32 single-envelope fan-out (one ``FanoutAudience`` envelope
per line wave, not one per session) a single deadline emits at
most a small constant of envelopes (~8: ring events, queue drain,
lease teardown, binding cascade), so the true worst-case
``tick`` burst is
``MAX_TICK_DEADLINES * 8`` = 2048 envelopes + the
:data:`OUTBOX_SOFT_CAP` steady-state floor (256) + a
:data:`MAX_LINES_TOTAL`-wide admin cascade (256) + teardown of
every live lease (``MAX_SESSIONS_TOTAL * MAX_LEASES_PER_SESSION``
= 4096) ~= 6656 - the flat ``8192`` headroom covers it with
margin, where the pre-round-32 O(sessions) fan-out arithmetic
(512-envelope ring waves) was the livelock risk the bounded tick
and single-envelope fan-out remove together."""

MAX_LIST_PAGE: int = 64
"""Page bound for the shared ``{offset?, limit?}`` window on the
listing calls (round 29, SA_quant_review_1 slice-B item 1; PR #74
review; value reduced 256 -> 64 in round 31, SA_swe2_review_1
item 9; adapter ``cursor`` input pinned round 33):
``call.get_lines``, ``admin.list_lines``,
``admin.list_leases``, ``admin.list_tickets``,
``admin.list_assignments``, ``admin.list_provider_bindings`` and
``admin.list_pending_claims`` accept an optional ``limit`` inside
``1..MAX_LIST_PAGE`` (default ``MAX_LIST_PAGE``) and return a
stable-ordered slice. The bound exists so a full page can never
overrun the wire's 64 KiB WS-TEXT frame budget at scale - the
round-29 value of 256 was arithmetic on smaller rows; with
``meta``/``provider_meta`` payloads a 256-row page overflowed the
frame. ``admin.list_provider_bindings`` answers with the COMPACT
projection for the same reason (``meta`` is stripped from the
listing rows; the field remains readable through
``admin.get_line``/the binding record itself).

Wire envelope (round 32, SA_swe2_review_2 item 13; ONE has_more
formula pinned round 34, iteration-2 audit item 21): every
listing reply - the seven methods above plus the non-kernel
``admin.list_keys``, which accepts the same
``{subject_id?, offset?, limit?}`` window - wraps its rows in
``{<rows>, has_more, cursor}``: ``cursor`` is the adapter's
opaque encoding of the authoritative resume offset and
``has_more`` derives from it by ONE formula everywhere -
``has_more := (cursor's resume offset) < (total rows matching
the query)`` - so ``has_more`` is ``true`` exactly when rows
exist past the resume point and ``cursor`` is ``null`` when
``has_more`` is false. The formula covers BOTH end-of-list and
the 64 KiB byte-cap case uniformly: a genuinely short page
(``len(rows) < limit``) has resume offset == total and answers
``false``, while a byte-cap truncated page keeps ``cursor`` at
the FIRST DROPPED row and answers ``true`` whenever rows remain
past it - there is no second page-length rule. The kernel
surface keeps returning ``list[T]`` - the slice contract
(stable order, bounded window) already carries everything the
adapter needs - and the ``has_more``/``cursor`` pair is
resolved by the LOCKED TWO-CALL PROBE (round 35, iteration-3
audit item 19 - replaces the undocumented "count the total"
reading, which the bare-list kernel surface cannot support
race-free): the adapter evaluates ``has_more`` by issuing a
SECOND kernel list call ``(offset + len(rows), limit=1)``
UNDER THE SAME hold of the kernel's serialized mutation gate
the first call ran in - kernel mutations are serialized
through the single ordered writer, so two back-to-back list
reads inside one lock hold observe ONE consistent snapshot
and no insert/delete can slide the tail between page and
probe; ``has_more := len(probe_rows) > 0``, and ``cursor``
encodes ``offset + len(rows)`` (or the first-dropped-row
index on byte-cap truncation) verbatim. A probe issued
without the gate - two independent reads - can race a
concurrent insert/delete and lie about the tail, which is
exactly why the kernel exposes the deterministic pair rather
than a count.

Cursor-as-request-input (round 33; scope binding pinned round
34, iteration-2 audit item 21): the reply-side ``cursor``
round-32 defined is also accepted back on the next request -
the params of every listing call above MAY carry ``cursor``
INSTEAD of ``offset`` (mutually exclusive on one request - an
``offset``+``cursor`` pair is ``INVALID_ARGUMENT``), and a
malformed, expired or foreign cursor is ``INVALID_ARGUMENT``
(fail-closed; a bad cursor never silently rewinds to offset 0).
A cursor is BOUND to the listing context it was minted under -
the concrete ``(method, filter-tuple, ordering)`` triple - so
replaying it against a different method, a different
``subject_id?``/filter combination or a different sort order is
``INVALID_ARGUMENT`` with ``data.reason: "CURSOR_MISMATCH"``
(the typed sub-code, round 34): a cursor can never be replayed
across listing contexts and silently answer the wrong query.
The kernel surface is unchanged - the adapter translates its
cursor into the kernel's ``offset``/``limit`` window and the
kernel never sees the opaque token."""

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

MAX_HEARTBEAT_INTERVAL_MS: int = 86_400_000
"""Upper bound for a declared heartbeat interval (round 30,
SA_carbon_review_19 slice-C item 21): 24 h in milliseconds - the
value every ``heartbeat_interval_ms`` field and
:func:`heartbeat_interval_ms_from_sec` enforces. A declared interval
must also be positive and finite; sub-millisecond intervals clamp to
1 ms inside the converter."""

RING_DEDUP_MIN_GAP_SEC: float = 30.0
"""Best-effort ring-dedup horizon (round 31, SA_swe2_review_1 item
20): when a ring report carries NO ``provider_call_id`` the kernel
can only correlate by ``(line_id, caller_id)`` - two same-number
ring reports within this many seconds are treated as ONE physical
ring (dedup), while reports further apart are genuinely distinct
calls (a hangup-and-redial). With ``provider_call_id`` present the
dedup is exact instead: distinct handles are distinct calls no
matter the gap. The residual ambiguity is documented - a fast
same-number redial inside the window merges, an adapter that
always fills ``provider_call_id`` removes it."""

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
    entry point applies (round 32, SA_swe2_review_2 item 4: the
    bound is ABSOLUTE, so this clamp is the only ``now``-relative
    rule - a provider stamp is rejected only when naive or
    outside the absolute plausibility range, skew handling is
    exactly the ``+MAX_CLOCK_SKEW_SEC`` future normalization
    above)."""
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
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    # Deterministic scalar fallback (round 30, SA_carbon_review_19
    # slice-A item 14): an unanticipated non-str scalar (``bytes``,
    # ``Decimal``, ...) is rendered through ``str()`` so
    # ``json.dumps(to_log_dict())`` can never crash on it.
    return str(value)


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
    detail: _CheckedFreeText = Field(default=None, max_length=MAX_DETAIL_LEN)
    free_lines: _CheckedFreeLines = ()
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

E164_PATTERN: re.Pattern[str] = re.compile(r"^\+[1-9][0-9]{6,14}$")
"""Strict E.164 shape gate (api-contract.md "Dial validation"):
``+`` followed by a non-zero country-code digit and 7-15 digits
total. Local/national forms, missing ``+``, bare-``+`` hybrids,
spaces, punctuation and extension markers never pass - the check
runs BEFORE phonenumbers parsing so malformed input cannot borrow
a region. The digit class is ASCII ``[0-9]``, NOT ``\\d`` (round
33): Python's ``\\d`` matches Unicode digits (Arabic-Indic,
Devanagari, full-width), which would smuggle a non-E.164
codepoint past the shape gate into ``phonenumbers.parse`` -
E.164 is defined on ASCII digits only, so the pattern must be
too."""

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
36; vocabulary wording corrected in the PR #82 review round): this
phonenumbers release exposes no ``is_valid_region_code``
helper, so the known-region set is derived as the union of
``COUNTRY_CODE_TO_REGION_CODE`` values - the same derivation the
helper itself performs upstream. The accepted vocabulary is the
PHONENUMBERS-SUPPORTED uppercase alpha-2 region set - the ISO
3166-1-style regions PLUS phonenumbers' telephony extensions such
as ``"XK"`` (Kosovo), ``"AC"`` (Ascension) and ``"TA"`` (Tristan
da Cunha) - NOT strict ISO 3166-1 only. A syntactically-alpha-2
code like
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
    """Pure E.164 + alpha-2 region cross-check (round 24, review-13 M1;
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
    - ``country`` not uppercase alpha-2 in shape, or alpha-2 but not
      a KNOWN phonenumbers-supported region (e.g. ``"ZZ"``) ->
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
                detail="country is not uppercase alpha-2",
            ),
        )
    if country != _NON_GEO_REGION and country not in _ISO_REGION_CODES:
        raise PhonebridgeError(
            ErrorCode.INVALID_ARGUMENT,
            ErrorData(
                reason=ErrorReason.UNKNOWN_COUNTRY_CODE,
                detail="country is not a known phonenumbers-supported alpha-2 region",
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
    if len(set(caps)) != len(caps):
        raise ValueError("caps must not contain duplicate intents")
    return caps


class _FrozenMeta(dict[str, str]):
    """Immutable, serializable string map backing every bounded
    ``meta`` field (round 34, iteration-2 audit item 3 - replaces
    the round-32 ``MappingProxyType`` wrapper, which turned out
    non-serializable: ``json.dumps`` cannot encode a proxy,
    ``copy.deepcopy``/``pickle`` cannot reduce one, and Pydantic
    JSON-mode serialization produced nothing usable for the
    admin-listing mirror). A ``dict`` subclass keeps
    ``isinstance(x, dict)`` true, so ``json.dumps``,
    ``model_dump(mode="json")``, ``pickle``, ``deepcopy`` and
    :func:`_mask_for_log` all treat the stored map as an ordinary
    JSON object, while every mutator is blocked so the map stays
    read-only at runtime exactly like the proxy did.

    - ``__setitem__``/``__delitem__``/``clear``/``pop``/
      ``popitem``/``setdefault``/``update``/``|=`` raise
      ``TypeError`` - a mutation attempt fails loudly instead of
      silently diverging kernel state from the persisted/listing
      mirror. The block guards ordinary write paths; it is not a
      sandbox (a determined caller could still dispatch
      ``dict.__setitem__`` explicitly), so treat it as the
      fail-closed contract marker, not a security boundary.
    - ``copy()``/``__copy__``/``__deepcopy__`` return the instance
      itself - copying an immutable map is a no-op, which keeps
      ``deepcopy`` of a containing model cheap and safe.
    - ``__reduce__`` returns ``(type(self), (dict(self),))`` so
      ``pickle`` round-trips through a plain-dict payload with no
      surrogate objects."""

    @staticmethod
    def _immutable(*args: Any, **kwargs: Any) -> NoReturn:
        raise TypeError("_FrozenMeta is immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable
    __ior__ = _immutable

    def copy(self) -> _FrozenMeta:
        return self

    def __copy__(self) -> _FrozenMeta:
        return self

    def __deepcopy__(self, memo: dict[int, Any]) -> _FrozenMeta:
        return self

    def __reduce__(self) -> tuple[type[_FrozenMeta], tuple[dict[str, str]]]:
        return (type(self), (dict(self),))


def _bounded_meta(meta: Mapping[str, str]) -> Mapping[str, str]:
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
        # Round 31 (SA_swe2_review_1 item 31): control characters
        # (NUL, CR/LF, ANSI) and whitespace-only entries are
        # rejected - provider meta is free-form external text that
        # lands in admin listings and logs.
        _require_no_control_text(key, "meta key")
        _require_no_control_text(value, "meta value")
    # Round 28 (review-18 item 16): return a COPY, not the inbound
    # mapping - the model then owns its map outright, so an adapter
    # mutating the dict it distilled from the wire afterwards can
    # never corrupt the stored kernel fact. Hardened to a defensive
    # DEEP copy in round 30 (SA_carbon_review_19 slice-A item 14):
    # values are ``str`` (immutable) today, so the deep copy is cheap
    # insurance against any future widened value type. Round 34
    # (iteration-2 audit item 3): the copy is stored as
    # :class:`_FrozenMeta` - a ``dict`` subclass whose mutators are
    # blocked - so the STORED map stays read-only at runtime like
    # the round-32 ``MappingProxyType`` did, while remaining fully
    # serializable (``json.dumps``/``model_dump(mode="json")``),
    # ``deepcopy``- and ``pickle``-safe, and readable by
    # :func:`_mask_for_log`'s ``dict`` branch. ``dict(meta)``
    # materializes first so the deep copy always sees a plain
    # mapping.
    return _FrozenMeta(copy.deepcopy(dict(meta)))


# ---------------------------------------------------------------------------
# Fallback policy (design §5.3, ADR-01)
#
# Typed discriminated union on `policy`. The wire spellings
# "forward_via_line:<line_id>" / "record_and_telegram:<channel>" are
# single strings - splitting "<policy>:<param>" is the config adapter's
# job; the domain sees typed fields only. A compact spelling carries
# only its positional params - every remaining field MUST have a
# default so the compact form can never produce an unconstructable
# model (round-31 fix round, PR #78 warning 4); the dict/JSON form is
# the explicit-override path. No forward reference to LineAssignment.
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
    RedactedString, ``target_country`` the uppercase alpha-2
    phonenumbers-supported region cross-check assertion). Round 24 (review-13 M1) moves the check
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
    ``forward_via_line:<line_id>:<e164_number>:<country>``.

    Terminal-forward availability (round 33): a forward chain is
    only as servable as its TERMINAL leg - the target line's own
    ``fallback_policy`` (e.g. an ``answer_and_announce`` whose
    announcement asset the adapter validated at that line's
    ``configure_line`` time, or another ``forward_via_line`` hop)
    is what runs if the bridged leg dies, so availability is
    evaluated end-to-end at config time: a terminal policy whose
    referenced asset/target fails validation is rejected on the
    LINE THAT OWNS IT, never discovered at ring time.

    Second-leg execution (round 34, iteration-2 audit item 18):
    the worker first ANSWERS the source ring through its
    ``fallback_call_token``, then performs the second leg as an
    ordinary ``call.acquire`` + ``call.dial`` pair on the TARGET
    line (``system:fallback_worker`` keeps its documented
    LINE_EXCLUSIVE_NOT_OWNER bypass and every other rule -
    intent check, lease cap, queue semantics). The second leg
    therefore rides a bounded leg budget (~20 s - the caller is
    billable from the source-ring accept instant, so an unresolved
    second leg can never run unbounded): a BUSY or
    ``RINGING_LOCKED`` target gets exactly ONE
    ``call.acquire{enqueue:true}`` queue turn inside the budget -
    ONE bounded queued acquire attempt, never a retry loop - while
    an unservable/unreachable target (``PROVIDER_UNREACHABLE``,
    ``LINE_NOT_FOUND``, ``DIAL_REJECTED``) is terminal
    immediately. A queue refusal or a leg still unresolved at
    budget lapse is a FORWARD FAILURE the same way: the
    worker ends the source-side lease through
    ``call.release``/``call.hangup`` and the ANSWERED source
    call emits its ordinary lease teardown -
    ``event.call_state_changed{state:"disconnected",
    reason:"agent_hangup"}`` for the worker's source lease -
    NEVER ``no_pickup``/``missed_call`` (round 35, iteration-3
    audit item 14: those reasons describe an UNANSWERED ring,
    and the worker already answered this one - the caller hears
    the disconnect like a failed call transfer). No terminal
    announcement is owed on the forwarding hop either - the
    ``answer_and_announce`` machinery terminates a RING, and
    this hop carries no announcement asset by design; the
    fallback decision already committed this ring's fate, so a
    refused second leg can never resurrect a different policy.
    Announcement source: this
    model deliberately carries NO ``announcement_ref`` - while
    the second leg is in ``DIALING``/``ALERTING`` the caller
    hears the TARGET line's early media (network ringback or
    the network's own intercept announcement) bridged through
    the worker's two leases; a spoken "please hold" asset is
    the TARGET line's terminal-policy concern, never a field of
    the forwarding hop. On ``ACTIVE`` the worker bridges the
    two media legs and both leases live by the ordinary
    lease/epoch rules - a teardown of either leg ends the
    bridge and the surviving lease follows normal teardown
    semantics."""

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

        Uniform error shape (round 28, review-18 item 16; docstring
        corrected round 31, SA_swe2_review_1 item 38): errors inside
        model validators surface as ``ValueError`` (wrapped into
        ``ValidationError`` by pydantic), never as
        ``PhonebridgeError`` - error-code mapping is the ADAPTER's
        job. The earlier claim that the typed ``PhonebridgeError``
        stays reachable through ``__cause__`` was WRONG: the inner
        ``raise ... from exc`` chains only on the ``ValueError``
        itself, but the adapter-facing object is pydantic's outer
        ``ValidationError`` whose ``__cause__`` is ``None`` - the
        wrapped context ValueErrors are reachable via
        ``ValidationError.errors()``, not the exception chain. The
        typed sub-code therefore rides the MESSAGE tail
        (``... invalid: <CODE>:<REASON>`` - the ``ErrorCode`` value
        AND the ``ErrorData.reason`` member, round 34, iteration-2
        audit item 24: the earlier ``<CODE>``-only tail collapsed
        the four distinct dial-rejection branches into one generic
        code, so the wire-facing mapping could never recover the
        typed reason); an adapter needing it reads it off the
        message tail or re-derives it by calling
        :func:`validate_e164_country` itself - the generic mapping
        stays ``INVALID_ARGUMENT`` either way."""
        try:
            validate_e164_country(self.target_number, self.target_country)
        except PhonebridgeError as exc:
            reason = exc.data.reason.value if exc.data.reason is not None else "NONE"
            raise ValueError(
                f"forward_via_line dial target invalid: {exc.code.value}:{reason}"
            ) from exc
        return self


class RecordAndTelegramFallback(ContractModel):
    """Worker policy: answer, play the pre-record announcement,
    record, post to a Telegram channel.

    Two config spellings (round-31 fix round, PR #78 warning 4):

    - compact ``record_and_telegram:<channel>`` - carries ONLY the
      channel selector; ``announcement_ref`` and
      ``max_recording_sec`` fill from their defaults
      (``builtin:beep`` / 300 s), so the compact form always
      constructs a valid model and pre-round-31 persisted policy
      JSON lacking the new fields hydrates unchanged;
    - dict/JSON ``{"policy": "record_and_telegram", "channel": ...,
      "announcement_ref": ..., "max_recording_sec": ...}`` - the
      explicit-override form for a provisioned asset or a
      non-default recording bound."""

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
    announcement_ref: _CheckedAnnouncementRef = "builtin:beep"
    """Pre-record announcement selector, ALWAYS populated (round 31,
    SA_swe2_review_1 item 26; design "the caller is always told
    they are being recorded"): the WAV asset the worker plays
    BEFORE recording starts - the recording leg can never run
    unannounced. The default ``builtin:beep`` is the packaged beep
    asset (round-31 fix round, PR #78 warning 4): the compact
    ``record_and_telegram:<channel>`` spelling carries no ref, so
    the field fills from the default - a provisioned asset is
    selected through the dict/JSON form only. The widened
    :func:`_check_announcement_ref` grammar permits ``:``/``/``
    (namespaced asset ids) because this ref never rides the compact
    ``<policy>:<param>`` split - unlike
    :attr:`AnswerAndAnnounceFallback.announcement_ref`, which does
    and keeps the strict ``_CheckedId`` grammar."""
    max_recording_sec: float = Field(default=300.0, gt=0, le=86400.0)
    """Wall-clock bound on the recording itself (round 31,
    SA_swe2_review_1 item 26; 300 s default - also the value the
    compact ``record_and_telegram:<channel>`` spelling fills, PR
    #78 warning 4): the single authoritative recording-length
    bound - the design's earlier ambiguity
    (``max_call_duration_sec`` vs a dedicated 300 s knob) resolves
    to this policy-level field, while the line's optional
    ``max_call_duration_sec`` cap still bounds the WHOLE call
    independently (the stricter of the two wins at runtime)."""


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
    spelling: ``answer_and_announce:<announcement_ref>`` - the ref
    rides :data:`_CheckedAnnouncementRef` since round 32
    (SA_swe2_review_2 item 19 - it accepts ``builtin:*`` namespaced
    selectors like ``builtin:wav/ringback`` the strict
    ``_CheckedId`` grammar rejected): the adapter's compact
    ``<policy>:<param>`` split is FIRST-colon only, so a ``:``
    inside the ref stays inside the param; the ``..``/leading-``/``
    bans make the grammar traversal-safe, and WAV existence is
    validated ADAPTER-side at ``configure_line``, never here."""

    policy: Literal["answer_and_announce"] = "answer_and_announce"
    announcement_ref: _CheckedAnnouncementRef


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
    :class:`ProviderBinding` and break its ``gt=0`` construction.

    Guards (round 30, SA_carbon_review_19 slice-C item 21): the input
    MUST be a finite positive real number - ``inf``/``nan``,
    non-numeric and non-positive values are rejected with
    ``ValueError`` rather than silently converted, and the converted
    result is bounded by :data:`MAX_HEARTBEAT_INTERVAL_MS` (the same
    upper bound every ``heartbeat_interval_ms`` wire field
    enforces)."""
    if isinstance(interval_sec, bool) or not isinstance(interval_sec, (int, float)):
        raise ValueError("heartbeat interval must be a real number of seconds")
    if not math.isfinite(interval_sec) or interval_sec <= 0:
        raise ValueError("heartbeat interval must be finite and > 0 seconds")
    converted = int(interval_sec * 1000 + 0.5)
    if converted > MAX_HEARTBEAT_INTERVAL_MS:
        raise ValueError(f"heartbeat interval exceeds {MAX_HEARTBEAT_INTERVAL_MS} ms")
    return max(1, converted)


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
    heartbeat_degraded_after: int = Field(default=2, ge=1, le=3600)
    """Missed pings before the binding's health reports degraded -
    the first rung of the liveness escalation ladder, surfaced via
    ``event.line_health_changed`` (round 25, review-15 item 15).
    Bounded at 3600 (round 31, SA_swe2_review_1 item 17): the count
    feeds ``threshold * heartbeat_interval`` arithmetic inside
    ``tick`` - an unbounded knob could produce absurd elapsed-time
    products, and 3600 missed pings already covers a full hour of
    silence at the fastest legal cadence."""
    heartbeat_down_after: int = Field(default=3, ge=1, le=3600)
    """Missed pings before the binding is declared UNREACHABLE -
    MUST strictly exceed ``heartbeat_degraded_after`` (unreachable
    is the terminal escalation of degraded, not a parallel state;
    the teardown follows design §5.4). Same 3600 bound as the
    degraded rung (round 31, SA_swe2_review_1 item 17)."""
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
    hangup_hold_sec: float = Field(default=10.0, ge=1.0, le=120.0)
    """Bound on the ``TERMINATING`` hold (round 32, SA_swe2_review_2
    item 1): after :meth:`LineManager.hangup` parks an in-flight
    lease in ``TERMINATING`` and queues ``line.hangup``, the kernel
    waits at most this long for the provider's ``disconnected``
    confirmation - on lapse ``tick`` ends the lease with
    ``CallEndReason.NETWORK_ERROR`` as a KERNEL-side timeout fact
    (never a synthesized provider ``disconnected`` fact, which
    would fabricate a provider report), frees the line and drains
    the FIFO so a lost/wedged hangup can never hold the line BUSY
    forever. Tight 1..120 s bound (not the generic one-day
    ceiling): the hold is a modem-confirmation grace, not a
    session-length knob."""
    provider_command_ack_sec: float = Field(default=2.0, gt=0, le=60.0)
    """Bounded ``{accepted}`` handshake deadline for
    ``line.dial``/``line.answer`` (round 33): the adapter MUST
    report the provider's distilled reply
    (``{accepted, provider_call_id?, reason?}``) through
    :meth:`LineManager.ingest_command_ack` within this window
    after the command left the kernel - a timeout or transport
    error invokes :meth:`LineManager.compensate_dial`/
    :meth:`LineManager.compensate_answer` instead, and the kernel
    stamps the same deadline on the lease's
    :attr:`CallLease.pending_command` so ``tick`` enforces it as
    a backstop when no adapter report ever arrives. The bound is
    PER-LINE effective config (a slower transport profile can
    widen it), NOT the generic one-day ceiling: a command
    handshake that cannot answer inside 60 s is a dead link, not
    a knob."""

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
    profile: _CheckedProfile | None = Field(default=None, max_length=MAX_PROFILE_LEN)
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
        if len(set(caps)) != len(caps):
            raise ValueError("caps must not contain duplicate intents")
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
    """Global FIFO rank of the head ticket ELIGIBLE for this line
    (round 34, iteration-2 audit item 22 - resolves the
    pool-ticket gap): the count covers a ticket whose preferred
    ``line_id`` names this line AND every pool ticket
    (``line_id=None``) - a pool ticket competes for every
    eligible pool line, so it is queued for this line too.
    ``None`` means no eligible ticket waits."""
    profile: _CheckedProfile | None = Field(default=None, max_length=MAX_PROFILE_LEN)
    """Same profile resolution rule as :class:`LineSummary`
    (round 28, review-18 item 13): ``binding.profile`` while a live
    provider binding is attached, else ``assignment.profile`` - the
    admin listing shows what the stored label actually resolved to,
    bounded by :data:`MAX_PROFILE_LEN`."""
    provider_meta: Mapping[str, str] = Field(default_factory=dict, validate_default=True)
    """Live binding metadata mirror - the admin-scoped home of the
    field removed from :class:`LineSummary` (round 28, review-18
    item 13): filled with ``binding.meta`` while a live provider
    binding is attached, empty for UNBOUND lines. The
    ``_bounded_meta`` invariant holds, so a row can never carry
    more than the register ingress allowed. ``validate_default``
    (round 34) runs the default through the same
    :func:`_bounded_meta` wrap, so even an omitted mirror is a
    :class:`_FrozenMeta` - immutable AND JSON/pickle/deepcopy-safe."""
    orphan_deadline_at: AwareDatetime | None = None
    """The live binding's :attr:`ProviderBinding.orphan_deadline_at`
    mirrored verbatim on the admin row (round 34, iteration-2 audit
    item 7): an operator watching ``admin.get_line``/
    ``admin.list_lines`` sees the exact instant the orphan-grace
    cascade fires without opening the binding record. Channel-local
    like the field it mirrors - it is NEVER persisted on the
    ``lines`` row and a restored/hydrated binding re-derives its own
    fresh deadline instead of trusting a stored one. Model-coherent:
    non-``None`` IFF ``liveness is LivenessState.WS_DISCONNECTED`` -
    every other rung carries ``None`` (there is nothing to orphan,
    or the grace already resolved)."""
    wake_hooks: frozenset[str] = Field(default_factory=frozenset)
    """The line's stored :attr:`LineAssignment.wake_hooks` set
    mirrored verbatim (round 34, iteration-2 audit item 18): the
    admin surface stays exhaustive - an operator reads exactly the
    opaque hook ids the kernel will route this line's wake
    envelopes to, and the same bound/id rule as the assignment
    field applies so a row can never widen what the store
    held."""

    @field_validator("provider_meta")
    @classmethod
    def _provider_meta_bounds(cls, meta: Mapping[str, str]) -> Mapping[str, str]:
        """Same ``_bounded_meta`` invariant as the register ingress
        (round 28, review-18 item 13) - the admin listing mirror
        never exceeds what ``provider.register`` accepted."""
        return _bounded_meta(meta)

    @field_validator("wake_hooks")
    @classmethod
    def _wake_hooks_bounded(cls, hooks: frozenset[str]) -> frozenset[str]:
        """Same bound/id rule as :attr:`LineAssignment.wake_hooks`
        (round 34): the mirror can never exceed what the
        ``configure_line``/``provision_line`` ingress accepted."""
        if len(hooks) > MAX_WAKE_HOOKS_PER_LINE:
            raise ValueError(f"wake_hooks exceeds {MAX_WAKE_HOOKS_PER_LINE} entries")
        for hook in hooks:
            _check_id(hook)
        return hooks

    @field_validator("owner_agent_id")
    @classmethod
    def _owner_is_never_system(cls, owner: str | None) -> str | None:
        """The admin row mirrors durable ownership - same
        ``system:*`` ban as :class:`LineAssignment` (round 30,
        SA_carbon_review_19 slice-C item 13)."""
        return _owner_agent_is_never_system(owner)

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
        if len(set(caps)) != len(caps):
            raise ValueError("caps must not contain duplicate intents")
        return caps

    @model_validator(mode="after")
    def _caps_iff_bound(self) -> AdminLineView:
        """Same caps<->liveness pairing as :class:`LineSummary`
        (round 27, review-17 item 3), without the unbound->FREE
        corollary (item 9), plus the ``provider_meta`` twin
        (round 31, SA_swe2_review_1 item 35): an ``UNBOUND`` row has
        no provider attachment, so it can report neither caps nor
        provider meta - a non-empty map would leak provider state
        into a line that owns none. Round 34 adds the
        ``orphan_deadline_at`` rung coherence (iteration-2 audit
        item 7): the mirror is set exactly while the binding sits
        ``WS_DISCONNECTED`` - ``None`` everywhere else mirrors the
        same ``_binding_invariants`` rule the field itself obeys."""
        if (self.caps == ()) != (self.liveness is LivenessState.UNBOUND):
            raise ValueError("caps is non-empty iff liveness is not unbound")
        if self.liveness is LivenessState.UNBOUND and self.provider_meta:
            raise ValueError("provider_meta must be empty when liveness is unbound")
        if (self.orphan_deadline_at is not None) != (
            self.liveness is LivenessState.WS_DISCONNECTED
        ):
            raise ValueError("orphan_deadline_at is set iff liveness is ws_disconnected")
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
    profile: _CheckedProfile | None = Field(default=None, max_length=MAX_PROFILE_LEN)
    """Administrative line label from the stored config (round 27,
    review-17 items 58/80): provisioning-side metadata - NOT the
    provider-declared profile. ``LineSummary.profile`` resolves
    ``binding.profile`` while a live binding is attached and falls
    back to this stored label otherwise."""
    mode: AssignmentMode
    claimable: bool
    adapter_mac: _CheckedMacAddress = None
    """Bluetooth MAC of the line's local adapter (round 31,
    SA_swe2_review_1 item 10): the hardware selector the pairing
    window and the hci-agent bind the line to - persisted on the
    ``lines`` row, ``None`` until provisioned/configured."""
    paired_phone_mac: _CheckedMacAddress = None
    """Bluetooth MAC of the paired phone this line serves (round 31,
    SA_swe2_review_1 item 10): the ONLY device the pairing window's
    SSP auto-accept may confirm - an unset field means no phone is
    paired and the window confirms nothing. Unique across the
    configured line set when set (round 34, iteration-2 audit
    item 23): the pairing window correlates the SSP confirmation
    by MAC, so two lines naming the same ``paired_phone_mac``
    could each auto-confirm the other's pairing request - the
    same uniqueness rule :meth:`LineAdminKernel.configure_line`
    enforces for ``adapter_mac`` applies here (``None`` is
    exempt - absence is not an identity)."""
    transport_profile: TransportProfile = "loopback"
    """How the provider half reaches this kernel (round 31,
    SA_swe2_review_1 item 10): the wire's profile vocabulary
    (hci-agent.md tunnel profiles) carried on the ``lines`` row so
    the provider adapter picks the right listener/dialer."""
    line_home_country: _CheckedCountryAlpha2 = None
    """Uppercase alpha-2 telephony home region of the line's number
    range (round
    31, SA_swe2_review_1 item 10; display-only rule pinned round
    32, SA_swe2_review_2 - supersedes the earlier
    "default region for dial normalization" wording, round 34,
    iteration-2 audit item 23): a DISPLAY/normalization hint
    carried for operator tooling and caller-id rendering -
    ``call.dial`` ALWAYS requires the caller's own ``country``
    assertion and never falls back to this field, so it can
    never silently feed a wrong region into dial normalization.
    ``None`` until configured. Vocabulary (PR #82 review round): the
    phonenumbers-supported region set - ISO-style regions plus
    telephony extensions like ``"XK"``/``"AC"``/``"TA"`` - not
    strict ISO 3166-1 only."""
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
    review-13 H4; owner-only stamping corrected round 30,
    SA_carbon_review_19 slice-A item 4; operation list unified
    round 32, SA_swe2_review_2 item 26): refreshed to ``now`` ONLY
    by OWNER line operations - EVERY owner-attributed mutation on
    the line counts identically: acquire/dial/accept/hangup/
    answer/release/send_dtmf/reattach_media/claim, with no
    privileged subset (an owner that hangs a call up or reattaches
    media is exercising the line exactly like one that dials) -
    and by ``drop_session`` closing the owner's LAST open
    session - the moment the idle window starts. Provider-side
    facts (``provider.ping``/liveness, ``ingest_ring``,
    ``ingest_call_state``) and admin ``configure_line`` MUST NOT
    stamp it: provider traffic is not owner activity, and a config
    write must never reset the idle clock of a sleeping owner. A
    line whose ``owner_agent_id`` still holds at least one open
    session is NEVER idle - a secretary agent sitting 25 h on a
    live socket waiting for inbound is ACTIVE and must not trigger
    a false ``event.idle_alert``; the idle clock only runs against
    a session-less owner. Ownership-grant stamping (round 34,
    iteration-2 audit item 23): ``assign_line``/``approve_claim``
    minting the new ``assignment_id`` stamps ``last_seen_at`` to
    the grant instant - the idle window starts when the owner
    gains the line, and an assignment row must never carry a
    stamp older than the grant itself (otherwise a line assigned
    to an unconnected owner could fire ``idle_alert`` on the
    very next ``tick``)."""
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
    wake_hooks: frozenset[str] = Field(default_factory=frozenset)
    """Kernel-visible wake-hook capability of the line (round 34,
    iteration-2 audit item 2): the OPAQUE ids of the
    ``escalation_hooks`` entries the admin hook store resolves for
    this line's rings - hook target URIs, secrets and payload
    details stay outside the kernel entirely. The set is the
    configuration FACT :meth:`LineManager.ingest_ring` needs to
    stamp a :class:`WakeAudience` envelope on a ring whose live
    audience is empty: a SHARED_POOL ring with zero live
    ``receive_inbound`` sessions, or an EXCLUSIVE line whose owner
    holds no live session (round 34 extends the zero-member rule to
    the exclusive case - a sleeping owner is woken exactly like a
    sleeping pool, through the tokenless
    :class:`IncomingCallWakeEvent` projection). An EMPTY set means
    no hook can wake anyone for this line - the ring then emits no
    ``wake`` envelope and simply waits out its accept window.
    Bounded at :data:`MAX_WAKE_HOOKS_PER_LINE`, each member a
    checked id; hydrated alongside the ``lines`` row by
    ``hydrate_lines`` and mirrored verbatim on
    ``admin.list_assignments`` like every other durable field."""

    @field_validator("wake_hooks")
    @classmethod
    def _wake_hooks_bounded(cls, hooks: frozenset[str]) -> frozenset[str]:
        """Round 34 bound: each hook id obeys :func:`_check_id`
        (opaque hook identifiers are id-shaped like every other
        kernel handle) and the set is capped at
        :data:`MAX_WAKE_HOOKS_PER_LINE`."""
        if len(hooks) > MAX_WAKE_HOOKS_PER_LINE:
            raise ValueError(f"wake_hooks exceeds {MAX_WAKE_HOOKS_PER_LINE} entries")
        for hook_id in hooks:
            _check_id(hook_id)
        return hooks

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
        rejected just like an ``assign_line`` request. Shared check:
        :func:`_owner_agent_is_never_system` (round 30)."""
        return _owner_agent_is_never_system(owner)

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
    attached - UNBOUND is the listing-only absence of this record.

    Persistence contract (round 31, SA_swe2_review_1 item 22;
    cache semantics pinned round 32, SA_swe2_review_2 items
    14/15; watermark fields typed round 33): the
    ``provider_bindings`` row is a BEST-EFFORT CACHE
    of the live record, not a second source of truth - a
    write-through failure raises the ``config_stale`` condition
    plus an admin alert and NOTHING ELSE: no kernel compensation,
    no ``restore_line`` call (that hook is ``LineAssignment``-
    shaped only), because the provider's next successful
    ``provider.register``/``provider.reregister`` rewrites the row
    anyway. ``last_seen_at`` writes are DEBOUNCED through the
    writer task - at most one persisted stamp per ``>= 30 s``
    coalesce window, never one row write per ``provider.ping``;
    the kernel's in-memory copy still updates per fact (liveness
    math reads memory, never the row). The persisted row ALSO
    carries the TWO typed provider-stamp watermarks
    (``liveness_stamp_watermark`` - the highest applied
    ``checked_at``; ``call_stamp_watermark`` - the highest
    applied ``observed_at``) plus ``channel_seq``, so a restart
    cannot re-apply replayed facts below the recorded watermarks
    and ``channel_seq`` reseeds as ``max(stored) + 1`` across
    restarts - a hydrated generation is never re-issued, so a
    stale socket's facts can never pass the live-generation check
    against a restored binding. Durability split (round 33):
    watermarks and ``channel_seq`` are the durable half - they
    survive reregister AND restart verbatim at the line's
    binding scope - while ``orphan_deadline_at`` is
    channel-local: it exists only while the channel generation
    that armed it is live, is NEVER persisted, and is
    recomputed/superseded the moment a fresh binding binds the
    line (a superseded channel's deadline must never gate the
    successor's facts). A hydrated row loads DEAD by
    construction (``liveness: ws_disconnected`` + an
    already-expired ``orphan_deadline_at``; see
    ``LineAdminKernel.hydrate_lines``) and the hydrated-dead
    ``tick`` cascade SUPPRESSES the external escalation hooks a
    live-binding teardown would fire - the restart window is
    tagged by the hydration epoch, so webhooks never re-alert on
    provider deaths that predate the process."""

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
    profile: _CheckedProfile | None = Field(default=None, max_length=MAX_PROFILE_LEN)
    """Bounded when present (round 27, review-17 items 27/46): the
    same ``1..MAX_PROFILE_LEN`` rule as
    :attr:`ProviderRegistration.profile` - the binding carries the
    label the provider declared so the ``LineSummary`` mirror can
    never hold an unlistable value."""
    meta: Mapping[str, str] = Field(default_factory=dict, validate_default=True)
    """Opaque provider metadata carried over from the registration
    (round 27, review-17 item 27): ``register_provider`` MUST copy
    the incoming ``ProviderRegistration.meta`` verbatim so admin
    diagnostics can inspect it; the same ``_bounded_meta``
    invariant applies. Typed ``Mapping`` + stored as
    :class:`_FrozenMeta` at validation (round 32 pinned the
    read-only requirement; round 34, iteration-2 audit item 3
    swaps the non-serializable ``MappingProxyType`` wrapper for
    the dict subclass): the stored map is read-only at runtime -
    a mutation attempt is a ``TypeError``, not a silent
    divergence - while ``json.dumps``/``model_dump(mode="json")``/
    ``pickle``/``deepcopy``/``_mask_for_log`` treat it as a plain
    JSON object.

    Ownership rule (round 28, review-18 item 16): the binding holds
    the kernel's OWN copy - ``_bounded_meta`` returns a fresh map
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
    heartbeat_interval_ms: int = Field(default=5000, gt=0, le=MAX_HEARTBEAT_INTERVAL_MS)
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
    false ping timeout). Upper bound ``le=MAX_HEARTBEAT_INTERVAL_MS``
    (round 29, SA_quant_review_1 slice-A item 8; the named constant
    replaced the literal bound in round 30, SA_carbon_review_19
    slice-C item 21): the exact millisecond
    counterpart of ``heartbeat_interval_sec <= 86400`` on
    :class:`LineTimeouts`/``TimeoutConfig`` - a hydrated
    ``ProviderBinding`` bypassing
    :func:`heartbeat_interval_ms_from_sec` can no longer carry an
    interval the config model would have rejected."""
    heartbeat_degraded_after: int = Field(ge=1, le=3600)
    """Missed-ping threshold before the binding's health reports
    degraded - STAMPED at register/reregister from the line's
    effective ``LineTimeouts.heartbeat_degraded_after`` (round 25,
    review-15 item 32), exactly like ``heartbeat_interval_ms``: the
    value is frozen for the life of this binding and a
    ``configure_line`` patch never retro-applies mid-session. No
    default: the kernel MUST stamp the line's effective value
    explicitly. Same 3600 upper bound as the config knob (round 31,
    SA_swe2_review_1 item 17) - a hydrated binding can never carry
    a threshold the config model would reject."""
    heartbeat_down_after: int = Field(ge=1, le=3600)
    """Missed-ping threshold before the binding is declared
    UNREACHABLE - same stamping rule as
    ``heartbeat_degraded_after``; MUST strictly exceed it (the
    escalation ladder has no reachable rung above unreachable).
    Same 3600 upper bound (round 31, SA_swe2_review_1 item 17)."""
    connected_at: AwareDatetime
    last_seen_at: AwareDatetime
    orphan_deadline_at: AwareDatetime | None = None
    """Orphan-grace deadline (round 27, review-17 item 76;
    persistence corrected round 32, SA_swe2_review_2 items
    14/15): the field is a KERNEL-side deadline, NOT a durable
    ``provider_bindings`` column - the cache row persists only
    the best-effort bookkeeping a restart needs (identity,
    stamped thresholds, ``channel_seq``, ``last_seen_at``, stamp
    watermarks), and a deadline written before the process died
    would be meaningless anyway because the sockets it guarded
    died with the process. The deadline is therefore RE-DERIVED
    on hydrate: ``hydrate_lines`` loads every stored binding
    DEAD (``liveness: ws_disconnected``) and stamps an
    ALREADY-EXPIRED ``orphan_deadline_at`` so the first ``tick``
    runs the orphan cascade deterministically instead of relying
    on wall time. The live value is projected verbatim onto the
    admin-scoped binding listing (``ProviderBindingSummary``
    carries it, round 32 item 24) so an operator can see a
    binding inside its grace window - a ``null`` there means
    ``ws_disconnected`` is not the binding's rung."""
    channel_seq: int = Field(ge=1)
    """Generation of the control socket this binding last accepted a
    fact from (round 30, SA_carbon_review_19 slice-A item 10; the
    orphan-recovery comparison hardened round 34, iteration-2
    audit item 8): the
    adapter assigns every ACCEPTED provider control socket a
    monotonically increasing ``channel_seq`` and stamps it on
    ``ProviderRegistration``/``ProviderLiveness``; the binding records
    the highest generation seen. A ``ws_disconnected`` binding
    recovers ONLY through a fact stamped with a channel_seq
    STRICTLY NEWER than this recorded generation - which in
    practice only ``provider.reregister`` satisfies (the register
    mints a fresh binding on a fresh socket generation). An
    EQUAL-generation fact arriving after the socket drop is a
    delayed fact replayed off the dead socket: it is recorded but
    NEVER disarms ``orphan_deadline_at``, and an older-generation
    fact is stale outright - the generation that died can never
    resurrect itself.

    Ownership rule (round 33): the adapter's accepted-socket
    counter is the SINGLE authoritative generator - every
    ``provider.register``/``provider.reregister`` mints the next
    generation from it - and the persisted value is only a
    RESEED floor: hydration restores ``max(stored) + 1`` so a
    restart never reissues a generation a dead socket already
    used. The kernel never mints a ``channel_seq`` itself."""
    liveness_stamp_watermark: AwareDatetime | None = None
    """Durable per-line watermark of the highest ``checked_at``
    stamp applied from liveness/ping facts (round 33 - the typed
    field the round-31 "provider-stamp watermark" persistence
    paragraph named but never modeled). DURABLE: the row stores
    it verbatim, ``hydrate_lines`` restores it, and it survives a
    reregister at the line's binding scope - a stamp replayed by
    a NEW binding still dedupes/clamps against it
    (:meth:`LineManager.note_provider_liveness` "Dedup scope").
    ``None`` means no liveness fact has been applied yet."""
    call_stamp_watermark: AwareDatetime | None = None
    """Durable per-line RESTART REPLAY FLOOR of the highest
    ``observed_at`` stamp applied from call-state and ring facts
    (round 33; re-scoped round 34, iteration-2 audit item 11):
    same durability rule as :attr:`liveness_stamp_watermark`
    (persisted verbatim, survives reregister at binding scope)
    but its LIVE ordering role is narrower than a single
    line-wide maximum - the per-identity dedup/ordering of
    :meth:`LineManager.ingest_call_state` is scoped per CALL
    IDENTITY (see ``MAX_CALL_STAMP_WATERMARKS_PER_LINE``), so a
    stamp from call B below call A's mark is never dropped as
    stale. This scalar survives as the restart floor: a call-state
    fact naming NO live call identity (no ``lease_id``/
    ``call_token``/``provider_call_id`` the kernel holds) whose
    stamp sits at-or-below it is a probable pre-restart replay and
    is dropped - live identities keep their own per-identity
    watermark instead. ``None`` means no call-state/ring fact has
    been applied yet."""

    @field_validator("caps")
    @classmethod
    def _caps_are_telecom_only(cls, caps: tuple[Intent, ...]) -> tuple[Intent, ...]:
        return _provider_caps_check(caps)

    @field_validator("meta")
    @classmethod
    def _meta_bounds(cls, meta: Mapping[str, str]) -> Mapping[str, str]:
        """Same ``_bounded_meta`` invariant as the register ingress
        (round 27, review-17 item 27) - enforced on the stored
        binding so a carried-over map can never be unbounded."""
        return _bounded_meta(meta)

    @field_validator("liveness")
    @classmethod
    def _liveness_is_a_binding_state(cls, liveness: LivenessState) -> LivenessState:
        """``UNBOUND`` can never be stored on a binding (round 30,
        SA_carbon_review_19 slice-C item 4): the state is a derived
        :class:`LineSummary` listing value meaning "no binding
        attached" - a ProviderBinding existing at all proves a
        provider is attached."""
        if liveness is LivenessState.UNBOUND:
            raise ValueError("unbound is a derived listing state, never a binding liveness")
        return liveness

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
        binding carries no orphan-grace window. Round 30
        (SA_carbon_review_19 slice-C item 4) adds the converse: a
        ``ws_disconnected`` binding MUST carry its
        ``orphan_deadline_at`` - the state exists precisely to arm
        the 10 s grace, and a disconnected binding without the
        deadline could never be reaped nor recovered."""
        if self.heartbeat_down_after <= self.heartbeat_degraded_after:
            raise ValueError("heartbeat_down_after must exceed heartbeat_degraded_after")
        if (self.orphan_deadline_at is not None) != (
            self.liveness is LivenessState.WS_DISCONNECTED
        ):
            raise ValueError("orphan_deadline_at is set exactly when liveness is ws_disconnected")
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
    (round 20): a still-UNDIALED lease whose ``grant_expires_at``
    lapses is torn down as
    ``call_state_changed{reason: "grant_ttl_expired"}`` by
    ``tick()``. Phase coherence (model-enforced by
    ``_lease_invariants``, rounds 30/31 - SA_swe2_review_1 items
    2/37; tightened round 32, SA_swe2_review_2 item 21): the
    field is REQUIRED on ``ACQUIRED`` - every grant carries a
    TTL, an undialed lease without one would park the line
    forever - and MUST lie at-or-after ``created_at``; it is
    consumed the instant the grant is spent, so EVERY later phase
    - ``DIALING``/``ANSWERING``/``ALERTING``/``ACTIVE``/
    ``TERMINATING``/``ENDED`` - MUST carry ``None`` (the
    round-30 tolerance for a still-recorded deadline on
    ``DIALING``/``ALERTING`` is gone: a consumed deadline is not
    documentation, it is a stale field a reader could
    mis-enforce).

    Expired-grant error pin (round 34, iteration-2 audit item
    20; verb scope narrowed round 35, iteration-3 audit item
    20 - the wire vocabulary already reserves the pairing):
    once ``tick`` has ended the lease ``grant_ttl_expired`` an
    ACTION verb naming its ``lease_id`` - ``call.dial``,
    ``call.send_dtmf``, ``call.reattach_media`` - answers
    ``PhonebridgeError(ErrorCode.RESOURCE_EXHAUSTED,
    ErrorData(reason=ErrorReason.GRANT_TTL_EXPIRED))``: the
    grant's deadline ran out is an expiry fact, NOT a token
    staleness fact, so ``STALE_TOKEN`` is inadmissible here (the
    same split the wire error table pins: grant-TTL expiry is
    never token-class). CLEANUP verbs stay idempotent through
    the ``_closed_leases`` tombstone ring instead: a
    ``call.hangup``/``call.release{lease_id}`` naming ANY
    already-ended lease - grant-expired included - is an
    idempotent no-op (``{ok}``), same as a release that ran
    before the expiry. Ordering (round 35): ownership resolves
    BEFORE the expiry verdict - a LIVE foreign lease is
    ``OWNER_MISMATCH`` as always, and only once the lease is
    known ended does the tombstone path apply, so a foreign
    caller can never distinguish a dead foreign lease from a
    never-issued id (the same no-oracle pin
    :data:`MAX_CLOSED_TICKETS` documents for tickets)."""
    profile: _CheckedProfile | None = Field(default=None, max_length=MAX_PROFILE_LEN)
    """The ``acquire(profile=...)`` label the agent asked for, saved
    on the lease (round 28, review-18 item 13): the acquire contract
    says the chosen profile rides the grant to the dial command and
    the admin listing, so it MUST be recorded here rather than
    recomputed - ``admin.list_leases`` reports it directly off the
    stored field. On a QUEUED acquire the lease inherits the value
    verbatim from :attr:`QueueTicket.profile` (round-28 fix,
    PR #72 review) - the label survives queueing unchanged. Bounded
    like every profile carrier by :data:`MAX_PROFILE_LEN`."""
    provider_call_id: _CheckedProviderCallId = None
    """The provider's own call handle recorded on the lease (round
    33): the adapter-distilled identifier the provider echoed on the
    ``{accepted}`` reply to ``line.dial``/``line.answer`` - or the
    handle a ``provider.call_state``/``state_snapshot`` fact carried
    when the kernel bound that call to this lease. DURABLE while the
    lease lives: :meth:`LineManager.ingest_command_ack` stamps it
    once and the sole-live-lease correlation rule of
    :meth:`ingest_call_state` reads it as positive identity evidence
    (an unqualified update whose ``provider_call_id`` agrees with
    this field MAY bind; a disagreeing handle is conflicting
    evidence that blocks the bind). :attr:`DialResult.provider_call_id`
    is the agent-facing mirror of this stored value; ``None`` means
    the provider stack never returned a stable handle."""
    pending_command: CommandAckKind | None = None
    """The provider command whose bounded ``{accepted}`` handshake is
    still open (round 33): ``"dial"`` on a lease ``dial()`` just
    minted, ``"answer"`` on a lease ``accept()`` just minted,
    ``None`` once the kernel processed the reply (or the command
    never applied). The field IS the typed command-ack gate -
    :meth:`LineManager.ingest_command_ack` resolves the ack against
    it, :meth:`tick` enforces the stamped
    ``provider_command_ack_sec`` deadline when no report arrives
    (identical effects to :meth:`compensate_dial`/
    :meth:`compensate_answer`), and a snapshot cannot reconcile a
    lease away while its command's reply may still be in flight -
    see :data:`SNAPSHOT_RECONCILED_PHASES`. Model-coherent values:
    ``"dial"`` is legal while the lease sits ``DIALING``/
    ``ALERTING``/``ACTIVE``, ``"answer"`` while it sits
    ``ANSWERING``/``ACTIVE`` (round 34, iteration-2 audit item 1 -
    the handshake outlives a phase advance: a qualified provider
    fact MAY move ``DIALING -> ALERTING``/``ACTIVE`` or
    ``ANSWERING -> ACTIVE`` while the command reply is still in
    flight, and the marker MUST survive that advance so the late
    ack can still record ``provider_call_id`` on the dial path and
    clear the bounded handshake; the marker clears ONLY on the ack
    resolution itself or atomically with the lease's teardown -
    never because the call progressed)."""
    provider_command_ack_deadline_at: AwareDatetime | None = None
    """The kernel-clock instant ``tick`` enforces the pending
    command's lapse at (round 33): stamped at
    ``now + line.effective.provider_command_ack_sec`` when the
    lease mints with a ``pending_command``, cleared together with
    that marker the moment the handshake resolves
    (:meth:`LineManager.ingest_command_ack`,
    :meth:`compensate_dial`/:meth:`compensate_answer`, or the
    dead-binding teardown). KERNEL receipt time, never a provider
    stamp - the deadline arms the backstop that runs the identical
    terminal effect as the adapter timeout path, and it counts as
    one deadline against :data:`MAX_TICK_DEADLINES`. Legal ONLY
    while ``pending_command`` is armed: the pair is set and
    cleared atomically, so a lease can never carry a deadline
    with nothing pending nor a pending command with no
    deadline. Teardown atomicity (round 33, PR #82 review; phase-
    advance carve-out round 34, iteration-2 audit item 1):
    whenever a lease is TORN DOWN through ANY path - agent
    hangup/release/drop_session, provider/binding
    death, adapter compensation, or a ``tick`` deadline - the pair
    clears WITH the teardown, and a command acknowledgement
    arriving afterwards is stale (dedupe/no-op through
    ``_closed_leases``): it can never resurrect or mutate the
    torn-down lease. A forward PHASE ADVANCE is NOT a teardown: a
    qualified provider fact moving ``DIALING -> ALERTING``/
    ``ACTIVE`` or ``ANSWERING -> ACTIVE`` leaves the pair armed so
    the still-in-flight reply keeps its resolution path."""
    agent_media_epoch: int = Field(default=0, ge=0)
    """Durable AGENT-role media stream epoch (round 33; per-role
    split round 35, iteration-3 audit item 1): the epoch of the
    CURRENT agent-role half of this lease's media credential pair.
    ``0`` means no pair was ever allocated (a still-``ACQUIRED``
    grant); the first ``MediaAllocator.allocate`` stamps ``1`` and
    every subsequent AGENT-role reissue - ``call.reattach_media``
    only - increments it by one. The epoch is PER-ROLE on purpose:
    a provider-side reissue (``provider.reattach_media`` or the
    reregister re-pair) bumps ONLY
    :attr:`provider_media_epoch`, so a live agent media socket
    authenticated under the current agent epoch survives a
    provider-role re-pair untouched - the single lease-wide epoch
    of round 34 could never express that (reissuing one role
    would have burned the other role's socket)."""
    provider_media_epoch: int = Field(default=0, ge=0)
    """Durable PROVIDER-role media stream epoch - the mirror of
    :attr:`agent_media_epoch` (round 35, iteration-3 audit item
    1): stamped ``1`` by the first ``MediaAllocator.allocate``
    and incremented by every provider-role reissue
    (``provider.reattach_media``, the ``provider.reregister``
    re-pair). The pair-epoch pair ``(agent_media_epoch,
    provider_media_epoch)`` scopes the media-frame watermarks:
    the pacer's PCM watermark and the ``pre_dtmf_consumed`` DTMF
    cursor ride the PROVIDER epoch (they are provider-side
    stream artifacts), while the agent uplink ``0x02`` flush
    watermark rides the AGENT epoch - a role's reissue drops
    only stamps minted under THAT role's outgoing epoch, never
    the sibling's."""

    @model_validator(mode="after")
    def _lease_invariants(self) -> CallLease:
        """Round 25 (review-15 item 41): a lease is an EPHEMERAL
        dial/receive hold - ``Intent.EXCLUSIVE_LINE`` can never ride
        it (durable exclusivity lives on ``LineAssignment`` only,
        granted via claim+approval or admin assign; a lease holding
        it would smuggle a durable claim into a socket-bound
        record). And ``answered_at`` is an activity stamp - it can
        never precede the lease's own ``created_at``.

        Round 33 adds the command-ack and media-epoch coherence
        rules (phase window widened round 34, iteration-2 audit
        item 1): ``pending_command`` names the ``line.dial``/
        ``line.answer`` handshake still open on the lease, so it is
        legal ONLY while a phase the command could still be driving
        is live - ``"dial"`` on ``DIALING``/``ALERTING``/``ACTIVE``,
        ``"answer"`` on ``ANSWERING``/``ACTIVE``, ``None`` everywhere
        else (a settled lease simply carries ``None``, and a
        ``TERMINATING``/``ENDED`` lease can never carry a marker
        because teardown clears it atomically); and BOTH media
        epochs are ``0`` exactly while the lease sits ``ACQUIRED``
        (no credential pair allocated yet) and ``>= 1`` once any
        call-bearing phase is reached (round 35: the pair is
        allocated as a unit, so both role epochs start at ``1``
        together and then advance independently per role reissue)
        - an ``ENDED`` lease keeps whatever epoch pair it died
        with."""
        if self.intent is Intent.EXCLUSIVE_LINE:
            raise ValueError("a CallLease can never hold exclusive_line intent")
        if self.answered_at is not None and self.answered_at < self.created_at:
            raise ValueError("answered_at must not precede created_at")
        if self.phase is CallPhase.RINGING:
            raise ValueError("CallPhase.RINGING is reserved - never a lease phase")
        if self.phase in (
            CallPhase.ACQUIRED,
            CallPhase.DIALING,
            CallPhase.ANSWERING,
            CallPhase.ALERTING,
        ) and (self.answered_at is not None or self.answer_confidence == "confirmed"):
            raise ValueError(
                "answered_at/'confirmed' answer_confidence require an established call"
            )
        if self.phase is not CallPhase.ACQUIRED and self.grant_expires_at is not None:
            # Round 32 (SA_swe2_review_2 item 21): the deadline
            # field exists ONLY while the grant is unspent - a
            # post-ACQUIRED phase carrying it would document a
            # consumed deadline a reader could mis-enforce.
            raise ValueError("grant_expires_at is consumed once the grant was spent")
        if self.phase is CallPhase.ACQUIRED and self.grant_expires_at is None:
            raise ValueError("an ACQUIRED lease must carry grant_expires_at")
        if self.grant_expires_at is not None and self.grant_expires_at < self.created_at:
            raise ValueError("grant_expires_at must not precede created_at")
        # Round 33/34: the command-ack marker is phase-coherent - a
        # pending "dial" rides every phase the in-flight handshake
        # can legitimately reach while the provider's reply is still
        # out (DIALING -> ALERTING/ACTIVE forward advances keep it),
        # a pending "answer" rides ANSWERING -> ACTIVE; every other
        # phase (ACQUIRED, TERMINATING, ENDED - teardown clears the
        # marker atomically - and a settled lease) carries None.
        if self.pending_command == "dial" and self.phase not in (
            CallPhase.DIALING,
            CallPhase.ALERTING,
            CallPhase.ACTIVE,
        ):
            raise ValueError("pending_command 'dial' requires phase DIALING/ALERTING/ACTIVE")
        if self.pending_command == "answer" and self.phase not in (
            CallPhase.ANSWERING,
            CallPhase.ACTIVE,
        ):
            raise ValueError("pending_command 'answer' requires phase ANSWERING/ACTIVE")
        # Round 33: the ack deadline and the pending marker are one
        # atomic pair - a deadline with no pending command would be a
        # phantom tick entry, a pending command with no deadline
        # could wedge forever past the backstop.
        if (self.pending_command is None) != (self.provider_command_ack_deadline_at is None):
            raise ValueError(
                "pending_command and provider_command_ack_deadline_at are set/cleared together"
            )
        if (
            self.provider_command_ack_deadline_at is not None
            and self.provider_command_ack_deadline_at < self.created_at
        ):
            raise ValueError("provider_command_ack_deadline_at must not precede created_at")
        # Round 33: no call leg exists before a dial was dispatched,
        # so an ACQUIRED lease carries neither a provider handle nor
        # a media epoch; any call-bearing phase owns a pair.
        if self.phase is CallPhase.ACQUIRED and self.provider_call_id is not None:
            raise ValueError("an ACQUIRED lease carries no provider_call_id")
        if self.phase is CallPhase.ACQUIRED and (
            self.agent_media_epoch != 0 or self.provider_media_epoch != 0
        ):
            raise ValueError("an ACQUIRED lease has media epochs 0")
        if self.phase in (
            CallPhase.DIALING,
            CallPhase.ANSWERING,
            CallPhase.ALERTING,
            CallPhase.ACTIVE,
            CallPhase.TERMINATING,
        ) and (self.agent_media_epoch < 1 or self.provider_media_epoch < 1):
            raise ValueError("a call-bearing phase requires media epochs >= 1")
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

    Re-check triggers (round 32, SA_swe2_review_2 item 16 - the
    enumeration the round-31 "re-check every free line" rule
    implies): the pending set is re-evaluated at EVERY mutation
    that can change eligibility, not only inside ``tick`` - a
    lease ENDED/line freed (hangup, provider ``disconnected``,
    ``hangup_hold_sec`` lapse, dead-man teardown, orphan
    cascade), a fresh ``provider.register`` binding a previously
    unbound line, a ``provider.capability_update`` widening or
    narrowing caps, a claim resolution or ``release_assignment``
    returning a line to the pool, a new ticket enqueue (the new
    ticket itself may be
    immediately servable), and every ``tick`` deadline expiry.
    A grant is issued only from a re-check that ran AFTER the
    triggering mutation committed, so a ticket can never observe
    a line the same transaction already allocated elsewhere.
    """

    ticket_id: _CheckedTicketId
    seq: int = Field(ge=0)
    session_id: _CheckedSessionId
    intent: Intent
    preferred_line_id: _CheckedLineId | None = None
    enqueued_at: AwareDatetime
    idempotency_key: _CheckedIdempotencyKey = Field(
        default=None, min_length=1, max_length=MAX_IDEMPOTENCY_KEY_LEN
    )
    """Bounded when present (round 23, review-12 M2; text gate
    round 32, SA_swe2_review_2 item 20): a key is either
    absent (no dedup - every call is independent) or a non-empty
    string of at most ``MAX_IDEMPOTENCY_KEY_LEN`` chars free of
    control bytes/surrogates and never whitespace-only; an empty,
    overlong or exotic key is rejected as INVALID_ARGUMENT at the
    boundary."""
    profile: _CheckedProfile | None = Field(default=None, max_length=MAX_PROFILE_LEN)
    """The ``acquire(profile=...)`` label recorded on the ticket
    (round-28 fix, PR #72 review - audit item 20 named BOTH
    :class:`CallLease` and QueueTicket, and the queued path was the
    gap): the per-call label MUST survive queueing, so the kernel
    saves the accepted value here and the :class:`CallLease` minted
    when the ticket is granted INHERITS it verbatim. Same
    ``1..MAX_PROFILE_LEN`` bound as :attr:`CallLease.profile` -
    ``None`` means the acquire carried no label."""

    @field_validator("intent")
    @classmethod
    def _intent_is_dial_out(cls, intent: Intent) -> Intent:
        """``call.acquire`` serves ONLY ``dial_out`` (round 32,
        SA_swe2_review_2 item 16 - the pin the api-contract "Queue
        semantics" section already documented but the model never
        enforced): inbound rings are consumed by
        ``call.accept(call_token)`` without a grant, durable
        exclusivity rides ``call.claim_exclusive``, so a ticket
        carrying any other intent is a defect row that could only
        park forever - reject it at construction instead."""
        if intent is not Intent.DIAL_OUT:
            raise ValueError("queue tickets exist for dial_out acquires only")
        return intent

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
    free_lines: _CheckedFreeLines = ()
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
    provider_call_id: _CheckedProviderCallId = None
    """The provider's own call handle for the dialed leg (round 32,
    SA_swe2_review_2 item 18; handshake-locked round 33): the
    agent-facing mirror of :attr:`CallLease.provider_call_id` -
    the handle the provider echoed on the ``{accepted}`` reply to
    ``line.dial``, recorded through
    :meth:`LineManager.ingest_command_ack` and read back as the
    lease's external-handle evidence (:meth:`ingest_call_state`
    correlates unqualified updates against it). Because the wire
    ``call.dial`` answer waits for the bounded command handshake
    (round 33), a result that reaches the agent carries the
    FINAL recorded value - ``None`` means the provider stack
    returned no stable handle on an accepted dial."""
    media_endpoint: _CheckedMediaEndpoint = Field(max_length=MAX_ENDPOINT_LEN)
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
    media_endpoint: _CheckedMediaEndpoint = Field(max_length=MAX_ENDPOINT_LEN)
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
    media_endpoint: _CheckedMediaEndpoint = Field(max_length=MAX_ENDPOINT_LEN)
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

    media_endpoint: _CheckedMediaEndpoint = Field(max_length=MAX_ENDPOINT_LEN)
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

    @field_validator("requester_agent_id")
    @classmethod
    def _requester_is_never_system(cls, requester: str) -> str:
        """A claim is filed by an external agent (round 30,
        SA_carbon_review_19 slice-C item 13): ``system:*`` service
        identities never request durable exclusivity - the fallback
        worker executes admin-configured policy, it does not file
        claims."""
        return cast(str, _owner_agent_is_never_system(requester))


class ClaimResult(ContractModel):
    """call.claim_exclusive result: claim is a REQUEST - moderator
    approval in the admin surface grants it; never self-granted (§4.2)."""

    claim_id: _CheckedClaimId
    status: Literal["pending_approval"] = "pending_approval"


class PendingRing(ContractModel):
    """A ring discovered via ``state_snapshot`` during
    ``provider.reregister`` and minted through the ``ingest_ring``
    path (round 30, SA_carbon_review_19 slice-A item 11): a snapshot
    ``ProviderActiveCall`` element carrying ``state ==
    ProviderCallState.RINGING`` is processed through the ring path
    like any inbound ring - the kernel mints its ``call_token`` and
    reports it back inside :class:`ProviderRegisterResult` so the
    adapter can answer ``provider.call_state{state:"ringing"}``
    style correlation for the rediscovered ring."""

    call_token: _CheckedCallToken
    caller_id: _CheckedCallerNumber | None = None
    provider_call_id: _CheckedProviderCallId = None
    """The echoed provider handle of the snapshot row the ring was
    minted from (round 32, SA_swe2_review_2 item 18): the adapter
    relays it back so the provider can bind its own call object to
    the kernel-minted ``call_token`` - same exact-identity role as
    :attr:`IncomingRingIngress.provider_call_id`, now available on
    the rediscovery direction too."""
    observed_at: AwareDatetime


class ProviderRegisterResult(ContractModel):
    """``register_provider`` return value (round 30,
    SA_carbon_review_19 slice-A item 11): the created/superseded
    :class:`ProviderBinding` plus the rings its ``state_snapshot``
    caused to be minted through the ``ingest_ring`` path. The
    adapter relays ``pending_rings`` back to the provider so each
    rediscovered ring keeps one kernel-minted ``call_token`` -
    deduplicated by ring identity, never re-minted on a repeat
    register. The same tuple shape is reused on the ``provider.ping``
    reply path (round 34, iteration-2 audit item 12) to deliver the
    tokens of ``pending_ring`` entries promoted after a drain -
    see :meth:`LineManager.note_provider_liveness`. Snapshot
    ``ringing`` elements inside a ``provider.ping{state_snapshot}``
    (round 34, iteration-2 audit item 24 - the ping-side token
    return) mint through the same ``ingest_ring`` path and their
    ``call_token``/``provider_call_id`` echo rides the SAME
    ``pending_rings`` reply field of that ping - there is no second
    token channel, so a snapshot ring can never be minted silently
    without a delivery path back to the provider."""

    binding: ProviderBinding
    pending_rings: tuple[PendingRing, ...] = ()
    auto_created_assignment: LineAssignment | None = None
    """The ``LineAssignment`` record this register mutation
    AUTO-CREATED for a previously unconfigured ``line_id``
    (round 35, iteration-3 audit item 4), ``None`` when the
    register landed on an already-configured line. The field
    exists for the register-scoped rollback path: when the
    adapter's write-through of the new provider binding fails
    AFTER :meth:`LineManager.register_provider` committed, it
    compensates with :meth:`LineManager.restore_line`
    ``(line_id, None)`` - which removes the auto-created record
    AND tombstones the binding minted in the same mutation in
    one atomic rollback (the normal live-binding quiescence
    guard would otherwise block the compensation forever -
    see ``restore_line``). When ``None``, no register-scoped
    rollback privilege exists and the ordinary removal rules
    apply."""


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
    media_endpoint: _CheckedMediaEndpoint = Field(max_length=MAX_ENDPOINT_LEN)
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
    media_endpoint: _CheckedMediaEndpoint = Field(max_length=MAX_ENDPOINT_LEN)
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
    reason: _CheckedFreeText = Field(default=None, max_length=MAX_REASON_LEN)
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
    media_endpoint: _CheckedMediaEndpoint = Field(max_length=MAX_ENDPOINT_LEN)
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


class ProviderCommandAck(ContractModel):
    """Typed provider-command acknowledgement FACT (round 33): the
    adapter-distilled ``{accepted, provider_call_id?, reason?}``
    reply to a down-channel ``line.dial`` (``{accepted, reason?}``
    on ``line.answer`` - the answer reply carries no
    provider-call-handle field, PR #82 review), reported
    to the kernel through :meth:`LineManager.ingest_command_ack`.

    The acknowledgement is a first-class kernel fact, not a
    transport result: until it arrives (or its bounded
    :attr:`LineTimeouts.provider_command_ack_sec` deadline lapses)
    the lease carries :attr:`CallLease.pending_command`, is excluded
    from snapshot reconciliation, and the pending ``call.dial``/
    ``call.accept`` wire reply stays open. ``command`` names which
    handshake the ack resolves (:data:`CommandAckKind`);
    ``lease_id`` the lease ``dial()``/``accept()`` minted;
    ``call_token`` echoes the ring token ONLY on the ``answer``
    path (the kernel needs it for the ring-scoped reject
    :meth:`compensate_answer` semantics prescribe on a refused
    answer); ``provider_call_id`` is the provider's own call handle
    echoed on success - :meth:`ingest_command_ack` records it as
    the lease's durable external-handle evidence
    (:attr:`CallLease.provider_call_id`) - and is legal ONLY on an
    ACCEPTED ``command:"dial"`` acknowledgement (round 33, PR #82
    review): ``line.answer`` carries no provider-call-handle field
    at all because ring correlation stays ``call_token``/
    ``line_id``, so an answer ack - accepted OR refused - MUST NOT
    carry it. ``reason`` is bounded
    free text the provider attached to a refusal - never trusted
    as anything but bounded diagnostic input (rule 10: it may
    carry provider-controlled text, so it is never logged raw
    beyond its bound).

    A timeout or transport error is NOT reported through this
    model - there is no reply to distill - the adapter invokes
    :meth:`compensate_dial`/:meth:`compensate_answer` directly
    instead, and the kernel-side deadline stamped at command time
    is the backstop for an adapter that never calls either."""

    line_id: _CheckedLineId
    assignment_id: _CheckedAssignmentId
    """Same binding-id discipline as :attr:`ProviderLiveness.assignment_id`:
    the ack rides the binding's own control channel, so a stale or
    superseded id resolves through the ``_closed_assignments`` ring
    as ``STALE_TOKEN``."""
    command: CommandAckKind
    lease_id: _CheckedLeaseId
    call_token: _CheckedCallToken | None = None
    accepted: bool
    provider_call_id: _CheckedProviderCallId = None
    reason: _CheckedFreeText = Field(default=None, max_length=MAX_REASON_LEN)
    """Bounded refusal text (round 33): same ``1..MAX_REASON_LEN``
    hygiene as :attr:`ProviderRevokeCommand.reason` - provider-
    controlled diagnostic input, never echoed into agent-facing
    ``detail`` fields verbatim."""

    @model_validator(mode="after")
    def _ack_shape(self) -> ProviderCommandAck:
        """Reply-shape coherence (round 33; answer-ack handle
        restriction added in the PR #82 review round): ``call_token``
        echoes the ring token ONLY on the ``answer`` handshake - a
        dial has no ring to correlate; ``provider_call_id`` is
        legal ONLY on an ACCEPTED ``"dial"`` ack - the provider's
        call handle exists only where a dial could have minted a
        call object, so EVERY ``"answer"`` ack (accepted or
        refused) forbids it, and a refused command of either kind
        mints no handle;
        and ``reason`` is the refusal explanation - an accepted
        reply carries none."""
        if self.command == "dial" and self.call_token is not None:
            raise ValueError("call_token is ring-scoped - never set on a dial ack")
        if self.command == "answer" and self.call_token is None:
            raise ValueError("an answer ack must echo the ring's call_token")
        if self.command == "answer" and self.provider_call_id is not None:
            raise ValueError(
                "provider_call_id is dial-only - ring correlation stays call_token/line_id"
            )
        if self.accepted:
            if self.reason is not None:
                raise ValueError("reason carries the refusal detail - never set on accepted")
        elif self.provider_call_id is not None:
            raise ValueError("a refused command mints no provider_call_id")
        return self


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
    each token to ``(lease_id, expected_role, role_epoch)``
    (round 34, iteration-2 audit item 17; PER-ROLE epoch round
    35, iteration-3 audit item 1 - the epoch joins the binding
    tuple so the ``(lease_id, role, epoch)`` watermark scoping of
    :attr:`CallLease.agent_media_epoch`/
    :attr:`CallLease.provider_media_epoch` is actually
    implementable: a socket authenticated under a superseded
    epoch OF ITS OWN ROLE is rejected at ``media.auth`` and any
    frame or pacer/DTMF stamp minted under it is dropped as
    stale, while the sibling role's socket - bound to the
    sibling epoch that a single-role ``reissue`` never moved -
    stays valid). The
    binary PCM/DTMF frames themselves need NO epoch field - the
    token binding already fixes the socket's epoch for its whole
    lifetime, so frame scoping follows the socket, never a
    client-supplied header. A compromised
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

    agent_media_endpoint: _CheckedMediaEndpoint = Field(max_length=MAX_ENDPOINT_LEN)
    provider_media_endpoint: _CheckedMediaEndpoint = Field(max_length=MAX_ENDPOINT_LEN)
    agent_media_token: _CheckedMediaToken
    provider_media_token: _CheckedMediaToken
    agent_media_epoch: int = Field(ge=1)
    """AGENT-role epoch this pair view was minted under (round 35,
    iteration-3 audit item 1 - replaces the single lease-wide
    ``media_epoch`` of round 34): ``1`` for the first pair,
    incremented ONLY by agent-role reissues
    (``call.reattach_media``). The MediaRouter records it in the
    agent token's binding so the ``(lease_id, "agent",
    agent_media_epoch)`` scoping of the uplink ``0x02`` flush
    watermark can be enforced on the wire-facing side too: a
    credential minted under an older agent epoch can never
    authenticate after the lease's agent epoch moved on - while
    it stays valid across any provider-role reissue, whose epoch
    advance it does not share."""
    provider_media_epoch: int = Field(ge=1)
    """PROVIDER-role epoch this pair view was minted under - the
    mirror of :attr:`agent_media_epoch` (round 35): ``1`` for the
    first pair, incremented ONLY by provider-role reissues
    (``provider.reattach_media``, the ``provider.reregister``
    re-pair). It scopes the provider-side stream artifacts - the
    pacer's PCM watermark and the ``pre_dtmf_consumed`` DTMF
    cursor - to ``(lease_id, "provider", provider_media_epoch)``,
    so a provider re-pair can never splice old-epoch audio or
    resurrect a consumed DTMF watermark while the agent's socket
    keeps streaming under its untouched epoch. Both epoch fields
    travel inside the credential object only - the wire
    ``media.auth`` frame stays ``{token, role}`` because the
    router already knows the epoch from the token binding, and a
    client-asserted epoch would add nothing but a spoofable
    field."""

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
    ``incoming_call`` fans out to every live
    ``Intent.RECEIVE_INBOUND`` session (round 22, review-10 HIGH-4);
    since round 32 (SA_swe2_review_2 item 2) that fan-out rides ONE
    :class:`FanoutAudience` envelope carrying the member list, so
    per-session ``SessionAudience`` envelopes remain for genuinely
    session-scoped facts and the EventBus stays a stateless router
    with no session-intent table."""

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


_FanoutMember = Annotated[
    AgentAudience | SessionAudience,
    Field(discriminator="scope"),
]
"""Recipient inside a :class:`FanoutAudience` member list - only
concrete ``agent``/``session`` recipients may ride a fanout
(``broadcast``/``admin``/nested ``fanout`` are structurally
excluded)."""


class FanoutAudience(ContractModel):
    """Bounded multi-recipient routing target (round 32,
    SA_swe2_review_2 item 2): ONE envelope carries the whole
    recipient list for a fan-out fact - a shared-pool
    ``incoming_call``/``missed_call`` reaching every live
    ``Intent.RECEIVE_INBOUND`` session emits a single
    ``FanoutAudience`` envelope, so a line-wide wave costs
    O(lines) outbox envelopes instead of O(sessions). The EventBus
    expands the envelope once per member at publish time and stays
    a stateless router - it never re-enters kernel tables after
    the fact, so the member list IS the stamped routing truth
    (same rule as :data:`EventAudience` overall). ``members`` is
    bounded at :data:`MAX_SESSIONS_TOTAL` - the widest legal
    fan-out is "every live session"."""

    scope: Literal["fanout"] = "fanout"
    members: tuple[_FanoutMember, ...] = Field(min_length=1, max_length=MAX_SESSIONS_TOTAL)

    @field_validator("members")
    @classmethod
    def _members_are_unique(cls, members: tuple[_FanoutMember, ...]) -> tuple[_FanoutMember, ...]:
        """Round 34 (iteration-2 audit item 24): one recipient may
        ride the member list exactly once - a duplicate member
        would make the EventBus deliver the same envelope twice to
        one subscriber and double-count the
        :data:`MAX_SESSIONS_TOTAL` fanout bound."""
        keys = [
            (m.scope, m.agent_id if isinstance(m, AgentAudience) else m.session_id) for m in members
        ]
        if len(set(keys)) != len(keys):
            raise ValueError("fanout members must not contain duplicate recipients")
        return members


class WakeAudience(ContractModel):
    """Zero-live-socket-member audience (round 33): a shared-pool
    ``incoming_call``/``missed_call`` whose fan-out resolved to NO
    live ``Intent.RECEIVE_INBOUND`` session while configured wake
    hooks still exist to fire. The audience carries no member list
    - socket fan-out is empty BY CONSTRUCTION - and only the
    ``escalation_hooks``/wake-hook delivery leg consumes the
    envelope - on ``incoming_call`` the payload is the tokenless
    :class:`IncomingCallWakeEvent` projection (round 33, PR #82
    review), so a wake delivery STRUCTURALLY cannot carry the raw
    ``call_token``, which is meaningless to a non-session
    recipient and is a one-shot credential; on ``missed_call`` it
    carries :class:`MissedCallWakeEvent` (round 35, iteration-3
    audit item 7) - tokenless AND masked-caller, because the
    authenticated :class:`MissedCallEvent` would JSON-serialize
    the RAW caller number onto the unauthenticated hook spool.
    The kernel stamps it
    exactly when
    a ring arrives on a line with zero live receive-intent sessions
    AND at least one configured wake hook: a pool line with no
    subscribers and no hooks emits no ``incoming_call`` envelope at
    all (there is nothing to deliver), while
    :class:`FanoutAudience` keeps the ``members`` ``min_length=1``
    invariant - a member list can never be empty, and a fake
    member row must never be invented to satisfy it. Legal ONLY for
    ``incoming_call``/``missed_call`` - the two ring-lifecycle
    events a wake hook exists for - enforced by
    :data:`EVENT_AUDIENCE_SCOPES` like every other
    (event, scope) pair."""

    scope: Literal["wake"] = "wake"


EventAudience = Annotated[
    BroadcastAudience
    | AgentAudience
    | SessionAudience
    | AdminAudience
    | FanoutAudience
    | WakeAudience,
    Field(discriminator="scope"),
]
"""Discriminated union of event routing targets (round 21, HIGH-4).
The audience is stamped by the kernel inside the mutation
transaction - the EventBus is a stateless router over it and never
re-enters kernel tables after the fact (a torn-down lease/session
must not strand its queued event)."""


EVENT_AUDIENCE_SCOPES: dict[EventName, frozenset[str]] = {
    EventName.INCOMING_CALL: frozenset({"agent", "session", "fanout", "wake"}),
    EventName.MISSED_CALL: frozenset({"agent", "session", "fanout", "wake"}),
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
    EventName.MEDIA_OVERFLOW: frozenset({"session"}),
}
"""Authoritative event -> legal ``audience.scope`` set (round 25,
review-15 item 31) - the audience-union literal each event name may
be stamped with, enforced by :class:`EventEnvelope` at construction:

- ``incoming_call``/``missed_call`` carry caller-number PII and a
  one-shot ``call_token``: ``agent`` (exclusive owner), ``session``
  (one envelope per live RECEIVE_INBOUND session,
  round 22 HIGH-4), ``fanout`` (round 32, SA_swe2_review_2 item
  2 - ONE envelope carrying the whole RECEIVE_INBOUND member list,
  so a line-wide wave is O(lines) envelopes, not O(sessions)) or
  ``wake`` (round 33 - the zero-live-member case: configured wake
  hooks still fire while socket fan-out is empty by construction;
  on ``incoming_call`` the wake envelope carries ONLY the
  tokenless :class:`IncomingCallWakeEvent` projection - PR #82
  review - and on ``missed_call`` only
  :class:`MissedCallWakeEvent` - round 35, iteration-3 audit
  item 7 - while every non-wake audience keeps the token-bearing
  :class:`IncomingCallEvent`) -
  NEVER ``broadcast``;
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
        # Round 34 (iteration-2 audit item 24): the worker dispatch
        # is bound structurally, not just by scope - the event
        # carries a live ``fallback_call_token`` credential, so its
        # agent audience can only ever be the reserved
        # ``system:fallback_worker`` identity.
        if self.name is EventName.FALLBACK_TRIGGERED and not (
            isinstance(self.audience, AgentAudience)
            and self.audience.agent_id == SYSTEM_FALLBACK_WORKER
        ):
            raise ValueError("fallback_triggered routes only to the system:fallback_worker agent")
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
        routing choice. Round 33 (PR #82 review): ``incoming_call``
        additionally splits by audience - :class:`WakeAudience`
        accepts ONLY the tokenless :class:`IncomingCallWakeEvent`
        projection while ``agent``/``session``/``fanout`` keep the
        token-bearing :class:`IncomingCallEvent`, so a live
        ``call_token`` can never be constructed into a wake-hook
        delivery (and the credential-free projection can never be
        fanned out to a session that needs it)."""
        expected = EVENT_PAYLOAD_TYPES.get(self.name)
        if self.name is EventName.INCOMING_CALL and self.audience.scope == "wake":
            # Round 33 (PR #82 review): the wake projection is
            # TOKENLESS - a ``wake``-scoped ``incoming_call`` may
            # carry ONLY :class:`IncomingCallWakeEvent`, never the
            # token-bearing :class:`IncomingCallEvent`; the
            # non-wake audiences keep the table's pin.
            expected = IncomingCallWakeEvent
        if self.name is EventName.MISSED_CALL and self.audience.scope == "wake":
            # Round 35 (iteration-3 audit item 7): ``missed_call``
            # gets the same wake projection treatment - a
            # ``wake``-scoped envelope carries ONLY
            # :class:`MissedCallWakeEvent` (masked caller, no
            # ``RedactedString`` surface), while the
            # authenticated-audience spelling keeps the table's
            # pin.
            expected = MissedCallWakeEvent
        if expected is not None and not isinstance(self.payload, expected):
            raise ValueError(
                f"payload {type(self.payload).__name__} does not match event {self.name.value!r}"
            )
        return self


class _IncomingCallEventBase(ContractModel):
    """Shared ring-projection fields of the two ``incoming_call``
    payload spellings (round 33, PR #82 review): ``line_id`` the
    ringing line, ``caller_id`` the redacted caller PII when the
    air side supplied it, ``expires_at`` the absolute accept-window
    deadline in RFC-3339 UTC. The public subclasses split on ONE
    axis - whether the one-shot ``call_token`` may ride the
    payload: session/agent/fanout delivery needs it for
    ``call.accept``, the wake-hook projection must never carry it."""

    line_id: _CheckedLineId
    caller_id: _CheckedCallerNumber | None = None
    expires_at: AwareDatetime


class IncomingCallEvent(_IncomingCallEventBase):
    """incoming_call payload: first valid call.accept(call_token) wins
    (wake-and-grant, §4.3/§5.2). ``caller_id`` is redacted PII
    (round-22 wire-name fix: the wire field is ``caller_id``, not
    ``caller``); ``expires_at`` is the absolute accept-window
    deadline in RFC-3339 UTC - the earlier ``expires_in_ms`` relative
    form would skew by queue delay unless recomputed against
    ``emitted_at`` at outbox drain (round 22, review-10 HIGH-1).

    This is the token-BEARING spelling (round 33, PR #82 review):
    legal on every non-``wake`` ``incoming_call`` audience
    (``agent``/``session``/``fanout``) where the recipient can act
    on the ring. :class:`EventEnvelope` enforces the split at
    construction - a token-bearing payload can never ride a
    ``wake`` audience, and the tokenless projection can never ride
    a live-socket one."""

    call_token: _CheckedCallToken


class IncomingCallWakeEvent(ContractModel):
    """Tokenless ``incoming_call`` wake projection (round 33, PR #82
    review): the ONLY payload spelling legal on a
    :class:`WakeAudience` ``incoming_call`` envelope. The
    escalation-hook delivery leg is outside the authenticated
    control path - a hook subscriber can never call ``call.accept``
    - so the wake payload carries only the fields a wake signal
    needs (``line_id``, ``caller_id?``, ``expires_at``) and
    STRUCTURALLY cannot leak the one-shot ``call_token``: the field
    simply does not exist on this model, so no serialization path
    can ever emit a live ring token to an unauthenticated hook.
    :class:`MissedCallEvent` carries no token either, but its
    ``caller_id`` is typed ``_CheckedCallerNumber`` - a
    ``RedactedString`` that JSON-serializes the RAW number onto
    an unauthenticated hook spool. It therefore gets its own
    sibling projection :class:`MissedCallWakeEvent` (round 35,
    iteration-3 audit item 7) under the same masked-plain-``str``
    rule.

    Masked caller projection (round 34, iteration-2 audit item 16 -
    the model is deliberately NOT an :class:`_IncomingCallEventBase`
    subclass any more): ``caller_id`` is a plain ``str`` carrying
    the MASKED rendering of the ring's caller (the ``str()`` of its
    ``RedactedString`` - e.g. ``"+373****45"``). A
    ``RedactedString`` field would serialize the RAW number in
    ``model_dump(mode="json")`` - correct on the authenticated
    socket, a PII leak on an unauthenticated webhook/exec spool -
    so the wake model stores only the masked form, which renders
    identically in every dump mode. :func:`_check_masked_caller`
    requires the mask marker, so a raw E.164 string can never
    validate into this field."""

    line_id: _CheckedLineId
    caller_id: _CheckedMaskedCaller | None = None
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
    line freeing up (round 17).

    Mutation-scoped coalescing (round 34, iteration-2 audit item
    14): within ONE kernel mutation a ticket emits AT MOST ONE
    ``queue_updated`` envelope carrying its FINAL position - a
    cascade that shifts a ticket through several ranks (a
    ``drop_session`` or ``tick`` teardown freeing several ahead
    positions at once) coalesces to the landing rank, never one
    envelope per intermediate step. That pins the queue fanout
    of any single mutation at most
    :data:`MAX_QUEUE_TICKETS_TOTAL` envelopes - strictly below
    :data:`OUTBOX_HARD_CAP` by construction - so a mass queue
    shift can never overflow the outbox or starve the teardown
    effects committed alongside it. The per-ticket envelope
    shape stays: position updates are session-private data, so a
    shared summary envelope could not carry them without
    leaking foreign ``ticket_id``s."""

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
    plus lease teardowns with ``reason=CallEndReason.PROVIDER_LOST``
    instead (round 25, review-15 item 15 - provider health is the
    liveness axis, not a LineState; the reason unified to the ONE
    provider-loss spelling in round 34, iteration-2 audit item 6 -
    an admin binding teardown IS a provider-binding death path, so
    it reports ``provider_lost`` identically to orphan-grace expiry
    and heartbeat-down cascade; the former ``admin_revoked`` member
    had no remaining producer and was dropped from the closed
    vocabulary), so an audience
    can never be stamped ``None``. ``reason`` stays free-form
    ``str``: the wire table documents no closed vocabulary for it -
    an admin-supplied revoke note is legal payload (round-21 review
    audit), bounded at :data:`MAX_REASON_LEN` (round 27, review-17
    item 52)."""

    assignment_id: _CheckedAssignmentId
    line_id: _CheckedLineId
    owner_agent_id: _CheckedAgentId
    reason: _CheckedFreeText = Field(default=None, max_length=MAX_REASON_LEN)

    @field_validator("owner_agent_id")
    @classmethod
    def _owner_is_never_system(cls, owner: str) -> str:
        """The event reports a DURABLE agent owner - ``system:*``
        can never hold a ``LineAssignment`` (round 30,
        SA_carbon_review_19 slice-C item 13)."""
        return cast(str, _owner_agent_is_never_system(owner))


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
    reason: _CheckedFreeText = Field(default=None, max_length=MAX_REASON_LEN)

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


class MediaOverflowEvent(ContractModel):
    """media_overflow payload (round 31, SA_swe2_review_1 item 8 -
    contract-side half; the ``event.*`` table row lands in batch 2):
    the media adapter dropped frames for ``lease_id`` on
    ``line_id`` - a contiguous PCM-loss interval worth surfacing to
    the agent so a clipped-word gap is distinguishable from silence.
    Emitted AT MOST once per contiguous loss interval (never per
    dropped frame - a saturated media path must not overflow the
    domain outbox too)."""

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

    @model_validator(mode="after")
    def _unreachable_is_terminal(self) -> LineHealthChangedEvent:
        """``unreachable -> ws_disconnected`` is forbidden (round 30,
        SA_carbon_review_19 slice-C item 4): the socket-drop fact
        must be mapped onto the ladder BEFORE the event is minted -
        a binding already UNREACHABLE that loses its control socket
        reports the socket-drop fact as ``old_state`` UNREACHABLE,
        but the legal destination on this channel stays governed by
        the ladder (a dead binding never re-enters the orphan-grace
        state; recovery goes through reregister, not a state
        flip)."""
        if (
            self.old_state is LivenessState.UNREACHABLE
            and self.new_state is LivenessState.WS_DISCONNECTED
        ):
            raise ValueError(
                "unreachable is terminal - a dead binding never re-enters ws_disconnected"
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


class MissedCallWakeEvent(ContractModel):
    """Tokenless ``missed_call`` wake projection (round 35,
    iteration-3 audit item 7): the ONLY payload spelling legal on
    a :class:`WakeAudience` ``missed_call`` envelope, mirroring
    :class:`IncomingCallWakeEvent`. A wake-hook delivery to the
    ``missed_call`` event is legal (the wake vocabulary covers
    ``incoming_call`` AND ``missed_call``), but the authenticated
    :class:`MissedCallEvent` payload types ``caller_id`` as a
    ``RedactedString`` - which ``model_dump(mode="json")``
    renders RAW, a PII leak onto an unauthenticated
    webhook/exec spool. This projection carries only the fields a
    wake needs: ``line_id``, the MASKED ``caller_id`` (plain
    ``str`` via :func:`_check_masked_caller`, same rule as
    :attr:`IncomingCallWakeEvent.caller_id` - the mask marker is
    required so a raw E.164 can never validate in), and the
    ``reason`` the ring ended with. ``max_call_duration`` stays
    inadmissible for the same lease-vs-ring fact reason as the
    authenticated spelling."""

    line_id: _CheckedLineId
    caller_id: _CheckedMaskedCaller | None = None
    reason: CallEndReason

    @model_validator(mode="after")
    def _reason_is_a_missed_call_reason(self) -> MissedCallWakeEvent:
        """Same ring-vs-lease exclusion as
        :class:`MissedCallEvent`."""
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
    a false alert. ``last_seen_at`` refreshes on OWNER line
    operations and when ``drop_session`` closes the owner's LAST
    session - that is the moment the idle window starts; provider
    facts and ``configure_line`` never stamp it (round 30,
    SA_carbon_review_19 slice-A item 4; see
    :attr:`LineAssignment.last_seen_at`)."""

    assignment_id: _CheckedAssignmentId
    line_id: _CheckedLineId
    owner_agent_id: _CheckedAgentId
    idle_since: AwareDatetime

    @field_validator("owner_agent_id")
    @classmethod
    def _owner_is_never_system(cls, owner: str) -> str:
        """The alert reports a DURABLE agent owner - ``system:*``
        can never hold a ``LineAssignment`` (round 30,
        SA_carbon_review_19 slice-C item 13)."""
        return cast(str, _owner_agent_is_never_system(owner))


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
    EventName.MEDIA_OVERFLOW: MediaOverflowEvent,
}
"""Authoritative event -> payload-model map (round 26, review-16
item 1): the ``(name, payload-type)`` counterpart of
:data:`EVENT_AUDIENCE_SCOPES`, enforced by
:class:`EventEnvelope` at construction so a mis-typed payload can
never ride a mismatched event name (e.g. caller PII under a
broadcast-legal name). Defined AFTER the event classes because
every value is a class object; :class:`EventEnvelope` looks names
up at validation time, so the forward position is safe.
Completeness is pinned by ``test_event_payload_types_map``.
Round 33 (PR #82 review): the table pins the LIVE-socket
``incoming_call`` payload - :class:`EventEnvelope` overrides the
lookup to :class:`IncomingCallWakeEvent` on a ``wake`` audience,
so the tokenless projection needs no row of its own."""


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
    provider_call_id: _CheckedProviderCallId = None
    """Provider-side call handle (round 31, SA_swe2_review_1 item
    20): the adapter-distilled identifier the provider uses for
    this call on its own bus - makes snapshot/ring dedup EXACT
    instead of the ``(line_id, caller_id)`` +
    :data:`RING_DEDUP_MIN_GAP_SEC` best-effort fallback. Optional
    because not every provider stack exposes a stable handle."""
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
    profile: _CheckedProfile | None = Field(default=None, max_length=MAX_PROFILE_LEN)
    """Bounded when present (round 26, review-16 item 4; rule 7 on an
    external ingress field): a non-empty label of at most
    :data:`MAX_PROFILE_LEN` chars - shared with
    :class:`ProviderCapabilityUpdate` and the :class:`LineSummary`
    mirror."""
    meta: Mapping[str, str] = Field(default_factory=dict, validate_default=True)
    """Opaque provider metadata (wire: ``provider.register{meta?}``
    object). Bounded (round 22, review-10 MEDIUM-5; CODING_HAZARDS
    rule 7 on an external JSON-RPC ingress field): at most
    ``MAX_PROVIDER_META_ENTRIES`` keys, each key non-empty and at
    most ``MAX_PROVIDER_META_KEY_LEN`` chars, each value at most
    ``MAX_PROVIDER_META_VALUE_LEN`` chars - a violation is a
    ValidationError (INVALID_ARGUMENT at the adapter). Typed
    ``Mapping`` + stored as :class:`_FrozenMeta` at validation
    (round 32 pinned the read-only requirement; round 34,
    iteration-2 audit item 3 swaps the non-serializable
    ``MappingProxyType`` wrapper for the dict subclass): the wire
    shape stays a plain JSON object, but the stored map is
    read-only at runtime - ``frozen=True`` alone covered only
    field reassignment - while remaining JSON/pickle/deepcopy-safe
    and ``_mask_for_log``-compatible.

    Ownership rule (round 28, review-18 item 16): the model stores
    its OWN copy of the map - ``_bounded_meta`` returns a fresh
    map at validation, so the adapter MUST NOT keep mutating the
    dict it distilled from the wire after construction (it could
    no longer corrupt the stored fact anyway, and the kernel
    copies it again into ``ProviderBinding.meta``)."""
    state_snapshot: tuple[ProviderActiveCall, ...] | None = Field(default=None, max_length=64)
    """The provider's view of live calls at registration time
    (round 21, CRITICAL-3): carried on ``provider.reregister``
    inside the 10 s orphan-grace window so the kernel can verify a
    surviving call still exists on the D-Bus before re-binding its
    lease (design §5.4). ``None`` on a first-time register. Bounded
    at 64 elements (round 31, SA_swe2_review_1 item 27): a snapshot
    is an unbounded-length tuple of provider-controlled rows and
    feeds reconciliation sweeps - the cap keeps both kernel memory
    and per-mutation fanout finite (a modem cannot genuinely
    multiplex more live calls than the bound).

    Reconciliation scope (round 28, review-18 item 10; formalized
    round 29, SA_quant_review_1 slice-A item 7): the
    snapshot is authoritative ONLY for leases that had a delivered
    ``line.dial``/``line.answer`` command - i.e. leases whose phase
    is a member of :data:`SNAPSHOT_RECONCILED_PHASES`
    (``DIALING``/``ANSWERING``/``ALERTING``/``ACTIVE``/``TERMINATING`` -
    the formal "dialing-or-later" set; phase is a ``str``-enum with no
    ordering, so membership in this set IS the test, never a
    comparison). An ``ACQUIRED`` lease is
    pre-dial: the provider could never see it on the D-Bus, so it
    is EXCLUDED from reconciliation - an empty snapshot does not
    kill undialed grants; ``CallPhase.RINGING`` is a reserved member
    no lease can carry since round 30 (slice-A item 16), so it is
    excluded by construction. The 1:1 provider-call to kernel-lease
    rule applies only to the reconciled (dialed) set. A
    ``provider.ping{state_snapshot}`` fact obeys the same scope
    rules as reregister reconciliation (round-21 review; restated
    round 28).

    Ambiguity rule (round 30, SA_carbon_review_19 slice-A item 11;
    zero-candidate leg revised round 31, SA_swe2_review_1 item 7):
    a snapshot element carrying ``lease_id=None`` AND
    ``call_token=None`` (the external-call/uncorrelated shape) is
    resolved against the line's orphaned leases and external-call
    records - MORE than one candidate makes the correlation
    ambiguous and the fact is rejected ``INVALID_ARGUMENT``, while
    ZERO candidates is NOT a defect: the entry reports a call the
    kernel never tracked (a handset-dialed call the gateway missed,
    e.g. across a daemon restart), so the kernel mints a fresh
    ``external_call`` tracking record for it instead of dropping
    the desync. Command-ack gate (round 33): a snapshot is ALSO
    non-authoritative over a lease still carrying
    :attr:`CallLease.pending_command` inside its bounded
    ``provider_command_ack_sec`` window - snapshot omission never
    ends a command-pending lease, though a snapshot row MAY still
    bind identity evidence (``provider_call_id``, ``caller_id``)
    to it. A snapshot element
    with ``state == ProviderCallState.RINGING`` is fed through the
    ``ingest_ring`` path: the kernel mints a ``call_token`` for it
    and returns it in :class:`ProviderRegisterResult.pending_rings`
    - deduplicated by ring identity (exact when the element carries
    ``provider_call_id``, best-effort via
    :data:`RING_DEDUP_MIN_GAP_SEC` otherwise), never double-minted."""
    channel_seq: int = Field(ge=1)
    """Generation of the control socket the register/reregister rides
    on (round 30, SA_carbon_review_19 slice-A item 10): the adapter
    assigns every ACCEPTED provider control socket a monotonically
    increasing ``channel_seq`` - the new binding records it as the
    live channel generation, so a delayed fact arriving on a
    still-lingering older socket can never disarm the orphan
    deadline (see :attr:`ProviderBinding.channel_seq` and
    :meth:`LineManager.note_provider_liveness`)."""

    @field_validator("caps")
    @classmethod
    def _caps_are_telecom_only(cls, caps: tuple[Intent, ...]) -> tuple[Intent, ...]:
        """A provider declares TELECOM capabilities only (round 24,
        review-13 M3)."""
        return _provider_caps_check(caps)

    @field_validator("meta")
    @classmethod
    def _meta_bounds(cls, meta: Mapping[str, str]) -> Mapping[str, str]:
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
    channel_seq: int = Field(ge=1)
    """Generation of the control socket this fact arrived on (round
    30, SA_carbon_review_19 slice-A item 10; strict-newer rule
    round 34, iteration-2 audit item 8; record-only rule round
    35, iteration-3 audit items 8/17): adapter-stamped like
    :attr:`ProviderRegistration.channel_seq`. On a ``ws_disconnected``
    binding a REACHABLE fact clears ``orphan_deadline_at`` ONLY when
    this is STRICTLY GREATER than :attr:`ProviderBinding.channel_seq`
    - which only a ``provider.reregister`` generation satisfies; a
    delayed fact carried on the dead generation itself (EQUAL seq)
    or a still-lingering OLDER socket is recorded but never
    disarms the orphan deadline. Generally: any fact arriving with
    an equal-or-older generation than the binding's live channel
    is RECORD-ONLY - it feeds the ``checked_at`` dedup watermark
    but mutates NO liveness state, disarms NO deadline and
    refreshes NO ``last_seen_at`` (see the "Record-only stale
    facts" paragraph of
    :meth:`LineManager.note_provider_liveness`)."""
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
    state_snapshot: tuple[ProviderActiveCall, ...] | None = Field(default=None, max_length=64)
    """Optional ``provider.ping{state_snapshot?}`` carrier (round-21
    review): same element shape as
    :attr:`ProviderRegistration.state_snapshot` - the calls the
    provider still sees on the D-Bus - so a ping can feed ongoing
    reconciliation the way reregister does inside the orphan-grace
    window. ``None`` when the ping carries no snapshot. Same 64-
    element bound as the registration carrier (round 31,
    SA_swe2_review_1 item 27). The
    reconciliation scope is identical to the register-time one
    (round 28, review-18 item 10; formalized round 29): only leases
    whose phase is a member of :data:`SNAPSHOT_RECONCILED_PHASES`
    are reconciled - an ``ACQUIRED`` or ``RINGING`` lease is
    excluded, an empty snapshot never kills an undialed grant, and
    a lease whose :attr:`CallLease.pending_command` is still armed
    inside its ``provider_command_ack_sec`` window is excluded the
    same way (round 33) - the provider's picture cannot reflect a
    command it has not answered yet."""

    @field_validator("state")
    @classmethod
    def _state_is_a_reported_fact(cls, state: LivenessState) -> LivenessState:
        """UNBOUND is rejected (round 26, review-16 item 13): a
        liveness REPORT always comes from an attached binding -
        ``unbound`` is a derived :class:`LineSummary` listing state,
        never a reported fact a provider could send. DEGRADED is
        rejected for the same reason (round 28, review-18 item 1):
        degradation is a KERNEL-COMPUTED rung of the heartbeat
        ladder, never a provider-reported observation.

        WS_DISCONNECTED remains legal on this distilled model but
        is NOT a provider-reportable wire state (round 31,
        SA_swe2_review_1 item 27): the ``provider.ping{state}``
        wire vocabulary is exactly ``reachable``/``unreachable`` -
        the adapter MUST reject a ping whose params literally spell
        ``"ws_disconnected"``. The member stays because the model
        is the distilled fact for BOTH ping reports AND the
        adapter-OBSERVED control-socket close that arms the orphan
        grace (see :meth:`LineManager.note_provider_liveness`) -
        socket loss is minted by the adapter, never reported by the
        provider."""
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
    validators in round 27, review-17 items 44/45; ``caller_id``
    reopened as evidence in round 33): a
    ``state="ringing"`` report is fed to
    :meth:`LineManager.ingest_ring`, NOT to this ingress - the ring
    path mints the ``call_token`` (see
    :class:`IncomingRingIngress`), so ``state == RINGING`` is
    REJECTED here. ``caller_id`` is OPTIONAL bounded evidence on
    this model since round 33: it MUST be the caller's normalized
    E.164 like the ring-ingress field, and the kernel uses it ONLY
    for the sole-live-lease correlation rule below - never to
    re-report or re-mint caller identity. This model carries the
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

    External calls (round 21, HIGH-6; attribution evidence pinned
    round 32, SA_swe2_review_2 item 10): an update carrying
    NEITHER ``call_token`` nor ``lease_id`` on a line NOT in
    ``RINGING_LOCKED`` is correlated against the line's live
    leases BEFORE the external mint. On a line IN
    ``RINGING_LOCKED`` the unqualified case resolves differently
    (round 35, iteration-3 audit item 12 - see
    :meth:`LineManager.ingest_call_state`): a call-ESTABLISHING
    unqualified state is the handset-answer case below minting
    ``external_call`` bound to the open ring (or consuming a
    parked ``pending_ring`` as correlation evidence), and an
    unqualified ``disconnected`` resolves by the deterministic
    priority open ring -> parked ``pending_ring`` ->
    ``external_call`` hold -> unmatched drop. With ZERO live candidates the
    kernel mints ``external_call`` unconditionally - it reports a
    human-initiated call on the physical phone (dialed or
    answered on the handset itself); the line is marked BUSY with
    reason ``"external_call"``, no agent lease is minted, no
    PipeWire bridge is attached (audio stays on the phone), NO
    Hangup is ever sent, and the state clears when the provider
    reports DISCONNECTED (design §9). With EXACTLY ONE live
    candidate - e.g. a ``DIALING`` lease whose provider omitted
    the ``lease_id`` echo - correlation requires EVERY
    attributable field the update carries to AGREE with the
    lease's evidence (``provider_call_id`` vs the lease's
    recorded handle, ``caller_id`` vs the dialed number, both
    normalized E.164 before comparison; the direction implied by
    the lease's phase): a field ABSENT on
    either side is unverifiable and never blocks correlation,
    but a field PRESENT on both sides that DISAGREES - a
    ``caller_id`` naming a different number than the dialed one,
    a ``provider_call_id`` naming a different modem call object -
    is conflicting evidence: the kernel correlates NOTHING, mints
    the ``external_call`` hold for the human-initiated call the
    update truly describes, and leaves the unmatched lease on its
    normal deadline/orphan-grace expiry (it is never ended
    early by evidence it cannot answer for). Positive-evidence
    floor (round 33): agreement alone is not enough - AT LEAST
    ONE corroborating field (``provider_call_id`` or normalized
    ``caller_id``) must be present on BOTH sides and agree before
    an unqualified update may bind. An update carrying neither
    evidence field - or carrying only fields the lease cannot
    answer for - can never bind an external provider call to an
    agent lease: the kernel mints the ``external_call`` hold and
    leaves the sole live lease untouched (a ``DIALING`` lease
    whose ``line.dial`` ack is still pending is protected
    TWICE - see :attr:`CallLease.pending_command`). Two or more live
    candidates with no token make the update unattributable -
    it is also minted ``external_call`` rather than guessed.

    State restriction of the external mint (round 34, iteration-2
    audit item 13): ``external_call`` is minted ONLY by
    call-establishing unqualified states - ``dialing``,
    ``alerting``, ``active``. An unqualified ``disconnected``
    report with zero attributable candidates mints NOTHING: a
    teardown fact cannot establish a BUSY hold (a hold minted for
    a call whose own report just said "ended" would be a phantom
    that no later fact could ever clear). The kernel records it
    as an UNMATCHED FACT under the bounded safety-net rule and
    drops it - the update still answers ``{ok}`` like every
    non-conflicting fact, because a stray ``disconnected`` is a
    plausible duplicate of a teardown the kernel already applied.

    Unmatched-fact safety net (round 34, iteration-2 audit item
    13): an ``external_call`` hold minted with NO correlation
    handle - neither ``provider_call_id`` nor a burned
    ``call_token`` to bind its lifecycle facts - is armed with
    the bounded dead-man deadline of
    :data:`UNMATCHED_CALL_SAFETY_SEC` (the flat
    ``mint + UNMATCHED_CALL_SAFETY_SEC`` stamp - the configured
    ``max_call_duration_sec`` never shortens an unmatched hold,
    a handset call may legitimately outlive the call cap):
    ``tick`` releases the line ``FREE`` past it with
    ``reason:"network_error"`` so a missed teardown fact can
    never wedge the line BUSY forever. A hold carrying a
    correlation handle reconciles through TWO channels instead
    (round 35, iteration-3 audit item 9): the later
    ``disconnected`` binds to it and ends the hold like a normal
    external call, and the same :data:`UNMATCHED_CALL_SAFETY_SEC`
    deadline arms REFRESHED by every provider fact bound to the
    handle - a lost terminal fact is then swept within one
    window of the last report rather than living forever, and a
    ``state_snapshot`` that no longer lists the handle releases
    it. The same bounded-deadline family covers a
    parked ``pending_ring`` that was never promoted - see
    :meth:`LineManager.ingest_ring`.

    Handset-answered ring (round 28, review-18 item 9; drain edge
    added round 30, SA_carbon_review_19 slice-A item 6): a
    ``state="active"`` update that DOES carry ``call_token`` while
    the line is still ``RINGING_LOCKED`` means a human answered the
    ringing call on the physical handset before any agent's
    ``call.accept`` burned the token. The ring closes WITHOUT
    minting a lease - the kernel marks the line
    ``BUSY{reason:"external_call"}`` exactly like a dialed external
    call (no agent lease, no PipeWire bridge, NO ``line.hangup`` is
    ever queued for it) and the state clears on the later
    ``disconnected`` report. The same applies when the echoed
    ``call_token`` was already BURNED by the fallback decision -
    the line is inside the ring-drain sub-phase and the burned
    token stays a CORRELATION key for that ring's lifecycle facts
    until the line is FREE (never an action token): the
    ``active`` report still mints the ``BUSY``/``external_call``
    hold, and the later ``disconnected`` correlated by the burned
    token ends it and returns the line to ``FREE`` - never
    ``STALE_TOKEN``. The UNQUALIFIED variant carries no token at
    all (round 35, iteration-3 audit item 12): a tokenless
    ``dialing``/``alerting``/``active`` fact on the
    ``RINGING_LOCKED`` line is still the same physical truth -
    the ringing call was picked up on the handset - so the
    kernel mints the ``BUSY``/``external_call`` hold and burns
    the open ring's ``call_token`` INTO the hold's correlation
    handle (the burned token keeps serving as the lifecycle
    key for the hold the same way it did for the drain), or
    consumes a parked ``pending_ring`` record when no open ring
    exists. The best-effort ``line.hangup`` allowed
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
    """OPTIONAL bounded correlation evidence (round 33 - reopens the
    round-27 ``None``-only pin of review-17 item 45): the caller's
    normalized E.164 like :attr:`IncomingRingIngress.caller_id`,
    carried on post-ring updates as one of the two positive
    evidence fields the sole-live-lease rule requires (the other
    is ``provider_call_id``). It is evidence, never identity: the
    kernel compares it against the dialed number - or the ring's
    recorded caller - and never re-reports or re-mints caller
    identity from it."""
    provider_call_id: _CheckedProviderCallId = None
    """Provider-side call handle (round 31, SA_swe2_review_1 item
    20): same dedup/correlation role as
    :attr:`IncomingRingIngress.provider_call_id` - when carried it
    makes the update's binding to a tracked call EXACT (distinct
    handles can never alias), and on the uncorrelated
    ``disconnected`` path it disambiguates WHICH external call or
    tracked lease the report ends."""
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
    provider_call_id: _CheckedProviderCallId = None
    """Provider-side call handle (round 31, SA_swe2_review_1 item
    20): the exact dedup key for repeat ring reports - a second
    report carrying the SAME handle is the same physical ring (the
    same ``call_token`` is returned), a DIFFERENT handle is a
    genuinely new call even within seconds. Absent the handle,
    dedup falls back to ``(line_id, caller_id)`` plus
    :data:`RING_DEDUP_MIN_GAP_SEC` - see :meth:`ingest_ring`."""
    observed_at: AwareDatetime
    """Provider-side observation stamp - ADVISORY ONLY, same rule as
    :attr:`ProviderLiveness.checked_at` and
    :attr:`ProviderCallStateUpdate.observed_at` (round 30,
    SA_carbon_review_19 slice-A item 13): a dedup/ordering key,
    never an input to deadline arithmetic. At ingestion the kernel
    MUST run it through :func:`clamp_provider_stamp` verbatim - a
    stamp deeper than :data:`MAX_CLOCK_SKEW_SEC` into the FUTURE is
    normalized to ``now``, a PAST stamp is retained VERBATIM as the
    dedupe key - so all three provider-stamp ingress paths share one
    clamping rule."""


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
    profile: _CheckedProfile | None = Field(default=None, max_length=MAX_PROFILE_LEN)
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
    reason: _CheckedFreeText = Field(default=None, max_length=MAX_REASON_LEN)
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
        provider-role address of the same pair.

        Epoch stamping (round 34, iteration-2 audit item 17;
        PER-ROLE split round 35, iteration-3 audit item 1): the
        returned :attr:`MediaCredentials.agent_media_epoch` and
        :attr:`MediaCredentials.provider_media_epoch` MUST equal
        the lease's current :attr:`CallLease.agent_media_epoch`/
        :attr:`CallLease.provider_media_epoch` - the first
        ``allocate`` stamps BOTH to ``1`` (the lease's own pair
        moves ``0 -> 1`` in the same mutation, the pair is minted
        as a unit); a single-role ``reissue`` stamps ONLY the
        addressed role's next value and the returned pair view
        carries the sibling role's UNCHANGED epoch. The
        MediaRouter records each epoch inside the role-scoped
        token binding, so the credential object is the channel
        that carries epoch knowledge to the media side."""
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
        guarantee).

        The reissued pair's epoch for the ADDRESSED role is
        INCREMENTED - ``old + 1`` - and the matching
        :attr:`CallLease.agent_media_epoch`/
        :attr:`CallLease.provider_media_epoch` field moves with
        it in the same mutation, while the SIBLING role's epoch
        is carried over unchanged (round 35, iteration-3 audit
        item 1): a reissue is a new media epoch for the
        reattached role by definition, so sockets authenticated
        under that role's outgoing epoch die with it - but the
        untouched role's socket, bound to its still-current
        epoch, keeps streaming. This is the load-bearing
        property: a provider-side re-pair (modem reconnect,
        ``provider.reregister``) must NEVER kill a healthy agent
        socket, and vice versa. The sibling side of the failed
        handshake is unaffected and keeps its credential binding
        until its own reissue or lease end."""
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

        Key expiry and the session cascade (round 34, iteration-2
        audit item 24): the API key's ``expires_at`` gates the
        HANDSHAKE only - ``agent.register_intent`` on an expired
        or revoked key is ``UNAUTHORIZED`` at the transport
        boundary, while an already-registered session is
        UNAFFECTED by its key's later expiry (the key
        authenticated an attach, it is not a session lease).
        REVOCATION is the stricter case: an ``admin.revoke_key``
        on the key a live session registered under cascades the
        session through the ordinary ``drop_session`` path - a
        revoked credential must not keep driving calls - while
        expiry alone never kills a live session (a key whose
        owner simply forgot to renew must not drop in-flight
        calls; renewal is a new key on the next attach).

        Session caps (round 25, review-15 item 30; rule 7): at most
        :data:`MAX_SESSIONS_PER_AGENT` concurrent open sessions per
        ``agent_id`` AND at most :data:`MAX_SESSIONS_TOTAL` open
        sessions across ALL agent ids (round 27, review-17 item 73 -
        the per-agent cap alone cannot bound kernel session state
        against an unbounded set of distinct identities) - overflow
        on either is RESOURCE_EXHAUSTED with
        ``ErrorReason.SESSION_LIMIT_EXCEEDED``.

        Re-register semantics (round 27, review-17 item 70;
        identity match pinned round 32, SA_swe2_review_2 item 17):
        every ``agent.register_intent`` call mints a NEW
        ``session_id`` - there is no "update intents" operation. A
        reconnecting agent does NOT resume its old session: the old
        one's tickets and leases were socket-bound and died with it
        via ``drop_session``; the new session starts clean
        (deliberate - session state must never migrate between
        sockets). Idempotent registration on the SAME transport
        socket (an adapter-level retry of the handshake, not a new
        connection) returns the EXISTING session verbatim ONLY when
        the presented ``(agent_id, intents, profile)`` triple is
        IDENTICAL to the live session's recorded triple - the same
        socket asking for a different intent set or a different
        label is a fresh registration of conflicting facts, which
        is ``INVALID_ARGUMENT`` carrying
        ``ErrorReason.IDEMPOTENCY_CONFLICT`` rather than a silent
        second session or a silent intent swap.

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
        EXCLUSIVE line only when the registering agent OWNS it AND
        the session declares ``Intent.RECEIVE_INBOUND`` (round 35,
        iteration-3 audit item 15 - same accept-eligibility gate
        as the ring-time fan-out: an owner session on a
        ``dial_out``-only registration gets no token it could
        never consume), for
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

        Preferred-``line_id`` semantics (round 35, iteration-3
        audit item 11): a ``call.acquire`` naming ``line_id`` is a
        PREFERRED-LINE request - the ticket is bound to that
        line and is served only when the line becomes FREE with
        a servable binding. The outcome by block class is
        deterministic: a ``RINGING_LOCKED`` line (or a BUSY
        line) with ``enqueue: true`` ENQUEUES the preferred
        ticket - occupancy is transient, the ring drains or the
        accepted call ends and the ticket advances - while
        ``enqueue: false`` answers ``LINE_BUSY_INCOMING`` (the
        ring-specific code) or ``AcquireRefused`` (busy pool
        alternative listing) as before. A preferred line whose
        binding is ``ws_disconnected`` inside orphan grace
        queues the same way - the binding may recover - but a
        preferred line with NO live binding at all
        (``unbound``: never registered; ``unreachable``: dead
        past grace) is ``PROVIDER_UNREACHABLE`` under BOTH
        enqueue modes: no socket exists that could ever serve
        the ticket, so queueing one would park the agent's FIFO
        slot forever (the fallback worker's
        ``forward_via_line`` second leg relies on exactly this
        verdict - see :class:`ForwardViaLineFallback`). A
        ``line_id`` absent from the configured set is
        ``LINE_NOT_FOUND`` and never queued.

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
        conflict; LINE_BUSY_INCOMING on a RINGING_LOCKED line taken
        WITHOUT ``enqueue`` (round 35, iteration-3 audit item 11 -
        see the preferred-``line_id`` semantics below). A busy
        pool returns AcquireRefused - routine result, never an error.
        ``free_lines`` inside a refusal carries only lines that could
        actually serve a retry (round 28, review-18 item 16;
        predicate pinned round 34, iteration-2 audit item 4):
        ``SHARED_POOL`` lines advertising ``DIAL_OUT`` whose binding
        liveness is in :data:`SERVABLE_LIVENESS_STATES` - a foreign
        EXCLUSIVE line or a dead/orphaned/unbound pool line
        (``unreachable``/``ws_disconnected``/``unbound`` alike) must
        never be suggested as an alternative.
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

        Queue liveness rules (round 28, review-18 item 12;
        enqueue-at-cap pin round 34, iteration-2 audit item 22):
        the session lease cap guards GRANT issuance, not queue
        admission - a ``call.acquire{enqueue: true}`` arriving
        while the session sits at ``MAX_LEASES_PER_SESSION``
        still ENQUEUES its ticket (the ``RESOURCE_EXHAUSTED``/
        ``SESSION_LIMIT_EXCEEDED`` refusal applies to the
        immediate-grant leg only); the queued ticket is then
        served by the skip rule below the moment a lease frees.
        A head-of-queue ticket whose session is AT
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
        ``SHARED_POOL`` lines with ``DIAL_OUT`` caps and a SERVABLE
        binding (:data:`SERVABLE_LIVENESS_STATES` - ``unreachable``,
        ``ws_disconnected`` and ``unbound`` all fail it, round 34)
        becomes empty - the check runs on every liveness/caps/
        assignment mutation, so a queued ticket can never wait on a
        pool that can never serve it. Orphan-grace deferral
        (round 35, iteration-3 audit item 10): the cancellation
        waits out the LAST eligible binding's orphan grace - a
        servable set that is empty only because its members sit
        ``ws_disconnected`` inside their ``orphan_deadline_at``
        windows does NOT cancel the ticket yet; the ticket stays
        queued until every eligible binding's deadline resolves
        (a ``provider.reregister`` restores a servable line and
        the ticket survives; the last lapse triggers the
        ``provider_lost`` cascade which cancels it). A preferred
        ``line_id`` ticket on a single line follows the same
        rule for that one line. The dedup table records the OUTCOME of
        a keyed acquire (the granted ``lease_id`` or the queued
        ``ticket_id``): a same-key retry inside the entry's
        :data:`IDEMPOTENCY_TTL_SEC` window returns the recorded
        outcome VERBATIM - WITH a liveness re-check (round 35,
        iteration-3 audit item 20): the recorded ``lease_id``/
        ``ticket_id`` must still resolve LIVE at replay time.
        A recorded ``AcquireGranted`` whose lease has since
        ended (``grant_ttl_expired``, teardown, provider loss)
        and a recorded ``AcquireQueued`` whose ticket resolved
        (granted, cancelled, expired) are NOT replayed verbatim
        - replaying a dead id would hand the agent a ``lease_id``
        that resolves to nothing; the stored outcome is
        superseded and the retry RE-EXECUTES fresh under the
        same key, recording the new outcome over the dead one
        (the liveness check is the kernel-side mirror of the
        dedup entry - the outcome stays "stored, never
        re-derived" only while the object it names is live);
        the same key with
        DIFFERENT parameters inside the window is
        ``INVALID_ARGUMENT`` carrying
        ``ErrorReason.IDEMPOTENCY_CONFLICT`` (round 32,
        SA_swe2_review_2 item 17), and a retry arriving after the
        TTL re-executes fresh - the contract is a bounded retry
        window, not a permanent alias; entries die with the
        session as documented below.

        Replayable outcomes only (round 34, iteration-2 audit item
        20): the recorded outcome is a SUCCESS-class result - a
        granted ``lease_id`` or a queued ``ticket_id``. Transient
        and refused outcomes are NEVER stored: an
        ``AcquireRefused`` (busy pool), a
        ``PROVIDER_UNREACHABLE``/``LINE_BUSY_INCOMING``/
        ``RESOURCE_EXHAUSTED`` typed error, and every adapter-side
        transport failure re-execute fresh on the retry's own
        key - recording them would let a momentary refusal pin
        the agent's key for up to ``IDEMPOTENCY_TTL_SEC`` and
        turn a recoverable hiccup into an hour-long dead alias.
        The conflict rule applies only where a record exists:
        identical-parameters replay serves the stored success,
        different parameters hit ``IDEMPOTENCY_CONFLICT`` - a key
        that only ever produced refused/transient outcomes has
        no record and stays free for the retry.

        In-flight idempotency (round 33, PR #82 review): the
        adapter creates the per-``(method, session_id)``
        idempotency entry BEFORE the kernel mutation or provider
        command is dispatched. A retry carrying the same
        ``idempotency_key`` with IDENTICAL parameters while the
        first request is still in flight JOINS/WAITS on that
        operation and receives the same outcome - it MUST NOT
        re-enter the kernel or dispatch a second
        ``line.dial``/``line.answer``. The same key carrying
        DIFFERENT parameters is ``INVALID_ARGUMENT`` with
        ``ErrorReason.IDEMPOTENCY_CONFLICT`` as always. Once the
        first request resolves the entry replays the recorded
        outcome verbatim until TTL/eviction. ``drop_session``
        kills any in-flight waiter with the session table - the
        join never outlives the session, and no dedup ever spans
        a reconnect.

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

        Reissue ordering vs a lower-seq competitor (round 34,
        iteration-2 audit item 22): "reissued at the head" means
        reinserted by its ORIGINAL ingress ``seq`` - ahead of every
        ticket queued after it, never ahead of a ticket whose
        ``seq`` is lower. A competitor that queued BEFORE the
        preempted ticket keeps its priority; the reissue restores
        the preempted ticket's own lost position, it does not
        promote it past earlier arrivals.

        Reissue eligibility re-check (round 35, iteration-3 audit
        item 13): the re-materialization re-validates the
        recorded ``preferred_line_id`` at insertion time - the
        world may have moved while the grant was live. If the
        preferred line can no longer serve the owner (it became
        EXCLUSIVE to another agent, lost its ``DIAL_OUT`` cap,
        or its binding died past grace), the ticket drops its
        ``preferred_line_id`` and re-enters as a POOL ticket at
        the same original ``seq``; if the owner then has NO
        eligible line at all, the ticket is cancelled
        ``queue_cancelled`` with the matching reason
        (``line_exclusive``/``capability_removed``/
        ``provider_lost`` per :class:`QueueCancelReason`)
        instead of materializing a ticket that could never be
        served. The reissue never mints a second grant - it
        restores exactly one ticket.

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
        is also BOUNDED twice over (round 29, SA_quant_review_1
        slice-B item 6; LRU+TTL policy pinned round 32,
        SA_swe2_review_2 item 17): at most
        :data:`MAX_IDEMPOTENCY_KEYS_PER_SESSION` recorded outcomes
        per session - the OLDEST entry is evicted to admit a new
        key (bounded LRU; the round-29 fail-closed
        ``RESOURCE_EXHAUSTED`` rule is REPLACED - a new key is
        never rejected for table fullness) - and every entry
        expires :data:`IDEMPOTENCY_TTL_SEC` after recording.
        ``position`` inside :class:`AcquireQueued` is the GLOBAL
        FIFO rank across all eligible tickets (item 48).

        Free-event eligibility re-check (round 31,
        SA_swe2_review_1 item 25): grant advancement is driven by
        EVERY line-freeing transition, not only the paths that end
        a lease on the freed line itself - a hangup/release/
        provider-``disconnected``/preemption/fallback-drain/
        admin-cascade freeing ANY line re-evaluates the head of the
        FIFO across ALL newly-eligible lines (a pool ticket may be
        served by whichever line freed, and a freed line may now
        satisfy a ticket the earlier sweep skipped). Skipped-head
        semantics stay per-attempt as documented above - the
        re-check is what makes the skip window close on the NEXT
        free event instead of the next ticket ingress.
        """
        ...

    @abstractmethod
    async def cancel_acquire(self, session_id: SessionId, ticket_id: TicketId) -> None:
        """call.cancel_acquire: withdraw a queued ticket. Idempotent:
        cancelling a dead/served ticket OWNED BY THE CALLING SESSION
        is a no-op, not an error. Ownership is checked BEFORE the
        not-found path (round 30, SA_carbon_review_19 slice-A item
        9a): a ``ticket_id`` that resolves to a LIVE ticket owned by
        a DIFFERENT session is ``OWNER_MISMATCH``, never a no-op -
        the idempotent clause applies only to the caller's own
        dead/served tickets, so a session can never cancel (or learn
        the liveness of) a foreign ticket.

        Closed-ticket tombstones (round 31, SA_swe2_review_1 items
        24/25): a ticket that was granted, cancelled or expired is
        recorded into the bounded ``_closed_tickets`` ring
        (:data:`MAX_CLOSED_TICKETS`, insertion-order FIFO where a
        read never refreshes position) tagged with its owning
        ``session_id``. Resolution order honors the no-oracle pin
        of item 24: live-ticket hit -> cancel it (owner) or
        ``OWNER_MISMATCH`` (foreign); EVERY non-live id - tombstone
        hit or never-issued ring miss alike - is the same
        idempotent no-op answered ``{ok}``, so a caller can never
        learn whether a foreign or never-issued ``ticket_id`` once
        existed. A granted ticket's post-grant cancel replays the
        recorded outcome idempotently rather than racing the
        grant."""
        ...

    @abstractmethod
    async def release(self, session_id: SessionId, lease_id: LeaseId) -> None:
        """call.release{lease_id}: end the EPHEMERAL CallLease (round 22,
        review-10 HIGH-3 lifecycle fix).

        The lease NEVER outlives the call and the call NEVER outlives
        the lease: ``release`` on an ``ACQUIRED`` (granted-but-undialed)
        lease simply frees the line; ``release`` during an in-flight
        call is hangup-intent and follows the SAME
        ``TERMINATING`` path as :meth:`hangup` (round 34,
        iteration-2 audit item 18 - supersedes the earlier
        "enqueues a ``ProviderHangupCommand`` and ends the lease"
        wording, which implied an immediate ENDED an in-flight
        teardown cannot promise): the kernel enqueues the
        ``ProviderHangupCommand``, transitions the lease to
        ``TERMINATING``, and the provider's ``disconnected`` fact
        or the ``hangup_hold_sec`` lapse closes it to ``ENDED`` -
        one terminating discipline for both agent teardown verbs,
        so a call can never stay up with no owning lease (the
        earlier "an in-flight
        call finishes" wording of §4.2 applied to the DURABLE
        assignment, not to a lease). ``hangup``/provider
        ``disconnected`` already end the lease implicitly, so a
        trailing ``release`` after them is an idempotent no-op.
        OWNER_MISMATCH on a foreign ``(session_id, lease_id)`` pair
        - resolved BEFORE the expiry verdict, exactly like
        :meth:`hangup` (round 35, iteration-3 audit item 20): a
        lease already ENDED - ``grant_ttl_expired`` included -
        resolves through the ``_closed_leases`` ring as an
        idempotent no-op, never ``GRANT_TTL_EXPIRED`` (the typed
        expiry error is reserved for action verbs - see
        :attr:`CallLease.grant_expires_at`), and a foreign owner
        on a live or dead lease alike is ``OWNER_MISMATCH``.
        Like every lease teardown (round 33, PR #82 review),
        releasing a lease still carrying an armed
        :attr:`CallLease.pending_command` clears the
        ``pending_command``/``provider_command_ack_deadline_at``
        pair atomically and queues the scoped
        :class:`ProviderHangupCommand` (lease-scoped for a pending
        ``"dial"``, RING-scoped ``call_token`` for a pending
        ``"answer"``); a late command acknowledgement is stale and
        can never resurrect or mutate the torn-down lease (see
        :meth:`ingest_command_ack`).

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
        provider boundary. ``country`` (uppercase alpha-2
        phonenumbers-supported region, round 12 - incl. telephony
        extensions like ``"XK"``/``"AC"``/``"TA"``, not strict ISO
        3166-1 only)
        is the caller's assertion cross-checked against the number via
        phonenumbers BEFORE the line is touched (DIAL_REJECTED with a
        typed reason); per-line ``allowed_number_types`` on the
        LineAssignment gates UNSUPPORTED_NUMBER_TYPE - only LineManager
        knows which line backs the lease, so this check cannot live in
        the transport adapter (round 15). OWNER_MISMATCH /
        PROVIDER_UNREACHABLE / DIAL_REJECTED apply; a duplicate dial on
        one lease is rejected with INVALID_STATE (round 20 - the
        INVALID_CALL_STATE case no longer rides STALE_TOKEN).

        Provider-side compensation and the command-ack handshake
        (Saga, round 23, review-12 H3; hangup-first sequencing
        hardened round 30, SA_carbon_review_19 slice-A item 5 /
        slice-B item 7; dedicated entry point round 31,
        SA_swe2_review_1 item 5; typed ack fact round 33):
        ``dial()`` itself performs no network await - it transitions
        the lease to ``DIALING``, stamps
        ``pending_command="dial"`` with the line's effective
        ``provider_command_ack_sec`` deadline, enqueues
        ``ProviderDialCommand`` and returns ``DialResult``
        immediately. The WIRE ``call.dial`` answer is different
        (round 33): the adapter withholds it until the bounded
        ``line.dial`` handshake resolves - it awaits the provider's
        ``{accepted, provider_call_id?, reason?}`` reply with the
        line's effective ``provider_command_ack_sec`` deadline and
        reports the distilled reply through
        :meth:`ingest_command_ack`. ``{accepted: true}`` clears
        ``pending_command`` and records ``provider_call_id`` on the
        lease, and the adapter then answers the agent's
        ``call.dial`` with the ``DialResult`` carrying that handle
        - the wire reply waits ONLY for the command handshake,
        never for cellular alerting or answer. On
        ``{accepted: false}`` the adapter reports the refusal
        through the same :meth:`ingest_command_ack` ingress (whose
        effects are the :meth:`compensate_dial` semantics below);
        on a timeout or transport error - no reply exists to
        distill - it MUST call :meth:`compensate_dial` FIRST:
        the ``ProviderHangupCommand``
        that call enqueues IS the best-effort air-side teardown of
        the possibly-live dialed call, and the lease ends atomically
        with ``network_error`` so the line frees - a lease can never
        wedge in DIALING forever. Either failure path answers the
        pending ``call.dial`` with ``PROVIDER_UNREACHABLE`` (the
        provider could not complete the bounded command handshake -
        rejected, timed out or unreachable all collapse to it; no
        new error code is minted). The kernel-side deadline stamped
        on :attr:`CallLease.pending_command` is the BACKSTOP - an
        adapter that never calls either entry point still gets the
        identical terminal effect from ``tick``. Synthesizing a
        ``disconnected``
        ``ingest_call_state`` fact is FORBIDDEN: the provider never
        reported it, and a fabricated fact would confuse dedup; late
        provider facts about the ended lease are deduplicated through
        the ``_closed_leases`` ring afterwards (never
        ``STALE_TOKEN``).

        Pre-dispatch vs post-dispatch grant survival (round 35,
        iteration-3 audit item 10): a ``PROVIDER_UNREACHABLE``
        verdict BEFORE ``line.dial`` is ever dispatched - the
        binding already sits ``ws_disconnected``/``unreachable``
        when ``dial()`` runs - leaves the ``ACQUIRED`` grant
        UNSPENT: the lease stays live under its
        ``grant_expires_at`` deadline so an agent holding a
        just-granted line is not burned by a socket that died
        between acquire and dial (the binding's orphan-grace
        resolution or the grant TTL bounds the window). A
        POST-dispatch handshake failure - refused, timed out or
        lost after the command went out - consumes the grant
        through the compensation path above: the lease ends
        ``network_error``, terminal, and the agent re-acquires.
        The grant never survives a dispatched-but-failed dial -
        only the never-dispatched one."""
        ...

    @abstractmethod
    async def accept(self, session_id: SessionId, call_token: CallToken) -> AcceptResult:
        """call.accept: ATOMIC lease issue + ring answer + media attach
        (§5.2 step 4). First valid accept wins; a second accept on the
        consumed token gets STALE_TOKEN; owner-scoped lines reject
        non-owners with OWNER_MISMATCH. The lease is minted in
        ``CallPhase.ANSWERING`` (round 31, SA_swe2_review_1 item 2):
        the ``line.answer`` command is in flight but the call is not
        established yet - provider ``state=active``/``disconnected``
        facts drive ``ANSWERING -> ACTIVE/TERMINATING/ENDED`` through
        :data:`CALL_PHASE_FORWARD_TRANSITIONS`, and the phase joins
        :data:`SNAPSHOT_RECONCILED_PHASES` because the provider can
        already observe the answered call. The minted lease carries
        NO ``grant_expires_at`` - the accept already consumed the
        ring's ``call_token``, so there is no pending grant deadline
        to record (the inbound ring's own ``call_token`` TTL was the
        deadline and it burned).

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

        Provider-side compensation and the answer-pending ring
        (Saga, round 23, review-12 H3; ring-scoped entry pinned
        round 32, SA_swe2_review_2 item 9; ``answer_pending``
        ring sub-state typed round 33): ``accept()`` consumes the
        ``call_token``, mints the ``ANSWERING`` lease with
        ``pending_command="answer"`` and the line's effective
        ``provider_command_ack_sec`` deadline, enqueues
        ``ProviderAnswerCommand`` and returns - the consumed ring
        moves from ``ringing`` into the kernel-internal
        ``answer_pending`` sub-state: its token is burned (a second
        ``call.accept`` still resolves ``STALE_TOKEN``), and
        ``tick`` MUST NOT run the fallback decision while the ring
        sits answer-pending inside its ack window - the answer
        handshake is in flight, so the accept window no longer owns
        the ring's fate. The adapter awaits the provider's
        ``{accepted}`` on ``line.answer`` with the same bounded
        deadline and reports the reply through
        :meth:`ingest_command_ack`: ``{accepted: true}`` clears
        ``pending_command`` and commits the ring (the lease stays
        ``ANSWERING`` until provider ``active``/``disconnected``
        facts drive the phase per
        :data:`CALL_PHASE_FORWARD_TRANSITIONS`); on
        ``{accepted: false}`` the same ingress applies the refusal
        effects below, and on a timeout or transport error the
        adapter MUST call
        :meth:`compensate_answer` FIRST - its RING-scoped
        ``ProviderHangupCommand`` (``call_token`` set,
        ``lease_id`` ``None`` - the frozen reject vehicle) is the
        best-effort air-side reject of the still-ringing call, the
        ``ANSWERING`` lease ends ``network_error`` in the same
        mutation, and the line stays ``RINGING_LOCKED`` until the
        provider's ``disconnected`` or the ``ring_drain_ttl_sec``
        lapse resolves the drain - a burned ring never re-enters
        the fallback decision either way. NOT
        :meth:`compensate_dial` - the
        physical inbound ring can outlive the failed answer, so the
        line is never freed early the way dial compensation frees
        it. A failed ``line.answer`` handshake answers the pending
        ``call.accept`` with ``PROVIDER_UNREACHABLE`` (same
        command-handshake failure code as ``call.dial``).
        Synthesizing a ``disconnected`` fact the provider never
        reported stays FORBIDDEN.

        Line-state timing and answer-pending ownership (round 34,
        iteration-2 audit item 5): the line stays
        ``RINGING_LOCKED`` through the whole ``answer_pending``
        window and flips to ``BUSY`` exactly when the provider's
        ``state="active"`` report drives the lease ``ANSWERING ->
        ACTIVE`` - the ``line_state_changed{new_state:"BUSY"}``
        event rides that same transaction, so observers can never
        see the line BUSY while the physical answer is still
        unconfirmed, nor RINGING_LOCKED after it landed. While the
        ring sits answer-pending the burned ``call_token`` stays
        the correlation key for the ANSWERING lease's lifecycle
        facts: an ``active`` report arriving BEFORE the
        ``line.answer`` ack resolves binds to the lease and
        advances it - it does NOT mint an ``external_call`` hold.
        A handset that picks up mid-handshake is indistinguishable
        from the commanded answer on the provider's report, so
        the lease's own handshake owns the fact and wins;
        ``call_token``-correlated facts can never be re-routed to
        the external mint while a live lease claims that
        correlation key. An ``active`` report arriving only AFTER
        the answer handshake already failed - lease ended, ring
        drained into the ring-drain sub-phase - is the drain-time
        handset-answer path of :class:`ProviderCallStateUpdate`
        and mints ``BUSY``/``external_call`` with no lease: the
        handset's pickup outlived the gateway's rejected answer.

        Token survival on a failed accept (round 35, iteration-3
        audit item 10): the burned ``call_token`` NEVER survives
        a failed ``line.answer`` handshake - refused, timed out
        or lost, the token stays burned and the ring drains
        under its own rule (a retry with the same token is
        ``STALE_TOKEN``; a fresh token is minted only by a
        genuinely new ring report or the fallback re-issue).
        The narrower pre-dispatch case mirrors ``call.dial``: a
        ``PROVIDER_UNREACHABLE`` verdict BEFORE
        ``line.answer`` is dispatched - the binding already
        dead when ``accept()`` runs - means the accept never
        committed: the token is NOT consumed and the ring
        stays ``ringing`` under its normal accept window, so
        an agent racing a socket drop is not burned by a
        command that never left.
        Answer teardown stays RING-scoped until ACTIVE is
        confirmed: every failed-answer path - refused ack,
        timeout, transport error, or the ring's own
        ``disconnected`` - resolves through the RING-scoped
        ``ProviderHangupCommand``/drain bookkeeping documented
        above, never through a lease-scoped hangup against an
        air-side call the gateway never established."""
        ...

    @abstractmethod
    async def hangup(self, session_id: SessionId, lease_id: LeaseId) -> None:
        """call.hangup: request call teardown and advance the
        ephemeral lease toward ENDED (round 22, review-10 HIGH-3;
        TERMINATING leg pinned round 31, SA_swe2_review_1 item
        37). On a lease in an in-flight phase (``DIALING``/
        ``ANSWERING``/``ALERTING``/``ACTIVE``) the kernel enqueues
        ``ProviderHangupCommand`` and transitions the lease to
        ``TERMINATING`` - the phase stays live until the provider's
        ``disconnected`` fact confirms the air-side teardown
        (``TERMINATING -> ENDED``) or the hold lapses, so
        ``TERMINATING`` is a real reachable phase, not a dead rung
        in the matrix. The hold is bounded
        (:attr:`LineTimeouts.hangup_hold_sec`, default 10 s, 1..120
        s - round 32, SA_swe2_review_2 item 1): ``call.hangup``
        returns to the agent immediately once the command is
        queued, and ``tick`` ends a ``TERMINATING`` lease whose
        hold lapsed with ``CallEndReason.NETWORK_ERROR`` as a
        KERNEL-side timeout fact - never a synthesized provider
        ``disconnected`` report, which would fabricate a provider
        fact - so a lost or wedged ``line.hangup`` can never hold
        the line BUSY forever or stall the FIFO. On an
        ``ACQUIRED`` (granted-but-undialed)
        lease there is no air-side call to confirm - the lease ends
        directly ``ENDED`` with no command queued. Either way the
        lease ends with the call: no separate ``call.release`` is
        needed, the queue advances, and a trailing ``release`` is an
        idempotent no-op (the earlier §5.1 "hangup + release"
        phrasing is superseded: a line can never stay BUSY because
        an agent forgot to release). A provider ``disconnected``
        report ends the lease the same way. Dead-man switch /
        orphan grace follow §5.4. OWNER_MISMATCH on a foreign
        ``(session_id, lease_id)`` pair - resolved BEFORE the
        expiry verdict (round 35, iteration-3 audit item 20): a
        lease already ENDED - ``grant_ttl_expired`` included -
        resolves through the ``_closed_leases`` ring as an
        idempotent no-op, never ``GRANT_TTL_EXPIRED`` (the typed
        expiry error is reserved for action verbs - see
        :attr:`CallLease.grant_expires_at`).

        Dead-channel teardown (round 35, iteration-3 audit item
        3): a hangup (or the ``release``/``drop_session``/
        ``tick`` teardowns that share this path) on a lease
        whose binding sits ``ws_disconnected`` still parks the
        lease ``TERMINATING`` under its ``hangup_hold_sec``
        deadline, but mints NO ``ProviderHangupCommand`` while
        the channel is dead - the outbox would drain it into a
        socket that does not exist. The parked ``TERMINATING``
        phase IS the durable teardown intent: a
        ``provider.reregister`` inside the orphan grace re-mints
        a fresh ``line.hangup{lease_id}`` on the NEW binding for
        every preserved ``TERMINATING`` lease inside the
        supersede mutation itself (the command intent survives
        through the phase, not through a drained outbox); the
        orphan-grace lapse takes the ``provider_lost`` cascade
        instead, and a ``hangup_hold_sec`` lapse with the
        channel still dead ends the lease ``network_error`` like
        any other hold lapse.

        Pending-command teardown (round 33, PR #82 review;
        ring-scoped answer pin round 34, iteration-2 audit item
        5): a hangup on a lease still carrying an armed
        :attr:`CallLease.pending_command` clears
        ``pending_command``/``provider_command_ack_deadline_at``
        atomically with the teardown - the queued
        ``ProviderHangupCommand`` keeps the scope the AIR-side
        reality dictates (a pending ``"dial"`` ends its DIALING
        lease through the lease-scoped command; a pending
        ``"answer"`` on the ``ANSWERING`` lease queues the
        RING-scoped command - ``call_token`` set, ``lease_id``
        ``None`` - because the air-side call is still the
        unanswered ring until a provider ``active`` confirms it,
        so the reject vehicle stays the frozen ring-scoped one
        until ``ACTIVE`` flips the scope) - the ring's own fate
        stays governed by the consumed-ring
        bookkeeping, never by the stale handshake) - and a command
        acknowledgement arriving afterwards is STALE: it resolves
        through ``_closed_leases`` as a dedupe/no-op and can never
        resurrect or mutate the torn-down lease (see
        :meth:`ingest_command_ack`)."""
        ...

    @abstractmethod
    async def send_dtmf(self, session_id: SessionId, lease_id: LeaseId, digits: str) -> None:
        """call.send_dtmf: IVR navigation tones; mandatory from v1.
        ``digits`` MUST match ``DTMF_PATTERN`` (<= MAX_DTMF_DIGITS
        chars) - violation is INVALID_ARGUMENT with reason
        INVALID_DTMF (round 20). INVALID_STATE unless the lease is
        ACTIVE or ALERTING AND the kernel-side ``audio_ready``
        latch is set for it (round 27, review-17 item 13 - tightens
        the round-25/26 gate which allowed bare ALERTING): in-band
        tones are synthesized by the LineProvider into
        ``phonebridge_uplink``, so before
        the provider reports SCO-up there is literally no uplink
        for a tone to ride - the ``0.3-0.6 s`` gap the ALERTING
        early-media rule was meant to close would swallow them
        anyway. Early media is precisely "SCO up while ALERTING",
        so ALERTING + ``audio_ready`` remains legal. The latch is
        kernel-side media state, not a lease field (round 31,
        SA_swe2_review_1 item 4 - :class:`CallLease` stays frozen):
        set by :meth:`ingest_audio_ready`, one-shot within the
        media-attachment epoch, and RESET by
        :meth:`ingest_audio_lost`, by ``provider.reregister`` and
        by the provider's media socket detaching - after a reset a
        fresh ``provider.audio_ready`` is required before tones
        are accepted again. Tones ride
        :class:`ProviderSendDtmfCommand`
        (wire contract: docs/api-contract.md, round 26, review-16
        item 6).

        Dead-channel verdict (round 35, iteration-3 audit item 3):
        a ``send_dtmf`` on a lease whose binding sits
        ``ws_disconnected`` (or worse) answers
        ``PROVIDER_UNREACHABLE`` and mints NO command - a tone is
        a real-time effect that can neither ride a dead socket
        nor be replayed after grace, so the deterministic
        refusal replaces queueing it into an outbox the dead
        channel can never drain. The same rule covers a tone
        ``line.send_dtmf`` that was dispatched but lost when the
        socket dropped: it is simply dropped - no tone replay
        exists."""
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
        never resurrect a torn-down media path. ``INVALID_STATE`` on
        an ``ACQUIRED`` lease (round 31, SA_swe2_review_1 item 33):
        a granted-but-undialed lease has no media pair minted yet -
        ``MediaAllocator.allocate`` runs inside ``dial``/``accept`` -
        so there is nothing to reissue and no epoch to re-arm."""
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
        foreign lease is ``STALE_TOKEN``/``OWNER_MISMATCH``. An
        ``ACQUIRED`` lease is ``INVALID_STATE`` (round 31,
        SA_swe2_review_1 item 33 - same rule as
        :meth:`reattach_media`): no media pair exists to reissue
        before ``dial``/``accept`` allocates one. The
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
        ``line_id`` absent from the configured line set entirely.

        Shared eligibility predicate (round 33; claim-visible split
        round 34, iteration-2 audit item 4): the claim test is
        the SAME predicate the
        ``get_lines(intent=Intent.EXCLUSIVE_LINE)`` filter applies
        - ``claimable && mode == SHARED_POOL && binding liveness in
        CLAIM_VISIBLE_LIVENESS_STATES`` - so a line the listing
        never offered can never be claimed either:
        ``claimable=False`` and a dead/absent binding
        (``UNBOUND``/``unreachable``) alike resolve
        ``LINE_NOT_CLAIMABLE`` under ``INVALID_STATE``, while a
        line already owned EXCLUSIVE by another agent keeps the
        ``LINE_EXCLUSIVE_NOT_OWNER`` conflict code. A
        ``ws_disconnected`` binding inside its orphan grace is
        CLAIM-VISIBLE - the claim is a moderator-resolved request
        that may legitimately outlive the grace - but it is NOT
        servable (:data:`SERVABLE_LIVENESS_STATES`): an approved
        claim still cannot mint a grant on the dead channel until
        ``provider.reregister`` re-attaches."""
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
        ``limit`` outside ``1..MAX_LIST_PAGE`` is INVALID_ARGUMENT.
        The ``Field`` bounds on the parameter annotations are
        DECLARATIVE wire bounds (round-29 re-review): ``Annotated``
        ``Field`` metadata on an ABC signature is inert at call
        time, so the bounds carry the same normative force as every
        other contract bound - the adapter MUST reject out-of-range
        values at the boundary and implementations MUST honour the
        window. The
        page is a STABLE-ORDERED slice: rows enumerate in
        ``line_id`` order, so offset-paging a quiescent listing
        never skips or repeats a row; a full ``limit``-sized page
        means "advance ``offset`` by ``limit``", a short page ends
        the list.

        ``intent=Intent.EXCLUSIVE_LINE`` filter semantics (round 24,
        review-13 M3; single-predicate pin round 33): returns the
        lines whose
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
        configured-fleet row). The filter and the
        :meth:`claim_exclusive` eligibility predicate are THE SAME
        predicate by contract (round 33; claim-visible split round
        34, iteration-2 audit item 4): ``claimable &&
        mode == SHARED_POOL && binding liveness in
        CLAIM_VISIBLE_LIVENESS_STATES`` - a line the
        listing offers is a line the claim accepts, so listing and
        claiming can never diverge on an edge. The claim-visible
        window deliberately INCLUDES ``ws_disconnected`` inside its
        orphan grace (a dead-socket line may re-attach before the
        claim resolves) while ``UNBOUND``/``unreachable`` still
        fail the liveness leg - claim visibility is not
        servability, so the offered row can never satisfy a grant
        until the binding reregisters.

        Adapter-side ``cursor`` input (round 33): the WIRE
        ``call.get_lines`` params MAY carry an opaque ``cursor``
        string instead of ``offset`` - cursor is adapter-owned
        pagination input that MUST be mutually exclusive with
        ``offset`` on the request and MUST reject a malformed or
        foreign cursor ``INVALID_ARGUMENT`` (fail-closed, never a
        silent rewind to offset 0). The kernel signature itself
        keeps ``offset``/``limit`` semantics only - the adapter
        translates its cursor into the kernel window; the kernel
        never sees the opaque token.

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
        ``event.assignment_granted`` copy predated its session.

        Compact rows (round 31, SA_swe2_review_1 item 9):
        ``LineSummary`` is the bounded hot-path view - it carries
        ``line_id``/``state``/``liveness``/``caps``/``profile``/
        ``assignment_id`` and deliberately NOT the provider's
        ``meta`` map or hardware fields: ``ProviderBinding.meta``
        (up to :data:`MAX_PROVIDER_META_ENTRIES` entries x
        :data:`MAX_PROVIDER_META_VALUE_LEN` chars) would multiply a
        page's payload for data an agent listing never needs. The
        admin surface reads the full metadata through
        :meth:`LineAdminKernel.list_provider_bindings`/
        :meth:`LineAdminKernel.get_line` instead."""
        ...

    # --- provider ingress (narrowed: facts and decisions, not transport) ---

    @abstractmethod
    async def register_provider(self, registration: ProviderRegistration) -> ProviderRegisterResult:
        """Provider attach fact -> :class:`ProviderRegisterResult`
        (round 20; the ≤0.0.12 signature returned the agent-side
        LineAssignment, conflating two object spaces; round 30,
        SA_carbon_review_19 slice-A item 11 wraps the binding with
        the rings the ``state_snapshot`` minted).

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
        SA_quant_review_1 slice-A item 7; ``ANSWERING`` joined the
        set round 31, SA_swe2_review_1 item 2 - the formal
        "dial-or-answer-commanded" set: ``DIALING``/``ANSWERING``/
        ``ALERTING``/``ACTIVE``/``TERMINATING``; an ``ACQUIRED`` or
        ``RINGING`` lease was never dialed, so an empty snapshot
        does not kill it). Command-ack gate (round 33): inside
        that set, a lease still carrying
        :attr:`CallLease.pending_command` is EXCLUDED from
        reconciliation while its bounded
        ``provider_command_ack_sec`` window is open - the provider
        cannot authoritatively assert a call it has not yet
        acknowledged, so a snapshot omission can never end a
        command-pending lease, and a snapshot row claiming the
        call binds identity evidence only. The single exception
        is a provider ``disconnected`` fact carrying matching
        strong identity (the echoed ``lease_id``, or a
        ``provider_call_id``/``call_token`` the kernel recorded)
        - a positive teardown report still wins over the pending
        handshake. The new binding also INHERITS the line's
        recorded provider-stamp watermarks
        (:attr:`ProviderBinding.liveness_stamp_watermark`/
        :attr:`call_stamp_watermark`) and reseeds
        ``channel_seq`` past the stored maximum (round 33): the
        watermarks are line-scoped durable state, so a
        reregistered provider cannot replay pre-watermark facts
        past the dedup gate. Reregister inside the
        orphan-grace window stays idempotent and preserves
        in-flight leases (wire: provider.reregister, 10 s orphan
        grace, §5.4). On the first
        registration of a new ``line_id`` the kernel ALSO initializes
        the line's default ``LineAssignment`` (SHARED_POOL,
        owner_agent_id=None) if none exists - the binding itself is
        the returned record. The auto-created record rides the FULL
        provisioning contract (round 34, iteration-2 audit item 15):
        it counts against :data:`MAX_LINES_TOTAL` like an
        ``admin.provision_line`` row - a register that would push
        the configured fleet past 256 is ``RESOURCE_EXHAUSTED``,
        never an uncounted line - and the adapter MUST persist its
        ``lines`` row under the same kernel-first write-through
        ordering, compensating a failed row write through
        ``restore_line(line_id, None)`` exactly like a failed
        provision (an auto-created record is durable config, not
        cache, so it can never ride the best-effort
        ``provider_bindings`` write path). The created record is
        also returned on
        :attr:`ProviderRegisterResult.auto_created_assignment`
        (round 35, iteration-3 audit item 4) so the adapter knows
        the rollback scope verbatim - and that same
        ``restore_line(line_id, None)`` call is privileged to
        remove the record DESPITE the binding this mutation just
        minted (the register-scoped compensation rolls the pair
        back atomically - see ``restore_line``); without the
        pairing the ordinary live-binding quiescence guard would
        make a failed write-through unrecoverable.

        The returned result's ``binding`` stamps
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
        ``provider_lost`` effect so the teardown
        events reach the SAME cycle's ``drain_events()``.
        The ``pending_command`` marker on such a lease resolves
        with it (round 33): binding death is authoritative over
        the ack gate - a handshake that can never complete is
        closed by the dead-binding purge, not left to lapse.

        TERMINATING retarget (round 35, iteration-3 audit item 3):
        a lease parked ``TERMINATING`` on the orphaned/superseded
        binding - torn down while the channel was dead, so no
        ``ProviderHangupCommand`` could be minted for it (see
        :meth:`hangup`) - re-BINDS under the snapshot rules like
        every other preserved lease, and the kernel mints a
        FRESH ``ProviderHangupCommand{lease_id}`` on the new
        ``assignment_id`` inside this same mutation: the parked
        phase is the durable record of the teardown intent the
        dead channel could not carry, so the retarget never
        depends on outbox entries that drained away. A
        ``TERMINATING`` lease absent from a present
        ``state_snapshot`` reconciles out normally - the
        provider's picture wins as for every reconciled phase.

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

        Plain ``provider.register`` on a second socket (round 34,
        iteration-2 audit item 24; fail-closed lease rule round
        35, iteration-3 audit item 5): the ``INVALID_STATE`` guard
        covers ONLY bindings with a live control channel
        (REACHABLE/DEGRADED). Against a WS_DISCONNECTED binding
        still inside orphan grace a plain ``register`` arriving on
        a NEW socket is a LEGAL fresh supersede, NOT a violation:
        the old socket is gone, so the kernel supersedes the
        orphaned row, mints a fresh ``assignment_id`` and
        ``channel_seq``. BUT ``provider.register`` carries NO
        ``state_snapshot`` - a register that declares no call
        view cannot preserve call state, so the orphan's live
        leases end ``provider_lost`` INSIDE this mutation (the
        same atomic teardown the dead-binding cascade performs,
        routed through the supersede instead of ``tick``): a
        preserved-but-unverified lease would be a zombie the new
        binding can never serve because the provider side never
        learned its ``lease_id``. The air-side calls the
        restarted provider still holds re-surface as
        ``external_call`` facts through the NEW binding's own
        ``provider.call_state`` reports - the truth comes back
        on provider evidence, never on a zombie lease. Open
        ring/``pending_ring``/``external_call`` records of the
        orphan reset with the same cascade (a physically
        still-ringing call is re-reported ``ringing`` on the new
        channel and mints a fresh token). ``provider.reregister``
        inside the same window stays the lease-PRESERVING
        reattach path - its ``state_snapshot`` is what makes
        preservation verifiable, which is exactly why the two
        methods diverge here.

        Hydrated-dead binding (round 31, SA_swe2_review_1 item 3):
        a ``ProviderBinding`` row loaded by
        :meth:`LineAdminKernel.hydrate_lines` sits in kernel state
        as WS_DISCONNECTED with an EXPIRED
        ``orphan_deadline_at`` - the sockets it recorded died with
        the old process. A ``provider.register`` arriving against
        it is LEGAL and is classified as a fresh attach: the
        request arrives on a NEW control socket, so the kernel
        supersedes the tombstone-pending row, mints a fresh
        ``assignment_id`` and a fresh ``channel_seq``, and the
        hydrated row's pending tombstone cascade never races the
        new binding (``tick`` tombstones the dead row, never the
        live one).

        Return shape (round 30, SA_carbon_review_19 slice-A item
        11): the result is :class:`ProviderRegisterResult` -
        ``binding`` is the created/superseded
        :class:`ProviderBinding`, ``pending_rings`` carries every
        :class:`PendingRing` minted through the ``ingest_ring``
        path for a snapshot element reporting
        ``ProviderCallState.RINGING``. The adapter relays each
        pending ring's ``call_token`` back to the provider so the
        rediscovered ring keeps exactly one kernel-minted
        correlation token - deduplicated by ring identity, never
        re-minted on a repeat register.
        :attr:`ProviderRegisterResult.auto_created_assignment`
        (round 35, iteration-3 audit item 4) carries the
        ``LineAssignment`` the register auto-created for a
        previously unconfigured ``line_id`` (``None`` when the
        line already existed) - the rollback-scope handle the
        write-through compensation needs, see
        :meth:`LineAdminKernel.restore_line`.
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

        Open-ring fate on ``RECEIVE_INBOUND`` removal (round 34,
        iteration-2 audit item 22): a capability shrink does NOT
        kill an already-open ring either - the physical call is
        still ringing on the modem, so the line stays
        ``RINGING_LOCKED`` and its accept/fallback/drain lifecycle
        completes exactly as if the caps never changed (an
        ``answer_pending`` handshake in flight likewise runs to
        its ack resolution). The removed intent only changes
        FUTURE admissions: later ``provider.call_state{ringing}``
        facts on a binding lacking ``RECEIVE_INBOUND`` take the
        caps-missing glare path of :meth:`ingest_ring`
        (INVALID_STATE, provider must hang up the call).

        ``profile`` patch semantics (round 27, review-17 item 25):
        ``None`` means "leave unchanged" - the standard
        None-means-unchanged convention. There is deliberately NO
        clear-profile flag in v1: a provider that wants to drop its
        label registers ``profile`` anew on its next
        register/reregister; the wire surface stays free of a
        second, redundant mutation path."""
        ...

    @abstractmethod
    async def note_provider_liveness(self, liveness: ProviderLiveness) -> tuple[PendingRing, ...]:
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

        Returns the ``pending_rings`` list for this fact (round 34,
        iteration-2 audit item 12): on a REACHABLE fact - i.e. the
        ``provider.ping`` ingress - the kernel hands back the
        promoted-ring tokens still undelivered on this binding's
        line (see :meth:`ingest_ring` "Token delivery for the
        promoted ring"), so the adapter rides them on the ping
        reply in the same
        :class:`ProviderRegisterResult.pending_rings` shape the
        register response uses. The returned tuple is bounded at
        :data:`MAX_LIST_PAGE` entries, is cleared from kernel state
        by the return itself (delivery is exactly-once - a lost
        reply is re-delivered by the provider's next re-report
        dedup leg, which answers ``{ok, call_token}``), and is
        EMPTY for ws_disconnected/unreachable facts or when no
        promoted ring is pending.

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
        Dedup scope (round 29, SA_quant_review_1 slice-B item 5;
        re-scoped round 30, SA_carbon_review_19 slice-B item 8):
        the ``checked_at`` dedup/ordering key is scoped to
        ``(line_id, accepted_stamp)`` - the per-LINE monotonic
        watermark survives a reregister, so a stamp replayed by a
        NEW binding still dedupes against the line's recorded
        watermark. Facts naming a superseded ``assignment_id`` are
        already rejected ``STALE_TOKEN`` through the
        ``_closed_assignments`` ring before dedup is consulted, so
        the binding id no longer needs to ride the key. The
        watermark is MONOTONE-MAX per scope (round 30,
        SA_carbon_review_19 slice-B item 9; ping-vs-fact split
        pinned round 31, SA_swe2_review_1 item 19): liveness/ping
        facts are REFRESH facts, so an out-of-order stamp is
        CLAMPED, not dropped - a ping carrying a stamp below the
        recorded watermark is processed with the watermark as its
        accepted stamp and still refreshes ``last_seen_at`` to
        kernel ``now`` (a provider whose clock rewound under an
        NTP step must not freeze its own heartbeat ladder). The
        watermark itself is the durable
        :attr:`ProviderBinding.liveness_stamp_watermark` field
        (round 33) - persisted verbatim on the row, restored by
        ``hydrate_lines``, and inherited by a reregistered binding
        at the line's binding scope. The
        dedupe-KEY semantics are unchanged - the same clamped stamp
        still detects a verbatim repeat - while ordering drops stay
        reserved for STATE facts
        (:meth:`ingest_call_state`/:meth:`ingest_ring`), where an
        out-of-order stamp below the watermark is dropped as a
        duplicate because state facts must never apply backwards.

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
        end with the ``provider_lost`` effect
        inside the same transaction (round 35, iteration-3 audit
        item 16 - the unified dead-binding reason, never
        ``network_error``: a binding death is provider loss, not a
        call-fault guess) - their teardown events reach
        the SAME cycle's ``drain_events()`` rather than outliving
        the command drain (the adapters drain events first, so
        teardown events minted during a LATER command drain would
        sit unsent until the next transaction). A dead binding also
        kills an armed ``pending_command`` the same way (round 33):
        a lease still waiting on its ``line.dial``/``line.answer``
        acknowledgement has no live socket left to answer on, so
        the pending handshake is cancelled inside the same
        transaction and the lease follows the dead-binding
        ``provider_lost`` end - a reregistered binding never
        inherits a half-completed handshake.

        Fact recording vs escalation (round 25, review-15 item 32):
        this method records facts and arms/disarms
        ``ProviderBinding.orphan_deadline_at`` - a WS_DISCONNECTED
        fact SETS the orphan window. A REACHABLE fact clears it ONLY
        when the fact's ``channel_seq`` is STRICTLY NEWER than the
        binding's recorded generation (round 34, iteration-2 audit
        item 8 - tightens the round-30 "equals the live generation"
        rule: an equal-generation REACHABLE arriving after the drop
        is a delayed fact replayed off the dead socket itself, so
        inside the grace window the ONLY authoritative recovery is
        ``provider.reregister`` on a fresh generation - a delayed
        ping stamped ``checked_at`` before the WS drop and replayed
        on the same or a still-lingering OLDER socket carries an
        equal-or-smaller ``channel_seq``, is recorded as a fact but
        MUST NOT disarm the orphan deadline, otherwise a dead
        session's last gasp could keep a zombie binding alive).

        Record-only stale facts (round 35, iteration-3 audit
        items 8/17 - the generalization of the strict-newer rule):
        a liveness fact whose ``channel_seq`` is SMALLER than the
        binding's recorded generation, or EQUAL to a generation
        whose channel already closed, is RECORD-ONLY - it feeds
        the ``checked_at`` dedup watermark and nothing else. It
        MUST NOT mutate ``binding.liveness`` (a stale-gas
        ``reachable`` cannot un-degrade or un-orphan the binding,
        and a stale ``ws_disconnected``/``unreachable`` cannot
        re-arm or re-run a cascade), MUST NOT disarm
        ``orphan_deadline_at``, and MUST NOT refresh
        ``last_seen_at`` (a dead generation's echo is not
        evidence the live channel is healthy). Only a fact on
        the CURRENT live generation mutates liveness at all -
        which is why recovery from ``ws_disconnected`` exists
        only through ``provider.reregister`` minting a strictly
        newer generation.
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
    async def ingest_ring(self, ring: IncomingRingIngress) -> IncomingCallEvent | None:
        """Ring ingress: physical call ringing -> RINGING_LOCKED +
        wake-and-grant routing (§4.3). The kernel MINTS the
        ``call_token`` here (round 22, review-10 MEDIUM-3 - the
        taxonomy reserves it to LineManager) and returns it inside
        the ``IncomingCallEvent``; the adapter MUST relay that token
        to the provider as the ``provider.call_state`` response
        ``{ok, call_token}`` so a pre-answer caller hangup can be
        correlated. Audience (round 22, HIGH-4): on an EXCLUSIVE
        line one ``AgentAudience`` envelope to the owner; on a
        SHARED_POOL line one envelope covering every live
        ``Intent.RECEIVE_INBOUND`` session - the single
        ``FanoutAudience`` envelope carrying the member list since
        round 32 - never a broadcast to dial_out-only sessions. The
        :data:`SYSTEM_FALLBACK_WORKER` session is excluded from that
        pool fan-out (round 28, review-18 item 16): the worker is
        dispatched via ``event.fallback_triggered`` after the
        accept-window decision, never as an ``incoming_call``
        subscriber; an EXCLUSIVE line's owner still receives the
        event even when the owner is not an ordinary pool
        subscriber - but ONLY through sessions declaring
        ``Intent.RECEIVE_INBOUND`` (round 35, iteration-3 audit
        item 15): the token-bearing ``incoming_call`` is an
        accept invitation, so an owner session that registered
        only ``dial_out`` MUST NOT receive a ``call_token`` it
        could never legally consume (``call.accept`` would
        reject it ``INVALID_INTENT``) and does NOT count as a
        live fan-out member for the wake decision either.
        Zero-member fan-out (round 33; exclusive-line
        extension round 34, iteration-2 audit item 2): a ring whose
        live audience is EMPTY emits the ``incoming_call`` through
        the typed :class:`WakeAudience` audience when the line's
        :attr:`LineAssignment.wake_hooks` set is non-empty - that
        covers BOTH a SHARED_POOL ring with no live
        ``RECEIVE_INBOUND`` session AND an EXCLUSIVE line whose
        owner holds no live session (a sleeping exclusive owner is
        woken exactly like a sleeping pool: the wake envelope
        carries no member list, so the hook delivery leg resolves
        the line's configured hooks itself and the owner's identity
        never has to ride an unauthenticated channel). The
        envelope has no socket member by construction and only the
        hook delivery leg consumes it, carrying the TOKENLESS
        :class:`IncomingCallWakeEvent` projection (round 33, PR #82
        review: the model has no ``call_token`` field, so a wake
        delivery can never leak the live ring token to an
        unauthenticated hook - and its ``caller_id`` is the MASKED
        rendering, never the raw number, round 34 item 16); with no
        hooks resolved at all the ring emits no ``incoming_call``
        envelope and simply waits out its accept window.

        Returns the incoming_call event - the same instance is ALSO
        appended to the outbox inside the mutation transaction; the
        EventBus publishes exclusively from ``drain_events()``, the
        return value exists for caller convenience (round 20).
        Atomic w.r.t. concurrent acquires on the same line
        (single-event-loop invariant). Idempotent: re-ingesting a
        ring on an already RINGING_LOCKED line returns the existing
        event with the SAME ``call_token`` - but ONLY while the ring
        is still live (see the consumed-ring cases below: during the
        ``answer_pending`` sub-state and the ring-drain sub-phase
        the method returns ``None``, never the burned token).

        Line-state contract (round 24, review-13 H3; rewritten for
        the liveness split in round 25, review-15 item 15 - the glare
        matrix is defined for EVERY call-occupancy state, including
        the classic race where the agent's ``call.dial`` is still in
        flight to the provider while the cellular network lands a
        genuinely first inbound call on the handset - Call Waiting
        filtering cannot see it):
        - ``FREE`` -> transition to ``RINGING_LOCKED``, mint the
          ``call_token``;
        - ``RINGING_LOCKED`` -> dedupe by RING IDENTITY (round 30,
          SA_carbon_review_19 slice-A item 6): while the open ring
          is still live (unconsumed ``call_token``) a re-report of
          the SAME physical ring returns the existing event with
          the same ``call_token``. Once the token was consumed -
          by a ``call.accept`` moving the ring to the
          ``answer_pending`` sub-state (round 33: the ring is
          committed to the in-flight ``line.answer`` handshake,
          private kernel bookkeeping like the drain, with the same
          no-wire-marker rule and ``RINGING_LOCKED`` reported
          throughout) or by the fallback decision starting the
          kernel-internal ring-drain sub-phase (round 29,
          SA_quant_review_1
          slice-A item 1):
          a RE-report of the SAME physical ring returns ``None`` -
          the burned ``call_token`` is NEVER handed out again; and
          a GENUINELY NEW physical ring arriving while the drain is
          pending is recorded as the line's ``pending_ring`` and
          also returns ``None`` (no token exists for it yet) - when
          the drain completes the kernel mints a FRESH
          ``call_token`` for it and emits a new
          ``IncomingCallEvent`` through the normal fan-out, so a
          call-waiting redial can never inherit the burned token
          nor be silently dropped. Token delivery for the promoted
          ring (round 34, iteration-2 audit item 12): the provider
          parked its ring report on a ``{ok:true}`` reply with NO
          token, so promotion delivers the minted token down the
          provider's own channel - the kernel records it for
          piggy-back on the NEXT ``provider.ping`` reply as the
          ``pending_rings`` list (the same
          :class:`ProviderRegisterResult.pending_rings` shape the
          register response uses, bounded at 64 entries and
          cleared once delivered), and the provider echoes the
          token on every later fact for that ring from then on. A
          provider re-report of the still-ringing call arriving
          BEFORE the piggy-back lands dedupes by ring identity
          against the now-promoted ring and returns
          ``{ok, call_token}`` like any live-ring re-report - the
          two legs can never mint a second token. The parked
          ``pending_ring`` is bounded like every unmatched record
          (round 34, iteration-2 audit item 13): if the drain it
          waits on never completes, the parked entry expires with
          the drain's own ``ring_drain_ttl_sec`` budget - a
          ``disconnected`` fact for it while parked simply clears
          the record, and no ``call_token`` is ever minted for a
          ring whose report already died;
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
        return applies only to a ring on the LIVE binding id.

        Dedup identity (round 31, SA_swe2_review_1 item 20; epoch
        pinned round 32, SA_swe2_review_2 item 18): when
        ``ring.provider_call_id`` is present the physical-ring
        identity is EXACT - a second report carrying the same
        handle is the same ring (the same ``call_token`` comes
        back), a different handle is a genuinely new call no
        matter how little time elapsed. With the handle absent the
        kernel falls back to ``(line_id, caller_id)`` plus
        :data:`RING_DEDUP_MIN_GAP_SEC` measured from the
        ``observed_at`` of the ring's FIRST sighting - the dedup
        window is anchored at the original report, NOT re-armed on
        every repeated report, so a CLIR/withheld-number caller
        (``caller_id`` absent - every repeat shares the same
        ``None`` key) whose provider re-reports a still-ringing
        call cannot keep the dedup epoch sliding forward forever
        and mask a genuine hangup-and-redial at the window's
        edge: a same-number report inside the anchored window is
        treated as the same ring (dedup), one past it as a real
        hangup-and-redial. The residual
        ambiguity is documented - a fast same-number redial inside
        the gap merges into the prior ring - and an adapter that
        always fills ``provider_call_id`` removes it entirely."""
        ...

    @abstractmethod
    async def ingest_call_state(self, update: ProviderCallStateUpdate) -> None:
        """Call-state ingress: bounded ProviderCallState update mapped
        into lease phase transitions. Stale/out-of-order updates are
        deduped by observed_at; DISCONNECTED ends the lease - an
        ephemeral CallLease NEVER outlives its call (auto-release,
        round 22, review-10 HIGH-3, §5.4).

        Command-ack gate (round 33): an update that does NOT carry
        matching strong identity (the echoed ``lease_id``, or a
        ``provider_call_id``/``call_token`` the kernel recorded) is
        unqualified and MUST NOT end or re-attribute a lease still
        carrying :attr:`CallLease.pending_command` inside its
        bounded ``provider_command_ack_sec`` window - the provider
        has not acknowledged the command, so its absence-report
        cannot silently end the pending lease (snapshot omission
        follows the same gate - see
        :data:`SNAPSHOT_RECONCILED_PHASES`). The single exception
        is a ``disconnected`` fact carrying matching strong
        identity: a positive teardown report always wins over the
        pending handshake and ends the lease normally. For the
        sole-live-lease correlation of an unqualified update the
        positive-evidence floor of
        :class:`ProviderCallStateUpdate` applies - AT LEAST ONE of
        ``provider_call_id`` or normalized ``caller_id`` must be
        present on both sides and agree before the update binds.

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
        advance to ``DIALING``/``ENDED`` - an inbound ring
        preempting an undialed grant ends the lease
        ``ENDED``/``preempted_by_incoming`` while the ring lives on
        the line's ring slot (round 30, SA_carbon_review_19
        slice-A item 16 removed the former
        ``ACQUIRED -> RINGING`` edge - ``CallPhase.RINGING`` is a
        reserved member no lease may carry);
        ``DIALING`` -> ``ALERTING``/``ACTIVE`` (an alerting-less
        answer - the network may report ``active`` before an
        ``alerting`` leg propagates or without one, a legal
        provider fact the matrix must not reject; added in the
        round-28 fix, PR #72 review)/``TERMINATING``/``ENDED``;
        ``ANSWERING`` -> ``ACTIVE``/``TERMINATING``/``ENDED`` (round 31,
        SA_swe2_review_1 item 2 - the provider's answer
        confirmation or rejection on the accept path);
        ``ALERTING`` -> ``ACTIVE``/``TERMINATING``/``ENDED``; ``ACTIVE`` ->
        ``TERMINATING``/``ENDED``; ``TERMINATING`` -> ``ENDED``;
        ``ENDED`` is terminal. The ``TERMINATING`` legs out of the
        in-flight phases are the :meth:`hangup` path (round-31 fix
        round, PR #78 warning 1): a hangup on a possibly-live
        dialed call parks the lease in ``TERMINATING`` awaiting
        the provider's ``disconnected`` or the hold timeout, so
        every phase where a call leg may exist admits the edge.
        A REGRESSION is never legal - an
        ``ACTIVE`` lease can never return to ``DIALING``/``ALERTING``
        (a provider reporting its own state machine restart is a
        defect fact, dropped like an out-of-order update), and
        phase can never skip backwards over ``ENDED``.

        Ended-lease facts (round 30, SA_carbon_review_19 slice-B
        item 7): a ``call_state`` update whose ``lease_id`` resolves
        through the ``_closed_leases`` ring is a dedupe/no-op -
        never ``STALE_TOKEN`` and never an external-call mint.
        Burned-ring-token edges (round 30, SA_carbon_review_19
        slice-A item 6): while a line sits in the ring-drain
        sub-phase the echoed BURNED ``call_token`` stays a
        CORRELATION key for that ring's lifecycle facts - it is
        never an action token. A ``state="active"`` update
        correlated by the burned ring token means the handset
        answered during the drain: the kernel transitions the line
        to ``BUSY``/``external_call`` exactly like the live-ring
        handset-answer path (no lease, no PipeWire bridge, no
        ``line.hangup``). A ``disconnected`` update correlated by
        the echoed burned token ends that ``external_call`` - or
        the pending drain itself - and returns the line to
        ``FREE``, never ``STALE_TOKEN``.

        Instant ``BUSY -> FREE -> BUSY`` coalescing (round 28,
        review-18 item 16): when a DISCONNECTED teardown is
        immediately followed inside the same transaction by the
        grant of the head queue ticket, the outbox carries a single
        ``line_state_changed{new_state:"BUSY"}`` for the NEW lease
        - the intermediate ``FREE`` is never published (see
        :class:`LineStateChangedEvent`).

        Binding-level reasons (round 27, review-17 item 18; full
        cascade pinned round 35, iteration-3 audit item 16):
        ``reason=dongle_lost`` on a ``disconnected`` update is NOT a
        per-call detail - it reports tunnel/USB death of the whole
        attachment, so the kernel MUST additionally mark the
        binding UNREACHABLE (``event.line_health_changed``), tear
        down the binding's REMAINING leases with
        ``CallEndReason.PROVIDER_LOST``, cancel its pending
        tickets as ``queue_cancelled{reason: "provider_lost"}``,
        reject its pending claims as
        ``claim_resolved{status:"rejected",
        reason:"provider_lost"}``, burn a still-OPEN ring on the
        line through the ring lifecycle
        (``MissedCallEvent{reason:"provider_lost"}`` - the ring
        died with the provider, so the missed-call event reports
        the binding loss, not ``no_pickup``), release any
        ``external_call`` hold the binding reported, and
        tombstone the binding into ``_closed_assignments`` -
        the SAME unified consequences an UNREACHABLE liveness
        fact carries (``provider_lost`` is THE dead-binding
        reason on every axis - lease ends, queue cancels, claim
        rejects, ring misses - never a call-scoped
        ``network_error``/``dongle_lost`` mix), triggered here
        by the provider's own report. The dead-binding outbox
        purge applies identically: every queued command for the
        dead binding is dropped in the same mutation and NO
        ``ProviderHangupCommand`` is minted for the torn-down
        leases - a tombstoned binding can never be given more
        work. ``stack_restart``
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

        Dedup scope (round 29, SA_quant_review_1 slice-B item 5;
        re-scoped round 30, SA_carbon_review_19 slice-B item 8;
        per-identity split round 34, iteration-2 audit item 11):
        the ``observed_at`` dedup/ordering key is always evaluated
        scoped to ``(line_id, call_identity, accepted_stamp)`` where
        ``call_identity`` resolves in priority order to the update's
        ``lease_id``, then ``call_token``, then
        ``provider_call_id``, then the shared ``_unqualified``
        bucket - the per-IDENTITY monotonic watermark survives a
        reregister (it is stored per identity, not per binding), so
        a stamp replayed by a NEW binding still dedupes against the
        identity's recorded watermark while a fact for call B is
        never dropped by call A's newer stamp; facts naming a
        superseded ``assignment_id`` are already rejected
        ``STALE_TOKEN`` through the ``_closed_assignments`` ring
        before dedup is consulted. The ``_unqualified`` bucket
        dedupes on the ``(accepted_stamp, state)`` PAIR, not the
        stamp alone (round 35, iteration-3 audit item 8): two
        distinct call states sharing one coarse provider stamp are
        a legal transition sequence, not a duplicate - a provider
        clock ticking in whole seconds emits ``dialing`` then
        ``alerting`` under the same ``observed_at``, and dropping
        the second as a "repeat" would freeze the call's reported
        progression. Only a verbatim ``(stamp, state)`` repeat in
        the bucket is a duplicate. The identity table is BOUNDED
        (:data:`MAX_CALL_STAMP_WATERMARKS_PER_LINE` entries per
        line, FIFO-evicted at the cap) so a handset churning call
        identities can never grow kernel state unboundedly.
        Each watermark is MONOTONE-MAX per identity
        (round 30, SA_carbon_review_19 slice-B item 9): a verbatim
        PAST stamp below the recorded maximum stays a dedupe key
        for detecting repeats but NEVER regresses the ordering -
        an out-of-order fact is dropped as a duplicate, never
        applied backwards, and phase legality is governed solely
        by :data:`CALL_PHASE_FORWARD_TRANSITIONS`, not by stamp
        comparisons. The durable
        :attr:`ProviderBinding.call_stamp_watermark` field
        (round 33) - persisted verbatim on the row, restored by
        ``hydrate_lines``, and inherited by a reregistered
        binding at the line's binding scope - survives only as the
        RESTART REPLAY FLOOR for facts naming NO live call
        identity; it never decides ordering between two live
        identities.

        Floor restriction (round 35, iteration-3 audit item 8):
        the durable ``call_stamp_watermark`` floor is restart-replay
        evidence, never proof a call never existed - it drops an
        unqualified fact ONLY when the fact is STRICTLY below the
        floor AND can mint no new state: an unqualified
        ``disconnected`` or a verbatim ``(stamp, state)`` repeat at
        or below the floor is dropped (it produces nothing), while
        a call-ESTABLISHING unqualified state
        (``dialing``/``alerting``/``active``) is never suppressed
        by the floor - a genuinely new handset call must not be
        silenced by watermark evidence, so it mints the
        ``external_call`` hold anyway and the
        :data:`UNMATCHED_CALL_SAFETY_SEC` deadline bounds the
        occasional false positive that a suppressed real call
        would have wedged silently.

        Occupancy is a UNION of holds (round 35, iteration-3 audit
        item 9): a line's ``BUSY`` is the union of every live hold
        on it - agent lease, ``external_call`` hold, ring record.
        A ``disconnected`` ending one hold while another still
        lives keeps the line ``BUSY`` - the held-reason column
        (``lease`` vs ``external_call``) shifts without a FREE
        blip, and ``line_state_changed{new_state:"FREE"}`` fires
        ONLY when the LAST hold ends. Correlated ``external_call``
        safety (same item): a hold minted WITH a correlation
        handle (``provider_call_id`` or a burned ``call_token``)
        still carries a reconciliation guarantee - it reconciles
        against ``state_snapshot`` like a reconciled-phase lease
        (a handle absent from an authoritative snapshot is
        released), and it arms the SAME
        :data:`UNMATCHED_CALL_SAFETY_SEC` deadline as an
        unmatched hold, REFRESHED on each provider fact bound to
        the hold's handle - a live reported call keeps itself
        alive while a terminal fact lost in flight is swept
        within one window of the last fact; an unmatched hold
        carries no handle to refresh against, so its mint-time
        deadline stands alone.

        Tokenless facts on a ``RINGING_LOCKED`` line (round 35,
        iteration-3 audit item 12): an unqualified
        call-ESTABLISHING fact (``dialing``/``alerting``/
        ``active``) arriving on a ``RINGING_LOCKED`` line is the
        handset-answer case - it mints ``BUSY``/``external_call``
        with no lease, burning the open ring's ``call_token`` as
        its correlation handle exactly like the token-echoed
        ``active`` above; when no open ring exists but a
        ``pending_ring`` record is parked, the fact mints the
        same hold consuming the parked record as correlation
        evidence (its ``provider_call_id`` becomes the handle
        when present, otherwise the hold is unmatched and the
        safety deadline applies). A tokenless ``disconnected``
        on ``RINGING_LOCKED`` resolves in deterministic priority:
        (1) the open ring first - it ends the ring, burns
        ``MissedCallEvent``, and drains like the by-line rule
        above; (2) absent an open ring, the parked
        ``pending_ring`` record is cleared (its provider-reported
        ring died before promotion); (3) absent both, a live
        ``external_call`` hold on the line is released; (4)
        nothing matched - the fact is recorded as an unmatched
        fact under the safety-net rule and dropped, never
        ``STALE_TOKEN``."""
        ...

    @abstractmethod
    async def ingest_audio_ready(
        self, line_id: LineId, assignment_id: AssignmentId, lease_id: LeaseId
    ) -> None:
        """provider.audio_ready ingress (round 14): the provider's local
        BT SCO path is up for ``lease_id`` -> emit AudioReadyEvent to
        the lease owner AND set the kernel-side ``audio_ready``
        latch :meth:`send_dtmf` gates on (round 27, review-17 item
        53; latch is kernel state, not a lease field - round 31,
        SA_swe2_review_1 item 4). One-shot per lease per
        media-attachment epoch; a duplicate or unknown-lease report
        is dropped (idempotent) -
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
        :meth:`ingest_call_state`), never a silent accept.

        Fact consistency (round 31, SA_swe2_review_1 item 33):
        ``line_id`` must be the lease's OWN line - a
        ``provider.audio_ready`` whose ``line_id`` names a different
        line than the lease's is dropped, because the SCO path is a
        physical line resource and a cross-line audio fact cannot
        be true. And the fact is gated by phase: only a lease in a
        call-bearing phase (``DIALING``/``ANSWERING``/``ALERTING``/
        ``ACTIVE``) may raise the latch - an ``ACQUIRED``
        (granted-but-undialed) lease has no call leg for SCO to
        serve, so ``audio_ready`` on it is dropped like a duplicate."""
        ...

    @abstractmethod
    async def ingest_audio_lost(
        self,
        line_id: LineId,
        assignment_id: AssignmentId,
        lease_id: LeaseId | None = None,
    ) -> None:
        """provider.audio_lost ingress (round 31, SA_swe2_review_1
        item 4; wire ``{line_id, assignment_id, lease_id?}``): the
        provider's BT SCO path dropped while its control channel
        stayed up. The kernel RESETS the per-lease ``audio_ready``
        latch - the kernel-side flag :meth:`send_dtmf` gates on -
        so a mid-call SCO flap revokes the DTMF/early-media
        capability until a fresh ``provider.audio_ready`` restores
        it, and the media pipeline can be restarted by a
        :meth:`reattach_media`/:meth:`reattach_provider_media`
        cycle. ``lease_id=None`` is the line-scoped form (the SCO
        path serves the LINE): the latch resets on every live lease
        of that line.

        TTL separation (round 32, SA_swe2_review_2 item 11;
        mint-side arming updated round 34, iteration-2 audit item
        9): an ``audio_lost`` fact RESETS the latch ONLY - it does
        NOT arm the :data:`MEDIA_PAIR_TTL_SEC` re-attach deadline.
        That deadline is armed by allocation events only: an
        initial pair minted for ``line.dial``/``line.answer``
        allocation arms when the provider's ``{accepted: true}``
        command acknowledgement lands (the
        ``provider_command_ack_deadline_at`` backstop bounds the
        pre-arm window - a pair whose handshake never completes
        dies with the lease instead of burning TTL on credentials
        nobody received), a ``MediaAllocator.reissue()`` pair -
        including the ``provider.reregister`` re-pair - arms at
        the reissue instant, and an
        :meth:`ingest_media_state` ``attached=False`` socket
        fact arms the re-attach window for the detached role. A
        SCO flap reported while the media socket is still
        attached leaves any running TTL untouched - the socket
        detaching (or not) is the fact that decides the lease's
        fate, not the radio-path report. ``audio_lost`` also
        NEVER emits :class:`MediaOverflowEvent` - that event is
        the media-router's PCM-drop signal
        (:meth:`ingest_media_overflow`), not an SCO-loss one;
        a repeated ``audio_lost`` inside one loss interval is
        still deduplicated to a no-op on the latch.

        Same id/hygiene rules as :meth:`ingest_audio_ready`:
        ``_check_line_id`` on ``line_id``, ``_check_id`` on
        ``lease_id``/``assignment_id``, ``STALE_TOKEN`` on a
        superseded binding, ``line_id`` must be the lease's own
        line (or, for the line-scoped form, the binding's line),
        and the fact is a no-op (idempotent) when the latch was
        already reset or the lease already ended."""
        ...

    @abstractmethod
    async def ingest_media_state(self, lease_id: LeaseId, role: MediaRole, attached: bool) -> None:
        """Media-side attachment fact ingress (round 30,
        SA_carbon_review_19 slice-A item 1): the media adapter
        reports when ONE role of a lease's media pair attaches or
        detaches.

        The adapter MUST call it with ``attached=True`` after a
        successful ``media.auth`` for that role and with
        ``attached=False`` when that role's media socket closes.
        A detach arms a fresh :data:`MEDIA_PAIR_TTL_SEC` re-attach
        deadline for the role (kernel-clock, of course) - an
        ``attached=True`` fact for the same ``(lease_id, role)``
        disarms it again, and ``MediaAllocator.reissue()`` /
        ``reattach_media``/``reattach_provider_media`` RE-ARMS a
        fresh 30 s window counted from the reissue instant. When
        a role's detach window lapses without a reattach,
        ``tick(now)`` ends the lease with
        ``CallEndReason.NETWORK_ERROR`` - a live call never
        silently loses its audio channel, mirroring the
        dead-man-switch teardown semantics.

        Latch reset on provider-role detach (round 34, iteration-2
        audit item 23): an ``attached=False`` fact for
        ``role="provider"`` additionally RESETS the lease's
        kernel-side ``audio_ready`` latch in the same mutation -
        the SCO path the latch attested is gone with the provider's
        media socket, so ``send_dtmf`` MUST NOT keep accepting
        tones against a dead uplink until a fresh
        ``provider.audio_ready`` re-arms it (the same reset
        :meth:`ingest_audio_lost` performs; an agent-role detach
        never touches the latch - the SCO uplink is the
        provider's side). Phase gate (round 32,
        SA_swe2_review_2 item 26): a fact on an ``ACQUIRED`` lease
        is ``PhonebridgeError(ErrorCode.INVALID_STATE)`` - the
        grant is still unspent, no media pair exists to attach.
        A fact for an
        unknown/already-ended ``lease_id`` resolves through the
        ``_closed_leases`` ring as a dedupe/no-op or
        ``PhonebridgeError(ErrorCode.STALE_TOKEN)`` - never an
        external-call mint, never a silent accept."""
        ...

    @abstractmethod
    async def ingest_media_overflow(self, line_id: LineId, lease_id: LeaseId) -> None:
        """Media-router overflow ingress (round 32,
        SA_swe2_review_2 item 11): the media adapter's bounded
        uplink buffer dropped PCM frames for ``lease_id`` on
        ``line_id`` (the ``0x04``-frame path of design §6.4 - the
        socket is ALWAYS read, drop-NEWEST applies, and the
        adapter reports the episode to the kernel). The kernel
        emits at most ONE :class:`MediaOverflowEvent` per
        contiguous overflow interval (per epoch between two
        non-overflowing windows) - repeated reports inside one
        episode dedupe, never one event per dropped frame, so a
        saturated media path cannot overflow the domain outbox
        too.

        This is the ONLY ``event.media_overflow`` source: the
        SCO-loss path (:meth:`ingest_audio_lost`) never emits it
        - a BT-side flap is latch bookkeeping, not a router
        buffer drop. The fact carries no teardown effect - the
        lease's fate stays governed by socket attach/detach
        facts and the :data:`MEDIA_PAIR_TTL_SEC` deadline. Same
        id/hygiene rules as :meth:`ingest_media_state`: unknown
        or ended ``lease_id`` resolves through the
        ``_closed_leases`` ring as dedupe/no-op or
        ``STALE_TOKEN``."""
        ...

    @abstractmethod
    async def compensate_dial(
        self, line_id: LineId, assignment_id: AssignmentId, lease_id: LeaseId
    ) -> None:
        """Dial compensation entry point (round 31,
        SA_swe2_review_1 item 5; ``line.answer`` split out to
        :meth:`compensate_answer` round 32, SA_swe2_review_2 item 9 -
        residual wording corrected round 34, iteration-2 audit item
        23): the adapter's FIRST call after a
        ``line.dial`` reply came back
        ``{accepted: false}``, timed out inside the ~2 s reply
        deadline, or hit a transport error - a failed
        ``line.answer`` handshake takes :meth:`compensate_answer`
        instead (its ring-scoped reject is a different vehicle).
        The kernel enqueues a
        best-effort ``ProviderHangupCommand`` for the lease's line
        (air-side teardown of the possibly-live call the provider
        may have started) and ends the lease with
        ``CallEndReason.NETWORK_ERROR`` in the same atomic step -
        ``DialResult`` promised an in-flight leg and
        the provider just refused it, so the lease must die, not
        idle.

        Why a dedicated entry point (split from ``hangup``, round
        30 sequencing hardened round 31): the agent-facing
        :meth:`hangup` is OWNER-scoped - the adapter holds no agent
        ``session_id`` and must not forge one - and its
        semantics are "agent asked to end a live call", whereas a
        rejected dial is a PROVIDER-fact teardown. This method is
        binding-scoped instead: ``assignment_id`` must resolve to
        the LIVE binding (a superseded id resolves through the
        ``_closed_assignments`` ring as
        ``PhonebridgeError(ErrorCode.STALE_TOKEN)``), ``line_id``
        must be the lease's own line, and ``lease_id`` must be a
        live lease in a dial/answer-in-flight phase
        (``DIALING``/``ANSWERING``/``ALERTING``/``ACTIVE`` - the
        provider can reject late). A lease that already ended is a
        dedupe/no-op through ``_closed_leases`` (a rejected-dial
        report racing a real teardown must not resurrect anything).
        On an ``ACQUIRED`` lease the call is ``INVALID_STATE`` -
        no dial was dispatched for it, so there is nothing to
        compensate. A lease already in ``TERMINATING`` is a
        dedupe/no-op (round 32, SA_swe2_review_2 item 26 - a late
        rejected-dial report racing a queued ``line.hangup`` must
        not mint a second hangup command nor re-end the lease;
        its provider-fact or ``hangup_hold_sec`` lapse closes it).

        Dead-channel compensation (round 35, iteration-3 audit
        item 3): the saga path obeys the same dead-binding
        command ban as :meth:`hangup` - when the lease's binding
        already sits ``ws_disconnected``/dead, NO
        ``ProviderHangupCommand`` is minted (a best-effort
        command has no socket to ride and the outbox would
        drain it into nothing). The lease outcome is unchanged:
        it still ends ``network_error`` in the same atomic step -
        the compensation's own verdict, not the command, is what
        ends the lease - and because the lease ENDS rather than
        parking ``TERMINATING``, there is no durable command
        intent for a ``provider.reregister`` to re-mint; the
        modem-side call is left to the provider's own
        reconnect/caller-side cleanup exactly like the
        dead-binding outbox purge defines (the orphan-grace
        lapse would take the ``provider_lost`` cascade for any
        lease still alive, so a refused-dial lease dying
        ``network_error`` first is consistent, not a second
        reason channel).

        Synthesizing a ``disconnected``
        :meth:`ingest_call_state` fact remains FORBIDDEN here
        too (the provider never reported one); the
        ``ProviderHangupCommand`` outbox effect IS the teardown
        and a real ``disconnected`` fact arriving later dedupes
        against the ended lease."""
        ...

    @abstractmethod
    async def compensate_answer(
        self,
        line_id: LineId,
        assignment_id: AssignmentId,
        lease_id: LeaseId,
        call_token: CallToken,
    ) -> None:
        """Ring-scoped answer compensation (round 32,
        SA_swe2_review_2 item 9): the adapter's FIRST call after a
        ``call.accept``/``line.answer`` reply came back
        ``{accepted: false}``, timed out inside the ~2 s reply
        deadline, or hit a transport error. The kernel enqueues a
        best-effort RING-scoped reject -
        ``ProviderHangupCommand`` with ``call_token`` set and
        ``lease_id`` ``None`` (the frozen reject vehicle: oFono
        ``VoiceCall.Hangup`` on a still-ringing call IS a reject;
        the wire deliberately carries no separate ``reject``
        command, review-12 C1) - ends the ``ANSWERING`` lease the
        accept minted, and keeps the line ``RINGING_LOCKED`` under
        the ring-drain rule.

        Distinction from :meth:`compensate_dial`: dial
        compensation is LINE-freeing - the lease ends and the
        line returns FREE in the same step because the kernel
        itself initiated the outbound leg. Answer compensation is
        RING-preserving: the physical inbound ring can outlive the
        failed answer (the ``accepted: false`` reply does NOT
        prove the carrier-side call is gone, and the queued
        reject can itself be lost), so the line MUST NOT be freed
        early - it stays ``RINGING_LOCKED`` until the provider's
        ``disconnected`` report or the ``ring_drain_ttl_sec``
        lapse in ``tick`` resolves the drain, exactly like the
        post-``MissedCallEvent`` drain window. ``lease_id`` names
        the ``ANSWERING``/``ALERTING`` lease the failed accept
        minted - it ends in the same atomic step - while
        ``call_token`` scopes the reject to the ring itself
        (the original ``call_token`` or the re-issued
        ``fallback_call_token``; same BY-LINE rule as
        :attr:`ProviderHangupCommand.call_token`).

        Binding/line correlation: ``assignment_id`` must resolve
        to the LIVE binding (superseded ids hit the
        ``_closed_assignments`` ring as
        ``PhonebridgeError(ErrorCode.STALE_TOKEN)``), ``line_id``
        must own both the lease and the ring, and the ring must
        still be open. An already-ended lease or closed ring is a
        dedupe/no-op (a lost-answer report racing a real
        ``disconnected`` must not resurrect or double-reject); a
        ``call_token`` the line never held is ``STALE_TOKEN``.
        A lease already in ``TERMINATING`` is likewise a
        dedupe/no-op (round 32, SA_swe2_review_2 item 26 - the
        same rule :meth:`compensate_dial` carries): a late
        rejected-answer report racing a queued ``line.hangup``
        mints NO second ring-scoped reject and never re-ends the
        lease - its provider fact or ``hangup_hold_sec`` lapse
        closes it.

        Dead-channel compensation (round 35, iteration-3 audit
        item 3): the same dead-binding command ban applies -
        when the ring's binding already sits
        ``ws_disconnected``/dead, NO ring-scoped
        ``ProviderHangupCommand`` is minted. The lease still
        ends in the same atomic step and the line still stays
        ``RINGING_LOCKED`` under the ring-drain rule, but the
        OPEN RING itself is the durable reject intent: a
        successful ``provider.reregister`` inside the orphan
        grace re-mints the ring-scoped reject
        (``ProviderHangupCommand{call_token}``) while the ring
        remains undrained, and a ring that drains on its
        ``ring_drain_ttl_sec`` or the orphan-grace cascade with
        no live channel simply never sends one - exactly the
        ``TERMINATING`` re-mint model :meth:`hangup` defines,
        carried by ring state instead of a parked lease.

        Post-ACTIVE answer compensation (round 35, iteration-3
        audit item 2): ``ANSWERING -> ACTIVE`` is legal WHILE the
        ``line.answer`` handshake is still open - the provider's
        ``active`` fact advances the phase without settling the
        pending command (see :attr:`CallLease.pending_command`).
        A refused/timed-out/lost handshake landing on that lease
        is NO LONGER ring-scoped: the provider has already
        attested the call is up, so the ring's fate was committed
        and the compensation switches to the LEASE-scoped
        teardown :meth:`compensate_dial` defines - a best-effort
        ``ProviderHangupCommand{lease_id}`` (air-side teardown of
        the live call the provider just confirmed), the lease
        ending ``network_error``, the line returning
        ``BUSY -> FREE`` in the same mutation, and
        ``event.call_state_changed{state:"disconnected"}`` to the
        owning session. It NEVER returns the line to
        ``RINGING_LOCKED`` and NEVER emits ``missed_call`` -
        ``missed_call`` describes an unanswered ring, and this
        call was confirmed active; the withheld ``call.accept``
        reply still answers ``PROVIDER_UNREACHABLE``.
        Synthesizing a ``disconnected``
        :meth:`ingest_call_state` fact stays FORBIDDEN here too -
        the queued reject command IS the teardown, and the drain
        closes on real provider facts only."""
        ...

    @abstractmethod
    async def ingest_command_ack(self, ack: ProviderCommandAck) -> None:
        """Provider-command acknowledgement ingress (round 33): the
        typed kernel fact for the ``{accepted, provider_call_id?,
        reason?}`` reply the provider returned to a down-channel
        ``line.dial``/``line.answer``. The adapter reports the
        distilled reply through this method within the line's
        effective ``provider_command_ack_sec`` deadline - a timeout
        or transport error carries no reply, so those take the
        :meth:`compensate_dial`/:meth:`compensate_answer` entry
        points instead.

        Correlation: ``ack.assignment_id`` must resolve to the LIVE
        binding (a superseded id resolves through the
        ``_closed_assignments`` ring as
        ``PhonebridgeError(ErrorCode.STALE_TOKEN)`` - the same
        superseded-binding rule as every ``provider.*`` fact),
        ``ack.line_id`` must own the lease, and ``ack.lease_id``
        must resolve to a live lease whose
        :attr:`CallLease.pending_command` equals ``ack.command``.
        An ack for a lease that already ended or already resolved
        its handshake is a dedupe/no-op through ``_closed_leases``
        (a late reply racing a compensation must never resurrect or
        double-apply); an ack that does not match the pending
        command - ``"answer"`` arriving on a dial-pending lease,
        or any ack with no pending marker at all - is dropped as a
        stale/foreign fact, never applied.

        Pending-command teardown atomicity (round 33, PR #82
        review): whenever a lease leaves ``DIALING``/``ANSWERING``
        through ANY path - agent :meth:`hangup`/:meth:`release`/
        :meth:`drop_session`, provider or binding death, adapter
        compensation, or a ``tick`` deadline - ``pending_command``
        and ``provider_command_ack_deadline_at`` are cleared
        atomically with the teardown, and a command
        acknowledgement arriving afterwards is STALE: it resolves
        through ``_closed_leases`` as the same dedupe/no-op above
        and can never resurrect or mutate the torn-down lease. A
        pending ``"dial"`` teardown still queues the lease-scoped
        :class:`ProviderHangupCommand`; a pending ``"answer"``
        teardown queues the RING-scoped
        ``ProviderHangupCommand{call_token}`` reject. The withheld
        ``call.dial``/``call.accept`` wire reply stays
        handshake-bound while the caller session is alive -
        BUT the teardown-race outcome is TERMINAL, never a
        dead-credential success (round 34, iteration-2 audit item
        10): ``{accepted: true}`` whose lease is STILL LIVE at
        reply time answers the normal
        ``DialResult``/``AcceptResult``; ``{accepted: true}``
        arriving for a lease the kernel already tore down - the
        closed-lease dedupe case above, where a provider
        ``disconnected``, an agent ``hangup``/``release``/
        ``drop_session``, binding death, or a ``tick`` deadline
        ended the lease between dispatch and reply - answers
        ``PhonebridgeError(ErrorCode.PROVIDER_UNREACHABLE)``
        instead of a ``DialResult``/``AcceptResult`` carrying
        media credentials. The media pair is revoked with the
        lease, so a "success" reply at that point would hand the
        agent dead credentials plus a ``lease_id`` that resolves
        to nothing - the teardown won the race and the reply
        MUST report it. Every handshake failure therefore
        collapses to the ONE terminal code -
        refused/timeout/unreachable and the teardown-race case all
        answer ``PROVIDER_UNREACHABLE`` - and a session that died
        before the handshake resolved gets no response at all.

        Answer-failure reporting (round 34, item 10): a failed
        ``call.accept`` NEVER exposes a ``lease_id`` - the
        withheld ``AcceptResult`` is the only channel that id
        could ride, so an error reply carries none - and the
        ring's outcome still reaches the answering session
        through the ring-lifecycle channel: the drain resolution
        mints the ring's ``event.missed_call`` exactly like a
        no-pickup ring (the failed answer burned the token but
        did not change that the call was never taken), so the
        agent observes the call it tried to take ending rather
        than a silently vanished ring.

        ``{accepted: true}`` effects: the kernel clears
        ``pending_command`` (releasing the command-ack gate - the
        lease becomes snapshot-reconcilable under the
        :data:`SNAPSHOT_RECONCILED_PHASES` membership rule) and, on
        the ``"dial"`` path, records ``ack.provider_call_id`` as
        the lease's durable external-handle evidence
        (:attr:`CallLease.provider_call_id` - the value the pending
        ``call.dial`` wire reply mirrors into
        :attr:`DialResult.provider_call_id`). An accepted
        ``"answer"`` commits the consumed ring - the
        ``answer_pending`` sub-state resolves, the token stays
        burned, and the lease remains ``ANSWERING`` until provider
        ``active``/``disconnected`` facts drive the phase. The ack
        itself NEVER advances a phase: ``{accepted}`` means "the
        provider took the command", not "the call progressed" -
        only provider call-state facts move phases.

        ``{accepted: false}`` effects: the refusal performs the
        IDENTICAL effects the matching compensation entry point
        carries, inside one mutation - for ``"dial"`` a best-effort
        lease-scoped ``ProviderHangupCommand`` then the
        ``network_error`` lease end and line-free path of
        :meth:`compensate_dial`; for ``"answer"`` the ring-scoped
        reject (``call_token`` set, ``lease_id`` ``None``) then the
        ``ANSWERING`` lease end of :meth:`compensate_answer`, the
        line staying ``RINGING_LOCKED`` under the drain rule. The
        dead-binding command ban applies identically (round 35,
        iteration-3 audit item 3): no ``ProviderHangupCommand`` is
        minted onto a binding that sits ``ws_disconnected``/dead;
        the lease end is unchanged, and a still-open ring's
        ring-scoped reject re-mints on a successful
        ``provider.reregister`` while the ring remains undrained.
        Phase-conditional (round 35, iteration-3 audit item 2):
        an ``"answer"`` refusal landing on a lease the provider
        already advanced to ``ACTIVE`` takes the LEASE-scoped
        arm instead (``ProviderHangupCommand{lease_id}``,
        ``network_error`` end, ``BUSY -> FREE``,
        ``call_state_changed`` - never ``missed_call``, never
        back to ``RINGING_LOCKED``) because the provider's own
        ``active`` fact already committed the call.
        ``ack.reason`` is recorded as bounded diagnostic input only
        - it is never synthesized into a provider fact and never
        echoed raw into agent-facing ``detail`` text."""
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
        "no live session" is never an immediate-fallback trigger
        (a zero-member ring carrying configured wake hooks emits its
        ``incoming_call`` through the typed :class:`WakeAudience`
        audience, round 33).

        Answer-pending suppression (round 33): the decision is legal
        ONLY on a ring still in the ``ringing`` sub-state. Once
        ``call.accept`` consumed the token and moved the ring to
        ``answer_pending``, ``tick`` MUST NOT run this decision for
        it - the ``line.answer`` handshake owns the ring's fate
        inside its bounded ``provider_command_ack_sec`` window; a
        direct call on an answer-pending ring (or a burned/draining
        one) resolves ``PhonebridgeError(ErrorCode.INVALID_STATE)``.
        A failed answer handshake ends the ``ANSWERING`` lease via
        :meth:`ingest_command_ack`/:meth:`compensate_answer` and the
        ring stays consumed under the drain rule - it NEVER
        re-enters this decision: the fallback window burned with the
        token, so no path resurrects a second decision for the same
        physical ring.

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

        Dead-man teardown through ``TERMINATING`` (round 35,
        iteration-3 audit item 6): the dead-man switch on a lease
        in an in-flight phase (``DIALING``/``ANSWERING``/
        ``ALERTING``/``ACTIVE``) is hangup-intent like
        :meth:`release` - the kernel queues
        ``ProviderHangupCommand``, parks the lease
        ``TERMINATING`` with ``agent_disconnected`` recorded as
        the pending end-reason, and the provider's
        ``disconnected`` fact or the ``hangup_hold_sec`` lapse
        closes it (an in-flight call must never be treated as
        instantly ended - the air-side teardown is real work
        that can itself fail). An ``ACQUIRED`` grant ends
        directly ``ENDED`` with ``agent_disconnected`` - no air
        leg exists to confirm. The dead-binding variant applies
        too: a session drop while the binding sits
        ``ws_disconnected`` mints no command (see the
        dead-channel teardown rule on :meth:`hangup`), and a
        binding that died already ends the lease
        ``provider_lost`` via the orphan cascade instead.

        Media teardown ordering (round 29, SA_quant_review_1
        slice-A item 9): the dead-man lease teardown calls
        ``MediaAllocator.revoke(lease_id)`` INSIDE this same
        mutation - the allocator side effects complete before
        ``drop_session`` returns, so a ``call_state_changed{
        disconnected}`` event drained afterwards can never precede
        the media teardown it announces; no socket outlives its
        ``lease_id`` even for an instant observable outside the
        mutation.

        Pending-command and in-flight-idempotency teardown (round
        33, PR #82 review): the dead-man teardown of a lease still
        carrying an armed :attr:`CallLease.pending_command` clears
        ``pending_command``/``provider_command_ack_deadline_at``
        atomically and queues the scoped
        :class:`ProviderHangupCommand` (lease-scoped for a pending
        ``"dial"``, RING-scoped ``call_token`` for a pending
        ``"answer"``) - a late command acknowledgement is stale
        and can never resurrect or mutate the torn-down lease (see
        :meth:`ingest_command_ack`). Session death also KILLS the
        withheld ``call.dial``/``call.accept`` wire reply for an
        unresolved handshake (a dead session is owed no response)
        and kills every in-flight ``idempotency_key`` waiter the
        session held: the dedup table dies with the session, so an
        in-flight join never outlives it and no dedup survives a
        reconnect."""
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

        ``max_call_duration_sec`` (round 28, review-18 item 7;
        ``ANSWERING`` folded in round 31, SA_swe2_review_1 item 2;
        TERMINATING routing round 35, iteration-3 audit item 6):
        when the line's effective cap is set, a lease in
        ``DIALING``/``ANSWERING``/``ALERTING``/``ACTIVE``/
        ``TERMINATING`` crossing
        ``now >= lease.created_at + max_call_duration_sec`` is torn
        down with ``reason: "max_call_duration"`` (the
        :class:`CallEndReason` member) - the cap bounds one call's
        wall-clock length, including a ``record_and_telegram``
        worker recording and a wedged answer/teardown leg.
        ``ACQUIRED`` is exempt (its ``grant_expires_at`` already
        bounds the undialed grant). A still-in-flight lease the cap
        catches routes through ``TERMINATING`` like every other
        kernel-initiated air-side teardown (round 35): the kernel
        queues ``line.hangup``, records ``max_call_duration`` as
        the pending end-reason on the TERMINATING entry, and the
        provider's ``disconnected`` or the ``hangup_hold_sec``
        lapse closes it - the recorded reason is a
        kernel-attributed cause that a later provider report does
        NOT override (the call ended BECAUSE of the cap). A lease
        already ``TERMINATING`` when its cap lands ends on the
        earlier of the two deadlines with the recorded reason
        ``max_call_duration`` when the cap fired first.
        Ring-drain best-effort ``line.hangup`` restriction (item
        9): the drain MAY queue ``line.hangup`` only on paths where
        the GATEWAY rejected the call (``reject`` fallback
        policy) - never on ``no_pickup``/missed outcomes, where
        the phone's own carrier-side end clears the ring.

        ``TERMINATING`` hold lapse (round 32, SA_swe2_review_2 item
        1; parking set widened round 35, iteration-3 audit item
        6): a lease parked in ``TERMINATING`` by any agent-or-
        kernel-initiated air-side teardown - :meth:`hangup`,
        :meth:`release` on an in-flight lease, :meth:`drop_session`'s
        dead-man teardown, the ``max_call_duration`` cap above -
        carries a private
        hold deadline stamped at transition time; at
        ``now >= hold_deadline`` ``tick`` ends it with
        ``CallEndReason.NETWORK_ERROR`` as a KERNEL-side timeout
        fact - never a synthesized provider ``disconnected``
        report, which would fabricate a provider fact - frees the
        line and lets the queue advance. The bound comes from the
        line's effective ``hangup_hold_sec`` (default 10 s, tight
        1..120 s range), NOT ``max_call_duration_sec``: a lost or
        wedged ``line.hangup`` can never leave the lease (and the
        line's FIFO) wedged in ``TERMINATING`` past the hold.

        ``TERMINATING -> ENDED`` reason precedence (round 35,
        iteration-3 audit item 16): every ``TERMINATING`` entry
        records a pending end-reason at parking time -
        ``agent_hangup`` for ``call.hangup``/``call.release``,
        ``agent_disconnected`` for :meth:`drop_session`,
        ``max_call_duration`` for the cap, ``network_error`` for a
        tick/media deadline teardown. The emitted reason on the
        closing ``call_state_changed{state:"disconnected"}`` is
        resolved as: (1) a provider ``disconnected`` fact arriving
        inside the hold carries the air-side truth - its reported
        ``reason`` wins when the teardown was AGENT-initiated
        (hangup/release/drop_session - the provider may know the
        remote already ended); (2) KERNEL-attributed trigger
        reasons (``max_call_duration``, ``provider_lost``,
        ``network_error``) are authoritative and are never
        overridden - the recorded cause IS why the call died;
        (3) a hold lapse emits ``network_error`` (kernel timeout
        fact) as pinned above; (4) absent a provider reason an
        agent-initiated teardown emits its recorded agent reason -
        kernel teardown of an agent-requested hangup defaults to
        ``agent_hangup``, not an undefined value.
        ACQUIRED-end events (round 35, item 16): EVERY lease end
        emits ``event.call_state_changed{state:"disconnected",
        reason}`` to the owning session, including the
        ``ACQUIRED`` ends that never held an air leg -
        ``grant_ttl_expired`` on grant-TTL lapse,
        ``preempted_by_incoming`` on ring preemption,
        ``agent_hangup`` on ``call.hangup``/``call.release`` of an
        undialed grant, ``agent_disconnected`` on
        ``drop_session``, ``provider_lost`` on binding death -
        a grant silently dying without any event would leave the
        agent holding a lease id that resolves to nothing.

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
        The ``decide_fallback`` half of the ring-timer pair runs
        ONLY while the ring sits in the ``ringing`` sub-state
        (round 33): a ring consumed by ``call.accept`` moves to
        ``answer_pending`` inside the ``line.answer`` handshake
        window, and ``tick`` MUST NOT run the fallback decision
        for it - the bounded ack deadline owns the ring's fate
        until the reply (or the lapse) resolves it.

        Command-ack deadline class (round 33): a lease carrying
        :attr:`CallLease.pending_command` whose stamped
        ``provider_command_ack_sec`` window lapsed without an
        :meth:`ingest_command_ack`/:meth:`compensate_dial`/
        :meth:`compensate_answer` report gets the IDENTICAL
        terminal effect the matching compensation path carries -
        ``"dial"`` ends the lease ``network_error`` after a
        best-effort lease-scoped ``ProviderHangupCommand`` and
        frees the line; ``"answer"`` ends the ``ANSWERING`` lease
        ``network_error`` after the ring-scoped reject and keeps
        the line ``RINGING_LOCKED`` under the drain rule. The
        lapse is a kernel-side timeout fact, never a synthesized
        provider reply, and the deadline counts against
        :data:`MAX_TICK_DEADLINES` like every other class - the
        adapter's bounded await is the fast path, this lapse is
        the backstop that keeps an ack-pending lease from wedging
        when no adapter call ever arrives. Pending-command
        teardown atomicity (round 33, PR #82 review): EVERY
        tick-driven end of a command-pending lease - the ack
        lapse above, ``max_call_duration_sec``, a media-pair
        deadline, the orphan cascade, or a ``hangup_hold_sec``
        lapse racing the armed handshake - clears
        ``pending_command``/``provider_command_ack_deadline_at``
        atomically with the teardown, and a command
        acknowledgement arriving afterwards is STALE: a
        dedupe/no-op through ``_closed_leases`` that can never
        resurrect or mutate the torn-down lease (see
        :meth:`ingest_command_ack`).
        ``tick`` also drives idle bookkeeping: an EXCLUSIVE
        line whose ``owner_agent_id`` holds at least one open
        session is never idle; ``last_seen_at`` is stamped by
        ``drop_session`` closing the owner's last session and by
        OWNER line operations only (round 30, SA_carbon_review_19
        slice-A item 4 - provider facts and ``configure_line``
        never stamp it; see :attr:`LineAssignment.last_seen_at`).

        Media deadlines (round 30, SA_carbon_review_19 slice-A
        item 1; handshake leg folded in round 31,
        SA_swe2_review_1 item 21; arming instant corrected round
        34, iteration-2 audit item 9): ``tick`` enforces
        :data:`MEDIA_PAIR_TTL_SEC` at BOTH ends of a pair's life.
        On the handshake side the expiry clock is measured from
        the pair's ARM instant, not its mint: an initial pair
        minted for ``line.dial``/``line.answer`` allocation arms
        only when the provider's ``{accepted: true}`` command
        acknowledgement lands (the
        ``provider_command_ack_deadline_at`` backstop bounds the
        pre-arm window - a pair whose handshake never resolves
        dies with the lease, never expires mid-handshake), while
        a pair minted by ``MediaAllocator.reissue()`` - including
        the ``provider.reregister`` re-pair - arms at the reissue
        instant; a pair that is still un-``media.auth``-ed when
        its armed ``MEDIA_PAIR_TTL_SEC`` window lapses is
        expired -
        the lease ends with ``CallEndReason.NETWORK_ERROR``,
        because a caller that never received its audio path
        cannot be served. On the detach side a lease whose
        detached media role outlived its re-attach window (armed
        by :meth:`ingest_media_state` detach facts ONLY - a SCO
        ``audio_lost`` fact never arms it, round 32 item 11;
        re-armed by ``MediaAllocator.reissue()``) ends with
        ``CallEndReason.NETWORK_ERROR`` exactly like the other
        connectivity-loss teardowns.

        Orphan cascade (round 31, SA_swe2_review_1 item 22; reason
        unified round 32, SA_swe2_review_2 item 12): when the
        heartbeat ladder (or an adapter-reported
        WS_DISCONNECTED/UNREACHABLE fact) declares a binding
        dead and its ``orphan_deadline_at`` grace elapses -
        or is already expired, as for a hydrated dead row -
        ``tick`` tombstones the binding AND cascades in one
        atomic step: every live lease on the dead binding ends
        with ``CallEndReason.PROVIDER_LOST`` (the ONE unified
        reason for "the provider channel died" - the earlier
        ``NETWORK_ERROR`` spelling contradicted the
        queue/claim siblings and is gone), every pending queue
        ticket served by it is cancelled
        (``event.queue_cancelled{reason: "provider_lost"}``), its
        pending claims reject ``provider_lost``, the binding row
        moves into the ``_closed_assignments`` ring so later
        facts resolve ``STALE_TOKEN``, and an expired
        ``fallback_call_token``/ring timer owned by the binding
        burns with its ``MissedCallEvent``. NO
        ``ProviderHangupCommand`` is queued for the dying binding
        (round 32, SA_swe2_review_2 item 12): a tombstoned
        binding can never receive commands - the dead-binding
        outbox purge wins, and the modem-side call is left for
        the provider's own reconnect/caller-side cleanup, exactly
        like the external-call bookkeeping the cascade resets.
        A hydrated
        ``ProviderBinding`` row follows the same cascade on the
        first ``tick`` after load - its ``orphan_deadline_at`` is
        already past - unless a fresh ``provider.register``
        superseded it first (see :meth:`register_provider`).

        Queue drain (round 31, SA_swe2_review_1 item 25; ordering
        pinned round 32, SA_swe2_review_2 item 16): each re-check
        walk evaluates pending tickets against EVERY
        currently-free line, not only the line that just freed
        or the line a ticket originally queued on - a ticket
        WITHOUT a ``preferred_line_id`` is eligible for any free
        ``SHARED_POOL`` line serving ``DIAL_OUT`` (tickets carry
        no ``ANY_LINE``/``SHARED`` intent - ``intent`` is
        ``DIAL_OUT`` only, model-enforced on
        :class:`QueueTicket`), and a ticket whose preferred line
        is busy MUST NOT block a younger ticket another free
        line can serve (head-of-line fairness is by ``seq``
        order within the eligible set - the mandatory global
        FIFO - never ``created_at``). A grant is
        idempotent post-issue: the ticket leaves the pending set
        atomically with the grant mint, so a repeated drain never
        double-grants, and a grant TTL lapse is TERMINAL for the
        grant - the lease ends ``grant_ttl_expired`` and the
        ticket's fate follows its own path (the preemption
        reissue rule owns head-of-queue reinsertion; a lapsed
        grant never silently re-queues a second live grant for
        the same ticket).

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

        Bounded deadline processing (round 32, SA_swe2_review_2
        item 2): ONE ``tick`` call processes at most
        :data:`MAX_TICK_DEADLINES` expired items IN TOTAL across
        ALL deadline classes combined - the bound is on the
        aggregate work slice, never a per-class quota (a tick
        facing 256 expired grant TTLs AND 256 expired orphan
        deadlines processes 256 items from the merged deadline
        set, not 512) - a
        mass-expiry wave (every line's accept window lapsing at
        once, a restart-expired orphan deadline on every hydrated
        binding) is split across consecutive ``tick`` calls with
        the remainder pending until the next call, NEVER dropped
        and NEVER rejected. Deadline work is kernel liveness
        bookkeeping, not a client request, so
        ``RESOURCE_EXHAUSTED`` is inadmissible on this path - the
        ``tick`` contract expresses backpressure as deferral, and
        the outbox hard cap still applies to the bounded slice the
        call actually commits (:data:`OUTBOX_HARD_CAP` accounting
        counts ``MAX_TICK_DEADLINES *`` max-envelopes-per-deadline
        at one envelope per line fan-out wave).

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
    #   an outbox at/above :data:`OUTBOX_SOFT_CAP` rejects the call
    #   with RESOURCE_EXHAUSTED while the kernel state is still
    #   untouched. The rejected agent simply retries.
    # * Teardown and admin-cascade paths are SOFT-CAP-EXEMPT (round
    #   30, SA_carbon_review_19 slice-A item 7 - the earlier
    #   "teardown paths NEVER raise RESOURCE_EXHAUSTED" wording is
    #   replaced by the two-level model): ``hangup``, ``release``,
    #   ``release_assignment``, ``drop_session``,
    #   ``ingest_call_state`` teardowns, provider-loss, ``tick()``
    #   expiries and the admin mutations ``revoke_assignment`` /
    #   ``remove_line`` (plus their lease/claim cascades) append
    #   their events/commands unconditionally against
    #   :data:`OUTBOX_HARD_CAP` - the final bounded-capacity guard
    #   every mutation still checks BEFORE committing partial
    #   state; overflow there is RESOURCE_EXHAUSTED. Teardown
    #   volume is bounded by the number of live leases and lines,
    #   both themselves bounded.
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
        ``provider_lost`` effect inside the same
        transaction (round 35, iteration-3 audit item 16 - the
        unified dead-binding reason everywhere a binding death
        ends a lease), so their lease-end events reach the SAME
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
    Bluetooth device. A window on a line whose
    ``paired_phone_mac`` is unset is INVALID_STATE (round 33):
    opening a pairing window with no device to confirm would only
    widen the SSP auto-accept surface to any phone in range -
    the supervisor rejects the request instead of holding a
    meaningless window."""

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
    heartbeat_degraded_after: int | None = Field(default=None, ge=1, le=3600)
    """Missed pings before the binding's health reports degraded
    (liveness axis, round 25, review-15 item 15). Bounded at 3600
    (round 31, SA_swe2_review_1 item 17) so a patch can never stage
    a threshold the effective model would reject."""
    heartbeat_down_after: int | None = Field(default=None, ge=1, le=3600)
    """Missed pings before the binding is declared UNREACHABLE (must
    exceed the degraded threshold when both are set). Same 3600
    bound (round 31, SA_swe2_review_1 item 17)."""
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
    hangup_hold_sec: float | None = Field(default=None, ge=1.0, le=120.0)
    """Bound on the ``TERMINATING`` hold (round 32, SA_swe2_review_2
    item 1): how long ``tick`` waits for the provider's
    ``disconnected`` after a ``line.hangup`` was queued before it
    ends the lease ``network_error`` as a kernel-side lapse
    (default 10 s, tight 1..120 s bound - a modem-confirmation
    grace, not a session knob)."""
    provider_command_ack_sec: float | None = Field(default=None, gt=0, le=60.0)
    """Bounded ``{accepted}`` handshake deadline for
    ``line.dial``/``line.answer`` (round 33): the PATCH counterpart
    of :attr:`LineTimeouts.provider_command_ack_sec` (default 2 s,
    tight ``0..60`` s bound - a command handshake that cannot
    answer in 60 s is a dead link)."""
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

        Idempotency (round 31, SA_swe2_review_1 item 37): re-applying
        the SAME ``(line_id, owner_agent_id)`` grant returns the
        EXISTING ``LineAssignment`` verbatim - the line already
        holds exactly the requested durable state, so the call is
        a no-op, NOT ``LINE_EXCLUSIVE_NOT_OWNER`` (that reason names
        a DIFFERENT incumbent owner) and NOT a fresh
        ``assignment_id`` rotation - the owner retains the id it was
        first granted, no ``assignment_granted`` copy re-fires, and
        an admin retry can never rotate the credential out from
        under a live owner.

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

        Assign on a non-idle line (round 28, review-18 item 14;
        ring rule hardened round 30, SA_carbon_review_19 slice-A
        item 8): ``assign_line`` is legal on a ``BUSY`` line -
        ownership and call occupancy are different axes; live
        leases stay with the sessions that own them (no
        ``(session_id, lease_id)`` ownership migrates) and the
        exclusivity grant changes who may acquire NEXT, not who
        owns what already runs. On a ``RINGING_LOCKED`` line it is
        deterministically ``PhonebridgeError(ErrorCode.INVALID_STATE)``
        - "settle the ring first": a delivered ``incoming_call``
        is never re-routed and the new owner never learned the
        ring's ``call_token``, so an exclusivity grant mid-ring
        would strand a ring its new owner could neither see nor
        accept."""
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

        Approve on a non-idle line (round 28, review-18 item 14;
        ring rule hardened round 30, SA_carbon_review_19 slice-A
        item 8): same isolation rule as :meth:`assign_line` - the
        grant is legal on a ``BUSY`` line, live leases stay with
        their sessions and the new owner inherits nothing already
        running (no foreign lease migrates). On a
        ``RINGING_LOCKED`` line it is deterministically
        ``PhonebridgeError(ErrorCode.INVALID_STATE)`` - "settle
        the ring first": no delivered ``incoming_call`` is
        re-routed and the new owner never learned the ring's
        ``call_token``."""
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
    async def list_pending_claims(
        self,
        *,
        offset: Annotated[int, Field(ge=0)] = 0,
        limit: Annotated[int, Field(ge=1, le=MAX_LIST_PAGE)] = MAX_LIST_PAGE,
    ) -> list[PendingClaim]:
        """Admin read (round 21, HIGH-7): every pending claim request.
        The admin surface needs this to show moderators what awaits
        approval - without it the pending set lives only in private
        kernel state. Bounded by
        :data:`MAX_PENDING_CLAIMS_TOTAL` (rule 7) and paged by the
        shared ``{offset?, limit?}`` window (round 30,
        SA_carbon_review_19 slice-B item 1 -
        :data:`MAX_LIST_PAGE`, same convention as
        :meth:`list_lines`); the stable slice order is
        ``(created_at, claim_id)``."""
        ...

    @abstractmethod
    async def list_assignments(
        self,
        *,
        offset: Annotated[int, Field(ge=0)] = 0,
        limit: Annotated[int, Field(ge=1, le=MAX_LIST_PAGE)] = MAX_LIST_PAGE,
    ) -> list[LineAssignment]:
        """Admin read (round 21, HIGH-7): every durable
        ``LineAssignment`` (agent-ownership object space), so the
        admin surface can resolve valid ``assignment_id`` targets for
        ``revoke_assignment`` without reaching into kernel internals.
        Bounded by :data:`MAX_LINES_TOTAL` (rule 7) and paged by the
        shared ``{offset?, limit?}`` window (round 30,
        SA_carbon_review_19 slice-B item 1 -
        :data:`MAX_LIST_PAGE`, same convention as
        :meth:`list_lines`); the stable slice order is
        ``line_id``."""
        ...

    @abstractmethod
    async def list_provider_bindings(
        self,
        *,
        offset: Annotated[int, Field(ge=0)] = 0,
        limit: Annotated[int, Field(ge=1, le=MAX_LIST_PAGE)] = MAX_LIST_PAGE,
    ) -> list[ProviderBinding]:
        """Admin read (round 21, HIGH-7): every ``ProviderBinding``
        (provider-attachment object space) - the second half of the
        ``assignment_id`` disambiguation documented on
        ``revoke_assignment``. Bounded by
        :data:`MAX_LINES_TOTAL` (rule 7 - at most one live binding
        per line) and paged by the shared ``{offset?, limit?}``
        window (round 30, SA_carbon_review_19 slice-B item 1 -
        :data:`MAX_LIST_PAGE`, same convention as
        :meth:`list_lines`); the stable slice order is
        ``line_id``.

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
    async def get_line(self, line_id: LineId) -> AdminLineView:
        """Admin read ``admin.get_line`` (round 31,
        SA_swe2_review_1 item 23): the single-line counterpart of
        :meth:`list_lines` - one configured line's full
        :class:`AdminLineView` (assignment/owner, live binding and
        its ``provider_meta``, live ``lease_id``/``queue_position``)
        fetched by id instead of by paging, so the admin detail view
        does not burn a ``limit``-sized page to reach one row.

        ``line_id`` goes through the same ``_check_line_id``
        hygiene as every method parameter (malformed id ->
        INVALID_ARGUMENT). An id absent from the configured line
        set resolves ``PhonebridgeError(ErrorCode.LINE_NOT_FOUND)``
        - the typed not-found, never a ``None`` return: an empty
        optional would conflate "line does not exist" with "line
        exists but carries no detail", and the admin surface needs
        the distinction for its error mapping. The returned view
        obeys the same invariants as a ``list_lines`` row -
        ``provider_meta`` empty when ``liveness == UNBOUND``."""
        ...

    @abstractmethod
    async def get_assignment(self, line_id: LineId) -> LineAssignment:
        """Raw durable-record read (round 34, iteration-2 audit item
        18): the :class:`LineAssignment` row itself - not the
        projected :class:`AdminLineView` - so the admin adapter can
        perform its write-through bookkeeping: capture the
        PRE-mutation snapshot a later :meth:`restore_line`
        compensation restores verbatim, and diff the post-mutation
        record against the row it is about to upsert. ``get_line``
        cannot serve this - its projected view drops durable fields
        the store row carries (``timeouts``, ``allowed_number_types``,
        ``idle_revoke_after``, ``wake_hooks``, the hardware fields),
        so a restore fed from it would silently downgrade the
        record.

        Same hygiene/error shape as :meth:`get_line`:
        ``_check_line_id`` on the parameter (malformed ->
        INVALID_ARGUMENT), an absent id resolves
        ``PhonebridgeError(ErrorCode.LINE_NOT_FOUND)``. Read-only:
        the returned model is an immutable snapshot - mutating it
        changes nothing in kernel state."""
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
        itself and RECORDS its id into the bounded
        ``_closed_assignments`` ring tagged as the line-side object
        space (round 30, SA_carbon_review_19 slice-A item 12 -
        replaces the earlier "no tombstone is kept" wording):
        removal and revoke share one tombstone semantic, so a late
        ``call.release``/``release_assignment`` on the deleted id
        resolves through the ring by the recorded previous owner -
        idempotent no-op for the owner, ``STALE_TOKEN``/error for a
        foreign session - instead of falling through to an
        unresolvable id. Emits no ``assignment_revoked`` for a
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
        adapter_mac: str | None = None,
        paired_phone_mac: str | None = None,
        transport_profile: TransportProfile | None = None,
        line_home_country: str | None = None,
        wake_hooks: frozenset[str] | None = None,
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
        ``wake_hooks`` (round 34, iteration-2 audit items 2/18)
        seeds :attr:`LineAssignment.wake_hooks` at create time -
        ``None`` provisions a hook-less line; a set value obeys
        :data:`MAX_WAKE_HOOKS_PER_LINE` and the ``_check_id``
        member rule, so the wire surface and the stored record
        are the same shape (hook configuration is admin-surface
        config, never hidden state outside the wire).

        Hardware fields (round 31, SA_swe2_review_1 item 10):
        ``adapter_mac``/``paired_phone_mac`` follow the
        ``_check_mac_address`` grammar (``AA:BB:CC:DD:EE:FF`` or
        ``None``) and ``line_home_country`` the uppercase alpha-2
        ``_check_country_alpha2`` grammar (phonenumbers-supported
        regions incl. telephony extensions, not strict ISO 3166-1
        only); ``transport_profile``
        takes the :data:`TransportProfile` vocabulary
        (``loopback``/``direct_mtls``/``wireguard``, default
        ``loopback``). They persist on the ``lines`` row: the
        pairing window's SSP auto-accept is bound to
        ``paired_phone_mac`` (an unset field means the window
        confirms nothing - and ``admin.pair_window`` on a line
        without one is INVALID_STATE, round 33: opening a window
        that could confirm no device would silently widen the
        attack surface to ANY neighboring phone) and the provider
        adapter picks its
        listener/dialer from ``transport_profile``. MAC uniqueness
        (round 33; ``paired_phone_mac`` joined round 34,
        iteration-2 audit item 23): ``adapter_mac`` identifies ONE
        physical HCI
        adapter, so two configured lines MUST NOT carry the same
        value - a duplicate ``adapter_mac`` patch is
        ``INVALID_ARGUMENT`` (two lines would otherwise fight for
        the same Bluetooth hardware, and the PipeWire-facing node
        naming derives the device identity from it).
        ``paired_phone_mac`` carries the same uniqueness rule for
        the same reason in mirror: the pairing window's SSP
        auto-accept correlates the confirmation BY MAC, so two
        lines naming one phone could each auto-confirm the
        other's pairing. ``None`` on either field is exempt -
        absence is not an identity. The same
        check applies inside :meth:`configure_line`.

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
    ) -> bool:
        """Admin ``line.revoke``: end a durable binding.

        Return contract (round 25, review-15 item 14; simplified to
        a boolean round 31, SA_swe2_review_1 item 23): the method
        returns ``True`` when the id resolved to an agent-owned
        ``LineAssignment`` that was reverted to SHARED_POOL (the
        admin learns "an ownership grant ended" - the successor
        pool record is readable through :meth:`list_assignments`/
        :meth:`get_line` when the surface needs it) and ``False``
        when the id resolved to a ``ProviderBinding`` - the provider
        object space ends outright, there is no ownership grant -
        or when an idempotent retry hit the ``_closed_assignments``
        tombstone of an already-ended record (a second revoke ends
        nothing new). The earlier ``LineAssignment | None`` return
        dragged the whole post-revert snapshot across the admin
        boundary for a detail one follow-up read supplies on
        demand; the boolean keeps the wire response minimal.

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
        its leases with ``reason=CallEndReason.PROVIDER_LOST``
        (round 34, iteration-2 audit item 6 - the ONE unified
        provider-binding death reason across orphan-grace expiry,
        heartbeat-down, ``dongle_lost`` and this admin revoke path;
        ``network_error`` names only saga-compensation and
        deadline-lapse paths),
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
        :data:`MAX_CLOSED_ASSIGNMENTS`) and return ``False`` again -
        never ``STALE_TOKEN`` for an id the kernel once owned.
        Round 30 (SA_carbon_review_19 slice-A item 12) pins the
        symmetric rule for a DEAD ``LineAssignment`` id: an
        idempotent retry by the recorded previous owner returns
        ``False`` and MUST NEVER fall through to the line's NEW
        current record - that record could name a different owner's
        binding, and guessing would revoke the wrong object
        space.
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
        adapter_mac: str | None = None,
        paired_phone_mac: str | None = None,
        transport_profile: TransportProfile | None = None,
        line_home_country: str | None = None,
        wake_hooks: frozenset[str] | None = None,
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

        Hardware fields (round 31, SA_swe2_review_1 item 10;
        uniqueness/region pins round 33):
        ``adapter_mac``/``paired_phone_mac``/
        ``transport_profile``/``line_home_country`` patch the
        same-named :class:`LineAssignment` fields under the same
        ``None``-means-unchanged convention - a set MAC must match
        the ``AA:BB:CC:DD:EE:FF`` grammar, a set country two
        uppercase alpha-2 letters that name a KNOWN
        phonenumbers-supported
        region (``_check_country_alpha2`` rejects an unassigned
        code like ``"ZZ"`` - a nonsense region would silently feed
        the dial-normalization default), a set profile the
        :data:`TransportProfile` vocabulary (INVALID_ARGUMENT
        otherwise). A set ``adapter_mac`` MUST be unique across the
        configured line set (round 33): the MAC identifies one
        physical HCI adapter and the PipeWire-facing node naming
        derives the device identity from it - a patch repeating an
        ``adapter_mac`` another configured line already carries is
        ``INVALID_ARGUMENT`` (two lines can never share one
        adapter). ``paired_phone_mac`` carries the same uniqueness
        rule (round 34, iteration-2 audit item 23): the pairing
        window's SSP auto-accept correlates by MAC, so a patch
        naming a ``paired_phone_mac`` another configured line
        already carries is ``INVALID_ARGUMENT`` too. There is
        deliberately no clear-hardware flag in
        v1 - re-pairing writes a NEW ``paired_phone_mac``; a line
        whose phone is gone is reprovisioned, not un-paired.

        ``wake_hooks`` (round 34, iteration-2 audit items 2/18)
        patches :attr:`LineAssignment.wake_hooks` as a WHOLE-SET
        replace - ``None`` leaves the stored set, a set value
        (including the empty frozenset, the explicit way to take
        a line's wake hooks down) becomes the new set verbatim:
        the opaque hook ids are set-valued config, so a partial
        add/remove patch grammar would only invite divergent
        merge semantics. The set obeys
        :data:`MAX_WAKE_HOOKS_PER_LINE` and the ``_check_id``
        member rule; an out-of-shape value is INVALID_ARGUMENT.
        The patch takes effect on the NEXT ring - an already
        minted wake envelope is immutable.

        Write-through ordering (round 28, review-18 item 6): the
        kernel applies the patch FIRST and the adapter upserts the
        ``lines`` row SECOND - the kernel is the source of truth,
        so a config that fails model validation never reaches the
        stored row, and a row that fails to persist never leaves
        kernel state ahead of the store (the adapter's upsert runs
        after the kernel commit, outside the mutation critical
        section).

        Forward-policy referential check (round 32,
        SA_swe2_review_2 item 24): a patch that NARROWS a
        constraint other records reference - a smaller
        ``allowed_number_types`` set, a ``fallback_policy`` swap
        that invalidates a stored ``forward_to`` number kind -
        re-validates every existing ``forward_to`` policy and
        referencing fallback against the NEW constraint; a patch
        that would orphan a configured forward is
        ``INVALID_ARGUMENT`` (the admin must clear the
        referencing policy first), never a silent config whose
        stored policy can no longer execute.

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
        strict-E.164 + alpha-2 region checks ``call.dial``
        applies -
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

    @abstractmethod
    async def hydrate_lines(
        self, rows: list[LineAssignment], bindings: list[ProviderBinding]
    ) -> None:
        """Batch store-load hydration (round 30, SA_carbon_review_19
        slice-A item 2): the ONLY path that restores durable state
        at daemon start - a single batch call, never per-row
        ``assign_line``/``register_provider`` replays.

        Semantics: restore every record VERBATIM -
        ``assignment_id``, ``owner_agent_id``, ``mode``,
        ``last_seen_at``, ``profile``, timeouts and claimability are
        carried over unchanged; NO id is rotated and NO event or
        provider command is emitted (a hydrated grant is not a
        grant, it is stored truth - ``assignment_granted``/
        ``line_health_changed``/revoke commands must not replay on
        every restart). ``ProviderBinding`` rows hydrate into the
        binding set the same way - including their stamped
        heartbeat thresholds, ``channel_seq`` and the two durable
        provider-stamp watermarks
        (``liveness_stamp_watermark``/``call_stamp_watermark``,
        round 33) - but they are
        loaded DEAD, never live (round 31, SA_swe2_review_1 item
        3): the sockets a stored row describes died with the old
        process, so a hydrated binding enters kernel state as
        ``WS_DISCONNECTED`` with its ``orphan_deadline_at``
        ALREADY EXPIRED. The next ``tick`` then runs the standard
        orphan cascade on it (lease teardown, ticket/claim fates,
        tombstone into ``_closed_assignments``) unless a fresh
        ``provider.register`` supersedes the dead row first - the
        provider's legal reattachment path after a gateway restart
        is registration, not any "resume" message: a NEW control
        socket carrying a register mints a new live binding in
        place of the hydrated corpse (see
        :meth:`LineManager.register_provider` "Hydrated-dead
        binding"). No hydrated lease survives: ``CallLease``
        records are socket-bound and the store persists none, so
        the cascade's lease teardown is a no-op for a hydrated
        row and only the tombstone + ticket/claim fates apply.

        ``forward_via_line`` graph validation runs AFTER ALL rows
        are loaded (never per-row): every stored
        ``ForwardViaLineFallback.line_id`` MUST resolve to a loaded
        row - a dangling target is INVALID_ARGUMENT and the
        hydration is rejected whole - and the chain walk bounded at
        :data:`MAX_FORWARD_HOPS` must close no cycle (the same
        transitive-cycle rule :meth:`configure_line` enforces per
        patch, applied to the full loaded graph at once)."""
        ...

    @abstractmethod
    async def restore_line(self, line_id: LineId, snapshot: LineAssignment | None) -> None:
        """Write-through compensation (round 30, SA_carbon_review_19
        slice-A item 3; ``line_id`` parameter added round 31,
        SA_swe2_review_1 item 1): the ONLY kernel-side rollback hook
        - the admin adapter calls it when a ``lines``-row
        persistence (write-through SQLite upsert/delete) fails
        AFTER the kernel mutation already committed.

        Ordering pins (round 32, SA_swe2_review_2 item 3): the
        single ordered writer task is the ONLY component that
        performs SQLite writes - a mutation bundle is marked
        ``pending-write`` until its write confirms, admin
        mutations touching a record with an unconfirmed pending
        write wait (boundedly) for it, and provider/agent ingress
        is unaffected by the marker. Compensation itself is
        SERIALIZED through the same mutation queue like any other
        mutation - never invoked inline by the writer - so a
        restore can never interleave with an in-flight mutation,
        and if the failed record was legitimately re-mutated
        before the compensation lands (a later committed mutation
        changed the same record) the compensation MUST degrade to
        the ``config_stale`` condition plus admin alert instead of
        blindly overwriting the newer truth with the stale
        snapshot. The hook covers ``LineAssignment``-shaped
        durable records ONLY - a ``provider_bindings`` row is a
        best-effort cache entry (see :class:`ProviderBinding`), it
        is never rolled back through this hook.

        ``line_id`` selects the record the rollback targets -
        required because ``snapshot=None`` alone cannot name the
        line whose created record must be removed, and a snapshot's
        embedded ``line_id`` must agree with the parameter (a
        mismatch is INVALID_ARGUMENT, never a silent cross-line
        restore). ``snapshot`` is the pre-mutation
        ``LineAssignment`` the adapter captured before the failed
        write - the kernel restores it verbatim; ``None`` means
        "the mutation created the record", so the kernel REMOVES
        the record at ``line_id`` again.

        Guard parity with ``remove_line`` (round 34, iteration-2
        audit item 15): the ``snapshot=None`` removal path runs the
        SAME quiescence and forward-reference guards
        :meth:`remove_line` enforces - the kernel refuses to drop a
        record while a live binding, lease, queue ticket or pending
        claim still references it, and while another line's
        ``forward_via_line`` still targets it. A compensation that
        would violate those guards (live state accreted between the
        failed write and the rollback landing) degrades to the
        ``config_stale`` condition plus admin alert instead of
        deleting a referenced line. Symmetrically, a NON-``None``
        snapshot is re-validated against the CURRENT loaded set
        before it is restored: a ``forward_via_line`` target the
        snapshot names but that no longer resolves - or a chain the
        restored row would close into a cycle - is likewise a
        ``config_stale`` plus alert, never a silently restored
        dangling reference (the stored-graph validation rule of
        :meth:`hydrate_lines` applies to the rollback product too).

        Register-scoped rollback exemption (round 35, iteration-3
        audit item 4): the ONE case where a live binding does not
        block ``snapshot=None`` removal is the register's own
        compensation - the record was auto-created by
        :meth:`LineManager.register_provider` for a previously
        unconfigured ``line_id`` (visible to the adapter on
        :attr:`ProviderRegisterResult.auto_created_assignment`)
        and the provider-binding write-through then failed. The
        kernel removes the record AND tombstones the
        ``ProviderBinding`` minted in the same register mutation
        atomically - the binding joins ``_closed_assignments`` so
        its later facts resolve ``STALE_TOKEN``, a
        :class:`ProviderRevokeCommand` is queued on its still-open
        channel so the just-registered provider is told to detach,
        and any rings/``pending_rings`` the register minted with
        it die with the line. The exemption is bound to the
        pairing: the binding must be the one minted by that
        register mutation AND still pristine - no lease minted, no
        provider fact processed, no further mutation committed on
        it since. A binding that already accreted live facts makes
        the pair non-atomic and the compensation degrades to
        ``config_stale`` plus admin alert like every other guarded
        removal.

        Tombstone exemption (round 31, SA_swe2_review_1 item 1 -
        the exemption the rollback needs to stay honest): a
        ``snapshot`` carrying an ``assignment_id`` the kernel has
        already tombstoned (rotated/revoked since the adapter's
        capture, present in ``_closed_assignments``) is still
        restored VERBATIM - restoring the pre-mutation truth is
        precisely why this hook exists, so the closed-ring check
        that rejects stale ids everywhere else MUST NOT gate the
        rollback. The restored record then owns its
        ``assignment_id`` again (the tombstone entry is cleared),
        so a late ``release`` on it resolves live, not
        STALE_TOKEN. The call emits
        the COMPENSATING events the rollback implies - an
        ``assignment_revoked`` copy when a grant is rolled back, an
        ``assignment_granted`` copy when a revoke is rolled back -
        so the outbox the adapter already drained can never claim a
        durable change that the store does not reflect. The
        persistence failure itself surfaces to the operator as a
        ``config_stale`` condition plus an admin alert (the store
        and kernel diverged until the compensation lands) - the
        single-writer/kernel-first architecture of round 28 is
        preserved: this method exists exclusively to converge
        kernel state back after a failed write, never as a general
        undo API."""
        ...

    # --- transactional outbox drains (round 25, review-15 item 12) ---
    #
    # The admin mutations above append to the SAME bounded outbox the
    # agent-facing ``LineManager`` drains use - ``InMemoryLineManager``
    # implements both surfaces over one kernel state, so claim
    # resolutions, granted assignments and queue/claim fates ride the
    # identical drain contract documented on ``LineManager`` (soft-cap
    # pre-flight on agent resource-creation ops only, teardown/admin
    # cascades exempt and bounded by :data:`OUTBOX_HARD_CAP`, drain
    # after every call).

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
