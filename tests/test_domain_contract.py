"""Contract tests for phonebridge.domain.contract (roadmap item_03, issue #7).

Each GWT id rides in the parametrized case id so the pytest nodeid
renders as ``test_name[ITEM-03.GWT-N]`` (same convention as the
tooling-baseline tests).
"""

import copy
import inspect
import json
import pickle
import re
from collections.abc import Callable, Collection
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast, get_args, get_type_hints

import pytest
from pydantic import TypeAdapter, ValidationError

import phonebridge.domain.contract as contract
from phonebridge.domain.contract import (
    CALL_PHASE_FORWARD_TRANSITIONS,
    CALL_PHASE_WIRE_STATES,
    CLAIM_VISIBLE_LIVENESS_STATES,
    DTMF_PATTERN,
    ERROR_CODE_WIRE,
    EVENT_AUDIENCE_SCOPES,
    EVENT_PAYLOAD_TYPES,
    EVENT_WIRE_NAMES,
    MAX_CALL_STAMP_WATERMARKS_PER_LINE,
    MAX_CALLER_ID_LEN,
    MAX_CLOCK_SKEW_SEC,
    MAX_CLOSED_ASSIGNMENTS,
    MAX_CLOSED_LEASES,
    MAX_CLOSED_TICKETS,
    MAX_CTRL_FRAME_LEN,
    MAX_DETAIL_LEN,
    MAX_DTMF_DIGITS,
    MAX_DTMF_STREAM_PAUSE_SEC,
    MAX_ENDPOINT_LEN,
    MAX_FORWARD_HOPS,
    MAX_HEARTBEAT_INTERVAL_MS,
    MAX_IDEMPOTENCY_KEY_LEN,
    MAX_IDEMPOTENCY_KEYS_PER_SESSION,
    MAX_LEASES_PER_SESSION,
    MAX_LINES_TOTAL,
    MAX_LIST_PAGE,
    MAX_PENDING_CLAIMS_PER_AGENT,
    MAX_PENDING_CLAIMS_TOTAL,
    MAX_PROFILE_LEN,
    MAX_PROVIDER_META_ENTRIES,
    MAX_PROVIDER_META_KEY_LEN,
    MAX_PROVIDER_META_VALUE_LEN,
    MAX_QUEUE_TICKETS_TOTAL,
    MAX_REASON_LEN,
    MAX_SESSIONS_PER_AGENT,
    MAX_SESSIONS_TOTAL,
    MAX_TICKETS_PER_SESSION,
    MAX_TOKEN_LEN,
    MAX_WAKE_HOOKS_PER_LINE,
    MEDIA_AUTH_TIMEOUT_SEC,
    MEDIA_PAIR_TTL_SEC,
    OUTBOX_HARD_CAP,
    OUTBOX_SOFT_CAP,
    PROVIDER_COMMAND_WIRE_NAMES,
    RING_DEDUP_MIN_GAP_SEC,
    SERVABLE_LIVENESS_STATES,
    SNAPSHOT_RECONCILED_PHASES,
    SYSTEM_AGENT_PREFIX,
    SYSTEM_FALLBACK_WORKER,
    UNMATCHED_CALL_SAFETY_SEC,
    AcceptResult,
    AcquireGranted,
    AcquireQueued,
    AcquireRefused,
    AcquireResult,
    AdminAudience,
    AdminLineView,
    AgentAudience,
    AnswerAndAnnounceFallback,
    AssignmentGrantedEvent,
    AssignmentMode,
    AssignmentRevokedEvent,
    AudioReadyEvent,
    BroadcastAudience,
    CallEndReason,
    CallLease,
    CallPhase,
    CallStateChangedEvent,
    CallToken,
    ClaimId,
    ClaimResolvedEvent,
    DialResult,
    ErrorCode,
    ErrorData,
    ErrorReason,
    EventAudience,
    EventEnvelope,
    EventName,
    FallbackDecision,
    FallbackPolicy,
    FallbackTriggeredEvent,
    FanoutAudience,
    ForwardViaLineFallback,
    IdleAlertEvent,
    IncomingCallEvent,
    IncomingCallWakeEvent,
    IncomingRingIngress,
    Intent,
    LeaseId,
    LineAdminKernel,
    LineAssignment,
    LineHealthChangedEvent,
    LineId,
    LineManager,
    LineState,
    LineStateChangedEvent,
    LineSummary,
    LineTimeouts,
    LivenessState,
    MediaCredentials,
    MediaOverflowEvent,
    MediaReattachResult,
    MediaRole,
    MediaToken,
    MediaTransport,
    MissedCallEvent,
    MissedCallWakeEvent,
    NoPickupFallback,
    PairWindowRequest,
    PairWindowResponse,
    PendingClaim,
    PendingRing,
    PhonebridgeError,
    ProviderActiveCall,
    ProviderAnswerCommand,
    ProviderBinding,
    ProviderCallState,
    ProviderCallStateUpdate,
    ProviderCapabilityUpdate,
    ProviderCommand,
    ProviderDialCommand,
    ProviderDisconnectReason,
    ProviderHangupCommand,
    ProviderLiveness,
    ProviderMediaReattachRequest,
    ProviderMediaReattachResult,
    ProviderReattachMediaCommand,
    ProviderRegisterResult,
    ProviderRegistration,
    ProviderRevokeCommand,
    ProviderSendDtmfCommand,
    QueueCancelledEvent,
    QueueCancelReason,
    QueueReadyEvent,
    QueueTicket,
    QueueUpdatedEvent,
    RecordAndTelegramFallback,
    RedactedString,
    RejectFallback,
    SessionAudience,
    SessionId,
    TicketId,
    TimeoutConfig,
    ensure_aware_utc,
    heartbeat_interval_ms_from_sec,
    is_system_agent_id,
    secure_compare,
    validate_e164_country,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
API_CONTRACT_MD = REPO_ROOT / "docs" / "api-contract.md"

# Arbitrary fixed epoch (round 32, SA_swe2_review_2 item 4): a
# deterministic kernel-clock value for tests - NOT a "current
# time" assertion. All stamp plausibility bounds are absolute, so
# the constant can never age into rejection; tests that inject it
# as ``now`` exercise kernel arithmetic, not wall-clock truth.
NOW_UTC = datetime(2026, 11, 13, 12, 0, 0, tzinfo=timezone.utc)
# libphonenumber's reserved documentation example for MD (mobile) -
# never a real subscriber. The fixed-line example "+37322212345" is
# used where a second valid MD number is needed.
_RAW_NUMBER = "+37362112345"


def _assignment(**overrides: Any) -> LineAssignment:
    kwargs: dict[str, Any] = {
        "assignment_id": contract.AssignmentId("asg-1"),
        "line_id": LineId("line-main"),
        "owner_agent_id": None,
        "mode": AssignmentMode.SHARED_POOL,
        "claimable": True,
        "last_seen_at": NOW_UTC,
    }
    kwargs.update(overrides)
    return LineAssignment(**kwargs)


def _ticket(ticket_id: str, seq: int) -> QueueTicket:
    return QueueTicket(
        ticket_id=TicketId(ticket_id),
        seq=seq,
        session_id=SessionId("sess-1"),
        intent=Intent.DIAL_OUT,
        enqueued_at=NOW_UTC,
    )


# ---------------------------------------------------------------------------
# ITEM-03.GWT-1: module imports cleanly, all public names exist
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("_gwt", [pytest.param(None, id="ITEM-03.GWT-1")])
def test_public_surface_exists(_gwt: object) -> None:
    public = {
        "AssignmentId",
        "CallToken",
        "LeaseId",
        "MediaToken",
        "LineId",
        "TicketId",
        "Intent",
        "LineState",
        "CallPhase",
        "ErrorCode",
        "RedactedString",
        "LineAssignment",
        "CallLease",
        "QueueTicket",
        "LineSummary",
        "AcquireResult",
        "LineManager",
        "PhonebridgeError",
    }
    for name in public:
        assert hasattr(contract, name), f"missing public name: {name}"
    assert set(contract.__all__) >= public
    # Round 30 (SA_carbon_review_19 slice-C item 20): the surface
    # pin is COMPLETE, not a subset - EVERY exported name resolves
    # on the module (no dangling ``__all__`` entries) and the export
    # list carries no duplicates.
    assert len(contract.__all__) == len(set(contract.__all__))
    for name in contract.__all__:
        assert not name.startswith("_")
        assert hasattr(contract, name), f"__all__ exports missing name: {name}"

    # Enum values match the wire spellings verbatim.
    assert {i.value for i in Intent} == {
        "dial_out",
        "receive_inbound",
        "exclusive_line",
    }
    # Round 25 (review-15 item 15): pure call-occupancy vocabulary -
    # provider health moved to LivenessState / line_health_changed.
    assert {s.value for s in LineState} == {
        "FREE",
        "BUSY",
        "RINGING_LOCKED",
    }
    assert {s.value for s in LivenessState} == {
        "reachable",
        # Round 28 (review-18 item 1): the kernel-computed middle
        # rung of the heartbeat ladder - never a reported fact.
        "degraded",
        "ws_disconnected",
        "unreachable",
        # Round 26 (review-16 item 13): the derived provider-less
        # listing state - reported by LineSummary only, never by a
        # binding/liveness fact.
        "unbound",
    }
    assert {p.value for p in CallPhase} == {
        "acquired",
        "dialing",
        "ringing",
        # Round 31 (SA_swe2_review_1 item 2): the answer-in-flight
        # phase - accept()'s inbound mirror of DIALING.
        "answering",
        "alerting",
        "active",
        "terminating",
        "ended",
    }


# ---------------------------------------------------------------------------
# ITEM-03.GWT-2: RedactedString masking / wire / safe_dump / error hygiene
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("_gwt", [pytest.param(None, id="ITEM-03.GWT-2")])
def test_redacted_string_contract(_gwt: object) -> None:
    event = IncomingCallEvent(
        line_id=LineId("line-main"),
        call_token=CallToken(RedactedString("tok-abc")),
        caller_id=RedactedString(_RAW_NUMBER),
        expires_at=NOW_UTC,
    )
    caller = event.caller_id
    assert caller is not None

    # repr/str are masked; raw number never appears.
    assert _RAW_NUMBER not in repr(caller)
    assert _RAW_NUMBER not in str(caller)
    assert "****" in repr(caller)
    assert str(caller) == "+373****45"

    # Explicit in-memory / wire accessors expose the full value.
    assert caller.expose() == _RAW_NUMBER
    assert caller.to_wire() == _RAW_NUMBER

    # Wire serialization (json mode) emits the full value.
    wire_json = event.model_dump_json()
    assert _RAW_NUMBER in wire_json
    wire_dict = event.model_dump(mode="json")
    assert wire_dict["caller_id"] == _RAW_NUMBER

    # Log dumps are masked in both helper spellings.
    safe = event.safe_dump()
    assert safe["caller_id"] == "+373****45"
    assert _RAW_NUMBER not in repr(safe)
    log_dict = event.to_log_dict()
    assert _RAW_NUMBER not in repr(log_dict)

    # Round 20 fail-closed: the container is NOT a str, so none of
    # CPython's C-level string paths (json encoder, concatenation,
    # implicit isinstance(x, str) checks in loggers) can leak PII.
    assert not isinstance(caller, str)
    with pytest.raises(TypeError):
        json.dumps(caller)
    with pytest.raises(TypeError):
        # python-mode dumps hold the container -> json.dumps on the
        # whole dump fails closed instead of emitting the raw value.
        json.dumps(event.model_dump())
    with pytest.raises(TypeError):
        "x" + caller  # type: ignore[operator]
    # Equality is RedactedString-to-RedactedString only: a raw str can
    # never silently stand in for a secret.
    assert caller != _RAW_NUMBER
    assert caller == RedactedString(_RAW_NUMBER)
    assert hash(caller) == hash(RedactedString(_RAW_NUMBER))
    # Constructing from an existing RedactedString is idempotent.
    assert RedactedString(caller).expose() == _RAW_NUMBER
    # Re-validating a python-mode dump (which carries the container)
    # succeeds - the union schema accepts an existing instance.
    assert IncomingCallEvent.model_validate(event.model_dump()) == event


def test_mask_for_log_set_and_frozenset() -> None:
    """Round-18 hardening: set/frozenset containers of RedactedString
    must be masked recursively (a future ``set[RedactedString]`` field
    must not leak raw values through safe_dump), and the dump is a
    deterministic sorted list."""
    values = {
        "tags": {RedactedString("bbb-secret-2"), RedactedString("aaa-secret-1")},
        "frozen": frozenset({RedactedString("ccc-secret-3")}),
    }
    masked = contract._mask_for_log(values)
    for container in (masked["tags"], masked["frozen"]):
        assert isinstance(container, list)
        assert all(isinstance(v, str) for v in container)
    assert "aaa-secret-1" not in repr(masked)
    assert "ccc-secret-3" not in repr(masked)
    # Deterministic ordering for reproducible logs.
    assert masked["tags"] == sorted(masked["tags"])

    # Existing frozenset field (allowed_number_types) dumps as a
    # sorted list, never a raw frozenset repr.
    dumped = _assignment().safe_dump()
    assert isinstance(dumped["allowed_number_types"], list)


@pytest.mark.parametrize("_gwt", [pytest.param(None, id="ITEM-03.GWT-2")])
def test_validation_error_hides_pii_input(_gwt: object) -> None:
    """hide_input_in_errors: a rejected payload must not echo the raw
    caller number into the ValidationError text."""
    with pytest.raises(ValidationError) as exc_info:
        IncomingCallEvent.model_validate(
            {
                "line_id": "line-main",
                "call_token": "tok-abc",
                "caller_id": {"number": _RAW_NUMBER},  # invalid type, carries PII
                "expires_at": NOW_UTC,
            }
        )
    assert _RAW_NUMBER not in str(exc_info.value)


@pytest.mark.parametrize("_gwt", [pytest.param(None, id="ITEM-03.GWT-2")])
def test_provider_token_is_redacted(_gwt: object) -> None:
    """Round 27 (review-17 item 32): ``ProviderRegistration`` carries
    NO token field - the per-line shared secret is consumed and
    verified by the ADAPTER during the provider handshake BEFORE the
    domain model is built, so the kernel sees only the distilled
    fact and a raw token can never be logged, persisted or
    replayed through it."""
    assert "token" not in ProviderRegistration.__pydantic_fields__
    reg = ProviderRegistration(
        line_id=LineId("line-main"),
        caps=[Intent.DIAL_OUT],
        channel_seq=1,
    )
    assert "token" not in reg.model_dump_json()
    # A stale caller still passing a token is rejected by the
    # extra-forbid policy, never silently absorbed.
    with pytest.raises(ValidationError):
        ProviderRegistration(  # type: ignore[call-arg]
            line_id=LineId("line-main"),
            token=RedactedString("line-secret-token-value"),
            caps=[Intent.DIAL_OUT],
            channel_seq=1,
        )


@pytest.mark.parametrize("_gwt", [pytest.param(None, id="ITEM-03.GWT-2")])
def test_redacted_string_format_spec_stays_masked(_gwt: object) -> None:
    """A non-empty format spec must never emit the raw value:
    __format__ routes through the masked string."""
    redacted = RedactedString(_RAW_NUMBER)
    rendered_forms = [
        f"{redacted}",
        f"{redacted!s}",
        f"{redacted!r}",
        f"{redacted:>20}",
        f"{redacted:<20}",
        f"{redacted:^20}",
        f"{redacted:.3}",
        format(redacted, ">20"),
        format(redacted, ""),
    ]
    for rendered in rendered_forms:
        assert _RAW_NUMBER not in rendered
    # Alignment still works - on the masked text.
    assert f"{redacted:>20}" == "+373****45".rjust(20)


@pytest.mark.parametrize("_gwt", [pytest.param(None, id="ITEM-03.GWT-2")])
def test_redacted_string_short_values_masked(_gwt: object) -> None:
    """Short secrets must not expose more than half their characters:
    <=4 chars mask fully, above that the visible frame is capped at
    half the length."""
    expected = {
        "": "****",
        "a": "****",
        "abcd": "****",
        "abcde": "****de",
        "abcdef": "a****ef",
        "token42": "t****42",
        "123456789": "12****89",
        "12345678901": "123****01",
        "+37362112345": "+373****45",
    }
    for raw, masked in expected.items():
        assert str(RedactedString(raw)) == masked
        visible = len(masked) - masked.count("*")
        assert visible <= len(raw) // 2


def test_redacted_string_hash_eq_consistency() -> None:
    """Round 29 (SA_quant_review_1 slice-C item 2): the
    ``__hash__``/``__eq__`` contract - equal raw values compare
    equal AND hash equal (``__hash__`` derives from the raw value,
    not the masked projection), different raw values compare and
    hash different, and dict/set membership resolves through
    ``__eq__``."""
    first = RedactedString("secret-alpha")
    same = RedactedString("secret-alpha")
    other = RedactedString("secret-beta")
    assert first == same
    assert hash(first) == hash(same)
    assert first != other
    # Hash is raw-derived (not masked): two distinct raw values
    # must not collide for these pinned inputs.
    assert hash(first) != hash(other)
    # Membership follows __eq__: a fresh instance with the same raw
    # value hits the same dict key / set element.
    table = {first: "v"}
    assert table[same] == "v"
    assert other not in table
    assert {first, same} == {same}
    # A raw str can never stand in for a secret - not on lookup,
    # not in a set, in either comparison direction.
    assert "secret-alpha" not in table
    assert first != "secret-alpha"
    assert "secret-alpha" not in {first}


# ---------------------------------------------------------------------------
# ITEM-03.GWT-3: ErrorCode drift test against docs/api-contract.md
# ---------------------------------------------------------------------------


def _doc_error_codes() -> set[str]:
    """Parse the `code` column of the "## Error codes" table in
    docs/api-contract.md - the source of truth for ErrorCode."""
    text = API_CONTRACT_MD.read_text(encoding="utf-8")
    section = text.split("## Error codes", 1)[1]
    section = section.split("\n## ", 1)[0]
    # First-column backticked codes in table rows only; the prose line
    # "`NO_FREE_LINE` was REMOVED" is outside the table and excluded.
    return set(re.findall(r"^\|\s*`([A-Z][A-Z_]*)`", section, flags=re.MULTILINE))


def _doc_error_codes_loose() -> set[str]:
    """Loose-parse counterpart of :func:`_doc_error_codes` (round 27,
    review-17 item 74): the strict regex anchors on a first-column
    backticked code, which silently misses a row whose code column
    gained bold/italic markup or moved a column over. This variant
    accepts optional ``**``/``_`` wrappers around the backticked
    code in ANY leading table cell so formatting drift is caught by
    a count/set mismatch instead of a silently shrinking set."""
    text = API_CONTRACT_MD.read_text(encoding="utf-8")
    section = text.split("## Error codes", 1)[1]
    section = section.split("\n## ", 1)[0]
    return set(
        re.findall(
            r"^\|\s*(?:[*_]{1,2})?`([A-Z][A-Z_]*)`(?:[*_]{1,2})?",
            section,
            flags=re.MULTILINE,
        )
    )


@pytest.mark.parametrize("_gwt", [pytest.param(None, id="ITEM-03.GWT-3")])
def test_error_codes_match_contract_doc(_gwt: object) -> None:
    doc_codes = _doc_error_codes()
    assert doc_codes, "parsed zero error codes - doc table format changed?"
    enum_codes = {c.value for c in ErrorCode}
    assert enum_codes == doc_codes, (
        f"ErrorCode drift vs docs/api-contract.md: "
        f"missing={doc_codes - enum_codes}, "
        f"extra={enum_codes - doc_codes}"
    )
    # Round 27 (review-17 item 74): the strict parse must not hide
    # rows - a formatted code column the regex missed would shrink
    # ``doc_codes`` while the loose parse still sees the row.
    loose = _doc_error_codes_loose()
    assert loose == doc_codes, f"Error-code table rows the strict parse missed: {loose - doc_codes}"


# ---------------------------------------------------------------------------
# Additional invariants
# ---------------------------------------------------------------------------


def test_naive_datetime_rejected_and_aware_normalized() -> None:
    """CODING_HAZARDS rule 6: naive datetime is a defect."""
    with pytest.raises(ValidationError):
        _assignment(last_seen_at=datetime(2026, 11, 13, 12, 0, 0))  # naive

    plus2 = timezone(timedelta(hours=2))
    aware = datetime(2026, 11, 13, 14, 0, 0, tzinfo=plus2)
    assignment = _assignment(last_seen_at=aware)
    assert assignment.last_seen_at == NOW_UTC
    assert assignment.last_seen_at.tzinfo == timezone.utc

    lease = CallLease(
        lease_id=LeaseId("lease-1"),
        line_id=LineId("line-main"),
        owner_agent_id="agent-1",
        session_id=SessionId("sess-1"),
        intent=Intent.DIAL_OUT,
        created_at=NOW_UTC,
        # Round 31 (SA_swe2_review_1 item 37): an ACQUIRED lease MUST
        # carry its grant deadline.
        grant_expires_at=NOW_UTC + timedelta(seconds=5),
    )
    with pytest.raises(ValidationError):
        CallLease.model_validate(
            {
                "lease_id": "lease-1",
                "line_id": "line-main",
                "owner_agent_id": "agent-1",
                "session_id": "sess-1",
                "intent": "dial_out",
                "created_at": "2026-11-13T12:00:00",  # naive ISO string
            }
        )
    assert lease.created_at == NOW_UTC


def test_queue_ticket_fifo_ordering() -> None:
    """QueueTicket orders by strictly monotonic seq = FIFO guarantee."""
    t1, t2, t3 = _ticket("t-1", 1), _ticket("t-2", 2), _ticket("t-3", 3)
    assert sorted([t3, t1, t2]) == [t1, t2, t3]
    assert t1 < t2 < t3
    assert not (t3 < t1)
    # Cross-type comparison delegates via NotImplemented -> TypeError.
    assert t1.__lt__(123) is NotImplemented
    with pytest.raises(TypeError):
        _ = t1 < 123


def test_acquire_result_union_discrimination() -> None:
    adapter = TypeAdapter(AcquireResult)

    granted = adapter.validate_python(
        {
            "granted": True,
            "line_id": "line-main",
            "lease_id": "lease-9",
            "expires_at": NOW_UTC.isoformat(),
        }
    )
    assert isinstance(granted, AcquireGranted)
    assert granted.lease_id == LeaseId("lease-9")
    assert granted.expires_at == NOW_UTC

    queued = adapter.validate_python(
        {
            "granted": False,
            "queued": True,
            "ticket_id": "ticket-7",
            "position": 2,
        }
    )
    assert isinstance(queued, AcquireQueued)
    assert queued.position == 2

    refused = adapter.validate_python({"granted": False, "free_lines": []})
    assert isinstance(refused, AcquireRefused)
    assert refused.free_lines == ()
    # Round 21 (MEDIUM-8): DTOs are frozen and collections are tuples -
    # JSON output still emits arrays, but no subscriber can mutate a
    # shared model.
    assert refused.model_dump(mode="json")["free_lines"] == []
    with pytest.raises(ValidationError):
        refused.granted = True  # type: ignore[assignment,misc]


def test_call_lease_answer_defaults_provisional() -> None:
    """'active' is PROVISIONAL: default confidence is provisional and
    answered_at is None until an answer is observed. Round 22
    (review-10 CRITICAL-2): the lease carries ``session_id`` - a
    missing session is a ValidationError."""
    grant_deadline = NOW_UTC + timedelta(seconds=5)
    lease = CallLease(
        lease_id=LeaseId("lease-1"),
        line_id=LineId("line-main"),
        owner_agent_id="agent-1",
        session_id=SessionId("sess-1"),
        intent=Intent.DIAL_OUT,
        created_at=NOW_UTC,
        grant_expires_at=grant_deadline,
    )
    assert lease.answered_at is None
    assert lease.answer_confidence == "provisional"
    assert lease.phase == CallPhase.ACQUIRED  # round 20: pre-dial phase
    # Round 31 (SA_swe2_review_1 item 37): the deadline is REQUIRED
    # on ACQUIRED and must sit at-or-after created_at.
    assert lease.grant_expires_at == grant_deadline
    assert lease.session_id == SessionId("sess-1")
    with pytest.raises(ValidationError):
        CallLease.model_validate(
            {
                "lease_id": "lease-1",
                "line_id": "line-main",
                "owner_agent_id": "agent-1",
                "intent": "dial_out",
                "created_at": NOW_UTC.isoformat(),
                # session_id absent - (session_id, lease_id) is the
                # ownership pair, it can never default
            }
        )


def test_fallback_policy_union_and_assignment_default() -> None:
    adapter = TypeAdapter(contract.FallbackPolicy)
    assert isinstance(adapter.validate_python({"policy": "no_pickup"}), NoPickupFallback)
    assert isinstance(adapter.validate_python({"policy": "reject"}), RejectFallback)
    fwd = adapter.validate_python(
        {
            "policy": "forward_via_line",
            "line_id": "line-b",
            "target_number": "+37322212345",
            "target_country": "MD",
        }
    )
    assert isinstance(fwd, ForwardViaLineFallback)
    assert fwd.line_id == LineId("line-b")
    # Round 23 (review-12 C2): the policy must carry the full dial
    # target - the FallbackWorker has no admin scope to query config.
    assert fwd.target_number.to_wire() == "+37322212345"
    assert fwd.target_country == "MD"
    # ...the target is PII-safe in log dumps (masked, never raw).
    assert "+37322212345" not in repr(fwd.safe_dump())
    for missing in ({}, {"target_country": "MD"}, {"target_number": "+37322212345"}):
        with pytest.raises(ValidationError):
            adapter.validate_python({"policy": "forward_via_line", "line_id": "line-b", **missing})
    rec = adapter.validate_python(
        {
            "policy": "record_and_telegram",
            "channel": "ops-alerts",
            "announcement_ref": "rec-note",
        }
    )
    assert isinstance(rec, RecordAndTelegramFallback)
    assert rec.channel == "ops-alerts"
    # Round 31 (SA_swe2_review_1 item 26): the recording leg always
    # announces - ``announcement_ref`` is always populated and
    # ``max_recording_sec`` bounds the recording independently of
    # the line's whole-call cap.
    assert rec.announcement_ref == "rec-note"
    assert rec.max_recording_sec == 300.0
    # Round-31 fix round (PR #78 warning 4): the compact
    # ``record_and_telegram:<channel>`` spelling - and any
    # pre-round-31 persisted policy JSON - carries no
    # announcement/recording fields, so BOTH carry defaults that
    # keep the model constructable: ``builtin:beep`` + 300 s.
    compact = adapter.validate_python({"policy": "record_and_telegram", "channel": "ops-alerts"})
    assert isinstance(compact, RecordAndTelegramFallback)
    assert compact.announcement_ref == "builtin:beep"
    assert compact.max_recording_sec == 300.0
    direct = RecordAndTelegramFallback(channel="ops-alerts")
    assert direct.announcement_ref == "builtin:beep"
    assert direct.max_recording_sec == 300.0
    # The widened ref grammar permits namespaced asset ids
    # (``builtin:beep`` itself) while the ``channel`` field keeps
    # the strict no-":" split-safe grammar.
    with pytest.raises(ValidationError):
        adapter.validate_python({"policy": "record_and_telegram", "channel": "ops:alerts"})
    with pytest.raises(ValidationError):
        adapter.validate_python(
            {
                "policy": "record_and_telegram",
                "channel": "ops-alerts",
                "announcement_ref": "rec-note",
                "max_recording_sec": 0.0,
            }
        )
    announce = adapter.validate_python(
        {"policy": "answer_and_announce", "announcement_ref": "busy-msg-wav"}
    )
    assert isinstance(announce, AnswerAndAnnounceFallback)
    assert announce.announcement_ref == "busy-msg-wav"
    # Round 22 (review-10 MEDIUM-2): the policy carries the
    # announcement selector - it cannot be empty or absent. Round 32
    # (SA_swe2_review_2 item 19) widened the ref grammar: namespaced
    # selectors (``wav:busy-msg``) and relative paths are LEGAL -
    # the adapter resolves the ref against the announcement root -
    # while a leading ``/`` and any ``..`` path segment stay
    # forbidden (traversal escapes can never reach the file lookup).
    with pytest.raises(ValidationError):
        adapter.validate_python({"policy": "answer_and_announce"})
    with pytest.raises(ValidationError):
        adapter.validate_python({"policy": "answer_and_announce", "announcement_ref": ""})
    namespaced = adapter.validate_python(
        {"policy": "answer_and_announce", "announcement_ref": "wav:busy-msg"}
    )
    assert isinstance(namespaced, AnswerAndAnnounceFallback)
    assert namespaced.announcement_ref == "wav:busy-msg"
    for bad_ref in ("/abs/path.wav", "../up.wav", "a/../b.wav", "..", "seg/../x"):
        with pytest.raises(ValidationError):
            adapter.validate_python({"policy": "answer_and_announce", "announcement_ref": bad_ref})
    with pytest.raises(ValidationError):
        adapter.validate_python({"policy": "spill_to_pool"})  # never existed

    # Default is no_pickup (carrier voicemail).
    assert _assignment().fallback_policy.policy == "no_pickup"
    # Round-15 allowlist default must include the NANP composite type
    # or ordinary US/CA mobiles are rejected as UNSUPPORTED_NUMBER_TYPE.
    assert "FIXED_LINE_OR_MOBILE" in _assignment().allowed_number_types
    assert {"FIXED_LINE", "MOBILE", "VOIP", "TOLL_FREE"} <= _assignment().allowed_number_types
    # Round 19: pool/provider attachments have no agent owner, and the
    # owner⇔mode invariant is enforced at the model boundary.
    assert _assignment().owner_agent_id is None
    with pytest.raises(ValidationError):
        _assignment(owner_agent_id="agent-1")  # shared_pool cannot own
    with pytest.raises(ValidationError):
        _assignment(mode=AssignmentMode.EXCLUSIVE)  # exclusive needs owner
    exclusive = _assignment(mode=AssignmentMode.EXCLUSIVE, owner_agent_id="agent-1")
    assert exclusive.owner_agent_id == "agent-1"


def test_line_admin_kernel_surface() -> None:
    """Round 19: the admin kernel is a separate narrow ABC — the
    agent-facing LineManager deliberately exposes no privileged
    mutations (approve/reject/revoke/configure). Round 21 (HIGH-7)
    adds the typed read surface (list_*) so the admin adapter never
    reaches into kernel internals."""
    assert set(LineAdminKernel.__abstractmethods__) == {
        "assign_line",
        "approve_claim",
        "reject_claim",
        "revoke_assignment",
        "configure_line",
        "list_pending_claims",
        "list_assignments",
        "list_provider_bindings",
        # Round 27 (review-17 items 3/26): the admin inventory surface -
        # line views, live leases, live tickets and line removal.
        "list_lines",
        # Round 31 (SA_swe2_review_1 item 23): the single-line read
        # counterpart of list_lines - one full AdminLineView by id.
        "get_line",
        # Round 34 (iteration-2 audit item 18): the raw durable-record
        # read the adapter uses for write-through snapshot capture.
        "get_assignment",
        "list_leases",
        "list_tickets",
        "remove_line",
        # Round 28 (review-18 item 6): the create half of the line
        # lifecycle - provision a quiescent configured line.
        "provision_line",
        # Round 30 (SA_carbon_review_19 slice-A items 2/3): batch
        # hydration at daemon start plus the write-through
        # compensation hook the admin adapter invokes on a failed
        # lines-row persistence.
        "hydrate_lines",
        "restore_line",
        # Round 25 (review-15 item 12): the admin surface drains the
        # SAME shared outbox the agent surface does.
        "drain_events",
        "drain_provider_commands",
    }
    with pytest.raises(TypeError):
        LineAdminKernel()  # type: ignore[abstract]


def _doc_event_table() -> dict[str, dict[str, bool]]:
    """Parse the "## APICallService → Agent — `event.*`" table in
    docs/api-contract.md: wire event name -> {field: is_optional}
    (round 22, review-10 HIGH-1 - the payload-field counterpart of
    :func:`_doc_line_command_table`)."""
    text = API_CONTRACT_MD.read_text(encoding="utf-8")
    section = text.split("## APICallService → Agent", 1)[1]
    section = section.split("\n## ", 1)[0]
    table: dict[str, dict[str, bool]] = {}
    for m in re.finditer(r"^\|\s*`(event\.[a-z_]+)`\s*\|\s*`\{([^}]*)\}`", section, flags=re.M):
        params: dict[str, bool] = {}
        for p in m.group(2).split(","):
            p = p.strip()
            if p:
                params[p.rstrip("?")] = p.endswith("?")
        table[m.group(1)] = params
    return table


# Round 30 (SA_carbon_review_19 slice-C item 22): the tests use the
# contract's own EVENT_PAYLOAD_TYPES mapping - the hand-maintained
# local copy (EVENT_PAYLOAD_TYPES) was a second source of truth that
# could silently drift from the module-level table it asserted on.


def test_event_wire_name_parity() -> None:
    """Round 19b drift test: EVERY domain EventName resolves to a
    documented ``event.*`` wire name — a new domain event can never
    be left an orphan on the wire (the pre-round-19b
    ASSIGNMENT_REVOKED gap). The mapping is also verified against the
    ``event.*`` table rows in docs/api-contract.md.

    Round 22 (review-10 HIGH-1) extends the check to the payload
    schema: every event model's fields must match the ``{...}``
    param list of its wire row by name AND by required-vs-optional
    (the doc's ``?`` suffix vs the model field carrying a default) -
    the same discipline ``test_provider_command_wire_parity``
    applies to ``line.*`` commands."""
    assert set(EVENT_WIRE_NAMES) == set(EventName)
    for wire in EVENT_WIRE_NAMES.values():
        assert wire.startswith("event.")
    # Deliberate rename, not drift: domain queue_ready → wire
    # line_acquired (grant fact, not queue internals).
    assert EVENT_WIRE_NAMES[EventName.QUEUE_READY] == "event.line_acquired"
    assert EVENT_WIRE_NAMES[EventName.ASSIGNMENT_REVOKED] == "event.assignment_revoked"
    # 1:1 names elsewhere.
    for name in EventName:
        if name is not EventName.QUEUE_READY:
            assert EVENT_WIRE_NAMES[name] == f"event.{name.value}"

    # Doc-side parity: every wire name in the mapping must appear as a
    # table row `| `event.<name>` |` in docs/api-contract.md — and
    # every such row must be covered by the mapping (no orphans in
    # either direction). Round 32 (SA_swe2_review_2 item 23) lifted
    # the round-31 exemption: ``event.media_overflow`` was promoted
    # into the normative event table, so no reserved name survives.
    api_contract = (Path(__file__).parents[1] / "docs" / "api-contract.md").read_text(
        encoding="utf-8"
    )
    doc_event_rows = set(re.findall(r"^\|\s*`(event\.[a-z_]+)`\s*\|", api_contract, flags=re.M))
    assert doc_event_rows == set(EVENT_WIRE_NAMES.values())

    # Payload parity: every EventName has exactly one payload model,
    # and the model's field names + optionality match the wire row.
    assert set(EVENT_PAYLOAD_TYPES) == set(EventName)
    table = _doc_event_table()
    assert table, "parsed zero event.* payloads - doc table format changed?"
    assert set(table) == set(EVENT_WIRE_NAMES.values())
    for name, model in EVENT_PAYLOAD_TYPES.items():
        wire_name = EVENT_WIRE_NAMES[name]
        model_fields = model.__pydantic_fields__
        doc_params = table[wire_name]
        assert set(model_fields) == set(doc_params), (
            f"{model.__name__} drift vs {wire_name}: "
            f"missing={set(doc_params) - set(model_fields)}, "
            f"extra={set(model_fields) - set(doc_params)}"
        )
        model_optional = {f for f in model_fields if not model_fields[f].is_required()}
        doc_optional = {p for p, optional in doc_params.items() if optional}
        assert model_optional == doc_optional, (
            f"{model.__name__} optionality drift vs {wire_name}: "
            f"doc-optional-only={doc_optional - model_optional}, "
            f"model-optional-only={model_optional - doc_optional}"
        )


def test_event_envelope_and_payloads() -> None:
    payload = IncomingCallEvent(
        line_id=LineId("line-main"),
        call_token=CallToken(RedactedString("tok-1")),
        caller_id=RedactedString(_RAW_NUMBER),
        expires_at=NOW_UTC,
    )
    envelope = EventEnvelope[IncomingCallEvent](
        name=EventName.INCOMING_CALL,
        audience=AgentAudience(agent_id="agent-1"),
        emitted_at=NOW_UTC,
        payload=payload,
    )
    assert envelope.payload.call_token == CallToken(RedactedString("tok-1"))
    assert isinstance(envelope.audience, AgentAudience)

    csc = CallStateChangedEvent(
        lease_id=LeaseId("l-1"),
        line_id=LineId("line-main"),
        # Round 25 (review-15 item 41): reason rides only
        # "disconnected" - a reason on a live state is a defect.
        state="disconnected",
        reason=CallEndReason.NORMAL,
        answer_confidence="provisional",
    )
    qr = QueueReadyEvent(
        ticket_id=TicketId("t-1"),
        session_id=SessionId("s-1"),
        lease_id=LeaseId("l-1"),
        line_id=LineId("line-main"),
        intent=Intent.DIAL_OUT,
        expires_at=NOW_UTC,
    )
    qu = QueueUpdatedEvent(
        session_id=SessionId("s-1"),
        ticket_id=TicketId("t-1"),
        position=2,
    )
    ar = AssignmentRevokedEvent(
        assignment_id=contract.AssignmentId("asg-1"),
        line_id=LineId("line-main"),
        owner_agent_id="agent-1",
    )
    are = AudioReadyEvent(lease_id=LeaseId("l-1"), line_id=LineId("line-main"))
    ft = FallbackTriggeredEvent(
        line_id=LineId("line-main"),
        call_token=CallToken(RedactedString("fb-tok")),
        caller_id=RedactedString(_RAW_NUMBER),
        # Round 25 (review-15 item 31): fallback_triggered dispatches
        # WORKER policies only - a core policy is unconstructible.
        policy=RecordAndTelegramFallback(channel="ops-alerts", announcement_ref="rec-note"),
        expires_at=NOW_UTC,
    )
    lsc = LineStateChangedEvent(
        line_id=LineId("line-main"),
        old_state=LineState.FREE,
        new_state=LineState.BUSY,
    )
    mc = MissedCallEvent(line_id=LineId("line-main"), reason=CallEndReason.ACCEPT_WINDOW_EXPIRED)
    ia = IdleAlertEvent(
        assignment_id=contract.AssignmentId("asg-1"),
        line_id=LineId("line-main"),
        owner_agent_id="agent-1",
        idle_since=NOW_UTC,
    )
    for ev in (csc, qr, qu, ar, are, ft, lsc, mc, ia):
        assert ev.model_dump_json()


def test_call_end_reason_wired_into_events() -> None:
    """Round-21 review (WARNING-1): the ``reason`` fields of
    call_state_changed / missed_call / line_state_changed ride the
    CLOSED CallEndReason vocabulary - including ``external_call``,
    the documented wire value for human-initiated calls (design §9).
    The provider-side ProviderCallStateUpdate.reason deliberately
    stays on the narrower ProviderDisconnectReason."""
    # EXTERNAL_CALL exists and ProviderDisconnectReason is a strict
    # subset (every provider-reported reason is a legal event reason).
    assert CallEndReason.EXTERNAL_CALL.value == "external_call"
    provider_values = {r.value for r in ProviderDisconnectReason}
    event_values = {r.value for r in CallEndReason}
    assert provider_values < event_values

    # Valid enum members and their raw wire spellings both validate.
    lsc = LineStateChangedEvent(
        line_id=LineId("line-main"),
        old_state=LineState.FREE,
        new_state=LineState.BUSY,
        reason=CallEndReason.EXTERNAL_CALL,
    )
    assert lsc.reason is CallEndReason.EXTERNAL_CALL
    csc = CallStateChangedEvent.model_validate(
        {
            "lease_id": "l-1",
            "line_id": "line-main",
            "state": "disconnected",
            "reason": "provider_lost",
        }
    )
    assert csc.reason is CallEndReason.PROVIDER_LOST

    # Round 22 (review-10 HIGH-1): ``state`` rides the WIRE
    # vocabulary - an internal CallPhase ("ended") has no wire
    # spelling and is rejected.
    with pytest.raises(ValidationError):
        CallStateChangedEvent.model_validate(
            {
                "lease_id": "l-1",
                "line_id": "line-main",
                "state": "ended",
            }
        )

    # A reason outside the closed vocabulary is rejected on all three
    # event payloads.
    with pytest.raises(ValidationError):
        CallStateChangedEvent(
            lease_id=LeaseId("l-1"),
            line_id=LineId("line-main"),
            state="disconnected",
            reason="made_up_reason",
        )
    with pytest.raises(ValidationError):
        LineStateChangedEvent(
            line_id=LineId("line-main"),
            old_state=LineState.BUSY,
            new_state=LineState.FREE,
            reason="made_up_reason",
        )
    with pytest.raises(ValidationError):
        MissedCallEvent(
            line_id=LineId("line-main"),
            reason="made_up_reason",
        )
    # reason is REQUIRED on missed_call (wire table marks it non-?).
    with pytest.raises(ValidationError):
        MissedCallEvent(line_id=LineId("line-main"))  # type: ignore[call-arg]


def test_ingress_payloads_validate() -> None:
    # Round 27 (review-17 items 44/45): ``provider.call_state`` is
    # POST-RING only - ``state="ringing"`` rides
    # ``IncomingRingIngress`` (the kernel mints the call_token there),
    # and ``caller_id`` is a ring-only fact never re-reported on
    # post-ring updates. Both are hard model rejections.
    with pytest.raises(ValidationError):
        ProviderCallStateUpdate(
            line_id=LineId("line-main"),
            assignment_id=contract.AssignmentId("asg-1"),
            call_token=CallToken(RedactedString("tok-1")),
            state=ProviderCallState.RINGING,
            observed_at=NOW_UTC,
        )
    # Round 33: ``caller_id`` is REOPENED on post-ring updates as
    # bounded correlation evidence (one of the two positive-evidence
    # fields the sole-live-lease rule requires - the other is
    # ``provider_call_id``). It is evidence, never identity: the
    # field accepts the same normalized-E.164 shape as the
    # ring-ingress carrier.
    ProviderCallStateUpdate(
        line_id=LineId("line-main"),
        assignment_id=contract.AssignmentId("asg-1"),
        caller_id=RedactedString(_RAW_NUMBER),
        state=ProviderCallState.ACTIVE,
        observed_at=NOW_UTC,
    )
    # ...while the pre-answer hangup the token exists for remains
    # legal: the provider echoes the kernel-minted call_token on the
    # DISCONNECTED update for a still-open ring.
    ProviderCallStateUpdate(
        line_id=LineId("line-main"),
        assignment_id=contract.AssignmentId("asg-1"),
        call_token=CallToken(RedactedString("tok-1")),
        state=ProviderCallState.DISCONNECTED,
        reason=ProviderDisconnectReason.NORMAL,
        observed_at=NOW_UTC,
    )
    # Round 21: external human-initiated call - an update with NEITHER
    # call_token nor lease_id is legal and means "the owner dialed or
    # answered on the handset itself" (line -> BUSY external_call,
    # never Hangup; design §9).
    external = ProviderCallStateUpdate(
        line_id=LineId("line-main"),
        assignment_id=contract.AssignmentId("asg-1"),
        state=ProviderCallState.ACTIVE,
        observed_at=NOW_UTC,
    )
    assert external.call_token is None and external.lease_id is None
    # reason rides the CLOSED ProviderDisconnectReason vocabulary.
    ProviderCallStateUpdate(
        line_id=LineId("line-main"),
        assignment_id=contract.AssignmentId("asg-1"),
        lease_id=LeaseId("l-1"),
        state=ProviderCallState.DISCONNECTED,
        reason=ProviderDisconnectReason.NORMAL,
        observed_at=NOW_UTC,
    )
    with pytest.raises(ValidationError):
        ProviderCallStateUpdate.model_validate(
            {
                "line_id": "line-main",
                "assignment_id": "asg-1",
                "lease_id": "l-1",
                "state": "disconnected",
                "reason": "provider_lost",  # gateway-minted, not provider-reported
                "observed_at": NOW_UTC,
            }
        )
    ProviderLiveness(
        line_id=LineId("line-main"),
        # Round 25 (review-15 item 32): assignment_id is REQUIRED -
        # a liveness fact names its binding.
        assignment_id=contract.AssignmentId("bind-1"),
        state=LivenessState.UNREACHABLE,
        checked_at=NOW_UTC,
        channel_seq=1,
    )
    # Round 21 (CRITICAL-3): ws_disconnected is the WS-drop fact that
    # starts the orphan-grace window - distinct from UNREACHABLE
    # (heartbeat timeout -> immediate teardown).
    ProviderLiveness(
        line_id=LineId("line-main"),
        assignment_id=contract.AssignmentId("asg-1"),
        state=LivenessState.WS_DISCONNECTED,
        checked_at=NOW_UTC,
        channel_seq=1,
    )
    # Round-21 review (WARNING-2): a ping may carry the same
    # state_snapshot shape as reregister - ProviderLiveness is its
    # domain carrier.
    ping = ProviderLiveness(
        line_id=LineId("line-main"),
        assignment_id=contract.AssignmentId("asg-1"),
        state=LivenessState.REACHABLE,
        checked_at=NOW_UTC,
        channel_seq=1,
        state_snapshot=[
            ProviderActiveCall(lease_id=LeaseId("l-1"), state=ProviderCallState.ACTIVE),
            ProviderActiveCall(state=ProviderCallState.DIALING),  # external call
        ],
    )
    assert ping.state_snapshot is not None
    assert ping.state_snapshot[1].lease_id is None
    # Round 21 (CRITICAL-3): reregister carries a state_snapshot of
    # the provider's live calls for orphan-grace reconciliation.
    reg = ProviderRegistration(
        line_id=LineId("line-main"),
        caps=[Intent.DIAL_OUT],
        channel_seq=1,
        state_snapshot=[
            ProviderActiveCall(lease_id=LeaseId("l-1"), state=ProviderCallState.ACTIVE),
            ProviderActiveCall(state=ProviderCallState.DIALING),  # external call
        ],
    )
    assert reg.state_snapshot is not None
    assert reg.state_snapshot[1].lease_id is None
    # Round 22 (review-10 MEDIUM-3): provider.call_state carries
    # caller_id on ringing; the token is NO LONGER an ingress input -
    # the kernel mints it inside ingest_ring and the adapter relays it
    # back as the provider.call_state response {ok, call_token}.
    ring = IncomingRingIngress(
        line_id=LineId("line-main"),
        assignment_id=contract.AssignmentId("asg-1"),
        caller_id=RedactedString(_RAW_NUMBER),
        observed_at=NOW_UTC,
    )
    assert "call_token" not in IncomingRingIngress.__pydantic_fields__
    assert ring.caller_id is not None
    # Round 27 (review-17 items 44/45): caller_id and the RINGING
    # state are ring-ingress facts ONLY - they are rejected on
    # post-ring updates even together.
    with pytest.raises(ValidationError):
        ProviderCallStateUpdate(
            line_id=LineId("line-main"),
            assignment_id=contract.AssignmentId("asg-1"),
            caller_id=RedactedString(_RAW_NUMBER),
            state=ProviderCallState.RINGING,
            observed_at=NOW_UTC,
        )
    # Round 22 (review-10 CRITICAL-1): capability/profile refresh on
    # an active binding - no token.
    cap_update = ProviderCapabilityUpdate(
        line_id=LineId("line-main"),
        assignment_id=contract.AssignmentId("asg-1"),
        caps=(Intent.DIAL_OUT, Intent.RECEIVE_INBOUND),
        profile="secretary",
    )
    assert "token" not in ProviderCapabilityUpdate.__pydantic_fields__
    assert cap_update.profile == "secretary"
    decision = FallbackDecision(
        line_id=LineId("line-main"),
        call_token=CallToken(RedactedString("tok-1")),
        policy=NoPickupFallback(),
    )
    assert decision.policy.policy == "no_pickup"


def test_phonebridge_error_shape() -> None:
    err = PhonebridgeError(
        ErrorCode.STALE_TOKEN,
        ErrorData(reason=ErrorReason.ACCEPT_WINDOW_EXPIRED, detail="window lapsed"),
    )
    assert err.code is ErrorCode.STALE_TOKEN
    assert err.data.reason is ErrorReason.ACCEPT_WINDOW_EXPIRED
    assert "STALE_TOKEN" in str(err)


# ---------------------------------------------------------------------------
# LineManager ABC: async surface, abstractness, minimal fake roundtrip
# ---------------------------------------------------------------------------


class _FakeLineManager(LineManager):
    """Minimal concrete implementation exercising the async surface.

    Round 30 (SA_carbon_review_19 slice-C item 5): the fake MUST be
    contract-conforming, not a blind stub - it tracks the intents a
    session registered so ``accept`` can enforce the
    ``Intent.RECEIVE_INBOUND`` capability gate, and it tracks the
    live ``ProviderBinding`` ids minted by ``register_provider`` so
    every ``provider.*`` ingress rejects a superseded or never-seen
    ``assignment_id`` with ``STALE_TOKEN`` (the closed-assignment
    ring semantics a real kernel implements)."""

    def __init__(self) -> None:
        self._session_seq = 0
        self._intents: dict[str, set[Intent]] = {}
        self._live_bindings: set[str] = set()

    async def register_intent(
        self,
        agent_id: str,
        intents: Collection[Intent],
        *,
        profile: str | None = None,
    ) -> SessionId:
        self._session_seq += 1
        session_id = SessionId(f"sess-{self._session_seq}")
        self._intents[str(session_id)] = set(intents)
        return session_id

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
        return AcquireGranted(
            line_id=LineId("line-main"), lease_id=LeaseId("l-1"), expires_at=NOW_UTC
        )

    async def cancel_acquire(self, session_id: SessionId, ticket_id: TicketId) -> None:
        return None

    async def release(self, session_id: SessionId, lease_id: LeaseId) -> None:
        return None

    async def release_assignment(
        self, session_id: SessionId, assignment_id: contract.AssignmentId
    ) -> None:
        return None

    async def dial(
        self,
        session_id: SessionId,
        lease_id: LeaseId,
        number: RedactedString,
        country: str,
    ) -> DialResult:
        return DialResult(
            lease_id=lease_id,
            media_endpoint="unix:///run/m.sock",
            media_token=MediaToken(RedactedString("mt")),
        )

    async def accept(self, session_id: SessionId, call_token: CallToken) -> AcceptResult:
        # Contract-conforming capability gate (round 30, item 5):
        # ``accept`` is an INBOUND operation - a session that never
        # registered Intent.RECEIVE_INBOUND cannot answer a ring.
        if Intent.RECEIVE_INBOUND not in self._intents.get(str(session_id), set()):
            raise PhonebridgeError(ErrorCode.INVALID_INTENT)
        return AcceptResult(
            lease_id=LeaseId("l-2"),
            line_id=LineId("line-main"),
            media_endpoint="unix:///run/m.sock",
            media_token=MediaToken(RedactedString("mt")),
        )

    async def hangup(self, session_id: SessionId, lease_id: LeaseId) -> None:
        return None

    async def send_dtmf(self, session_id: SessionId, lease_id: LeaseId, digits: str) -> None:
        return None

    async def reattach_media(self, session_id: SessionId, lease_id: LeaseId) -> MediaReattachResult:
        return MediaReattachResult(
            lease_id=lease_id,
            media_endpoint="unix:///run/m.sock",
            media_token=MediaToken(RedactedString("mt-2")),
        )

    async def reattach_provider_media(
        self, request: ProviderMediaReattachRequest
    ) -> ProviderMediaReattachResult:
        # Round 27 (review-17 item 4): provider-side re-pairing when
        # the media socket died but the control channel stayed up.
        return ProviderMediaReattachResult(
            media_endpoint="unix:///run/m.sock",
            media_token=MediaToken(RedactedString("mt-3")),
        )

    async def claim_exclusive(self, session_id: SessionId, line_id: LineId) -> contract.ClaimResult:
        return contract.ClaimResult(claim_id=ClaimId("claim-1"))

    async def get_lines(
        self,
        session_id: SessionId,
        intent: Intent | None = None,
        *,
        offset: int = 0,
        limit: int = contract.MAX_LIST_PAGE,
    ) -> list[LineSummary]:
        return [
            LineSummary(
                line_id=LineId("line-main"),
                caps=[Intent.DIAL_OUT],
                state=LineState.FREE,
                # Round 26 (review-16 item 13): non-empty caps require
                # a bound liveness - the new UNBOUND default is for
                # provider-less rows only.
                liveness=LivenessState.REACHABLE,
            )
        ]

    def _require_live_binding(self, assignment_id: contract.AssignmentId) -> None:
        """Shared provider-ingress guard: every ``provider.*`` fact
        on a superseded or never-registered ``assignment_id`` is a
        STALE_TOKEN-class fact - the closed-assignment ring keeps it
        a dedupe/no-op instead of a resurrected binding."""
        if str(assignment_id) not in self._live_bindings:
            raise PhonebridgeError(ErrorCode.STALE_TOKEN)

    async def register_provider(self, registration: ProviderRegistration) -> ProviderRegisterResult:
        # Round 30 (SA_carbon_review_19 slice-A item 11): the register
        # ingress returns the binding plus any rings its
        # state_snapshot re-minted through the ingest_ring path.
        self._live_bindings.add("bind-1")
        return ProviderRegisterResult(
            binding=ProviderBinding(
                assignment_id=contract.AssignmentId("bind-1"),
                line_id=registration.line_id,
                caps=registration.caps,
                # Round 25 (review-15 item 32): stamped thresholds from the
                # line's effective LineTimeouts - required, no default.
                heartbeat_degraded_after=2,
                heartbeat_down_after=3,
                connected_at=NOW_UTC,
                last_seen_at=NOW_UTC,
                channel_seq=registration.channel_seq,
            ),
            pending_rings=(
                PendingRing(
                    call_token=CallToken(RedactedString("tok-snap")),
                    observed_at=NOW_UTC,
                ),
            ),
        )

    async def note_provider_liveness(self, liveness: ProviderLiveness) -> tuple[PendingRing, ...]:
        self._require_live_binding(liveness.assignment_id)
        return ()

    async def ingest_ring(self, ring: IncomingRingIngress) -> IncomingCallEvent | None:
        # Round 30 (SA_carbon_review_19 slice-A item 6): the return is
        # nullable - a ring parked while the previous ring's drain is
        # pending reports no event yet.
        self._require_live_binding(ring.assignment_id)
        return IncomingCallEvent(
            line_id=ring.line_id,
            call_token=CallToken(RedactedString("tok-minted")),
            caller_id=ring.caller_id,
            expires_at=NOW_UTC,
        )

    async def update_provider_capabilities(
        self, update: ProviderCapabilityUpdate
    ) -> ProviderBinding:
        self._require_live_binding(update.assignment_id)
        return ProviderBinding(
            assignment_id=update.assignment_id,
            line_id=update.line_id,
            caps=update.caps,
            profile=update.profile,
            heartbeat_degraded_after=2,
            heartbeat_down_after=3,
            connected_at=NOW_UTC,
            last_seen_at=NOW_UTC,
            channel_seq=1,
        )

    async def ingest_call_state(self, update: ProviderCallStateUpdate) -> None:
        self._require_live_binding(update.assignment_id)
        return None

    async def ingest_media_state(
        self, lease_id: LeaseId, role: contract.MediaRole, attached: bool
    ) -> None:
        # Round 30 (SA_carbon_review_19 slice-A item 1): media-side
        # attach/detach fact ingress - the kernel arms/disarms the
        # MEDIA_PAIR_TTL_SEC re-attach deadline per role.
        return None

    async def ingest_audio_ready(
        self,
        line_id: LineId,
        assignment_id: contract.AssignmentId,
        lease_id: LeaseId,
    ) -> None:
        self._require_live_binding(assignment_id)
        return None

    async def ingest_audio_lost(
        self,
        line_id: LineId,
        assignment_id: contract.AssignmentId,
        lease_id: LeaseId | None = None,
    ) -> None:
        # Round 31 (SA_swe2_review_1 item 4): SCO-loss fact resets
        # the kernel-side audio_ready latch; ``lease_id=None`` is the
        # line-scoped reset form.
        self._require_live_binding(assignment_id)
        return None

    async def ingest_media_overflow(self, line_id: LineId, lease_id: LeaseId) -> None:
        # Round 32 (SA_swe2_review_2 item 11): the media-router
        # PCM-drop ingress - the ONLY event.media_overflow source;
        # the SCO-loss path above never emits it.
        return None

    async def compensate_dial(
        self,
        line_id: LineId,
        assignment_id: contract.AssignmentId,
        lease_id: LeaseId,
    ) -> None:
        # Round 31 (SA_swe2_review_1 item 5): the adapter's
        # binding-scoped compensation for a rejected/timed-out
        # line.dial or line.answer - NOT the owner-scoped hangup.
        self._require_live_binding(assignment_id)
        return None

    async def compensate_answer(
        self,
        line_id: LineId,
        assignment_id: contract.AssignmentId,
        lease_id: LeaseId,
        call_token: CallToken,
    ) -> None:
        # Round 32 (SA_swe2_review_2 item 9): ring-scoped answer
        # compensation - ring reject by call_token while the line
        # stays RINGING_LOCKED under the drain rule.
        self._require_live_binding(assignment_id)
        return None

    async def ingest_command_ack(self, ack: contract.ProviderCommandAck) -> None:
        # Round 33: the typed provider-command acknowledgement fact
        # ingress - the same binding-scope guard as every other
        # ``provider.*`` fact (a superseded assignment_id is
        # STALE_TOKEN through the closed-assignment ring).
        self._require_live_binding(ack.assignment_id)
        return None

    async def decide_fallback(self, line_id: LineId, call_token: CallToken) -> FallbackDecision:
        return FallbackDecision(line_id=line_id, call_token=call_token, policy=NoPickupFallback())

    async def drop_session(self, session_id: SessionId) -> None:
        return None

    async def tick(self, now: contract.AwareDatetime) -> None:
        # Round 30 (SA_carbon_review_19 slice-C item 5): the tick
        # clock is typed aware-UTC - a naive ``now`` is a contract
        # violation, not an implementation choice.
        return None

    def drain_events(self) -> list[EventEnvelope[Any]]:
        return [
            EventEnvelope[MissedCallEvent](
                name=EventName.MISSED_CALL,
                # Round 25 (review-15 item 31): missed_call carries
                # caller PII - legal scopes are agent|session only,
                # never broadcast.
                audience=AgentAudience(agent_id="agent-1"),
                emitted_at=NOW_UTC,
                payload=MissedCallEvent(line_id=LineId("line-main"), reason=CallEndReason.NORMAL),
            )
        ]

    def drain_provider_commands(self) -> list[ProviderCommand]:
        return [
            ProviderHangupCommand(
                line_id=LineId("line-main"),
                assignment_id=contract.AssignmentId("bind-1"),
                lease_id=LeaseId("l-1"),
            )
        ]


def test_line_manager_is_abstract() -> None:
    with pytest.raises(TypeError):
        LineManager()  # type: ignore[abstract]


async def test_line_manager_async_surface() -> None:
    """Non-e2e async exercise: EVERY abstract method is awaitable and
    returns its contracted result type (all 30 methods - 28 async +
    the two sync outbox drains; round 30 adds ``ingest_media_state``
    and the ``ProviderRegisterResult`` return on
    ``register_provider``, round 31 adds ``ingest_audio_lost`` and
    ``compensate_dial``, round 32 adds ``ingest_media_overflow``
    and ``compensate_answer``, round 33 adds
    ``ingest_command_ack``)."""
    assert set(LineManager.__abstractmethods__) == {
        "register_intent",
        "acquire",
        "cancel_acquire",
        "release",
        "release_assignment",
        "dial",
        "accept",
        "hangup",
        "send_dtmf",
        "reattach_media",
        # Round 27 (review-17 item 4): provider-side media re-pairing
        # while the control channel is still up.
        "reattach_provider_media",
        "claim_exclusive",
        "get_lines",
        "register_provider",
        "update_provider_capabilities",
        "note_provider_liveness",
        "ingest_ring",
        "ingest_call_state",
        "ingest_audio_ready",
        # Round 31 (SA_swe2_review_1 items 4/5): the SCO-loss latch
        # reset and the binding-scoped dial/answer compensation
        # entry points.
        "ingest_audio_lost",
        "compensate_dial",
        # Round 30 (SA_carbon_review_19 slice-A item 1): media
        # attach/detach facts ride their own ingress so the
        # MEDIA_PAIR_TTL_SEC re-attach window has a driver.
        "ingest_media_state",
        # Round 32 (SA_swe2_review_2 items 9/11): the media-router
        # overflow ingress and the ring-scoped answer compensation.
        "ingest_media_overflow",
        "compensate_answer",
        # Round 33: the typed provider-command acknowledgement fact
        # ingress - the kernel's ``{accepted}`` handshake gate.
        "ingest_command_ack",
        "decide_fallback",
        "drop_session",
        "tick",
        "drain_events",
        "drain_provider_commands",
    }
    mgr = _FakeLineManager()

    # --- agent-facing surface ---
    session = await mgr.register_intent("agent-1", {Intent.DIAL_OUT})
    result = await mgr.acquire(session, Intent.DIAL_OUT, idempotency_key="k-1")
    assert isinstance(result, AcquireGranted)
    assert isinstance(result.lease_id, str)
    assert result.expires_at.tzinfo is not None  # grant TTL rides the wire
    await mgr.cancel_acquire(session, TicketId("ticket-1"))
    dial = await mgr.dial(session, result.lease_id, RedactedString(_RAW_NUMBER), "MD")
    # Round 25 (review-15 item 3): media_token is a RedactedString
    # container, never a raw str.
    assert isinstance(dial.media_token, RedactedString)
    # Round 30 (SA_carbon_review_19 slice-C item 5): ``accept`` is a
    # capability-gated INBOUND op - the dial-out-only session above
    # MUST be refused INVALID_INTENT, and a session holding
    # Intent.RECEIVE_INBOUND is the one that answers the ring.
    with pytest.raises(PhonebridgeError) as excinfo:
        await mgr.accept(session, CallToken(RedactedString("tok-1")))
    assert excinfo.value.code is ErrorCode.INVALID_INTENT
    inbound_session = await mgr.register_intent("agent-2", {Intent.RECEIVE_INBOUND})
    accepted = await mgr.accept(inbound_session, CallToken(RedactedString("tok-1")))
    assert isinstance(accepted, AcceptResult)
    await mgr.send_dtmf(session, result.lease_id, "12#")
    reattached = await mgr.reattach_media(session, result.lease_id)
    # Round 25 (review-15 item 9): fresh agent-side media pair for a
    # live lease; the token stays a RedactedString container.
    assert isinstance(reattached, MediaReattachResult)
    assert isinstance(reattached.media_token, RedactedString)
    # Round 27 (review-17 item 4): provider-side re-pairing on a live
    # control channel - fresh provider-role credentials.
    provider_pair = await mgr.reattach_provider_media(
        ProviderMediaReattachRequest(
            line_id=LineId("line-main"),
            assignment_id=contract.AssignmentId("bind-1"),
            lease_id=result.lease_id,
        )
    )
    assert isinstance(provider_pair, ProviderMediaReattachResult)
    assert isinstance(provider_pair.media_token, RedactedString)
    await mgr.hangup(session, result.lease_id)
    await mgr.release(session, result.lease_id)
    lines = await mgr.get_lines(session)
    assert lines[0].state is LineState.FREE
    claim = await mgr.claim_exclusive(session, LineId("line-main"))
    assert claim.status == "pending_approval"

    # --- provider ingress ---
    register_result = await mgr.register_provider(
        ProviderRegistration(
            line_id=LineId("line-main"),
            caps=[Intent.DIAL_OUT],
            channel_seq=1,
        )
    )
    # Round 30 (SA_carbon_review_19 slice-A item 11): the register
    # ingress returns a ProviderRegisterResult - the binding plus the
    # rings a state_snapshot re-minted through ingest_ring.
    assert isinstance(register_result, ProviderRegisterResult)
    assert isinstance(register_result.binding, ProviderBinding)
    assert all(isinstance(r, PendingRing) for r in register_result.pending_rings)
    await mgr.note_provider_liveness(
        ProviderLiveness(
            line_id=LineId("line-main"),
            assignment_id=contract.AssignmentId("bind-1"),
            state=LivenessState.REACHABLE,
            checked_at=NOW_UTC,
            channel_seq=1,
        )
    )
    # Round 30 (SA_carbon_review_19 slice-C item 5): provider ingress
    # carries the LIVE ProviderBinding id - not a released agent-side
    # LineAssignment id; a stale/superseded one below is STALE_TOKEN.
    with pytest.raises(PhonebridgeError) as excinfo:
        await mgr.ingest_ring(
            IncomingRingIngress(
                line_id=LineId("line-main"),
                assignment_id=contract.AssignmentId("asg-1"),
                observed_at=NOW_UTC,
            )
        )
    assert excinfo.value.code is ErrorCode.STALE_TOKEN
    ring_event = await mgr.ingest_ring(
        IncomingRingIngress(
            line_id=LineId("line-main"),
            assignment_id=contract.AssignmentId("bind-1"),
            observed_at=NOW_UTC,
        )
    )
    assert isinstance(ring_event, IncomingCallEvent)
    # kernel-minted (round 22); a RedactedString container since
    # round 25 (review-15 item 3).
    assert isinstance(ring_event.call_token, RedactedString)
    updated_binding = await mgr.update_provider_capabilities(
        ProviderCapabilityUpdate(
            line_id=LineId("line-main"),
            assignment_id=contract.AssignmentId("bind-1"),
            caps=(Intent.DIAL_OUT,),
        )
    )
    assert isinstance(updated_binding, ProviderBinding)
    await mgr.release_assignment(session, contract.AssignmentId("asg-1"))
    await mgr.ingest_call_state(
        ProviderCallStateUpdate(
            line_id=LineId("line-main"),
            assignment_id=contract.AssignmentId("bind-1"),
            state=ProviderCallState.ACTIVE,
            observed_at=NOW_UTC,
        )
    )
    # Round 30 (SA_carbon_review_19 slice-C item 5): decide_fallback
    # is KERNEL-INTERNAL - adapters MUST NOT call it; the surface
    # pin is that the method exists, is declared internal-only, and
    # its FallbackDecision carries no worker token on a core policy.
    fallback_doc = LineManager.decide_fallback.__doc__ or ""
    assert "internal" in fallback_doc.lower()
    assert (
        FallbackDecision(
            line_id=LineId("line-main"),
            call_token=CallToken(RedactedString("tok-3")),
            policy=NoPickupFallback(),
        ).fallback_call_token
        is None
    )
    # Round 30 (SA_carbon_review_19 slice-A item 1): media attach/
    # detach facts feed the per-role re-attach deadline.
    await mgr.ingest_media_state(result.lease_id, "agent", True)
    await mgr.ingest_media_state(result.lease_id, "provider", False)
    await mgr.ingest_audio_ready(
        LineId("line-main"), contract.AssignmentId("bind-1"), result.lease_id
    )
    # Round 31 (SA_swe2_review_1 items 4/5): the SCO-loss latch reset
    # (lease-scoped AND line-scoped forms) and the binding-scoped
    # dial/answer compensation are part of the provider-ingress
    # surface; both ride the live-binding guard too.
    await mgr.ingest_audio_lost(
        LineId("line-main"), contract.AssignmentId("bind-1"), result.lease_id
    )
    await mgr.ingest_audio_lost(LineId("line-main"), contract.AssignmentId("bind-1"))
    await mgr.compensate_dial(LineId("line-main"), contract.AssignmentId("bind-1"), result.lease_id)
    with pytest.raises(PhonebridgeError) as excinfo:
        await mgr.compensate_dial(
            LineId("line-main"), contract.AssignmentId("asg-1"), result.lease_id
        )
    assert excinfo.value.code is ErrorCode.STALE_TOKEN
    # Round 32 (SA_swe2_review_2 items 9/11): the router overflow
    # ingress and the ring-scoped answer compensation ride the same
    # live-binding guard.
    await mgr.ingest_media_overflow(LineId("line-main"), result.lease_id)
    await mgr.compensate_answer(
        LineId("line-main"),
        contract.AssignmentId("bind-1"),
        result.lease_id,
        CallToken(RedactedString("tok-1")),
    )
    with pytest.raises(PhonebridgeError) as excinfo:
        await mgr.compensate_answer(
            LineId("line-main"),
            contract.AssignmentId("asg-1"),
            result.lease_id,
            CallToken(RedactedString("tok-1")),
        )
    assert excinfo.value.code is ErrorCode.STALE_TOKEN
    # Round 33: the typed ``{accepted}`` command-acknowledgement fact
    # ingress rides the same live-binding guard - a reply distilled
    # off a superseded channel can never resolve a pending handshake.
    await mgr.ingest_command_ack(
        contract.ProviderCommandAck(
            line_id=LineId("line-main"),
            assignment_id=contract.AssignmentId("bind-1"),
            command="dial",
            lease_id=result.lease_id,
            accepted=True,
            provider_call_id="ofono/call0",
        )
    )
    with pytest.raises(PhonebridgeError) as excinfo:
        await mgr.ingest_command_ack(
            contract.ProviderCommandAck(
                line_id=LineId("line-main"),
                assignment_id=contract.AssignmentId("asg-1"),
                command="dial",
                lease_id=result.lease_id,
                accepted=True,
            )
        )
    assert excinfo.value.code is ErrorCode.STALE_TOKEN
    await mgr.drop_session(session)

    # --- lifecycle hook + outbox drains (round 20) ---
    await mgr.tick(NOW_UTC)
    drained_events = mgr.drain_events()
    assert all(isinstance(env, EventEnvelope) for env in drained_events)
    drained_commands = mgr.drain_provider_commands()
    assert all(
        isinstance(
            cmd,
            (
                ProviderDialCommand,
                ProviderAnswerCommand,
                ProviderHangupCommand,
                ProviderSendDtmfCommand,
                ProviderRevokeCommand,
                ProviderReattachMediaCommand,
            ),
        )
        for cmd in drained_commands
    )


# ---------------------------------------------------------------------------
# Round 20 hardening: wire-state parity, outbox commands, DTMF, aware-UTC,
# idempotency key, fake admin kernel
# ---------------------------------------------------------------------------


def test_call_phase_wire_state_parity() -> None:
    """Round 20 drift test: every CallPhase has an explicit wire-state
    mapping in CALL_PHASE_WIRE_STATES, and the doc's
    event.call_state_changed state vocabulary equals the mapping's
    non-None values. Round 29 (SA_quant_review_1 slice-C item 3):
    the full mapping is pinned verbatim - which phases are
    kernel-internal (``None``) and which emit a concrete wire
    string, so a swapped pair can no longer hide behind set
    equality."""
    assert set(CALL_PHASE_WIRE_STATES.keys()) == set(CallPhase)
    assert CALL_PHASE_WIRE_STATES == {
        CallPhase.ACQUIRED: None,  # pre-dial grant - never emitted
        CallPhase.DIALING: "dialing",
        CallPhase.RINGING: None,  # surfaces as event.incoming_call
        # Round 31 (SA_swe2_review_1 item 2): answer-in-flight is
        # transient like TERMINATING - the four-value wire
        # vocabulary never spells it.
        CallPhase.ANSWERING: None,
        CallPhase.ALERTING: "alerting",
        CallPhase.ACTIVE: "active",
        CallPhase.TERMINATING: None,  # transient teardown leg
        CallPhase.ENDED: "disconnected",
    }
    doc = API_CONTRACT_MD.read_text(encoding="utf-8")
    row = next(line for line in doc.splitlines() if "`event.call_state_changed`" in line)
    state_seg = re.search(r"`state`:([^;(]+)", row)
    assert state_seg is not None
    doc_states = set(re.findall(r"`([a-z_]+)`", state_seg.group(1)))
    mapped = {v for v in CALL_PHASE_WIRE_STATES.values() if v is not None}
    assert doc_states == mapped


def test_call_phase_forward_matrix() -> None:
    """Round 28 (review-18 item 16; ``DIALING -> ACTIVE`` added in the
    round-28 fix, PR #72 review): the forward-only transition matrix
    is pinned verbatim - alerting-less answers are a legal provider
    fact, regressions are defects, and ENDED is terminal."""
    # Every phase has an explicit row - the matrix can never drift
    # into an implicit default.
    assert set(CALL_PHASE_FORWARD_TRANSITIONS) == set(CallPhase)
    assert CALL_PHASE_FORWARD_TRANSITIONS == {
        # Round 30 (SA_carbon_review_19 slice-A item 16): the
        # ACQUIRED -> RINGING edge is gone - inbound-ring state is a
        # LINE sub-state, never a lease phase; a ring preempting an
        # undialed grant ends it ENDED.
        CallPhase.ACQUIRED: frozenset({CallPhase.DIALING, CallPhase.ENDED}),
        CallPhase.DIALING: frozenset(
            {CallPhase.ALERTING, CallPhase.ACTIVE, CallPhase.TERMINATING, CallPhase.ENDED}
        ),
        # Round 31 (SA_swe2_review_1 item 2): the RINGING row keeps
        # an EMPTY target set - the enum member is reserved/dead for
        # wire compat, no lease may carry it and nothing may reach
        # it (the former ``RINGING -> ACTIVE/ENDED`` edges implied
        # the member was still a live phase).
        CallPhase.RINGING: frozenset(),
        # ANSWERING is accept()'s answer-in-flight phase - the
        # inbound mirror of DIALING, advanced only by the provider's
        # answer confirmation or a disconnected fact.
        CallPhase.ANSWERING: frozenset({CallPhase.ACTIVE, CallPhase.TERMINATING, CallPhase.ENDED}),
        CallPhase.ALERTING: frozenset({CallPhase.ACTIVE, CallPhase.TERMINATING, CallPhase.ENDED}),
        CallPhase.ACTIVE: frozenset({CallPhase.TERMINATING, CallPhase.ENDED}),
        CallPhase.TERMINATING: frozenset({CallPhase.ENDED}),
        CallPhase.ENDED: frozenset(),
    }
    # Round-31 fix round (PR #78 warning 1): TERMINATING is
    # reachable from EVERY phase where a call leg may exist -
    # hangup() parks a possibly-live dialed call in TERMINATING
    # awaiting the provider's disconnected/hold timeout; ACQUIRED
    # stays the one pre-dial phase that ends ENDED directly.
    for phase in (
        CallPhase.DIALING,
        CallPhase.ANSWERING,
        CallPhase.ALERTING,
        CallPhase.ACTIVE,
    ):
        assert CallPhase.TERMINATING in CALL_PHASE_FORWARD_TRANSITIONS[phase]
    assert CallPhase.TERMINATING not in CALL_PHASE_FORWARD_TRANSITIONS[CallPhase.ACQUIRED]
    # Forward-only shape: no self-loops, nothing leaves ENDED, and a
    # regression (ACTIVE -> DIALING/ALERTING) is never listed.
    assert CALL_PHASE_FORWARD_TRANSITIONS[CallPhase.ENDED] == frozenset()
    for old, targets in CALL_PHASE_FORWARD_TRANSITIONS.items():
        assert old not in targets
        for new in targets:
            assert new is not old
    assert CallPhase.ACTIVE not in CALL_PHASE_FORWARD_TRANSITIONS[CallPhase.ACTIVE]
    for regressed in (CallPhase.DIALING, CallPhase.ALERTING):
        assert regressed not in CALL_PHASE_FORWARD_TRANSITIONS[CallPhase.ACTIVE]
        assert regressed not in CALL_PHASE_FORWARD_TRANSITIONS[CallPhase.RINGING]
    # The alerting-less answer is the finding's case: DIALING may
    # advance straight to ACTIVE.
    assert CallPhase.ACTIVE in CALL_PHASE_FORWARD_TRANSITIONS[CallPhase.DIALING]
    # ACTIVE -> ENDED is a legal terminal transition (round 29,
    # SA_quant_review_1 slice-C item 1): the matrix pins the edge;
    # the event ``reason`` that accompanies the teardown is pinned
    # by test_call_end_reason_wired_into_events /
    # test_call_state_changed_reason_scoping, not by this matrix.
    assert CallPhase.ENDED in CALL_PHASE_FORWARD_TRANSITIONS[CallPhase.ACTIVE]


def test_provider_command_union_roundtrip() -> None:
    """Round 20: all six ProviderCommand variants serialize through
    TypeAdapter(ProviderCommand) and carry PII-safely (round 25
    batch 2 adds ProviderReattachMediaCommand)."""
    adapter = TypeAdapter(ProviderCommand)
    commands: list[ProviderCommand] = [
        ProviderDialCommand(
            line_id=LineId("line-main"),
            assignment_id=contract.AssignmentId("bind-1"),
            lease_id=LeaseId("l-1"),
            number=RedactedString(_RAW_NUMBER),
            media_endpoint="unix:///run/phonebridge/media.sock",
            media_token=MediaToken(RedactedString("p-tok-1")),
        ),
        ProviderAnswerCommand(
            line_id=LineId("line-main"),
            assignment_id=contract.AssignmentId("bind-1"),
            call_token=CallToken(RedactedString("tok-1")),
            lease_id=LeaseId("l-2"),
            media_endpoint="unix:///run/phonebridge/media.sock",
            media_token=MediaToken(RedactedString("p-tok-2")),
        ),
        ProviderHangupCommand(
            line_id=LineId("line-main"),
            assignment_id=contract.AssignmentId("bind-1"),
            lease_id=LeaseId("l-1"),
        ),
        ProviderSendDtmfCommand(
            line_id=LineId("line-main"),
            assignment_id=contract.AssignmentId("bind-1"),
            lease_id=LeaseId("l-1"),
            digits="123*#",
        ),
        ProviderRevokeCommand(
            line_id=LineId("line-main"),
            assignment_id=contract.AssignmentId("bind-1"),
            reason="admin_revoke",
        ),
        ProviderReattachMediaCommand(
            line_id=LineId("line-main"),
            assignment_id=contract.AssignmentId("bind-1"),
            lease_id=LeaseId("l-3"),
            media_endpoint="unix:///run/phonebridge/media.sock",
            media_token=MediaToken(RedactedString("p-tok-3")),
        ),
    ]
    for cmd in commands:
        dumped = cmd.model_dump()
        validated = adapter.validate_python(dumped)
        assert validated == cmd

    # The dial command's masked dump never leaks the dialed number.
    dial = commands[0]
    assert isinstance(dial, ProviderDialCommand)
    safe = dial.safe_dump()
    assert _RAW_NUMBER not in repr(safe)
    assert safe["number"] == "+373****45"
    # ...but the wire dump does carry it (provider needs the target).
    assert _RAW_NUMBER in dial.model_dump_json()


def test_ensure_aware_utc() -> None:
    """Round 20: the runtime helper rejects naive input and non-
    datetimes, and normalizes aware non-UTC input to UTC."""
    naive = datetime(2026, 11, 14, 12, 0, 0)
    with pytest.raises(ValueError):
        ensure_aware_utc(naive)
    with pytest.raises(TypeError):
        ensure_aware_utc("2026-11-14T12:00:00Z")  # type: ignore[arg-type]
    plus2 = datetime(2026, 11, 14, 14, 0, 0, tzinfo=timezone(timedelta(hours=2)))
    assert ensure_aware_utc(plus2) == datetime(2026, 11, 14, 12, 0, 0, tzinfo=timezone.utc)
    assert ensure_aware_utc(plus2).tzinfo is timezone.utc
    # Round 32 (SA_swe2_review_2 item 4): the plausibility bounds
    # are ABSOLUTE - a >= 2000-01-01 floor and a
    # ``datetime.max - gap`` ceiling - never now-relative, so a
    # stored/advisory stamp's legality cannot drift with the host
    # clock and the validator needs no injected ``now``.
    assert ensure_aware_utc(datetime(2000, 1, 1, tzinfo=timezone.utc)) == datetime(
        2000, 1, 1, tzinfo=timezone.utc
    )
    with pytest.raises(ValueError, match="min reasonable"):
        ensure_aware_utc(datetime(1999, 12, 31, 23, 59, 59, tzinfo=timezone.utc))
    with pytest.raises(ValueError, match="max reasonable"):
        ensure_aware_utc(datetime.max.replace(tzinfo=timezone.utc))


def test_dtmf_pattern() -> None:
    """Round 20: DTMF_PATTERN bounds digits to the legal alphabet and
    MAX_DTMF_DIGITS length."""
    assert DTMF_PATTERN.fullmatch("123*#ABCD,wW")
    assert not DTMF_PATTERN.fullmatch("")
    assert not DTMF_PATTERN.fullmatch("x")
    assert not DTMF_PATTERN.fullmatch("1" * (MAX_DTMF_DIGITS + 1))


def test_queue_ticket_idempotency_key() -> None:
    """Round 20: idempotency_key scopes the acquire retry-dedup."""
    ticket = QueueTicket(
        ticket_id=TicketId("t-1"),
        seq=1,
        session_id=SessionId("s-1"),
        intent=Intent.DIAL_OUT,
        enqueued_at=NOW_UTC,
        idempotency_key="idem-1",
    )
    assert ticket.idempotency_key == "idem-1"
    assert (
        QueueTicket(
            ticket_id=TicketId("t-2"),
            seq=2,
            session_id=SessionId("s-1"),
            intent=Intent.DIAL_OUT,
            enqueued_at=NOW_UTC,
        ).idempotency_key
        is None
    )
    # Round 23 (review-12 M2): a key is either absent or non-empty
    # and <= MAX_IDEMPOTENCY_KEY_LEN - empty/overlong keys are
    # INVALID_ARGUMENT-class violations at the model boundary.
    for bad in ("", "k" * (MAX_IDEMPOTENCY_KEY_LEN + 1)):
        with pytest.raises(ValidationError):
            QueueTicket(
                ticket_id=TicketId("t-3"),
                seq=3,
                session_id=SessionId("s-1"),
                intent=Intent.DIAL_OUT,
                enqueued_at=NOW_UTC,
                idempotency_key=bad,
            )
    assert MAX_IDEMPOTENCY_KEY_LEN == 128
    QueueTicket(
        ticket_id=TicketId("t-4"),
        seq=4,
        session_id=SessionId("s-1"),
        intent=Intent.DIAL_OUT,
        enqueued_at=NOW_UTC,
        idempotency_key="k" * MAX_IDEMPOTENCY_KEY_LEN,
    )


class _FakeLineAdminKernel(LineAdminKernel):
    """Minimal admin-kernel stub exercising all privileged and read
    methods (round 20 spec: the admin surface must be drivable
    independently of the agent surface; round 21 adds the list_*
    reads; round 22 adds ``assign_line`` + the timeout patch
    surface)."""

    async def assign_line(self, line_id: LineId, owner_agent_id: str) -> LineAssignment:
        return _assignment(mode=AssignmentMode.EXCLUSIVE, owner_agent_id=owner_agent_id)

    async def approve_claim(self, claim_id: ClaimId) -> LineAssignment:
        # Round 34 (iteration-2 audit item 25): an approved claim is
        # an ownership GRANT - the fake used to return a SHARED_POOL
        # owner-less record, which no real approval can produce (the
        # claim flow exists exactly to mint the EXCLUSIVE binding).
        return _assignment(mode=AssignmentMode.EXCLUSIVE, owner_agent_id="agent-1")

    async def reject_claim(self, claim_id: ClaimId, reason: str | None = None) -> None:
        return None

    async def list_pending_claims(
        self, *, offset: int = 0, limit: int = contract.MAX_LIST_PAGE
    ) -> list[PendingClaim]:
        # Round 30 (SA_carbon_review_19 slice-B item 1): every admin
        # listing is paged by the shared {offset?, limit?} window.
        return [
            PendingClaim(
                claim_id=ClaimId("claim-1"),
                line_id=LineId("line-main"),
                requester_agent_id="agent-1",
                session_id=SessionId("sess-1"),
                created_at=NOW_UTC,
            )
        ]

    async def list_assignments(
        self, *, offset: int = 0, limit: int = contract.MAX_LIST_PAGE
    ) -> list[LineAssignment]:
        return [_assignment()]

    async def list_provider_bindings(
        self, *, offset: int = 0, limit: int = contract.MAX_LIST_PAGE
    ) -> list[ProviderBinding]:
        return [
            ProviderBinding(
                assignment_id=contract.AssignmentId("bind-1"),
                line_id=LineId("line-main"),
                caps=(Intent.DIAL_OUT,),
                heartbeat_degraded_after=2,
                heartbeat_down_after=3,
                connected_at=NOW_UTC,
                last_seen_at=NOW_UTC,
                channel_seq=1,
            )
        ]

    async def list_lines(
        self, *, offset: int = 0, limit: int = contract.MAX_LIST_PAGE
    ) -> list[AdminLineView]:
        # Round 27 (review-17 item 3): the admin line view exposes
        # the durable/call ids the agent listing hides.
        return [await self.get_line(LineId("line-main"))]

    async def get_line(self, line_id: LineId) -> AdminLineView:
        # Round 31 (SA_swe2_review_1 item 23): the single-line read -
        # one full AdminLineView by id, LINE_NOT_FOUND-shaped lookup
        # semantics on the wire.
        return AdminLineView(
            line_id=line_id,
            caps=[Intent.DIAL_OUT],
            state=LineState.BUSY,
            liveness=LivenessState.REACHABLE,
            assignment_id=contract.AssignmentId("asg-1"),
            owner_agent_id="agent-1",
            lease_id=LeaseId("l-1"),
            queue_position=1,
        )

    async def get_assignment(self, line_id: LineId) -> LineAssignment:
        # Round 34 (iteration-2 audit item 18): the raw durable row
        # for write-through snapshot capture - LINE_NOT_FOUND-shaped
        # semantics like get_line.
        return LineAssignment(
            line_id=line_id,
            assignment_id=contract.AssignmentId("asg-1"),
            owner_agent_id=None,
            mode=contract.AssignmentMode.SHARED_POOL,
            claimable=True,
            last_seen_at=NOW_UTC,
        )

    async def list_leases(
        self, *, offset: int = 0, limit: int = contract.MAX_LIST_PAGE
    ) -> list[CallLease]:
        return [
            CallLease(
                lease_id=LeaseId("l-1"),
                line_id=LineId("line-main"),
                owner_agent_id="agent-1",
                session_id=SessionId("sess-1"),
                intent=Intent.DIAL_OUT,
                created_at=NOW_UTC,
                grant_expires_at=NOW_UTC + timedelta(seconds=5),
            )
        ]

    async def list_tickets(
        self, *, offset: int = 0, limit: int = contract.MAX_LIST_PAGE
    ) -> list[QueueTicket]:
        return [_ticket("t-1", 1)]

    async def remove_line(self, line_id: LineId) -> None:
        return None

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
        transport_profile: contract.TransportProfile | None = None,
        line_home_country: str | None = None,
        wake_hooks: frozenset[str] | None = None,
    ) -> LineAssignment:
        # Round 28 (review-18 item 6): the create half of the line
        # lifecycle - a fresh quiescent SHARED_POOL record; round 31
        # (SA_swe2_review_1 item 10) adds the hardware fields, round
        # 34 the wake-hook capability set.
        return _assignment(
            adapter_mac=adapter_mac,
            paired_phone_mac=paired_phone_mac,
            transport_profile=transport_profile or "loopback",
            line_home_country=line_home_country,
            wake_hooks=wake_hooks or frozenset(),
        )

    async def revoke_assignment(
        self, assignment_id: contract.AssignmentId, reason: str | None = None
    ) -> bool:
        # Round 31 (SA_swe2_review_1 item 23): the return shrank to a
        # boolean - True when an agent-owned LineAssignment reverted,
        # False on the provider-binding path or a tombstone-hit retry.
        return True

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
        transport_profile: contract.TransportProfile | None = None,
        line_home_country: str | None = None,
        wake_hooks: frozenset[str] | None = None,
    ) -> LineAssignment:
        return _assignment()

    async def hydrate_lines(
        self, rows: list[LineAssignment], bindings: list[ProviderBinding]
    ) -> None:
        # Round 30 (SA_carbon_review_19 slice-A item 2): batch
        # store-load hydration - records restore verbatim, no events,
        # no id rotation; round 31 (SA_swe2_review_1 item 3) loads
        # bindings dead (WS_DISCONNECTED, expired orphan deadline).
        return None

    async def restore_line(self, line_id: LineId, snapshot: LineAssignment | None) -> None:
        # Round 30 (SA_carbon_review_19 slice-A item 3): write-through
        # compensation - the ONLY kernel-side rollback hook after a
        # failed lines-row persistence; round 31 (SA_swe2_review_1
        # item 1) names the target record by line_id.
        return None

    def drain_events(self) -> list[EventEnvelope[Any]]:
        return []

    def drain_provider_commands(self) -> list[ProviderCommand]:
        return [
            ProviderRevokeCommand(
                line_id=LineId("line-main"),
                assignment_id=contract.AssignmentId("bind-1"),
            )
        ]


async def test_line_admin_kernel_all_methods() -> None:
    kernel = _FakeLineAdminKernel()
    # Round 22 (review-10 HIGH-5): admin assign is the durable-owner
    # override path - no pending claim needed.
    assigned = await kernel.assign_line(LineId("line-main"), "agent-9")
    assert assigned.mode is AssignmentMode.EXCLUSIVE
    assert assigned.owner_agent_id == "agent-9"
    granted = await kernel.approve_claim(ClaimId("claim-1"))
    assert isinstance(granted, LineAssignment)
    await kernel.reject_claim(ClaimId("claim-2"), reason="not claimable")
    pending = await kernel.list_pending_claims()
    assert pending[0].claim_id == ClaimId("claim-1")
    assert isinstance((await kernel.list_assignments())[0], LineAssignment)
    assert isinstance((await kernel.list_provider_bindings())[0], ProviderBinding)
    # Round 31 (SA_swe2_review_1 item 23): the revoke return shrank
    # to a boolean - True for an agent-owned revert, False on the
    # provider-binding/idempotent paths.
    post_revert = await kernel.revoke_assignment(contract.AssignmentId("asg-1"), reason="admin")
    assert post_revert is True
    # Round 27 (review-17 items 3/26): the admin inventory surface -
    # line views carrying the ids the agent listing hides, plus the
    # lease/ticket listings and the destructive line removal.
    lines = await kernel.list_lines()
    assert isinstance(lines[0], AdminLineView)
    assert lines[0].assignment_id == contract.AssignmentId("asg-1")
    # Round 31 (SA_swe2_review_1 item 23): the single-line read.
    detail = await kernel.get_line(LineId("line-main"))
    assert isinstance(detail, AdminLineView)
    assert detail.line_id == LineId("line-main")
    assert isinstance((await kernel.list_leases())[0], CallLease)
    assert isinstance((await kernel.list_tickets())[0], QueueTicket)
    # Round 30 (SA_carbon_review_19 slice-B item 1): the paged
    # listings accept the shared {offset?, limit?} window.
    assert isinstance((await kernel.list_assignments(offset=0, limit=1))[0], LineAssignment)
    assert isinstance((await kernel.list_pending_claims(limit=MAX_LIST_PAGE))[0], PendingClaim)
    assert isinstance((await kernel.list_provider_bindings(offset=0))[0], ProviderBinding)
    # Round 30 (SA_carbon_review_19 slice-A items 2/3): hydration and
    # the write-through compensation hook are part of the surface;
    # round 31 (SA_swe2_review_1 item 1) names the rollback target
    # by line_id.
    await kernel.hydrate_lines([_assignment()], [])
    await kernel.restore_line(LineId("line-main"), _assignment())
    await kernel.restore_line(LineId("line-main"), None)
    await kernel.remove_line(LineId("line-main"))
    # Round 28 (review-18 item 6): provision_line creates a fresh
    # quiescent line record; round 31 (SA_swe2_review_1 item 10)
    # accepts the hardware fields.
    provisioned = await kernel.provision_line(
        LineId("line-new"),
        profile="night-line",
        claimable=False,
        adapter_mac="AA:BB:CC:DD:EE:FF",
        paired_phone_mac="00:11:22:33:44:55",
        transport_profile="wireguard",
        line_home_country="MD",
    )
    assert isinstance(provisioned, LineAssignment)
    assert provisioned.adapter_mac == "AA:BB:CC:DD:EE:FF"
    assert provisioned.paired_phone_mac == "00:11:22:33:44:55"
    assert provisioned.transport_profile == "wireguard"
    assert provisioned.line_home_country == "MD"
    # Round 25 (item 12): admin surface drains the shared outbox.
    assert isinstance(kernel.drain_events(), list)
    assert isinstance(kernel.drain_provider_commands()[0], ProviderRevokeCommand)
    updated = await kernel.configure_line(
        LineId("line-main"),
        claimable=False,
        profile="renamed",
        accept_window_sec=10.0,
        idle_revoke_after=timedelta(hours=24),
        timeouts=TimeoutConfig(grant_ttl_sec=5.0, heartbeat_down_after=3),
    )
    assert isinstance(updated, LineAssignment)
    # Round 25 (review-15 item 22): the explicit off switch for the
    # alert-only idle window.
    cleared = await kernel.configure_line(LineId("line-main"), clear_idle_revoke_after=True)
    assert isinstance(cleared, LineAssignment)


def test_timeout_config_patch_model() -> None:
    """Round 22 (review-10 HIGH-5): TimeoutConfig is a PATCH model -
    every knob optional with a positive bound."""
    empty = TimeoutConfig()
    assert all(getattr(empty, f) is None for f in TimeoutConfig.__pydantic_fields__)
    patch = TimeoutConfig(grant_ttl_sec=5.0, orphan_grace_sec=10.0)
    assert patch.grant_ttl_sec == 5.0
    assert patch.heartbeat_down_after is None
    with pytest.raises(ValidationError):
        TimeoutConfig(grant_ttl_sec=0.0)
    with pytest.raises(ValidationError):
        TimeoutConfig(heartbeat_degraded_after=0)


def test_provider_registration_meta_bounded() -> None:
    """Round 22 (review-10 MEDIUM-5): meta is bounded - <=16 keys,
    keys 1..64 chars, values <=256 chars (rule 7 on a wire-facing
    field)."""
    base = {
        "line_id": "line-main",
        "caps": ["dial_out"],
        "channel_seq": 1,
    }
    ok = ProviderRegistration.model_validate(
        {**base, "meta": {f"k{i}": "v" for i in range(MAX_PROVIDER_META_ENTRIES)}}
    )
    assert len(ok.meta) == MAX_PROVIDER_META_ENTRIES
    with pytest.raises(ValidationError):
        ProviderRegistration.model_validate(
            {
                **base,
                "meta": {f"k{i}": "v" for i in range(MAX_PROVIDER_META_ENTRIES + 1)},
            }
        )
    with pytest.raises(ValidationError):
        ProviderRegistration.model_validate(
            {**base, "meta": {"k": "v" * (MAX_PROVIDER_META_VALUE_LEN + 1)}}
        )
    with pytest.raises(ValidationError):
        ProviderRegistration.model_validate(
            {**base, "meta": {"k" * (MAX_PROVIDER_META_KEY_LEN + 1): "v"}}
        )
    with pytest.raises(ValidationError):
        ProviderRegistration.model_validate({**base, "meta": {"": "v"}})


def test_pending_claim_bounds_documented() -> None:
    """Round 22 (review-10 MEDIUM-4): claim caps exist as contract
    constants and PendingClaim carries session_id - claims are
    socket-bound, so drop_session cancels them."""
    assert MAX_PENDING_CLAIMS_PER_AGENT == 8
    assert MAX_PENDING_CLAIMS_TOTAL == 256
    assert "session_id" in PendingClaim.__pydantic_fields__
    claim = PendingClaim(
        claim_id=ClaimId("claim-1"),
        line_id=LineId("line-main"),
        requester_agent_id="agent-1",
        session_id=SessionId("sess-1"),
        created_at=NOW_UTC,
    )
    assert claim.session_id == SessionId("sess-1")


# ---------------------------------------------------------------------------
# Round 21: line.* wire parity, media credential pair, event audiences
# ---------------------------------------------------------------------------


def _doc_line_command_table() -> dict[str, dict[str, bool]]:
    """Parse the "## Manager → Line — `line.*`" table in
    docs/api-contract.md: wire method name -> {param: is_optional}.
    The trailing ``?`` marker is KEPT (round-21 review): optionality
    must be compared against the model fields, not stripped away."""
    text = API_CONTRACT_MD.read_text(encoding="utf-8")
    section = text.split("## Manager → Line", 1)[1]
    section = section.split("\n## ", 1)[0]
    table: dict[str, dict[str, bool]] = {}
    for m in re.finditer(r"^\|\s*`(line\.[a-z_]+)`\s*\|\s*`\{([^}]*)\}`", section, flags=re.M):
        params: dict[str, bool] = {}
        for p in m.group(2).split(","):
            p = p.strip()
            if p:
                params[p.rstrip("?")] = p.endswith("?")
        table[m.group(1)] = params
    return table


def test_provider_command_wire_parity() -> None:
    """Round 21 (CRITICAL-1) drift test: every ProviderCommand
    discriminator resolves to a documented ``line.*`` method via
    PROVIDER_COMMAND_WIRE_NAMES, and each command model's fields match
    the params declared in the api-contract table - by name AND by
    required-vs-optional (the doc's ``?`` suffix vs the model field
    carrying a default; round-21 review: stripping ``?`` let
    optionality drift pass silently).

    ``command`` (the discriminator) and ``line_id`` are excluded from
    the comparison: commands ride the provider's own inbound
    connection, which is already bound to exactly one line, so the
    wire table carries no ``line_id`` param."""
    table = _doc_line_command_table()
    assert table, "parsed zero line.* commands - doc table format changed?"
    assert set(table) == set(PROVIDER_COMMAND_WIRE_NAMES.values())

    command_models: list[type[contract.ContractModel]] = [
        ProviderDialCommand,
        ProviderAnswerCommand,
        ProviderHangupCommand,
        ProviderSendDtmfCommand,
        ProviderRevokeCommand,
        ProviderReattachMediaCommand,
    ]
    assert len(command_models) == len(PROVIDER_COMMAND_WIRE_NAMES)
    for model in command_models:
        discriminator = model.__pydantic_fields__["command"].default
        wire_name = PROVIDER_COMMAND_WIRE_NAMES[discriminator]
        model_fields = model.__pydantic_fields__
        fields = set(model_fields) - {"command", "line_id"}
        doc_params = table[wire_name]
        assert fields == set(doc_params), (
            f"{model.__name__} drift vs {wire_name}: "
            f"missing={set(doc_params) - fields}, extra={fields - set(doc_params)}"
        )
        model_optional = {f for f in fields if not model_fields[f].is_required()}
        doc_optional = {p for p, optional in doc_params.items() if optional}
        assert model_optional == doc_optional, (
            f"{model.__name__} optionality drift vs {wire_name}: "
            f"doc-optional-only={doc_optional - model_optional}, "
            f"model-optional-only={model_optional - doc_optional}"
        )


def test_media_credentials_distinct_role_tokens() -> None:
    """Round 21 (CRITICAL-2): the per-lease credential pair issues two
    INDEPENDENT one-time tokens - agent and provider roles can never
    share one token (media.auth role spoofing fix). Round 25
    (review-15 item 6) splits the endpoint the same way:
    agent_media_endpoint vs provider_media_endpoint."""
    creds = MediaCredentials(
        agent_media_endpoint="unix:///run/phonebridge/media.sock",
        provider_media_endpoint="wss://media.internal/provider",
        agent_media_token=MediaToken(RedactedString("a-tok")),
        provider_media_token=MediaToken(RedactedString("p-tok")),
        agent_media_epoch=1,
        provider_media_epoch=1,
    )
    assert creds.agent_media_token != creds.provider_media_token
    assert creds.agent_media_endpoint != creds.provider_media_endpoint
    with pytest.raises(ValidationError):
        MediaCredentials(
            agent_media_endpoint="unix:///run/phonebridge/media.sock",
            provider_media_endpoint="wss://media.internal/provider",
            agent_media_token=MediaToken(RedactedString("same")),
            provider_media_token=MediaToken(RedactedString("same")),
            agent_media_epoch=1,
            provider_media_epoch=1,
        )
    for missing_endpoint in (
        {"provider_media_endpoint": "wss://media.internal/provider"},
        {"agent_media_endpoint": "unix:///run/phonebridge/media.sock"},
    ):
        with pytest.raises(ValidationError):
            MediaCredentials(
                agent_media_token=MediaToken(RedactedString("a")),
                provider_media_token=MediaToken(RedactedString("p")),
                agent_media_epoch=1,
                provider_media_epoch=1,
                **missing_endpoint,
            )


def test_event_audience_union_and_envelope() -> None:
    """Round 21 (HIGH-4): EventAudience is a discriminated union
    (broadcast | agent | session | admin - round 23) and a REQUIRED
    envelope field - every event leaves the kernel with a routing
    target."""
    adapter = TypeAdapter(EventAudience)
    assert isinstance(adapter.validate_python({"scope": "broadcast"}), BroadcastAudience)
    agent = adapter.validate_python({"scope": "agent", "agent_id": "system:fallback_worker"})
    assert isinstance(agent, AgentAudience)
    assert agent.agent_id == "system:fallback_worker"
    session = adapter.validate_python({"scope": "session", "session_id": "sess-1"})
    assert isinstance(session, SessionAudience)
    assert session.session_id == SessionId("sess-1")
    # Round 23 (review-12 H6/M6): admin-observable escalation
    # routing for the alert-only idle_alert event.
    assert isinstance(adapter.validate_python({"scope": "admin"}), AdminAudience)
    with pytest.raises(ValidationError):
        adapter.validate_python({"scope": "room"})

    # An envelope without an audience cannot be constructed.
    with pytest.raises(ValidationError):
        EventEnvelope[MissedCallEvent](
            name=EventName.MISSED_CALL,  # type: ignore[call-arg]
            emitted_at=NOW_UTC,
            payload=MissedCallEvent(line_id=LineId("line-main"), reason=CallEndReason.NORMAL),
        )


# ---------------------------------------------------------------------------
# Round 23 (architect review 12 - SA_carbon_review_12): Phase-0 contract
# freeze before M1-M5 - C1 hangup XOR, C2 forward target, C3 media port
# lifecycle, H1 log-dump JSON invariant, H6/M6 idle_alert + fallback TTL,
# M2 idempotency bounds + preempted-grant correlation.
# ---------------------------------------------------------------------------


def test_hangup_command_xor_target() -> None:
    """Round 23 (review-12 C1): ProviderHangupCommand carries exactly
    ONE target - ``lease_id`` for an established call, ``call_token``
    to reject a still-unanswered ring (no lease exists before accept).
    Zero and two targets are both rejected."""
    base = {
        "line_id": LineId("line-main"),
        "assignment_id": contract.AssignmentId("bind-1"),
    }
    by_lease = ProviderHangupCommand(**base, lease_id=LeaseId("l-1"))
    assert by_lease.call_token is None
    by_token = ProviderHangupCommand(**base, call_token=CallToken(RedactedString("tok-1")))
    assert by_token.lease_id is None
    with pytest.raises(ValidationError):
        ProviderHangupCommand(**base)  # neither target
    with pytest.raises(ValidationError):
        ProviderHangupCommand(
            **base, lease_id=LeaseId("l-1"), call_token=CallToken(RedactedString("tok-1"))
        )  # both - ambiguous


def test_to_log_dict_is_json_serializable() -> None:
    """Round 23 (review-12 H1) invariant: ``json.dumps`` on
    ``to_log_dict()`` MUST always succeed - datetime -> ISO UTC,
    timedelta -> total_seconds, Enum -> .value - and MUST never
    contain raw PII (the python-mode dump leaves those types live;
    only the "json" serializer handles them, and that path would
    emit raw RedactedString wire values)."""
    assignment = _assignment(
        mode=AssignmentMode.EXCLUSIVE,
        owner_agent_id="agent-1",
        idle_revoke_after=timedelta(hours=24),
    )
    dumped = json.dumps(assignment.to_log_dict())
    assert isinstance(dumped, str)
    # datetime serialized to ISO-8601, timedelta to seconds.
    assert assignment.to_log_dict()["last_seen_at"] == NOW_UTC.isoformat()
    assert assignment.to_log_dict()["idle_revoke_after"] == 24 * 3600.0
    assert assignment.to_log_dict()["mode"] == "exclusive"

    class _KitchenSink(contract.ContractModel):
        when: contract.AwareDatetime
        delta: timedelta
        state: LineState
        reason: CallEndReason
        secret_value: RedactedString
        tags: frozenset[str]
        maybe: str | None = None

    sink = _KitchenSink(
        when=NOW_UTC,
        delta=timedelta(milliseconds=1500),
        state=LineState.BUSY,
        reason=CallEndReason.PROVIDER_LOST,
        secret_value=RedactedString(_RAW_NUMBER),
        tags=frozenset({"a", "b"}),
    )
    log_dict = sink.to_log_dict()
    rendered = json.dumps(log_dict)  # must not raise
    assert log_dict["when"] == NOW_UTC.isoformat()
    assert log_dict["delta"] == 1.5
    assert log_dict["state"] == "BUSY"
    assert log_dict["reason"] == "provider_lost"
    assert log_dict["tags"] == ["a", "b"]
    assert _RAW_NUMBER not in rendered
    assert "****" in rendered


def test_media_allocator_port_lifecycle() -> None:
    """Round 23 (review-12 C3): the media port contract is allocate
    strictly at dial/accept + an idempotent synchronous
    ``revoke(lease_id)`` called on every CallLease -> ENDED. The
    annotation below makes mypy check the fake against the protocol -
    a missing ``revoke`` would fail the type gate."""
    calls: list[str] = []

    class FakeAllocator:
        """Contract-conforming fake (round 29, SA_quant_review_1
        slice-C items 8/9): it tracks the live pairs so the test can
        pin per-role reissue selectivity and the never-allocated
        rejection, not just the call sequence."""

        def __init__(self) -> None:
            self._live: dict[str, MediaCredentials] = {}

        def allocate(
            self, lease_id: LeaseId, line_id: LineId, session_id: SessionId | None = None
        ) -> MediaCredentials:
            calls.append(f"allocate:{lease_id}:{session_id}")
            creds = MediaCredentials(
                agent_media_endpoint="unix:///run/phonebridge/media.sock",
                provider_media_endpoint="wss://media.internal/provider",
                agent_media_token=MediaToken(RedactedString("a-tok")),
                provider_media_token=MediaToken(RedactedString("p-tok")),
                agent_media_epoch=1,
                provider_media_epoch=1,
            )
            self._live[str(lease_id)] = creds
            return creds

        def reissue(self, lease_id: LeaseId, role: contract.MediaRole) -> MediaCredentials:
            # Round 28 (review-18 item 2): the per-role reissue -
            # only the addressed half's credentials are replaced.
            calls.append(f"reissue:{lease_id}:{role}")
            if str(lease_id) not in self._live:
                # Contract violation guard (round 29, item 8): a
                # revoked or never-allocated lease MUST NOT mint
                # credentials - the fake rejects like a real port.
                raise LookupError(f"no live media pair for {lease_id}")
            old = self._live[str(lease_id)]
            if role == "agent":
                creds = old.model_copy(
                    update={"agent_media_token": MediaToken(RedactedString("a-tok-2"))}
                )
            else:
                creds = old.model_copy(
                    update={"provider_media_token": MediaToken(RedactedString("p-tok-2"))}
                )
            self._live[str(lease_id)] = creds
            return creds

        def revoke(self, lease_id: LeaseId) -> None:
            calls.append(f"revoke:{lease_id}")
            self._live.pop(str(lease_id), None)

    allocator: contract.MediaAllocator = FakeAllocator()
    creds = allocator.allocate(LeaseId("l-1"), LineId("line-main"))
    assert creds.agent_media_token != creds.provider_media_token
    # Round 25 (item 6): session_id selects the agent-side endpoint.
    remote = allocator.allocate(LeaseId("l-2"), LineId("line-main"), session_id=SessionId("sess-9"))
    assert remote.agent_media_endpoint == "unix:///run/phonebridge/media.sock"
    # Round 28 (review-18 item 2; selectivity pinned round 29,
    # SA_quant_review_1 slice-C item 9): reissue addresses one role
    # of a live pair - the returned pair view carries the NEW
    # provider token and the ORIGINAL agent token verbatim.
    reissued = allocator.reissue(LeaseId("l-1"), "provider")
    assert isinstance(reissued, MediaCredentials)
    assert reissued.provider_media_token != creds.provider_media_token
    assert reissued.provider_media_token.expose() == "p-tok-2"
    assert reissued.agent_media_token == creds.agent_media_token
    assert reissued.agent_media_token.expose() == "a-tok"
    assert reissued.agent_media_endpoint == creds.agent_media_endpoint
    # Round 30 (SA_carbon_review_19 slice-C item 5): the "agent"-role
    # reissue branch MUST be exercised on a LIVE pair - the old
    # post-revoke call died on the closed-pair guard before reaching
    # it, leaving the branch dead. Here the pair is still live: the
    # agent token rotates, the previously reissued provider token
    # (p-tok-2) is preserved verbatim.
    reissued_agent = allocator.reissue(LeaseId("l-1"), "agent")
    assert reissued_agent.agent_media_token.expose() == "a-tok-2"
    assert reissued_agent.agent_media_token != creds.agent_media_token
    assert reissued_agent.provider_media_token.expose() == "p-tok-2"
    # Round 29 (SA_quant_review_1 slice-C item 8): reissue on a
    # never-allocated lease is a contract violation - it MUST NOT
    # silently mint credentials on a pair that does not exist.
    with pytest.raises(LookupError):
        allocator.reissue(LeaseId("never-allocated"), "provider")
    allocator.revoke(LeaseId("l-1"))
    allocator.revoke(LeaseId("l-1"))  # idempotent - safe to call twice
    allocator.revoke(LeaseId("never-allocated"))  # unknown lease: no-op
    # A revoked pair is dead too - reissue must not resurrect it.
    with pytest.raises(LookupError):
        allocator.reissue(LeaseId("l-1"), "agent")
    assert calls == [
        "allocate:l-1:None",
        "allocate:l-2:sess-9",
        "reissue:l-1:provider",
        "reissue:l-1:agent",
        "reissue:never-allocated:provider",
        "revoke:l-1",
        "revoke:l-1",
        "revoke:never-allocated",
        "reissue:l-1:agent",
    ]


def test_round23_event_schema_freeze() -> None:
    """Round 23 (review-12 H6/M2/M6): fallback_triggered carries an
    absolute expires_at deadline; call_state_changed may carry
    reissued_ticket_id for a preempted immediate grant; idle_alert is
    a separate admin-observable event, never a revoke."""
    # expires_at is REQUIRED on fallback_triggered (fallback_accept_ttl).
    with pytest.raises(ValidationError):
        FallbackTriggeredEvent(
            line_id=LineId("line-main"),  # type: ignore[call-arg]
            call_token=CallToken(RedactedString("fb-tok")),
            policy=RecordAndTelegramFallback(channel="ops-alerts", announcement_ref="rec-note"),
        )
    ft = FallbackTriggeredEvent(
        line_id=LineId("line-main"),
        call_token=CallToken(RedactedString("fb-tok")),
        # Round 25 (review-15 item 31): worker policies only.
        policy=RecordAndTelegramFallback(channel="ops-alerts", announcement_ref="rec-note"),
        expires_at=NOW_UTC,
    )
    assert ft.expires_at == NOW_UTC

    # reissued_ticket_id: optional correlation of a preempted
    # enqueue=True immediate grant to its new head-of-queue ticket.
    csc = CallStateChangedEvent(
        lease_id=LeaseId("l-1"),
        line_id=LineId("line-main"),
        state="disconnected",
        reason=CallEndReason.PREEMPTED_BY_INCOMING,
        reissued_ticket_id=TicketId("t-9"),
    )
    assert csc.reissued_ticket_id == TicketId("t-9")
    assert csc.model_dump(mode="json")["reissued_ticket_id"] == "t-9"
    assert (
        CallStateChangedEvent(
            lease_id=LeaseId("l-1"), line_id=LineId("line-main"), state="active"
        ).reissued_ticket_id
        is None
    )

    # idle_alert: alert-only, AdminAudience routing, required fields.
    envelope = EventEnvelope[IdleAlertEvent](
        name=EventName.IDLE_ALERT,
        audience=AdminAudience(),
        emitted_at=NOW_UTC,
        payload=IdleAlertEvent(
            assignment_id=contract.AssignmentId("asg-1"),
            line_id=LineId("line-main"),
            owner_agent_id="agent-1",
            idle_since=NOW_UTC,
        ),
    )
    assert EVENT_WIRE_NAMES[EventName.IDLE_ALERT] == "event.idle_alert"
    assert isinstance(envelope.audience, AdminAudience)
    with pytest.raises(ValidationError):
        IdleAlertEvent(  # type: ignore[call-arg]
            assignment_id=contract.AssignmentId("asg-1"),
            line_id=LineId("line-main"),
            # owner_agent_id + idle_since are required: an admin alert
            # must name the idle owner and when it went idle.
        )


def test_timeout_config_fallback_accept_ttl() -> None:
    """Round 23 (review-12 M6): TimeoutConfig gains
    fallback_accept_ttl_sec - the FallbackWorker's deadline to accept
    a re-issued ring token (~10 s illustrative default)."""
    patch = TimeoutConfig(fallback_accept_ttl_sec=10.0)
    assert patch.fallback_accept_ttl_sec == 10.0
    assert TimeoutConfig().fallback_accept_ttl_sec is None
    with pytest.raises(ValidationError):
        TimeoutConfig(fallback_accept_ttl_sec=0.0)
    with pytest.raises(ValidationError):
        TimeoutConfig(fallback_accept_ttl_sec=-1)


# ---------------------------------------------------------------------------
# Round 24 (architect review 13 - SA_carbon_review_13), Phase-0 final
# contract freeze v0.0.19 - C2 effective LineTimeouts + ProviderBinding
# heartbeat_interval_ms, M1 model-level forward-target validation +
# self-loop, M3 telecom-only caps.
# ---------------------------------------------------------------------------


def test_line_timeouts_effective_model() -> None:
    """Round 24 (review-13 C2): LineTimeouts is the CONCRETIZED
    counterpart of TimeoutConfig - every field carries a resolved
    default (never None), and heartbeat_down_after must strictly
    exceed heartbeat_degraded_after."""
    eff = LineTimeouts()
    assert eff.grant_ttl_sec == 5.0
    assert eff.orphan_grace_sec == 10.0
    assert eff.heartbeat_interval_sec == 5.0
    assert eff.heartbeat_degraded_after == 2
    assert eff.heartbeat_down_after == 3
    assert eff.fallback_accept_ttl_sec == 10.0
    # Frozen like every ContractModel.
    with pytest.raises(ValidationError):
        eff.grant_ttl_sec = 9.0  # type: ignore[misc]
    # Bounds: same positivity contract as the patch model.
    with pytest.raises(ValidationError):
        LineTimeouts(grant_ttl_sec=0.0)
    with pytest.raises(ValidationError):
        LineTimeouts(heartbeat_degraded_after=0)
    # Ordering: DOWN is the terminal escalation of DEGRADED.
    with pytest.raises(ValidationError):
        LineTimeouts(heartbeat_degraded_after=3, heartbeat_down_after=3)
    with pytest.raises(ValidationError):
        LineTimeouts(heartbeat_degraded_after=5, heartbeat_down_after=3)
    ok = LineTimeouts(heartbeat_degraded_after=2, heartbeat_down_after=4)
    assert ok.heartbeat_down_after == 4


def test_timeout_config_heartbeat_ordering() -> None:
    """Round 24 (review-13 C2): the same down > degraded invariant is
    enforced on the PATCH model when both thresholds are set; a
    lone threshold stays legal (it merges against the stored
    effective value at configure_line time)."""
    TimeoutConfig()  # empty patch remains legal
    TimeoutConfig(heartbeat_degraded_after=2)
    TimeoutConfig(heartbeat_down_after=3)
    TimeoutConfig(heartbeat_degraded_after=2, heartbeat_down_after=4)
    with pytest.raises(ValidationError):
        TimeoutConfig(heartbeat_degraded_after=3, heartbeat_down_after=3)
    with pytest.raises(ValidationError):
        TimeoutConfig(heartbeat_degraded_after=5, heartbeat_down_after=3)


def test_line_assignment_carries_effective_timeouts() -> None:
    """Round 24 (review-13 C2): LineAssignment.timeouts exposes the
    resolved LineTimeouts so the admin adapter can write it through
    to SQLite verbatim - and it survives a JSON round-trip."""
    assignment = _assignment()
    assert assignment.timeouts == LineTimeouts()
    custom = _assignment(timeouts=LineTimeouts(heartbeat_interval_sec=7.5))
    assert custom.timeouts.heartbeat_interval_sec == 7.5
    hydrated = LineAssignment.model_validate(assignment.model_dump(mode="json"))
    assert hydrated.timeouts == LineTimeouts()


def test_line_timeouts_apply_patch_atomic_merge() -> None:
    """Round 24b (residual MEDIUM-HIGH): ``LineTimeouts.apply_patch``
    merges a ``TimeoutConfig`` patch via a FULL ``model_validate`` of
    the merged dict - atomic: a lone patch field that is legal in
    isolation but breaks the ``down > degraded`` ladder against the
    stored value maps to ``PhonebridgeError(INVALID_ARGUMENT)``,
    never a raw ``ValidationError``, and leaves the effective
    values untouched."""
    base = LineTimeouts()  # degraded=2, down=3
    # Valid single-field patch merges cleanly.
    merged = base.apply_patch(TimeoutConfig(grant_ttl_sec=7.5))
    assert merged.grant_ttl_sec == 7.5
    assert merged.heartbeat_degraded_after == 2
    assert merged.heartbeat_down_after == 3
    # An all-None patch returns the same effective values.
    assert base.apply_patch(TimeoutConfig()) == base
    # Ordering breach visible only after merge: degraded=4 alone is a
    # legal TimeoutConfig, yet 4 >= the stored down=3.
    with pytest.raises(PhonebridgeError) as excinfo:
        base.apply_patch(TimeoutConfig(heartbeat_degraded_after=4))
    assert excinfo.value.code is ErrorCode.INVALID_ARGUMENT
    # Round 24c: ErrorData.detail carries ALL joined validation
    # messages (PII/input-free pydantic msgs), not first-error-only.
    assert excinfo.value.data.detail is not None
    assert "heartbeat_down_after must exceed" in excinfo.value.data.detail
    # Same for a lowered down_after that meets the stored degraded.
    with pytest.raises(PhonebridgeError) as excinfo2:
        base.apply_patch(TimeoutConfig(heartbeat_down_after=2))
    assert excinfo2.value.code is ErrorCode.INVALID_ARGUMENT
    # The stored values are untouched: the frozen model cannot
    # half-patch and the merge yields a NEW instance.
    assert base.heartbeat_down_after == 3
    assert base.heartbeat_degraded_after == 2


def test_heartbeat_interval_ms_conversion() -> None:
    """Round 24c (reviewer finding on the round-24 C2 model): the
    sec -> ms conversion is pinned - ``heartbeat_interval_sec`` is
    floored at millisecond granularity (``ge=0.001``) on BOTH
    ``LineTimeouts`` and ``TimeoutConfig``, and the canonical
    ``heartbeat_interval_ms_from_sec`` helper rounds to the nearest
    ms clamped to >= 1, so a valid config can NEVER stamp a
    zero-cadence ``ProviderBinding.heartbeat_interval_ms``."""
    assert heartbeat_interval_ms_from_sec(5.0) == 5000
    assert heartbeat_interval_ms_from_sec(0.001) == 1
    assert heartbeat_interval_ms_from_sec(7.5) == 7500
    assert heartbeat_interval_ms_from_sec(0.0015) == 2
    assert heartbeat_interval_ms_from_sec(0.0014) == 1
    # Round 27 (review-17 item 37): halves round UP, never to even -
    # banker's rounding would stamp 2 ms for 0.0025 s.
    assert heartbeat_interval_ms_from_sec(0.0025) == 3
    assert heartbeat_interval_ms_from_sec(0.0035) == 4
    # Clamp guard for a caller bypassing model validation.
    assert heartbeat_interval_ms_from_sec(0.0004) == 1
    # Granularity floor on the effective model and the patch model.
    assert LineTimeouts(heartbeat_interval_sec=0.001).heartbeat_interval_sec == 0.001
    assert TimeoutConfig(heartbeat_interval_sec=0.001).heartbeat_interval_sec == 0.001
    with pytest.raises(ValidationError):
        LineTimeouts(heartbeat_interval_sec=0.0005)
    with pytest.raises(ValidationError):
        LineTimeouts(heartbeat_interval_sec=0.0)
    with pytest.raises(ValidationError):
        TimeoutConfig(heartbeat_interval_sec=0.0009)


def test_provider_binding_heartbeat_interval_ms() -> None:
    """Round 24 (review-13 C2): ProviderBinding stamps the ping
    cadence negotiated for THIS provider session so the
    provider.register response can carry heartbeat_interval_ms -
    default 5000, strictly positive."""
    binding = ProviderBinding(
        assignment_id=contract.AssignmentId("bind-1"),
        line_id=LineId("line-main"),
        caps=(Intent.DIAL_OUT,),
        heartbeat_degraded_after=2,
        heartbeat_down_after=3,
        connected_at=NOW_UTC,
        last_seen_at=NOW_UTC,
        channel_seq=1,
    )
    assert binding.heartbeat_interval_ms == 5000
    stamped = ProviderBinding(
        assignment_id=contract.AssignmentId("bind-2"),
        line_id=LineId("line-main"),
        caps=(Intent.DIAL_OUT,),
        heartbeat_interval_ms=7500,
        heartbeat_degraded_after=2,
        heartbeat_down_after=3,
        connected_at=NOW_UTC,
        last_seen_at=NOW_UTC,
        channel_seq=1,
    )
    assert stamped.heartbeat_interval_ms == 7500
    with pytest.raises(ValidationError):
        ProviderBinding(
            assignment_id=contract.AssignmentId("bind-3"),
            line_id=LineId("line-main"),
            caps=(Intent.DIAL_OUT,),
            heartbeat_interval_ms=0,
            heartbeat_degraded_after=2,
            heartbeat_down_after=3,
            connected_at=NOW_UTC,
            last_seen_at=NOW_UTC,
            channel_seq=1,
        )


def _e164_reason(number: str, country: str) -> ErrorReason:
    """Run ``validate_e164_country`` expecting a typed rejection and
    return its ``ErrorReason`` sub-code (round 27, review-17 item
    33): the helper raises ``PhonebridgeError`` directly - never a
    bare ``ValueError`` - so every branch is distinguishable
    without parsing prose."""
    with pytest.raises(PhonebridgeError) as excinfo:
        validate_e164_country(RedactedString(number), country)
    assert excinfo.value.code is ErrorCode.INVALID_ARGUMENT
    reason = excinfo.value.data.reason
    assert isinstance(reason, ErrorReason)
    return reason


def test_validate_e164_country_helper() -> None:
    """Round 24 (review-13 M1): the pure helper runs the strict
    E.164 shape gate, then phonenumbers parse + validity +
    region cross-check, returning the raw number on success.
    Round 27 (review-17 item 33): every rejection is a TYPED
    ``PhonebridgeError`` whose ``data.reason`` names the branch -
    no call site can mis-map a failure by matching English text."""
    assert validate_e164_country(RedactedString(_RAW_NUMBER), "MD") == _RAW_NUMBER
    assert validate_e164_country(RedactedString("+12015550123"), "US") == "+12015550123"
    # Shape gate: local/national forms, missing +, leading 0 country
    # code, punctuation and extensions never reach phonenumbers.
    for bad in ("62112345", "+01234567", "+373 621 12345", "+37362112345 ext.5"):
        assert _e164_reason(bad, "MD") is ErrorReason.INVALID_FORMAT_NOT_E164
    # Parses E.164 but not a valid subscriber number.
    assert _e164_reason("+19999999999", "US") is ErrorReason.INVALID_NUMBER
    # Valid number, wrong declared region (country is a cross-check
    # ASSERTION, never a fallback parse region).
    assert _e164_reason("+12015550123", "MD") is ErrorReason.COUNTRY_MISMATCH
    # country must be ISO alpha-2 (uppercase two letters).
    for bad_country in ("md", "MDA", "M1", ""):
        assert _e164_reason(_RAW_NUMBER, bad_country) is ErrorReason.UNKNOWN_COUNTRY_CODE


def test_forward_via_line_model_level_validation() -> None:
    """Round 24 (review-13 M1): ForwardViaLineFallback validates its
    dial target at CONSTRUCTION - a corrupt SQLite/config row can
    never hydrate an invalid policy - and LineAssignment bans the
    self-loop inside its own invariants validator."""
    # Valid policy constructs (the pre-existing wire-parity cases).
    fwd = ForwardViaLineFallback(
        line_id=LineId("line-b"),
        target_number=RedactedString("+37322212345"),
        target_country="MD",
    )
    assert fwd.target_country == "MD"

    # Invalid target_number shape / region mismatch / bad country are
    # rejected at the model boundary, not deferred to the worker.
    # Round 28 (review-18 item 16): errors inside model validators
    # surface as ValueError -> ValidationError, never as
    # PhonebridgeError - error-code mapping is the ADAPTER's job.
    # The typed ``PhonebridgeError`` stays reachable via
    # ``__cause__`` so an adapter can still read the sub-code.
    def _cause_reason(err: pytest.ExceptionInfo[ValidationError]) -> ErrorReason:
        inner = err.value.errors()[0].get("ctx", {}).get("error")
        assert isinstance(inner, ValueError)
        cause = inner.__cause__
        assert isinstance(cause, PhonebridgeError)
        assert cause.data.reason is not None
        return cause.data.reason

    with pytest.raises(ValidationError) as e164_err:
        ForwardViaLineFallback(
            line_id=LineId("line-b"),
            target_number=RedactedString("79099999"),  # no '+' prefix
            target_country="MD",
        )
    assert _cause_reason(e164_err) is ErrorReason.INVALID_FORMAT_NOT_E164
    with pytest.raises(ValidationError) as mismatch_err:
        ForwardViaLineFallback(
            line_id=LineId("line-b"),
            target_number=RedactedString("+12015550123"),  # valid US, declared MD
            target_country="MD",
        )
    assert _cause_reason(mismatch_err) is ErrorReason.COUNTRY_MISMATCH
    with pytest.raises(ValidationError) as country_err:
        ForwardViaLineFallback(
            line_id=LineId("line-b"),
            target_number=RedactedString("+37322212345"),
            target_country="md",  # not ISO alpha-2 uppercase
        )
    assert _cause_reason(country_err) is ErrorReason.UNKNOWN_COUNTRY_CODE
    # Self-loop: a forward target equal to the configured line is a
    # model-level violation on LineAssignment, independent of
    # configure_line (which DB hydration bypasses).
    with pytest.raises(ValidationError):
        _assignment(fallback_policy=fwd.model_copy(update={"line_id": LineId("line-main")}))
    # A forward target on a DIFFERENT line stays legal.
    ok = _assignment(fallback_policy=fwd)
    assert isinstance(ok.fallback_policy, ForwardViaLineFallback)


def test_provider_caps_are_telecom_only() -> None:
    """Round 24 (review-13 M3): provider-declared caps are non-empty
    and telecom-only - Intent.EXCLUSIVE_LINE is a LineAssignment
    property, never a hardware capability; rejected on the register
    ingress, the capability_update ingress and the binding record."""
    for model, base in (
        (
            ProviderRegistration,
            {"line_id": "line-main", "channel_seq": 1},
        ),
        (
            ProviderCapabilityUpdate,
            {"line_id": "line-main", "assignment_id": "bind-1"},
        ),
        (
            ProviderBinding,
            {
                "line_id": "line-main",
                "assignment_id": "bind-1",
                "heartbeat_degraded_after": 2,
                "heartbeat_down_after": 3,
                "connected_at": NOW_UTC.isoformat(),
                "last_seen_at": NOW_UTC.isoformat(),
                "channel_seq": 1,
            },
        ),
    ):
        for bad_caps in (
            [],  # empty caps declare nothing
            ["exclusive_line"],
            ["dial_out", "exclusive_line"],
        ):
            with pytest.raises(ValidationError):
                model.model_validate({**base, "caps": bad_caps})
        ok = model.model_validate({**base, "caps": ["dial_out", "receive_inbound"]})
        assert Intent.EXCLUSIVE_LINE not in ok.caps


# ---------------------------------------------------------------------------
# Round 25 (architect review 15 - SA_carbon_review_15), Phase-0 contract
# hardening v0.0.20 batch 1 - item 3 token redaction, item 4 JSON-RPC
# error-code wire map, items 21/37 reserved system identity + id
# hygiene, item 31 audience scoping + fallback delivery invariants,
# items 39/40 log-mask hardening.
# ---------------------------------------------------------------------------


def test_error_code_wire_map() -> None:
    """Round 25 (review-15 item 4): every ErrorCode member resolves to
    a unique JSON-RPC ``error.code`` integer inside the
    application-reserved band -32099..-32000."""
    assert set(ERROR_CODE_WIRE) == set(ErrorCode)
    codes = list(ERROR_CODE_WIRE.values())
    assert len(codes) == len(set(codes))
    for code in codes:
        assert -32099 <= code <= -32000


def test_dial_result_media_token_masked_in_log_dumps() -> None:
    """Round 25 (review-15 item 3): the media token is a
    RedactedString container - ``safe_dump()``/``to_log_dict()`` emit
    only the masked form, and ``json.dumps`` on the log dict succeeds
    with no raw secret."""
    result = DialResult(
        lease_id=LeaseId("l-1"),
        media_endpoint="unix:///run/m.sock",
        media_token=MediaToken(RedactedString("media-secret-token")),
    )
    safe = result.safe_dump()
    assert safe["media_token"] == str(RedactedString("media-secret-token"))
    assert "media-secret-token" not in repr(safe)
    rendered = json.dumps(result.to_log_dict())  # must not raise
    assert "media-secret-token" not in rendered


def test_mask_for_log_dict_keys_and_set_determinism() -> None:
    """Round 25 (review-15 items 39/40): dict KEYS are masked too -
    a RedactedString or Enum key can neither leak nor crash
    ``json.dumps`` - and set/frozenset elements are masked BEFORE the
    deterministic sort, so the ordering never depends on raw secrets
    or container iteration order."""
    masked = contract._mask_for_log({RedactedString("secret-key-value"): "v"})
    assert list(masked) == [str(RedactedString("secret-key-value"))]
    assert "secret-key-value" not in repr(masked)
    json.dumps(masked)  # must not raise
    enum_keyed = contract._mask_for_log({CallEndReason.NORMAL: "v"})
    assert enum_keyed == {"normal": "v"}
    # Two different secrets masking to the same string still produce
    # the same sorted output regardless of insertion order.
    a, b = RedactedString("aaaaxyzab"), RedactedString("aaaaqwrab")
    assert str(a) == str(b)  # equal masks, different raw secrets
    out1 = contract._mask_for_log({a, b})
    out2 = contract._mask_for_log({b, a})
    assert out1 == out2 == sorted(out1)


def test_event_audience_scopes_enforced() -> None:
    """Round 25 (review-15 item 31): EVENT_AUDIENCE_SCOPES covers
    every EventName, and the envelope rejects a ``(name, scope)``
    pair outside the table - a PII-bearing event can never be
    constructed with BroadcastAudience."""
    assert set(EVENT_AUDIENCE_SCOPES) == set(EventName)
    payload = MissedCallEvent(line_id=LineId("line-main"), reason=CallEndReason.NORMAL)
    # Legal scopes still construct (agent owner / per-session fanout).
    for audience in (
        AgentAudience(agent_id="agent-1"),
        SessionAudience(session_id=SessionId("sess-1")),
    ):
        EventEnvelope[MissedCallEvent](
            name=EventName.MISSED_CALL,
            audience=audience,
            emitted_at=NOW_UTC,
            payload=payload,
        )
    # A caller-PII event is unconstructible with broadcast routing.
    with pytest.raises(ValidationError):
        EventEnvelope[MissedCallEvent](
            name=EventName.MISSED_CALL,
            audience=BroadcastAudience(),
            emitted_at=NOW_UTC,
            payload=payload,
        )
    # Session-scoped lease facts cannot widen to agent routing.
    with pytest.raises(ValidationError):
        EventEnvelope[CallStateChangedEvent](
            name=EventName.CALL_STATE_CHANGED,
            audience=AgentAudience(agent_id="agent-1"),
            emitted_at=NOW_UTC,
            payload=CallStateChangedEvent(
                lease_id=LeaseId("l-1"), line_id=LineId("line-main"), state="active"
            ),
        )
    # line_state_changed keeps the documented true-broadcast scope.
    EventEnvelope[LineStateChangedEvent](
        name=EventName.LINE_STATE_CHANGED,
        audience=BroadcastAudience(),
        emitted_at=NOW_UTC,
        payload=LineStateChangedEvent(
            line_id=LineId("line-main"),
            old_state=LineState.FREE,
            new_state=LineState.BUSY,
        ),
    )


def test_fallback_decision_token_matches_worker_policy() -> None:
    """Round 25 (review-15 item 31): the re-issued
    ``fallback_call_token`` IS the worker delivery - a worker policy
    must carry it, a core policy must never carry it, and
    ``fallback_triggered`` dispatches worker policies only."""
    worker = RecordAndTelegramFallback(channel="ops-alerts", announcement_ref="rec-note")
    core = NoPickupFallback()
    tok = CallToken(RedactedString("fb-tok"))
    base = {
        "line_id": LineId("line-main"),
        "call_token": CallToken(RedactedString("tok-1")),
    }
    # Legal pairs: worker+token, core without token.
    FallbackDecision(**base, policy=worker, fallback_call_token=tok)
    FallbackDecision(**base, policy=core)
    # Worker without token / core with token are rejected.
    with pytest.raises(ValidationError):
        FallbackDecision(**base, policy=worker)
    with pytest.raises(ValidationError):
        FallbackDecision(**base, policy=core, fallback_call_token=tok)
    # fallback_triggered rejects core policies outright.
    for core_policy in (NoPickupFallback(), RejectFallback()):
        with pytest.raises(ValidationError):
            FallbackTriggeredEvent(
                line_id=LineId("line-main"),
                call_token=tok,
                policy=core_policy,
                expires_at=NOW_UTC,
            )


def test_system_agent_identity_and_id_hygiene() -> None:
    """Round 25 (review-15 items 21/37): the reserved ``system:*``
    prefix identifies kernel-internal identities; domain identifiers
    reject empty/overlong/colon-bearing values, and the ``default``
    alias never reaches a domain ``line_id``."""
    assert SYSTEM_AGENT_PREFIX == "system:"
    assert SYSTEM_FALLBACK_WORKER == "system:fallback_worker"
    assert is_system_agent_id(SYSTEM_FALLBACK_WORKER)
    assert not is_system_agent_id("agent-1")
    # The reserved identity stays legal on agent-scoped fields.
    assert AgentAudience(agent_id=SYSTEM_FALLBACK_WORKER).agent_id == SYSTEM_FALLBACK_WORKER
    assert AgentAudience(agent_id="system:fallback_worker").agent_id == "system:fallback_worker"
    # External ids: empty, colon-bearing and overlong are rejected;
    # a bare "system:" prefix with an empty suffix names nothing, and
    # the system: suffix obeys the same hygiene - whitespace-only and
    # colon-bearing suffixes are rejected too.
    for bad in (
        "",
        "a" * 129,
        "agent:x",
        SYSTEM_AGENT_PREFIX,
        "system:a:b",
        "system: ",
        "system:\t",
        "system:" + "a" * 129,
    ):
        with pytest.raises(ValidationError):
            AgentAudience(agent_id=bad)
    # "default" is an API-boundary alias, never a domain line_id.
    # (REACHABLE liveness keeps non-empty caps legal so the ONLY
    # failure under test is the line_id itself, round 26 item 13.)
    for bad_line in ("default", "", "line:x", "x" * 129):
        with pytest.raises(ValidationError):
            LineSummary(
                line_id=LineId(bad_line),
                caps=[Intent.DIAL_OUT],
                state=LineState.FREE,
                liveness=LivenessState.REACHABLE,
            )


# ---------------------------------------------------------------------------
# Round 25 batch 2 (v0.0.20) - item 2 claim/assignment events, item 6
# endpoint split, item 9 media reissue, item 14 revoke return, item 15
# LineState/liveness split, item 23 queue fate events, item 26
# ProviderActiveCall.call_token.
# ---------------------------------------------------------------------------


def test_claim_lifecycle_events() -> None:
    """Round 25 (review-15 item 2): claim_resolved reaches the claiming
    SESSION (assignment_id present iff approved); assignment_granted
    reaches the owner's AGENT audience."""
    approved = ClaimResolvedEvent(
        claim_id=ClaimId("claim-1"),
        line_id=LineId("line-main"),
        status="approved",
        assignment_id=contract.AssignmentId("asg-9"),
    )
    rejected = ClaimResolvedEvent(
        claim_id=ClaimId("claim-2"),
        line_id=LineId("line-main"),
        status="rejected",
        reason="not claimable",
    )
    # assignment_id iff approved - both mismatches are unconstructible.
    with pytest.raises(ValidationError):
        ClaimResolvedEvent(
            claim_id=ClaimId("claim-3"),
            line_id=LineId("line-main"),
            status="approved",
        )
    with pytest.raises(ValidationError):
        ClaimResolvedEvent(
            claim_id=ClaimId("claim-4"),
            line_id=LineId("line-main"),
            status="rejected",
            assignment_id=contract.AssignmentId("asg-9"),
        )
    granted = AssignmentGrantedEvent(
        assignment_id=contract.AssignmentId("asg-9"),
        line_id=LineId("line-main"),
        mode=AssignmentMode.EXCLUSIVE,
    )
    # Audience scoping is enforced at the envelope boundary.
    EventEnvelope[ClaimResolvedEvent](
        name=EventName.CLAIM_RESOLVED,
        audience=SessionAudience(session_id=SessionId("sess-1")),
        emitted_at=NOW_UTC,
        payload=approved,
    )
    EventEnvelope[ClaimResolvedEvent](
        name=EventName.CLAIM_RESOLVED,
        audience=SessionAudience(session_id=SessionId("sess-1")),
        emitted_at=NOW_UTC,
        payload=rejected,
    )
    EventEnvelope[AssignmentGrantedEvent](
        name=EventName.ASSIGNMENT_GRANTED,
        audience=AgentAudience(agent_id="agent-1"),
        emitted_at=NOW_UTC,
        payload=granted,
    )
    # ...and the wrong scope is rejected for both events.
    for audience in (BroadcastAudience(), AgentAudience(agent_id="agent-1")):
        with pytest.raises(ValidationError):
            EventEnvelope[ClaimResolvedEvent](
                name=EventName.CLAIM_RESOLVED,
                audience=audience,
                emitted_at=NOW_UTC,
                payload=approved,
            )
    with pytest.raises(ValidationError):
        EventEnvelope[AssignmentGrantedEvent](
            name=EventName.ASSIGNMENT_GRANTED,
            audience=SessionAudience(session_id=SessionId("sess-1")),
            emitted_at=NOW_UTC,
            payload=granted,
        )


def test_line_health_changed_event() -> None:
    """Round 25 (review-15 item 15): line_health_changed is the
    provider-health axis event - broadcast+admin scopes, old != new,
    LivenessState vocabulary (never LineState)."""
    health = LineHealthChangedEvent(
        line_id=LineId("line-main"),
        old_state=LivenessState.REACHABLE,
        new_state=LivenessState.UNREACHABLE,
    )
    # Round 27 (review-17 item 6): on an agent-owned EXCLUSIVE line
    # the health event routes ONLY to the owner's AgentAudience +
    # AdminAudience - broadcast is then forbidden because a private
    # line's health is private activity too. All three scopes are
    # legal envelope carriers; the kernel picks per ownership.
    for audience in (
        BroadcastAudience(),
        AdminAudience(),
        AgentAudience(agent_id="agent-1"),
    ):
        EventEnvelope[LineHealthChangedEvent](
            name=EventName.LINE_HEALTH_CHANGED,
            audience=audience,
            emitted_at=NOW_UTC,
            payload=health,
        )
    # session scope is never legal for the health axis - the fact
    # belongs to the line, not to one socket.
    with pytest.raises(ValidationError):
        EventEnvelope[LineHealthChangedEvent](
            name=EventName.LINE_HEALTH_CHANGED,
            audience=SessionAudience(session_id=SessionId("sess-1")),
            emitted_at=NOW_UTC,
            payload=health,
        )
    # A no-change "transition" is a defect, not a notification.
    with pytest.raises(ValidationError):
        LineHealthChangedEvent(
            line_id=LineId("line-main"),
            old_state=LivenessState.REACHABLE,
            new_state=LivenessState.REACHABLE,
        )
    # LineSummary carries the separate liveness column. Round 26
    # (review-16 item 13): the default is the derived UNBOUND state
    # for provider-less rows - which requires caps == () - while a
    # bound line reports the binding's health and mirrors its caps.
    row = LineSummary(line_id=LineId("line-pool"), caps=(), state=LineState.FREE)
    assert row.liveness is LivenessState.UNBOUND
    bound = LineSummary(
        line_id=LineId("line-main"),
        caps=[Intent.DIAL_OUT],
        state=LineState.BUSY,
        liveness=LivenessState.REACHABLE,
    )
    assert bound.liveness is LivenessState.REACHABLE


def test_queue_cancelled_event() -> None:
    """Round 25 (review-15 item 23): queue_cancelled names the ticket
    and the CLOSED QueueCancelReason; session-scoped only."""
    assert {r.value for r in QueueCancelReason} == {
        "line_exclusive",
        "provider_lost",
        "capability_removed",
        "claim_resolved",
    }
    cancelled = QueueCancelledEvent(
        session_id=SessionId("sess-1"),
        ticket_id=TicketId("t-1"),
        reason=QueueCancelReason.PROVIDER_LOST,
    )
    EventEnvelope[QueueCancelledEvent](
        name=EventName.QUEUE_CANCELLED,
        audience=SessionAudience(session_id=SessionId("sess-1")),
        emitted_at=NOW_UTC,
        payload=cancelled,
    )
    for audience in (BroadcastAudience(), AdminAudience(), AgentAudience(agent_id="a")):
        with pytest.raises(ValidationError):
            EventEnvelope[QueueCancelledEvent](
                name=EventName.QUEUE_CANCELLED,
                audience=audience,
                emitted_at=NOW_UTC,
                payload=cancelled,
            )
    # reason is required and closed - no free-form string.
    with pytest.raises(ValidationError):
        QueueCancelledEvent(  # type: ignore[call-arg]
            session_id=SessionId("sess-1"), ticket_id=TicketId("t-1")
        )
    with pytest.raises(ValidationError):
        QueueCancelledEvent(
            session_id=SessionId("sess-1"),
            ticket_id=TicketId("t-1"),
            reason="made_up",
        )


def test_reattach_media_surfaces() -> None:
    """Round 25 (review-15 item 9): MediaReattachResult carries fresh
    agent-side credentials; ProviderReattachMediaCommand rides the
    union + wire-name map."""
    result = MediaReattachResult(
        lease_id=LeaseId("l-1"),
        media_endpoint="wss://media.internal/agent",
        media_token=MediaToken(RedactedString("fresh-agent-token")),
    )
    safe = result.safe_dump()
    assert "fresh-agent-token" not in repr(safe)
    assert result.model_dump(mode="json")["media_token"] == "fresh-agent-token"

    # Union/wire parity: reattach_media resolves to line.reattach_media.
    assert PROVIDER_COMMAND_WIRE_NAMES["reattach_media"] == "line.reattach_media"
    adapter = TypeAdapter(ProviderCommand)
    parsed = adapter.validate_python(
        {
            "command": "reattach_media",
            "line_id": "line-main",
            "assignment_id": "bind-1",
            "lease_id": "l-1",
            "media_endpoint": "wss://media.internal/provider",
            "media_token": "p-tok",
        }
    )
    assert isinstance(parsed, ProviderReattachMediaCommand)
    assert parsed.media_token == MediaToken(RedactedString("p-tok"))


def test_provider_active_call_token_masked() -> None:
    """Round 25 (review-15 item 26): the optional call_token on a
    reregister snapshot element stays a RedactedString container -
    masked in safe_dump, raw on the wire dump."""
    # Round 27 (review-17 item 11): the snapshot element carries ONE
    # correlator - a still-ringing call names its call_token, never
    # both token and lease.
    with_token = ProviderActiveCall(
        call_token=CallToken(RedactedString("ring-token-secret")),
        state=ProviderCallState.RINGING,
    )
    safe = with_token.safe_dump()
    assert "ring-token-secret" not in repr(safe)
    assert safe["call_token"] == str(RedactedString("ring-token-secret"))
    rendered = json.dumps(with_token.to_log_dict())  # must not raise
    assert "ring-token-secret" not in rendered
    assert with_token.model_dump(mode="json")["call_token"] == "ring-token-secret"
    assert ProviderActiveCall(state=ProviderCallState.ACTIVE).call_token is None


# ---------------------------------------------------------------------------
# Round 25 batch 3 (v0.0.20) - item 22 numeric bounds, item 30 resource
# constants, item 32 binding health stamping, item 36 country/number-type
# validation, item 38 discriminator strictness, item 41 cross-field
# validators, item 42 error/clock/apply_patch hardening.
# ---------------------------------------------------------------------------


def _binding(**overrides: Any) -> ProviderBinding:
    kwargs: dict[str, Any] = {
        "assignment_id": contract.AssignmentId("bind-1"),
        "line_id": LineId("line-main"),
        "caps": (Intent.DIAL_OUT,),
        "heartbeat_degraded_after": 2,
        "heartbeat_down_after": 3,
        "connected_at": NOW_UTC,
        "last_seen_at": NOW_UTC,
        # Round 30 (SA_carbon_review_19 slice-A item 10): the control
        # socket generation is a required stamped field.
        "channel_seq": 1,
    }
    kwargs.update(overrides)
    return ProviderBinding(**kwargs)


def test_inf_nan_rejected_everywhere() -> None:
    """Round 25 (review-15 item 22): ``allow_inf_nan=False`` on the
    ContractModel base - inf/nan can never ride a float knob and
    explode ``timedelta`` arithmetic downstream."""
    for bad in (float("inf"), float("-inf"), float("nan")):
        with pytest.raises(ValidationError):
            LineTimeouts(grant_ttl_sec=bad)
        with pytest.raises(ValidationError):
            TimeoutConfig(orphan_grace_sec=bad)
        with pytest.raises(ValidationError):
            _assignment(accept_window_sec=bad)
        with pytest.raises(ValidationError):
            LineTimeouts.model_validate({"grant_ttl_sec": bad})


def test_time_knobs_one_day_ceiling() -> None:
    """Round 25 (review-15 item 22): every float time knob is capped
    at ``le=86400.0`` - one day; larger is a config bug, not a knob."""
    float_knobs = (
        "grant_ttl_sec",
        "orphan_grace_sec",
        "heartbeat_interval_sec",
        "fallback_accept_ttl_sec",
        "ring_drain_ttl_sec",
        "max_call_duration_sec",
    )
    for knob in float_knobs:
        assert getattr(LineTimeouts(**{knob: 86400.0}), knob) == 86400.0
        with pytest.raises(ValidationError):
            LineTimeouts(**{knob: 86400.01})
        assert getattr(TimeoutConfig(**{knob: 86400.0}), knob) == 86400.0
        with pytest.raises(ValidationError):
            TimeoutConfig(**{knob: 86400.01})
        with pytest.raises(ValidationError):
            LineTimeouts(**{knob: 1e20})
    assert _assignment(accept_window_sec=86400.0).accept_window_sec == 86400.0
    with pytest.raises(ValidationError):
        _assignment(accept_window_sec=86400.01)


def test_idle_revoke_after_positive_and_clear_flag() -> None:
    """Round 25 (review-15 item 22): a set ``idle_revoke_after`` must
    be strictly positive - ``None`` is the disabled state - and
    ``configure_line`` carries the explicit ``clear_idle_revoke_after``
    off switch the None-means-unchanged patch convention cannot
    express."""
    assert _assignment().idle_revoke_after is None
    assert _assignment(
        mode=AssignmentMode.EXCLUSIVE,
        owner_agent_id="agent-1",
        idle_revoke_after=timedelta(hours=24),
    ).idle_revoke_after == timedelta(hours=24)
    for bad in (timedelta(0), timedelta(seconds=-1), timedelta(days=-30)):
        with pytest.raises(ValidationError):
            _assignment(
                mode=AssignmentMode.EXCLUSIVE,
                owner_agent_id="agent-1",
                idle_revoke_after=bad,
            )
    # The off switch exists as a keyword param defaulting to False.
    params = inspect.signature(LineAdminKernel.configure_line).parameters
    assert params["clear_idle_revoke_after"].default is False
    assert params["idle_revoke_after"].default is None


def test_resource_bound_constants() -> None:
    """Round 25 (review-15 item 30): the resource caps are exported
    contract constants - sessions, tickets, leases and the two media
    socket deadlines. Round 27 (review-17 items 52/53/77): the
    string/collection bounds, closed-resource rings, global session
    cap, outbox soft cap and the control-frame cap join the exported
    surface so an implementation cannot grow them silently."""
    assert MAX_SESSIONS_PER_AGENT == 32
    assert MAX_TICKETS_PER_SESSION == 16
    assert MAX_QUEUE_TICKETS_TOTAL == 1024
    assert MAX_LEASES_PER_SESSION == 8
    assert MEDIA_PAIR_TTL_SEC == 30
    assert MEDIA_AUTH_TIMEOUT_SEC == 5
    assert MAX_DETAIL_LEN == 256
    assert MAX_REASON_LEN == 128
    assert MAX_PROFILE_LEN == 128
    assert MAX_CLOSED_ASSIGNMENTS == 1024
    assert MAX_CLOSED_LEASES == 1024
    assert MAX_SESSIONS_TOTAL == 512
    assert OUTBOX_SOFT_CAP == 256
    assert MAX_CTRL_FRAME_LEN == 4096
    for name in (
        "MAX_SESSIONS_PER_AGENT",
        "MAX_TICKETS_PER_SESSION",
        "MAX_QUEUE_TICKETS_TOTAL",
        "MAX_LEASES_PER_SESSION",
        "MEDIA_PAIR_TTL_SEC",
        "MEDIA_AUTH_TIMEOUT_SEC",
        "MAX_DETAIL_LEN",
        "MAX_REASON_LEN",
        "MAX_PROFILE_LEN",
        "MAX_PROVIDER_META_ENTRIES",
        "MAX_PROVIDER_META_KEY_LEN",
        "MAX_PROVIDER_META_VALUE_LEN",
        "MAX_FORWARD_HOPS",
        "MAX_IDEMPOTENCY_KEY_LEN",
        "MAX_PENDING_CLAIMS_PER_AGENT",
        "MAX_PENDING_CLAIMS_TOTAL",
        "MAX_DTMF_DIGITS",
        "MAX_CLOSED_ASSIGNMENTS",
        "MAX_CLOSED_LEASES",
        "MAX_SESSIONS_TOTAL",
        "OUTBOX_SOFT_CAP",
        "MAX_CTRL_FRAME_LEN",
        "MediaTransport",
        "AdminLineView",
        "ProviderMediaReattachRequest",
        "ProviderMediaReattachResult",
        "PairWindowRequest",
    ):
        assert name in contract.__all__


def test_provider_binding_stamped_thresholds() -> None:
    """Round 25 (review-15 item 32): the binding carries the missed-
    ping thresholds STAMPED at register/reregister - required fields,
    ``down > degraded`` enforced on the binding itself, and
    ``orphan_deadline_at`` legal only while WS_DISCONNECTED (item 41)."""
    binding = _binding()
    assert binding.heartbeat_degraded_after == 2
    assert binding.heartbeat_down_after == 3
    # Required: no implicit default may stand in for the stamped value.
    for missing in ("heartbeat_degraded_after", "heartbeat_down_after"):
        kwargs = {
            "assignment_id": contract.AssignmentId("bind-1"),
            "line_id": LineId("line-main"),
            "caps": (Intent.DIAL_OUT,),
            "heartbeat_degraded_after": 2,
            "heartbeat_down_after": 3,
            "connected_at": NOW_UTC,
            "last_seen_at": NOW_UTC,
            "channel_seq": 1,
        }
        del kwargs[missing]
        with pytest.raises(ValidationError):
            ProviderBinding(**kwargs)
    # The ladder invariant is re-checked on the stamped pair.
    with pytest.raises(ValidationError):
        _binding(heartbeat_degraded_after=3, heartbeat_down_after=3)
    with pytest.raises(ValidationError):
        _binding(heartbeat_degraded_after=5, heartbeat_down_after=3)
    with pytest.raises(ValidationError):
        _binding(heartbeat_degraded_after=0)
    # orphan_deadline_at exists only inside the WS-drop grace window.
    with pytest.raises(ValidationError):
        _binding(orphan_deadline_at=NOW_UTC)  # liveness defaults REACHABLE
    with pytest.raises(ValidationError):
        _binding(liveness=LivenessState.UNREACHABLE, orphan_deadline_at=NOW_UTC)
    orphan = _binding(liveness=LivenessState.WS_DISCONNECTED, orphan_deadline_at=NOW_UTC)
    assert orphan.orphan_deadline_at == NOW_UTC


def test_provider_liveness_requires_assignment_id() -> None:
    """Round 25 (review-15 item 32): a liveness fact without the
    binding id is unconstructible - it could cross-apply a stale
    close event to a re-registered binding."""
    fact = ProviderLiveness(
        line_id=LineId("line-main"),
        assignment_id=contract.AssignmentId("bind-1"),
        state=LivenessState.REACHABLE,
        checked_at=NOW_UTC,
        channel_seq=1,
    )
    assert fact.assignment_id == contract.AssignmentId("bind-1")
    with pytest.raises(ValidationError):
        ProviderLiveness(  # type: ignore[call-arg]
            line_id=LineId("line-main"),
            state=LivenessState.REACHABLE,
            checked_at=NOW_UTC,
            channel_seq=1,
        )


def test_validate_e164_country_region_membership() -> None:
    """Round 25 (review-15 item 36): ``country`` must be a KNOWN ISO
    3166-1 alpha-2 region - a syntactic but unknown code like "ZZ"
    never degrades to a COUNTRY_MISMATCH. Round 27 (review-17 item
    33): the distinction is carried by the typed ``ErrorReason``
    sub-code, not by message text."""
    for bad_country in ("ZZ", "AA", "QO"):
        assert _e164_reason(_RAW_NUMBER, bad_country) is ErrorReason.UNKNOWN_COUNTRY_CODE
    # Real regions still validate; the shape gate and the mismatch
    # branch remain distinct typed branches.
    assert validate_e164_country(RedactedString(_RAW_NUMBER), "MD") == _RAW_NUMBER
    assert _e164_reason(_RAW_NUMBER, "md") is ErrorReason.UNKNOWN_COUNTRY_CODE
    assert _e164_reason("+12015550123", "MD") is ErrorReason.COUNTRY_MISMATCH


def test_validate_e164_country_non_geographic_001() -> None:
    """Round 25 (review-15 item 36): ``country == "001"`` is legal
    ONLY for genuinely non-geographic numbering plans (+800 global
    toll-free); both mixed pairings are COUNTRY_MISMATCH (round 27,
    review-17 item 33: reported through the typed sub-code)."""
    assert validate_e164_country(RedactedString("+80012345678"), "001") == "+80012345678"
    # Non-001 number declared "001" -> mismatch.
    assert _e164_reason("+12015550123", "001") is ErrorReason.COUNTRY_MISMATCH
    # 001-region number declared a geographic country -> mismatch.
    assert _e164_reason("+80012345678", "US") is ErrorReason.COUNTRY_MISMATCH


def test_allowed_number_types_vocabulary() -> None:
    """Round 25 (review-15 item 36): ``allowed_number_types`` members
    must come from the phonenumbers type vocabulary - unknown/typo'd
    or lowercase members and an empty set are rejected, and the
    rejection never echoes the raw input."""
    assert _assignment(
        allowed_number_types=frozenset({"MOBILE", "FIXED_LINE_OR_MOBILE"})
    ).allowed_number_types == frozenset({"MOBILE", "FIXED_LINE_OR_MOBILE"})
    with pytest.raises(ValidationError):
        _assignment(allowed_number_types=frozenset())
    for bad in (frozenset({"NOT_A_TYPE"}), frozenset({"mobile"}), frozenset({"MOBILE", "XX"})):
        with pytest.raises(ValidationError) as excinfo:
            _assignment(allowed_number_types=bad)
        # Rule 10: the invalid member name is never echoed back.
        assert "XX" not in str(excinfo.value)
        assert "NOT_A_TYPE" not in str(excinfo.value)


def test_acquire_discriminator_strict() -> None:
    """Round 25 (review-15 item 38): ``granted`` is required and must
    be a bool - an empty dict can no longer silently produce
    AcquireRefused."""
    adapter = TypeAdapter(AcquireResult)
    for malformed in (
        {},
        {"queued": True, "ticket_id": "t-1", "position": 1},  # no granted
        {"granted": "yes"},
        {"granted": 1},  # int is not the bool True
        {"granted": None},
    ):
        # The discriminator's own ValueError propagates unwrapped
        # (ValidationError is itself a ValueError subclass).
        with pytest.raises(ValueError):
            adapter.validate_python(malformed)
    assert isinstance(adapter.validate_python({"granted": False}), AcquireRefused)
    assert isinstance(
        adapter.validate_python(
            {"granted": False, "queued": True, "ticket_id": "t-1", "position": 1}
        ),
        AcquireQueued,
    )
    assert isinstance(
        adapter.validate_python(
            {
                "granted": True,
                "line_id": "line-main",
                "lease_id": "l-1",
                "expires_at": NOW_UTC.isoformat(),
            }
        ),
        AcquireGranted,
    )


def test_call_state_changed_reason_scoping() -> None:
    """Round 25 (review-15 item 41): ``reason`` is a teardown fact -
    only ``state == "disconnected"`` may carry it; and
    ``reissued_ticket_id`` exists only on a preempted_by_incoming
    teardown."""
    base = {"lease_id": LeaseId("l-1"), "line_id": LineId("line-main")}
    for live_state in ("dialing", "alerting", "active"):
        with pytest.raises(ValidationError):
            CallStateChangedEvent(**base, state=live_state, reason=CallEndReason.NORMAL)
    # disconnected may carry a reason - and may omit it.
    CallStateChangedEvent(**base, state="disconnected", reason=CallEndReason.NORMAL)
    CallStateChangedEvent(**base, state="disconnected")
    # reissued_ticket_id only under PREEMPTED_BY_INCOMING.
    CallStateChangedEvent(
        **base,
        state="disconnected",
        reason=CallEndReason.PREEMPTED_BY_INCOMING,
        reissued_ticket_id=TicketId("t-9"),
    )
    with pytest.raises(ValidationError):
        CallStateChangedEvent(**base, state="disconnected", reissued_ticket_id=TicketId("t-9"))
    with pytest.raises(ValidationError):
        CallStateChangedEvent(
            **base,
            state="disconnected",
            reason=CallEndReason.NORMAL,
            reissued_ticket_id=TicketId("t-9"),
        )


def test_cross_field_validators_batch3() -> None:
    """Round 25 (review-15 item 41) remaining cross-field rules:
    ProviderCallStateUpdate correlator XOR, LineStateChangedEvent
    old != new, CallLease intent/time invariants, LineSummary caps."""
    # Alternative correlators: call_token XOR lease_id (neither is
    # the documented external-call/open-ring case).
    update_base = {
        "line_id": LineId("line-main"),
        "assignment_id": contract.AssignmentId("bind-1"),
        "state": ProviderCallState.ACTIVE,
        "observed_at": NOW_UTC,
    }
    ProviderCallStateUpdate(**update_base)  # neither - external call fact
    ProviderCallStateUpdate(**update_base, lease_id=LeaseId("l-1"))
    with pytest.raises(ValidationError):
        ProviderCallStateUpdate(
            **update_base,
            call_token=CallToken(RedactedString("tok-1")),
            lease_id=LeaseId("l-1"),
        )

    # A no-change line-state "transition" is a defect.
    with pytest.raises(ValidationError):
        LineStateChangedEvent(
            line_id=LineId("line-main"),
            old_state=LineState.BUSY,
            new_state=LineState.BUSY,
        )

    # A lease can never hold the durable exclusivity intent, and
    # answered_at can never precede created_at.
    lease_base = {
        "lease_id": LeaseId("l-1"),
        "line_id": LineId("line-main"),
        "owner_agent_id": "agent-1",
        "session_id": SessionId("sess-1"),
        "created_at": NOW_UTC,
    }
    with pytest.raises(ValidationError):
        CallLease(**lease_base, intent=Intent.EXCLUSIVE_LINE)
    with pytest.raises(ValidationError):
        CallLease(
            **lease_base,
            intent=Intent.DIAL_OUT,
            answered_at=NOW_UTC - timedelta(seconds=1),
        )
    # Round 30 (SA_carbon_review_19 slice-C item 14): answered_at is
    # legal only once the call is established - an ACTIVE-phase lease
    # may stamp it, a pre-dial ACQUIRED lease may not.
    ok_lease = CallLease(
        **lease_base,
        intent=Intent.RECEIVE_INBOUND,
        answered_at=NOW_UTC,
        phase=CallPhase.ACTIVE,
        # Round 33: a call-bearing phase owns a media credential
        # pair - the epochs the pair was minted under ride the
        # lease, so ACTIVE requires both role epochs >= 1 (split
        # into per-role counters in round 35).
        agent_media_epoch=1,
        provider_media_epoch=1,
    )
    assert ok_lease.answered_at == NOW_UTC
    with pytest.raises(ValidationError):
        CallLease(**lease_base, intent=Intent.RECEIVE_INBOUND, answered_at=NOW_UTC)

    # LineSummary mirrors the binding's telecom-only caps while a
    # provider is attached; an unbound/pool line has no binding and
    # legitimately reports an empty tuple (its claimability is a
    # LineAssignment fact, not a cap) - EXCLUSIVE_LINE stays banned.
    for bad_caps in ([Intent.EXCLUSIVE_LINE], [Intent.DIAL_OUT, Intent.EXCLUSIVE_LINE]):
        with pytest.raises(ValidationError):
            LineSummary(line_id=LineId("line-main"), caps=bad_caps, state=LineState.FREE)
    unbound = LineSummary(line_id=LineId("line-pool"), caps=(), state=LineState.FREE)
    assert unbound.caps == ()
    # Round 26 (review-16 item 13): an unbound/pool line reports
    # neither caps nor health - UNBOUND is the liveness default.
    assert unbound.liveness is LivenessState.UNBOUND
    summary = LineSummary(
        line_id=LineId("line-main"),
        caps=[Intent.DIAL_OUT, Intent.RECEIVE_INBOUND],
        state=LineState.FREE,
        liveness=LivenessState.REACHABLE,
    )
    assert summary.liveness is LivenessState.REACHABLE


def test_clock_and_media_allocator_runtime_checkable() -> None:
    """Round 25 (review-15 item 41): the injected pure ports are
    ``@runtime_checkable`` - wiring code can isinstance-probe them."""

    class _FakeClock:
        def __call__(self) -> datetime:
            return NOW_UTC

    class _FakeAllocator:
        def allocate(
            self, lease_id: LeaseId, line_id: LineId, session_id: SessionId | None = None
        ) -> MediaCredentials:
            return MediaCredentials(
                agent_media_endpoint="unix:///run/m.sock",
                provider_media_endpoint="wss://media.internal/provider",
                agent_media_token=MediaToken(RedactedString("a")),
                provider_media_token=MediaToken(RedactedString("p")),
                agent_media_epoch=1,
                provider_media_epoch=1,
            )

        def reissue(self, lease_id: LeaseId, role: contract.MediaRole) -> MediaCredentials:
            return MediaCredentials(
                agent_media_endpoint="unix:///run/m.sock",
                provider_media_endpoint="wss://media.internal/provider",
                agent_media_token=MediaToken(RedactedString("a2")),
                provider_media_token=MediaToken(RedactedString("p2")),
                agent_media_epoch=2,
                provider_media_epoch=2,
            )

        def revoke(self, lease_id: LeaseId) -> None:
            return None

    assert isinstance(_FakeClock(), contract.Clock)
    assert isinstance(_FakeAllocator(), contract.MediaAllocator)
    assert not isinstance(object(), contract.Clock)
    assert not isinstance(object(), contract.MediaAllocator)


def test_phonebridge_error_deepcopy_and_pickle() -> None:
    """Round 25 (review-15 item 42): ``args`` carries ``(code, data)``
    so deepcopy/pickle reconstruct through ``__init__`` instead of
    crashing on the stale ``code.value`` arg."""
    err = PhonebridgeError(
        ErrorCode.INVALID_ARGUMENT,
        ErrorData(reason=ErrorReason.INVALID_DTMF, detail="bad digits"),
    )
    clone = copy.deepcopy(err)
    assert clone.code is ErrorCode.INVALID_ARGUMENT
    assert clone.data == err.data
    assert clone.args == err.args == (ErrorCode.INVALID_ARGUMENT, err.data)
    assert str(clone) == str(err)
    restored = pickle.loads(pickle.dumps(err))
    assert restored.code is ErrorCode.INVALID_ARGUMENT
    assert restored.data == err.data
    assert str(restored) == str(err)
    # The no-data path round-trips too.
    bare = pickle.loads(pickle.dumps(PhonebridgeError(ErrorCode.STALE_TOKEN)))
    assert bare.data == ErrorData()


def test_apply_patch_detail_names_fields() -> None:
    """Round 25 (review-15 item 42): ``apply_patch`` failures carry
    the pydantic ``loc`` field path in ``"field: msg"`` form - a
    bare msg does not name the broken knob. The model-level ladder
    error keeps the bare-msg form (no loc)."""
    # model_copy bypasses validation -> a corrupt effective value the
    # merge must re-reject with its field name in detail.
    corrupt = LineTimeouts().model_copy(update={"grant_ttl_sec": -1.0})
    with pytest.raises(PhonebridgeError) as excinfo:
        corrupt.apply_patch(TimeoutConfig())
    assert excinfo.value.code is ErrorCode.INVALID_ARGUMENT
    detail = excinfo.value.data.detail
    assert detail is not None and "grant_ttl_sec:" in detail
    # Model-level (loc-less) failures keep the message-only form.
    with pytest.raises(PhonebridgeError) as excinfo2:
        LineTimeouts().apply_patch(TimeoutConfig(heartbeat_degraded_after=4))
    assert "heartbeat_down_after must exceed" in (excinfo2.value.data.detail or "")


def test_ring_drain_and_max_call_duration_timeouts() -> None:
    """Round 25 (review-15 items 16/24): ``ring_drain_ttl_sec``
    (~60 s default) bounds the post-decision ring-drain wait while
    the physical call still rings on the modem (design §5.2 step
    6); ``max_call_duration_sec`` is the OPTIONAL wall-clock cap
    whose effective ``None`` means "no cap" - disabled at the wire
    via the ``clear_max_call_duration`` flag, never via a zero
    patch."""
    # Defaults hydrate on the effective model.
    eff = LineTimeouts()
    assert eff.ring_drain_ttl_sec == 60.0
    assert eff.max_call_duration_sec is None
    # Patch-merge semantics: non-None patch fields replace the
    # stored value, an all-None patch is a no-op.
    patch = TimeoutConfig(ring_drain_ttl_sec=45.0, max_call_duration_sec=3600.0)
    merged = eff.apply_patch(patch)
    assert merged.ring_drain_ttl_sec == 45.0
    assert merged.max_call_duration_sec == 3600.0
    assert eff.ring_drain_ttl_sec == 60.0  # base untouched (atomic)
    # The explicit off switch resets a previously set cap to the
    # legitimate effective None - the flag itself never lands on
    # the merged model.
    cleared = merged.apply_patch(TimeoutConfig(clear_max_call_duration=True))
    assert cleared.max_call_duration_sec is None
    assert cleared.ring_drain_ttl_sec == 45.0
    # Clearing an already-unset cap is a harmless no-op.
    still_unset = LineTimeouts().apply_patch(TimeoutConfig(clear_max_call_duration=True))
    assert still_unset.max_call_duration_sec is None
    # clear=False behaves like an absent flag.
    uncleared = merged.apply_patch(TimeoutConfig(clear_max_call_duration=False))
    assert uncleared.max_call_duration_sec == 3600.0
    # Set-XOR-clear: value + True together is contradictory.
    with pytest.raises(ValidationError):
        TimeoutConfig(max_call_duration_sec=10.0, clear_max_call_duration=True)
    # Bound violations on BOTH models: 0, negative, >86400,
    # inf/nan (gt=0/le=86400.0 + allow_inf_nan=False).
    for bad in (0.0, -1.0, 86400.01, float("inf"), float("nan")):
        with pytest.raises(ValidationError):
            LineTimeouts(ring_drain_ttl_sec=bad)
        with pytest.raises(ValidationError):
            TimeoutConfig(ring_drain_ttl_sec=bad)
        with pytest.raises(ValidationError):
            LineTimeouts(max_call_duration_sec=bad)
        with pytest.raises(ValidationError):
            TimeoutConfig(max_call_duration_sec=bad)


# ---------------------------------------------------------------------------
# Round 26 (architect review 16 - SA_carbon_review_16), Phase-0 contract
# hardening - item 1 event payload-type map, item 2 forward-cycle hop
# bound, item 3 provider reason scoping, item 4 profile bound, item 5
# checked free_lines, item 13 UNBOUND liveness.
# ---------------------------------------------------------------------------


def test_event_payload_types_map() -> None:
    """Round 26 (review-16 item 1): EVENT_PAYLOAD_TYPES covers every
    EventName and agrees with the doc-verified payload table - and
    EventEnvelope rejects a ``(name, payload)`` mismatch, so a PII
    payload can never ride a broadcast-legal name."""
    assert set(EVENT_PAYLOAD_TYPES) == set(EventName)
    # Round 30 (SA_carbon_review_19 slice-C item 22): there is exactly
    # ONE payload table - the contract's; ``test_event_wire_name_parity``
    # verifies it against the doc's ``event.*`` rows, so a second
    # hand-maintained copy here would be pure drift surface.
    assert len(EVENT_PAYLOAD_TYPES) == len(EventName)
    for model in EVENT_PAYLOAD_TYPES.values():
        assert issubclass(model, contract.ContractModel)

    # A mis-typed pair is rejected even when the audience scope
    # itself is legal for the name: a broadcast-legal name carrying
    # caller-PII payload.
    with pytest.raises(ValidationError):
        EventEnvelope[IncomingCallEvent](
            name=EventName.LINE_STATE_CHANGED,
            audience=BroadcastAudience(),
            emitted_at=NOW_UTC,
            payload=IncomingCallEvent(
                line_id=LineId("line-main"),
                call_token=CallToken(RedactedString("tok-1")),
                caller_id=RedactedString(_RAW_NUMBER),
                expires_at=NOW_UTC,
            ),
        )
    # And the converse: a session/agent-legal name carrying a
    # broadcast-only payload model.
    with pytest.raises(ValidationError):
        EventEnvelope[LineStateChangedEvent](
            name=EventName.INCOMING_CALL,
            audience=AgentAudience(agent_id="agent-1"),
            emitted_at=NOW_UTC,
            payload=LineStateChangedEvent(
                line_id=LineId("line-main"),
                old_state=LineState.FREE,
                new_state=LineState.BUSY,
            ),
        )
    # A matching pair still constructs.
    EventEnvelope[LineStateChangedEvent](
        name=EventName.LINE_STATE_CHANGED,
        audience=BroadcastAudience(),
        emitted_at=NOW_UTC,
        payload=LineStateChangedEvent(
            line_id=LineId("line-main"),
            old_state=LineState.FREE,
            new_state=LineState.BUSY,
        ),
    )


def test_provider_call_state_reason_scoping() -> None:
    """Round 26 (review-16 item 3): the provider-side ``reason``
    mirrors the CallStateChangedEvent rule - it may ride only
    ``state == DISCONNECTED``; a reason on a live state would
    report why a still-live call ended."""
    base: dict[str, Any] = {
        "line_id": LineId("line-main"),
        "assignment_id": contract.AssignmentId("bind-1"),
        "lease_id": LeaseId("l-1"),
        "observed_at": NOW_UTC,
    }
    ProviderCallStateUpdate(
        **base,
        state=ProviderCallState.DISCONNECTED,
        reason=ProviderDisconnectReason.NORMAL,
    )
    ProviderCallStateUpdate(**base, state=ProviderCallState.DISCONNECTED)  # reason optional
    for live_state in (
        ProviderCallState.DIALING,
        ProviderCallState.ALERTING,
        ProviderCallState.ACTIVE,
    ):
        with pytest.raises(ValidationError):
            ProviderCallStateUpdate(
                **base, state=live_state, reason=ProviderDisconnectReason.NORMAL
            )
    # Round 30 (SA_carbon_review_19 slice-C item 21): RINGING is
    # rejected by the FIELD-level ``_state_is_post_ring`` guard (ring
    # facts ride IncomingRingIngress only) - it is pinned separately
    # so this loop exercises ONLY the model-level reason scoping,
    # never an earlier reject masquerading as it. RINGING fails with
    # AND without a reason, proving the field guard owns it.
    with pytest.raises(ValidationError):
        ProviderCallStateUpdate(**base, state=ProviderCallState.RINGING)
    with pytest.raises(ValidationError):
        ProviderCallStateUpdate(
            **base, state=ProviderCallState.RINGING, reason=ProviderDisconnectReason.NORMAL
        )


def test_profile_bounds() -> None:
    """Round 26 (review-16 item 4): ``profile`` is bounded to
    ``1..MAX_PROFILE_LEN`` chars when present - on
    ProviderRegistration, ProviderCapabilityUpdate AND the
    LineSummary mirror (the mirror shares the bound)."""
    assert MAX_PROFILE_LEN == 128
    overlong = "p" * (MAX_PROFILE_LEN + 1)
    reg_base: dict[str, Any] = {
        "line_id": LineId("line-main"),
        "caps": [Intent.DIAL_OUT],
        "channel_seq": 1,
    }
    cap_base: dict[str, Any] = {
        "line_id": LineId("line-main"),
        "assignment_id": contract.AssignmentId("bind-1"),
        "caps": [Intent.DIAL_OUT],
    }
    sum_base: dict[str, Any] = {
        "line_id": LineId("line-main"),
        "caps": [Intent.DIAL_OUT],
        "state": LineState.FREE,
        "liveness": LivenessState.REACHABLE,
    }
    # Overlong AND empty are rejected on all three carriers.
    for bad in (overlong, ""):
        with pytest.raises(ValidationError):
            ProviderRegistration(**reg_base, profile=bad)
        with pytest.raises(ValidationError):
            ProviderCapabilityUpdate(**cap_base, profile=bad)
        with pytest.raises(ValidationError):
            LineSummary(**sum_base, profile=bad)
    # Valid labels - including the boundary length - are legal.
    assert (
        ProviderRegistration(**reg_base, profile="p" * MAX_PROFILE_LEN).profile
        == "p" * MAX_PROFILE_LEN
    )
    assert ProviderCapabilityUpdate(**cap_base, profile="secretary").profile == "secretary"
    assert LineSummary(**sum_base, profile="front desk").profile == "front desk"
    # None stays the "no profile" state on all three.
    assert ProviderRegistration(**reg_base).profile is None
    assert ProviderCapabilityUpdate(**cap_base).profile is None
    assert LineSummary(**sum_base).profile is None


def test_free_lines_checked_line_ids() -> None:
    """Round 26 (review-16 item 5): ``free_lines`` entries go
    through ``_check_line_id`` on BOTH ErrorData and
    AcquireRefused - a colon-bearing, empty, overlong or
    ``"default"`` entry can never be echoed back to the wire."""
    ok = ErrorData(free_lines=[LineId("line-a"), LineId("line-b")])
    assert ok.free_lines == (LineId("line-a"), LineId("line-b"))
    assert AcquireRefused(free_lines=[LineId("line-a")]).free_lines == (LineId("line-a"),)
    adapter = TypeAdapter(AcquireResult)
    refused = adapter.validate_python({"granted": False, "free_lines": ["line-a"]})
    assert isinstance(refused, AcquireRefused)
    assert refused.free_lines == (LineId("line-a"),)
    # Colon-bearing (incl. whitespace+colon), empty, overlong and
    # the API-boundary alias are all rejected.
    for bad in ("line:x", " :x", "", "x" * 129, "default"):
        with pytest.raises(ValidationError):
            ErrorData(free_lines=[LineId(bad)])
        with pytest.raises(ValidationError):
            AcquireRefused(free_lines=[LineId(bad)])


def test_liveness_unbound_state() -> None:
    """Round 26 (review-16 item 13): UNBOUND is the derived
    provider-less listing state - LineSummary defaults to it, caps
    is non-empty IFF liveness differs from it, and UNBOUND can
    never appear as a reported fact (ProviderLiveness) or a
    transition endpoint (LineHealthChangedEvent)."""
    assert LivenessState.UNBOUND.value == "unbound"

    # The default listing row: a provider-less line reports neither
    # caps nor health.
    row = LineSummary(line_id=LineId("line-pool"), caps=(), state=LineState.FREE)
    assert row.liveness is LivenessState.UNBOUND

    # caps<->liveness IFF: a bound line mirrors non-empty caps.
    bound = LineSummary(
        line_id=LineId("line-main"),
        caps=[Intent.DIAL_OUT],
        state=LineState.FREE,
        liveness=LivenessState.REACHABLE,
    )
    assert bound.liveness is LivenessState.REACHABLE
    for health in (
        LivenessState.REACHABLE,
        LivenessState.DEGRADED,
        LivenessState.WS_DISCONNECTED,
        LivenessState.UNREACHABLE,
    ):
        LineSummary(
            line_id=LineId("line-main"),
            caps=[Intent.DIAL_OUT],
            state=LineState.FREE,
            liveness=health,
        )
        # Empty caps on a bound line is rejected.
        with pytest.raises(ValidationError):
            LineSummary(
                line_id=LineId("line-main"),
                caps=(),
                state=LineState.FREE,
                liveness=health,
            )
    # Non-empty caps on an unbound line is rejected.
    with pytest.raises(ValidationError):
        LineSummary(
            line_id=LineId("line-main"),
            caps=[Intent.DIAL_OUT],
            state=LineState.FREE,
            liveness=LivenessState.UNBOUND,
        )

    # Round 27 (review-17 item 9 - reverses the round-26 follow-up):
    # UNBOUND does NOT force state == FREE. During teardown/ring-drain
    # the binding is already gone while occupancy still drains, so a
    # provider-less row may legitimately report BUSY/RINGING_LOCKED -
    # the listing never lies about a still-open call.
    for occupied in (LineState.BUSY, LineState.RINGING_LOCKED):
        draining = LineSummary(
            line_id=LineId("line-pool"),
            caps=(),
            state=occupied,
            liveness=LivenessState.UNBOUND,
        )
        assert draining.state is occupied

    # A liveness REPORT always comes from an attached binding -
    # UNBOUND is never a reported fact.
    with pytest.raises(ValidationError):
        ProviderLiveness(
            line_id=LineId("line-main"),
            assignment_id=contract.AssignmentId("bind-1"),
            state=LivenessState.UNBOUND,
            checked_at=NOW_UTC,
            channel_seq=1,
        )

    # Transitions on a bound provider never produce UNBOUND -
    # binding revocation surfaces UNREACHABLE. Round 27 (review-17
    # item 8): UNBOUND is legal as the OLD state only - a
    # ``provider.register`` on a provider-less configured line mints
    # exactly one ``unbound -> reachable`` transition.
    for old, new in (
        (LivenessState.REACHABLE, LivenessState.UNBOUND),
        (LivenessState.UNREACHABLE, LivenessState.UNBOUND),
    ):
        with pytest.raises(ValidationError):
            LineHealthChangedEvent(line_id=LineId("line-main"), old_state=old, new_state=new)
    first_bind = LineHealthChangedEvent(
        line_id=LineId("line-main"),
        old_state=LivenessState.UNBOUND,
        new_state=LivenessState.REACHABLE,
    )
    assert first_bind.old_state is LivenessState.UNBOUND


def test_max_forward_hops_constant() -> None:
    """Round 26 (review-16 item 2): the transitive forward-chain
    walk the kernel MUST perform in ``configure_line`` is bounded -
    MAX_FORWARD_HOPS pins the hop ceiling (docs/specs/
    line-manager.md)."""
    assert MAX_FORWARD_HOPS == 8
    assert "MAX_FORWARD_HOPS" in contract.__all__
    assert "MAX_PROFILE_LEN" in contract.__all__
    assert "EVENT_PAYLOAD_TYPES" in contract.__all__


# ---------------------------------------------------------------------------
# Round 27 (review-17) - strict identifier grammar (item 36/38), bounded
# free-form fields (items 52/63), closed vocabularies and new model
# invariants (items 2/3/24/27/46/47/53/61/66/78).
# ---------------------------------------------------------------------------

# Strict-grammar violations shared across every checked-id carrier
# (round 27, review-17 item 36): first char must be alphanumeric,
# whitespace/control/path/glob/colon characters are rejected.
_BAD_IDS = (
    "-lead",  # leading non-alphanumeric
    ".dot",
    "_under",
    "lead space",
    "trailing ",
    "tab\tchar",
    "new\nline",
    "car\rreturn",
    "/slash",
    "back\\slash",
    "*glob",
    "?glob",
    "[bracket",
    "colon:x",
    "a:b",
    "",
    "x" * 129,
)


def test_checked_id_strict_charset() -> None:
    """Round 27 (review-17 item 36): every checked identifier matches
    ``[A-Za-z0-9][A-Za-z0-9._-]*`` - ids are interpolated into
    PipeWire node names and state-file paths, so separators,
    whitespace and glob metacharacters are rejected on EVERY
    checked-id carrier, not just ``line_id``."""
    for bad in _BAD_IDS:
        with pytest.raises(ValidationError):
            LineSummary(
                line_id=LineId(bad),
                caps=(),
                state=LineState.FREE,
                liveness=LivenessState.UNBOUND,
            )
        with pytest.raises(ValidationError):
            _ticket(bad, 1)  # ticket_id
        with pytest.raises(ValidationError):
            CallLease(
                lease_id=LeaseId(bad),
                line_id=LineId("line-main"),
                owner_agent_id="agent-1",
                session_id=SessionId("sess-1"),
                intent=Intent.DIAL_OUT,
                created_at=NOW_UTC,
                # Round 34 (iteration-2 audit item 25): the grant
                # deadline is a REQUIRED field - omitting it made
                # this a false-positive guard (ValidationError fired
                # on the missing field even with the charset check
                # removed).
                grant_expires_at=NOW_UTC + timedelta(seconds=5),
            )
        with pytest.raises(ValidationError):
            QueueTicket(
                ticket_id=TicketId("t-1"),
                seq=1,
                session_id=SessionId(bad),
                intent=Intent.DIAL_OUT,
                enqueued_at=NOW_UTC,
            )
        with pytest.raises(ValidationError):
            PendingClaim(
                claim_id=ClaimId(bad),
                line_id=LineId("line-main"),
                requester_agent_id="agent-1",
                session_id=SessionId("sess-1"),
                created_at=NOW_UTC,
            )
        with pytest.raises(ValidationError):
            AgentAudience(agent_id=bad)
        # ``_CheckedId`` carriers: worker-policy params.
        with pytest.raises(ValidationError):
            RecordAndTelegramFallback(channel=bad, announcement_ref="rec-note")
        # ``_CheckedAssignmentId`` carrier.
        with pytest.raises(ValidationError):
            _assignment(assignment_id=contract.AssignmentId(bad))
    # ``announcement_ref`` carriers run a WIDER grammar (round 32,
    # SA_swe2_review_2 item 19): ``:`` namespaces and ``/`` paths
    # are legal, so the strict-id list does not apply wholesale -
    # the banned subset is charset violations plus traversal.
    for bad_ref in (
        "lead space",
        "tab\tchar",
        "new\nline",
        "back\\slash",
        "*glob",
        "?glob",
        "[bracket",
        "/slash",
        "../escape",
        "a/../b",
        "",
        "x" * 129,
    ):
        with pytest.raises(ValidationError):
            AnswerAndAnnounceFallback(announcement_ref=bad_ref)
        with pytest.raises(ValidationError):
            RecordAndTelegramFallback(channel="ops-alerts", announcement_ref=bad_ref)
    # ...while namespaced/relative-path selectors stay legal.
    assert (
        AnswerAndAnnounceFallback(announcement_ref="ns:dir/file.wav").announcement_ref
        == "ns:dir/file.wav"
    )
    # Dots/dashes/underscores INSIDE the id stay legal.
    ok = LineSummary(
        line_id=LineId("line-1.main_2"),
        caps=(),
        state=LineState.FREE,
        liveness=LivenessState.UNBOUND,
    )
    assert ok.line_id == "line-1.main_2"


def test_system_identity_suffix_strictness() -> None:
    """Round 27 (review-17 item 38): the ``system:`` suffix obeys the
    FULL external-id hygiene - ``_check_id`` runs on the suffix, so
    leading separators, interior whitespace and control characters
    are rejected, not merely empty/colon-bearing suffixes."""
    for bad in (
        "system: x",
        "system:x y",
        "system:-lead",
        "system:.dot",
        "system:_under",
        "system:/path",
        "system:x\ty",
        "system:" + "x" * 129,
    ):
        with pytest.raises(ValidationError):
            AgentAudience(agent_id=bad)
    # Legal members of the reserved namespace still construct.
    assert AgentAudience(agent_id="system:media-gw_2").agent_id == "system:media-gw_2"


def test_durable_owner_never_system() -> None:
    """Round 27 (review-17 items 21/38): a ``system:*`` identity can
    never own a durable LineAssignment - internal roles hold no
    line ownership."""
    with pytest.raises(ValidationError):
        _assignment(mode=AssignmentMode.EXCLUSIVE, owner_agent_id=SYSTEM_FALLBACK_WORKER)
    with pytest.raises(ValidationError):
        _assignment(mode=AssignmentMode.EXCLUSIVE, owner_agent_id="system:anything")
    ok = _assignment(mode=AssignmentMode.EXCLUSIVE, owner_agent_id="agent-1")
    assert ok.owner_agent_id == "agent-1"


def test_bounded_detail_and_reason_fields() -> None:
    """Round 27 (review-17 items 52/63): every free-form
    ``detail``/``reason`` on a wire-facing model is bounded -
    ``MAX_DETAIL_LEN`` on ``ErrorData.detail``, ``MAX_REASON_LEN``
    on the operational reason fields."""
    overlong_detail = "d" * (MAX_DETAIL_LEN + 1)
    overlong_reason = "r" * (MAX_REASON_LEN + 1)
    assert ErrorData(detail="d" * MAX_DETAIL_LEN).detail == "d" * MAX_DETAIL_LEN
    with pytest.raises(ValidationError):
        ErrorData(detail=overlong_detail)
    cmd_base = {
        "line_id": LineId("line-main"),
        "assignment_id": contract.AssignmentId("bind-1"),
    }
    assert (
        ProviderRevokeCommand(**cmd_base, reason="r" * MAX_REASON_LEN).reason
        == "r" * MAX_REASON_LEN
    )
    with pytest.raises(ValidationError):
        ProviderRevokeCommand(**cmd_base, reason=overlong_reason)
    with pytest.raises(ValidationError):
        AssignmentRevokedEvent(
            assignment_id=contract.AssignmentId("asg-1"),
            line_id=LineId("line-main"),
            owner_agent_id="agent-1",
            reason=overlong_reason,
        )
    with pytest.raises(ValidationError):
        ClaimResolvedEvent(
            claim_id=ClaimId("claim-1"),
            line_id=LineId("line-main"),
            status="rejected",
            reason=overlong_reason,
        )
    with pytest.raises(ValidationError):
        FallbackDecision(
            line_id=LineId("line-main"),
            call_token=CallToken(RedactedString("tok-1")),
            policy=NoPickupFallback(),
            reason=overlong_reason,
        )
    # Boundary length stays legal on the event carriers.
    assert (
        AssignmentRevokedEvent(
            assignment_id=contract.AssignmentId("asg-1"),
            line_id=LineId("line-main"),
            owner_agent_id="agent-1",
            reason="r" * MAX_REASON_LEN,
        ).reason
        == "r" * MAX_REASON_LEN
    )


def test_missed_call_voicemail_ref_removed() -> None:
    """Round 27 (review-17 item 24): ``voicemail_ref`` was dropped
    from ``event.missed_call`` - no producer ever minted it, and a
    dead wire member is removed rather than carried. The extra-forbid
    policy rejects a stale sender that still sets it."""
    assert "voicemail_ref" not in MissedCallEvent.__pydantic_fields__
    with pytest.raises(ValidationError):
        MissedCallEvent.model_validate(
            {
                "line_id": "line-main",
                "reason": "normal",
                "voicemail_ref": "vm-123",
            }
        )


def test_assignment_granted_exclusive_only() -> None:
    """Round 27 (review-17 item 47): ``event.assignment_granted``
    fires only on the transition to EXCLUSIVE ownership - a
    ``shared_pool`` mode on the payload is a defect, not a
    notification."""
    ok = AssignmentGrantedEvent(
        assignment_id=contract.AssignmentId("asg-9"),
        line_id=LineId("line-main"),
        mode=AssignmentMode.EXCLUSIVE,
    )
    assert ok.mode is AssignmentMode.EXCLUSIVE
    with pytest.raises(ValidationError):
        AssignmentGrantedEvent(
            assignment_id=contract.AssignmentId("asg-9"),
            line_id=LineId("line-main"),
            mode=AssignmentMode.SHARED_POOL,
        )


def test_media_transport_closed_literal() -> None:
    """Round 27 (review-17 item 78): ``MediaTransport`` is the closed
    ``Literal["ws_pcm"]`` vocabulary - a second transport may only be
    added by a wire-document amendment, same rule as the event and
    command tables."""
    assert get_args(MediaTransport) == ("ws_pcm",)
    adapter = TypeAdapter(MediaTransport)
    assert adapter.validate_python("ws_pcm") == "ws_pcm"
    with pytest.raises(ValidationError):
        adapter.validate_python("rtp_pcm")


def test_pair_window_request_bounds() -> None:
    """Round 27 (review-17 item 61): ``admin.pair_window`` params are
    a typed model - the 300 s pairing-window ceiling is enforced at
    the boundary, not silently clamped."""
    req = PairWindowRequest(line_id=LineId("line-main"), duration_sec=60.0)
    assert req.duration_sec == 60.0
    assert PairWindowRequest(line_id=LineId("line-main"), duration_sec=300.0)
    for bad in (0.0, -1.0, 300.1):
        with pytest.raises(ValidationError):
            PairWindowRequest(line_id=LineId("line-main"), duration_sec=bad)
    with pytest.raises(ValidationError):
        PairWindowRequest(line_id=LineId("line:x"), duration_sec=60.0)


def test_line_summary_owner_scoped_fields() -> None:
    """Round 27 (review-17 item 2): ``LineSummary.assignment_id``
    is the owner-scoped self-identification the kernel fills on the
    caller's own EXCLUSIVE rows only. Round 28 (review-18 item 3):
    ``provider_meta`` is privileged provider hardware metadata and
    has been REMOVED from the agent-facing listing - it lives on
    ``AdminLineView`` now."""
    row = LineSummary(
        line_id=LineId("line-main"),
        caps=[Intent.DIAL_OUT],
        state=LineState.BUSY,
        liveness=LivenessState.REACHABLE,
        assignment_id=contract.AssignmentId("asg-1"),
    )
    assert row.assignment_id == contract.AssignmentId("asg-1")
    # Defaults: no assignment id.
    bare = LineSummary(line_id=LineId("line-pool"), caps=(), state=LineState.FREE)
    assert bare.assignment_id is None
    # Round 28 (review-18 item 3): the privileged mirror is gone -
    # not a field, not accepted, not on the wire dump.
    assert "provider_meta" not in LineSummary.__pydantic_fields__
    with pytest.raises(ValidationError):
        LineSummary.model_validate(
            {
                "line_id": "line-main",
                "caps": ["dial_out"],
                "state": "FREE",
                "provider_meta": {"model": "gt-1"},
            }
        )
    assert "provider_meta" not in row.model_dump()


def test_admin_line_view_invariants() -> None:
    """Round 27 (review-17 item 3): ``AdminLineView`` shares the
    caps<->liveness pairing of ``LineSummary``, exposes the durable
    and call ids the agent listing hides, and ``queue_position`` is
    a 1-based global FIFO rank."""
    view = AdminLineView(
        line_id=LineId("line-main"),
        caps=[Intent.DIAL_OUT],
        state=LineState.BUSY,
        liveness=LivenessState.REACHABLE,
        assignment_id=contract.AssignmentId("asg-1"),
        owner_agent_id="agent-1",
        lease_id=LeaseId("l-1"),
        queue_position=3,
        profile="front desk",
        provider_meta={"model": "gt-1"},
    )
    assert view.queue_position == 3
    # Round 28 (review-18 items 3/4): the admin view carries the
    # profile and the privileged provider_meta mirror that left
    # LineSummary - same ``_bounded_meta`` invariant as the register
    # ingress, same ``1..MAX_PROFILE_LEN`` as ProviderBinding.
    assert view.profile == "front desk"
    assert view.provider_meta == {"model": "gt-1"}
    for bad_profile in ("", "p" * (MAX_PROFILE_LEN + 1)):
        with pytest.raises(ValidationError):
            AdminLineView(
                line_id=LineId("line-main"),
                caps=[Intent.DIAL_OUT],
                state=LineState.FREE,
                liveness=LivenessState.REACHABLE,
                profile=bad_profile,
            )
    for bad_meta in (
        {f"k{i}": "v" for i in range(MAX_PROVIDER_META_ENTRIES + 1)},
        {"k": "v" * (MAX_PROVIDER_META_VALUE_LEN + 1)},
    ):
        with pytest.raises(ValidationError):
            AdminLineView(
                line_id=LineId("line-main"),
                caps=[Intent.DIAL_OUT],
                state=LineState.FREE,
                liveness=LivenessState.REACHABLE,
                provider_meta=bad_meta,
            )
    # Unbound rows carry no ids of a provider binding and stay legal
    # with a draining non-FREE occupancy (item 9).
    unbound = AdminLineView(line_id=LineId("line-pool"), caps=(), state=LineState.RINGING_LOCKED)
    assert unbound.liveness is LivenessState.UNBOUND
    # caps/liveness mismatch and a zero rank are rejected.
    with pytest.raises(ValidationError):
        AdminLineView(
            line_id=LineId("line-main"),
            caps=(),
            state=LineState.FREE,
            liveness=LivenessState.REACHABLE,
        )
    with pytest.raises(ValidationError):
        AdminLineView(
            line_id=LineId("line-main"),
            caps=[Intent.DIAL_OUT],
            state=LineState.FREE,
            liveness=LivenessState.REACHABLE,
            queue_position=0,
        )
    with pytest.raises(ValidationError):
        AdminLineView(
            line_id=LineId("line-main"),
            caps=[Intent.EXCLUSIVE_LINE],
            state=LineState.FREE,
            liveness=LivenessState.REACHABLE,
        )


def test_provider_binding_profile_and_meta_bounded() -> None:
    """Round 27 (review-17 item 27): ``ProviderBinding`` carries the
    registration's ``profile`` and ``meta`` forward - same
    ``1..MAX_PROFILE_LEN`` and ``_bounded_meta`` invariants as the
    ingress, so a stored row can never outgrow what was accepted."""
    binding = _binding(profile="front desk", meta={"model": "gt-1"})
    assert binding.profile == "front desk"
    assert binding.meta == {"model": "gt-1"}
    for bad in ("", "p" * (MAX_PROFILE_LEN + 1)):
        with pytest.raises(ValidationError):
            _binding(profile=bad)
    with pytest.raises(ValidationError):
        _binding(meta={"k": "v" * (MAX_PROVIDER_META_VALUE_LEN + 1)})
    with pytest.raises(ValidationError):
        _binding(meta={f"k{i}": "v" for i in range(MAX_PROVIDER_META_ENTRIES + 1)})


def test_provider_liveness_snapshot_reachable_only() -> None:
    """Round 27 (review-17 item 66): ``state_snapshot`` may ride only
    a REACHABLE ping - a WS_DISCONNECTED or UNREACHABLE fact
    describes a dead/dying channel whose call list the kernel must
    not reconcile against."""
    snap = [ProviderActiveCall(lease_id=LeaseId("l-1"), state=ProviderCallState.ACTIVE)]
    ok = ProviderLiveness(
        line_id=LineId("line-main"),
        assignment_id=contract.AssignmentId("bind-1"),
        state=LivenessState.REACHABLE,
        checked_at=NOW_UTC,
        channel_seq=1,
        state_snapshot=snap,
    )
    assert ok.state_snapshot == tuple(snap)
    for dead in (LivenessState.WS_DISCONNECTED, LivenessState.UNREACHABLE):
        with pytest.raises(ValidationError):
            ProviderLiveness(
                line_id=LineId("line-main"),
                assignment_id=contract.AssignmentId("bind-1"),
                state=dead,
                checked_at=NOW_UTC,
                channel_seq=1,
                state_snapshot=snap,
            )


def test_provider_active_call_correlators_xor() -> None:
    """Round 27 (review-17 item 11, PR #68 review): the snapshot
    element's correlators are ALTERNATIVE - carrying BOTH
    ``call_token`` and ``lease_id`` is rejected, while carrying
    NEITHER is the documented external-call case and constructs."""
    with pytest.raises(ValidationError):
        ProviderActiveCall(
            call_token=CallToken(RedactedString("ring-token")),
            lease_id=LeaseId("l-1"),
            state=ProviderCallState.ACTIVE,
        )
    external = ProviderActiveCall(state=ProviderCallState.DIALING)
    assert external.call_token is None
    assert external.lease_id is None
    # Documented leniency, pinned explicitly (round 29,
    # SA_quant_review_1 slice-C item 10): correlator<->state
    # coherence is DELIBERATELY NOT model-checked (round 27,
    # review-17 item 11) - a token-carrying ACTIVE element is legal
    # because the snapshot may describe a ring the air side already
    # answered before ``line.answer`` landed. This assert makes the
    # open contract item visible: a future coherence validator
    # would flip it red and must be a deliberate contract decision,
    # not a silent tightening.
    mixed = ProviderActiveCall(
        call_token=CallToken(RedactedString("ring-token")),
        state=ProviderCallState.ACTIVE,
    )
    assert mixed.call_token is not None
    assert mixed.lease_id is None


def test_call_lease_carries_no_audio_latch() -> None:
    """Round 31 (SA_swe2_review_1 item 4): the ``audio_ready`` flag
    lives KERNEL-SIDE, not on the frozen lease - the
    ``audio_ready_received`` field that round 27 added was removed
    because media-latch state mutates per attachment epoch while
    ``CallLease`` records the call's identity/timing facts. The
    kernel resets the latch on ``provider.audio_lost`` /
    ``provider.reregister`` / a provider media-socket drop and
    ``send_dtmf`` consults it."""
    assert "audio_ready_received" not in CallLease.__pydantic_fields__
    lease = CallLease(
        lease_id=LeaseId("l-1"),
        line_id=LineId("line-main"),
        owner_agent_id="agent-1",
        session_id=SessionId("sess-1"),
        intent=Intent.DIAL_OUT,
        created_at=NOW_UTC,
        grant_expires_at=NOW_UTC + timedelta(seconds=5),
    )
    assert not hasattr(lease, "audio_ready_received")


def test_provider_media_reattach_models() -> None:
    """Round 27 (review-17 item 4): ``provider.reattach_media`` takes
    a typed request naming the live binding + lease and returns
    fresh provider-role media credentials (endpoint + one-shot
    token)."""
    req = ProviderMediaReattachRequest(
        line_id=LineId("line-main"),
        assignment_id=contract.AssignmentId("bind-1"),
        lease_id=LeaseId("l-1"),
    )
    assert req.lease_id == LeaseId("l-1")
    result = ProviderMediaReattachResult(
        media_endpoint="unix:///run/phonebridge-media.sock",
        media_token=MediaToken(RedactedString("p-secret")),
    )
    assert "p-secret" not in repr(result.safe_dump())
    assert result.model_dump(mode="json")["media_token"] == "p-secret"
    with pytest.raises(ValidationError):
        ProviderMediaReattachRequest(
            line_id=LineId("line-main"),
            assignment_id=contract.AssignmentId("bind-1"),
            lease_id=LeaseId("bad:id"),
        )


# ---------------------------------------------------------------------------
# Round 28 (SA_carbon_review_18) contract tests
# ---------------------------------------------------------------------------


def test_liveness_degraded_transition_matrix() -> None:
    """Round 28 (review-18 item 1; ``degraded -> ws_disconnected``
    added in the round-28 fix, PR #72 review): DEGRADED is the
    kernel-computed middle rung of ``reachable -> degraded ->
    unreachable`` - it is entered ONLY from ``reachable`` and
    resolves into ``reachable``, ``unreachable`` or
    ``ws_disconnected`` (socket loss overrides the elapsed-time
    rung so the orphan grace stays reachable); every other touch
    of the rung is rejected."""
    line_id = LineId("line-main")
    for old, new in (
        (LivenessState.REACHABLE, LivenessState.DEGRADED),
        (LivenessState.DEGRADED, LivenessState.REACHABLE),
        (LivenessState.DEGRADED, LivenessState.UNREACHABLE),
        (LivenessState.DEGRADED, LivenessState.WS_DISCONNECTED),
    ):
        ev = LineHealthChangedEvent(line_id=line_id, old_state=old, new_state=new)
        assert ev.new_state is new
    # No other transition touches the degraded rung - degradation is
    # kernel-computed, so neither a WS drop nor an unreachable fact
    # can enter it, and it cannot resolve to unbound or self-loop.
    for old, new in (
        (LivenessState.WS_DISCONNECTED, LivenessState.DEGRADED),
        (LivenessState.UNREACHABLE, LivenessState.DEGRADED),
        (LivenessState.UNBOUND, LivenessState.DEGRADED),
        (LivenessState.DEGRADED, LivenessState.UNBOUND),
        (LivenessState.DEGRADED, LivenessState.DEGRADED),
    ):
        with pytest.raises(ValidationError):
            LineHealthChangedEvent(line_id=line_id, old_state=old, new_state=new)
    # The pre-existing legal transitions on the reported states still
    # hold under the new validator.
    for old, new in (
        (LivenessState.REACHABLE, LivenessState.WS_DISCONNECTED),
        (LivenessState.REACHABLE, LivenessState.UNREACHABLE),
        (LivenessState.WS_DISCONNECTED, LivenessState.REACHABLE),
        (LivenessState.WS_DISCONNECTED, LivenessState.UNREACHABLE),
        (LivenessState.UNREACHABLE, LivenessState.REACHABLE),
        (LivenessState.UNBOUND, LivenessState.REACHABLE),
    ):
        LineHealthChangedEvent(line_id=line_id, old_state=old, new_state=new)


def test_provider_liveness_rejects_degraded() -> None:
    """Round 28 (review-18 item 1): DEGRADED is kernel-computed,
    never a provider-reported observation - ProviderLiveness refuses
    it the same way it refuses the derived UNBOUND state."""
    base: dict[str, Any] = {
        "line_id": LineId("line-main"),
        "assignment_id": contract.AssignmentId("bind-1"),
        "checked_at": NOW_UTC,
        "channel_seq": 1,
    }
    for bad in (LivenessState.DEGRADED, LivenessState.UNBOUND):
        with pytest.raises(ValidationError):
            ProviderLiveness(**base, state=bad)
    for ok in (
        LivenessState.REACHABLE,
        LivenessState.WS_DISCONNECTED,
        LivenessState.UNREACHABLE,
    ):
        ProviderLiveness(**base, state=ok)


def test_call_end_reason_max_call_duration() -> None:
    """Round 28 (review-18 item 7): ``max_call_duration`` is the
    closed-vocabulary reason the kernel stamps when the absolute
    call cap tears down a DIALING/ALERTING/ACTIVE lease."""
    assert CallEndReason.MAX_CALL_DURATION.value == "max_call_duration"
    ev = CallStateChangedEvent(
        lease_id=LeaseId("l-1"),
        line_id=LineId("line-main"),
        state="disconnected",
        reason=CallEndReason.MAX_CALL_DURATION,
    )
    assert ev.reason is CallEndReason.MAX_CALL_DURATION
    wire = ev.model_dump(mode="json")
    assert wire["reason"] == "max_call_duration"


def test_confirmed_answer_confidence_gate() -> None:
    """Round 28 (review-18 item 16): ``answer_confidence: "confirmed"``
    may ride ONLY ``active``/``disconnected`` - a confirmed answer
    while ``dialing``/``alerting`` reports a human pickup that
    cannot have happened yet; ``provisional`` stays legal on the
    early phases."""
    base: dict[str, Any] = {"lease_id": LeaseId("l-1"), "line_id": LineId("line-main")}
    for state in ("active", "disconnected"):
        ev = CallStateChangedEvent(**base, state=state, answer_confidence="confirmed")
        assert ev.answer_confidence == "confirmed"
    for early in ("dialing", "alerting"):
        with pytest.raises(ValidationError):
            CallStateChangedEvent(**base, state=early, answer_confidence="confirmed")
        CallStateChangedEvent(**base, state=early, answer_confidence="provisional")


def test_provider_registration_reregister_flag() -> None:
    """Round 28 (review-18 item 3): ``reregister`` is the
    adapter-distilled method flag - ``False`` for
    ``provider.register`` (the default), ``True`` for
    ``provider.reregister``; it is kernel-facing metadata, not a
    wire field the provider could spoof."""
    base: dict[str, Any] = {
        "line_id": LineId("line-main"),
        "caps": [Intent.DIAL_OUT],
        "channel_seq": 1,
    }
    assert ProviderRegistration(**base).reregister is False
    assert ProviderRegistration(**base, reregister=True).reregister is True


def test_provider_meta_is_an_owned_copy() -> None:
    """Round 28 (review-18 item 16): ``_bounded_meta`` hands the model
    a FRESH dict - mutating the adapter's source map after
    construction cannot corrupt the stored fact on either
    ProviderRegistration or ProviderBinding."""
    source = {"model": "gt-1"}
    reg = ProviderRegistration(
        line_id=LineId("line-main"), caps=[Intent.DIAL_OUT], meta=source, channel_seq=1
    )
    binding = _binding(meta=source)
    source["model"] = "tampered"
    source["new"] = "v"
    assert reg.meta == {"model": "gt-1"}
    assert binding.meta == {"model": "gt-1"}


def test_provider_meta_is_serializable_and_immutable() -> None:
    """Round 34 (iteration-2 audit item 3): the stored ``meta`` map
    is the immutable-yet-JSON-shaped ``_FrozenMeta`` - it must
    deepcopy, pickle, JSON-encode and feed ``_mask_for_log`` like a
    plain dict (the ``MappingProxyType`` wrapper it replaces could
    do none of those), while every mutator fails loudly and the
    default value is itself validated by the bound."""
    import copy as copy_module
    import json

    binding = _binding(meta={"model": "gt-1", "fw": "1.2"})
    meta = binding.meta
    # Immutable surface: every write path raises.
    mutators: list[Callable[[contract._FrozenMeta], Any]] = [
        lambda m: m.__setitem__("k", "v"),
        lambda m: m.update({"k": "v"}),
        lambda m: m.pop("model"),
        lambda m: m.clear(),
    ]
    frozen = cast(contract._FrozenMeta, meta)
    for mutate in mutators:
        with pytest.raises(TypeError):
            mutate(frozen)
    # Serial shapes all work: the map IS a dict to the encoders.
    assert json.dumps(meta) == json.dumps({"model": "gt-1", "fw": "1.2"})
    assert pickle.loads(pickle.dumps(meta)) == meta
    assert copy_module.deepcopy(meta) == meta
    dumped = binding.model_dump(mode="json")
    assert dumped["meta"] == {"model": "gt-1", "fw": "1.2"}
    # ``_mask_for_log`` treats it as an ordinary mapping - values
    # survive verbatim, nothing raises.
    masked = contract._mask_for_log({"meta": meta})
    assert masked["meta"] == {"model": "gt-1", "fw": "1.2"}
    # The DEFAULT (omitted meta) is validated too - an empty map
    # stores as the frozen empty mapping, and a constructed
    # over-bounded default can never reach storage.
    default_binding = _binding()
    assert default_binding.meta == {}
    assert isinstance(default_binding.meta, contract._FrozenMeta)


def test_checked_token_and_caller_number_bounds() -> None:
    """Round 28 (review-18 items 16/17): call/media tokens are
    non-empty and at most ``MAX_TOKEN_LEN``; ``number``/``caller_id``
    fields are non-empty when present and at most
    ``MAX_CALLER_ID_LEN`` (a withheld identity is ``None``, never
    ``""``)."""
    overlong_token = "t" * (MAX_TOKEN_LEN + 1)
    overlong_number = "1" * (MAX_CALLER_ID_LEN + 1)
    event_base: dict[str, Any] = {
        "line_id": LineId("line-main"),
        "call_token": CallToken(RedactedString("tok-1")),
        "expires_at": NOW_UTC,
    }
    # Token bound on the ring event and on provider commands.
    for bad in ("", overlong_token):
        with pytest.raises(ValidationError):
            IncomingCallEvent(**{**event_base, "call_token": CallToken(RedactedString(bad))})
        with pytest.raises(ValidationError):
            ProviderAnswerCommand(
                line_id=LineId("line-main"),
                assignment_id=contract.AssignmentId("bind-1"),
                call_token=CallToken(RedactedString(bad)),
                lease_id=LeaseId("l-1"),
                media_endpoint="wss://media.internal/provider",
                media_token=MediaToken(RedactedString("p-tok")),
            )
        with pytest.raises(ValidationError):
            ProviderMediaReattachResult(
                media_endpoint="unix:///run/m.sock",
                media_token=MediaToken(RedactedString(bad)),
            )
    # Caller-number bound on the ring event and the dial command.
    for bad in ("", overlong_number):
        with pytest.raises(ValidationError):
            IncomingCallEvent(**event_base, caller_id=RedactedString(bad))
        with pytest.raises(ValidationError):
            ProviderDialCommand(
                line_id=LineId("line-main"),
                assignment_id=contract.AssignmentId("bind-1"),
                lease_id=LeaseId("l-1"),
                number=RedactedString(bad),
                media_endpoint="wss://media.internal/provider",
                media_token=MediaToken(RedactedString("p-tok")),
            )
        with pytest.raises(ValidationError):
            MissedCallEvent(
                line_id=LineId("line-main"),
                caller_id=RedactedString(bad),
                reason=CallEndReason.NORMAL,
            )
    # Boundary lengths stay legal; withheld caller_id stays None.
    ok = IncomingCallEvent(
        **{
            **event_base,
            "call_token": CallToken(RedactedString("t" * MAX_TOKEN_LEN)),
            "caller_id": RedactedString("1" * MAX_CALLER_ID_LEN),
        }
    )
    assert ok.caller_id is not None
    assert IncomingCallEvent(**event_base).caller_id is None


def test_media_endpoint_bounds() -> None:
    """Round 28 (review-18 item 17): every media endpoint field is
    non-empty and at most ``MAX_ENDPOINT_LEN`` - on the credential
    pair, the agent-facing results and the provider commands."""
    overlong = "wss://" + "e" * MAX_ENDPOINT_LEN
    creds_base: dict[str, Any] = {
        "agent_media_endpoint": "unix:///run/m.sock",
        "provider_media_endpoint": "wss://media.internal/provider",
        "agent_media_token": MediaToken(RedactedString("a")),
        "provider_media_token": MediaToken(RedactedString("p")),
        "agent_media_epoch": 1,
        "provider_media_epoch": 1,
    }
    for bad in ("", overlong):
        with pytest.raises(ValidationError):
            MediaCredentials(**{**creds_base, "agent_media_endpoint": bad})
        with pytest.raises(ValidationError):
            MediaCredentials(**{**creds_base, "provider_media_endpoint": bad})
        with pytest.raises(ValidationError):
            DialResult(
                lease_id=LeaseId("l-1"),
                media_endpoint=bad,
                media_token=MediaToken(RedactedString("a")),
            )
        with pytest.raises(ValidationError):
            ProviderMediaReattachResult(
                media_endpoint=bad,
                media_token=MediaToken(RedactedString("p")),
            )
    # The boundary length is legal.
    edge = "wss://" + "e" * (MAX_ENDPOINT_LEN - 6)
    assert len(edge) == MAX_ENDPOINT_LEN
    assert (
        DialResult(
            lease_id=LeaseId("l-1"),
            media_endpoint=edge,
            media_token=MediaToken(RedactedString("a")),
        ).media_endpoint
        == edge
    )


def test_idle_revoke_after_upper_bound() -> None:
    """Round 28 (review-18 item 15): ``idle_revoke_after`` is bounded
    ABOVE too - a value past one day is a config bug, not an alert
    window."""
    _assignment(idle_revoke_after=timedelta(seconds=86400))  # boundary ok
    _assignment(idle_revoke_after=None)
    with pytest.raises(ValidationError):
        _assignment(idle_revoke_after=timedelta(seconds=86400) + timedelta(milliseconds=1))
    for bad in (timedelta(0), timedelta(seconds=-1)):
        with pytest.raises(ValidationError):
            _assignment(idle_revoke_after=bad)


def test_call_lease_profile() -> None:
    """Round 28 (review-18 item 4; QueueTicket leg added in the
    round-28 fix, PR #72 review - audit item 20 named BOTH models):
    ``CallLease.profile`` preserves the ``acquire(profile=...)``
    label on the grant so admin lease listings can show which
    profile won the line, and ``QueueTicket.profile`` carries the
    same label through queueing so the granted lease inherits it."""
    base: dict[str, Any] = {
        "lease_id": LeaseId("l-1"),
        "line_id": LineId("line-main"),
        "owner_agent_id": "agent-1",
        "session_id": SessionId("sess-1"),
        "intent": Intent.DIAL_OUT,
        "created_at": NOW_UTC,
        "grant_expires_at": NOW_UTC + timedelta(seconds=5),
    }
    assert CallLease(**base, profile="front-desk").profile == "front-desk"
    assert CallLease(**base).profile is None
    for bad in ("", "p" * (MAX_PROFILE_LEN + 1)):
        with pytest.raises(ValidationError):
            CallLease(**base, profile=bad)

    # Ticket -> lease inheritance: the ticket stores the label under
    # the same Optional bounded-string semantics, so the lease minted
    # at grant time can carry it forward verbatim.
    ticket = _ticket("t-1", 1)
    assert ticket.profile is None
    labelled = QueueTicket(
        ticket_id=TicketId("t-2"),
        seq=2,
        session_id=SessionId("sess-1"),
        intent=Intent.DIAL_OUT,
        enqueued_at=NOW_UTC,
        profile="front-desk",
    )
    assert labelled.profile == "front-desk"
    for bad in ("", "p" * (MAX_PROFILE_LEN + 1)):
        with pytest.raises(ValidationError):
            QueueTicket(
                ticket_id=TicketId("t-3"),
                seq=3,
                session_id=SessionId("sess-1"),
                intent=Intent.DIAL_OUT,
                enqueued_at=NOW_UTC,
                profile=bad,
            )
    # Field parity: identical annotation shape on both carriers is
    # what makes verbatim inheritance expressible.
    lease_field = CallLease.model_fields["profile"]
    ticket_field = QueueTicket.model_fields["profile"]
    assert lease_field.annotation == ticket_field.annotation
    assert lease_field.is_required() == ticket_field.is_required() is False


def test_missed_call_reason_excludes_max_call_duration() -> None:
    """Round-28 fix (PR #72 review): ``max_call_duration`` rides the
    shared :class:`CallEndReason` vocabulary for lease teardowns
    but is INADMISSIBLE on ``event.missed_call`` - a missed call is
    an unanswered ring that never held a lease, so the model
    rejects the lease-scoped reason."""
    with pytest.raises(ValidationError):
        MissedCallEvent(
            line_id=LineId("line-main"),
            reason=CallEndReason.MAX_CALL_DURATION,
        )
    # The admissible missed_call reasons still construct.
    for reason in (
        CallEndReason.NORMAL,
        CallEndReason.ACCEPT_WINDOW_EXPIRED,
        CallEndReason.PROVIDER_LOST,
    ):
        ev = MissedCallEvent(line_id=LineId("line-main"), reason=reason)
        assert ev.reason is reason


def test_media_role_literal() -> None:
    """Round 28 (review-18 item 2): ``MediaRole`` is the closed
    ``Literal["agent", "provider"]`` vocabulary
    :meth:`MediaAllocator.reissue` addresses - a third role may only
    be added by a contract amendment."""
    assert get_args(MediaRole) == ("agent", "provider")
    adapter = TypeAdapter(MediaRole)
    assert adapter.validate_python("agent") == "agent"
    assert adapter.validate_python("provider") == "provider"
    with pytest.raises(ValidationError):
        adapter.validate_python("worker")


def test_pair_window_response_model() -> None:
    """Round 28 (review-18 item 14): ``admin.pair_window`` returns the
    typed ``PairWindowResponse`` carrying the absolute UTC expiry of
    the pairing window."""
    resp = PairWindowResponse(line_id=LineId("line-main"), expires_at=NOW_UTC)
    assert resp.expires_at == NOW_UTC
    assert "PairWindowResponse" in contract.__all__
    # A naive stamp is rejected (rule 4: aware UTC only).
    with pytest.raises(ValidationError):
        PairWindowResponse(
            line_id=LineId("line-main"),
            expires_at=datetime(2026, 11, 13, 12, 0, 0),
        )
    with pytest.raises(ValidationError):
        PairWindowResponse(line_id=LineId("bad:id"), expires_at=NOW_UTC)


def test_round28_bounds_constants_exported() -> None:
    """Round 28 (review-18 items 11/16/17): the new bound constants
    are public so adapters and tests can pin the same numbers."""
    for name in (
        "MAX_CLOCK_SKEW_SEC",
        "MAX_TOKEN_LEN",
        "MAX_ENDPOINT_LEN",
        "MAX_CALLER_ID_LEN",
        "MediaRole",
        "PairWindowResponse",
    ):
        assert name in contract.__all__
    assert MAX_CLOCK_SKEW_SEC == 30.0
    assert MAX_TOKEN_LEN == 256
    assert MAX_ENDPOINT_LEN == 512
    assert MAX_CALLER_ID_LEN == 64


def test_round29_bounds_constants_exported() -> None:
    """Round 29 (SA_quant_review_1 slice-A item 10, slice-B item 6):
    the new bound constants are public so adapters and tests can
    pin the same numbers."""
    for name in (
        "MAX_IDEMPOTENCY_KEYS_PER_SESSION",
        "MAX_LINES_TOTAL",
        "MAX_LIST_PAGE",
        "SNAPSHOT_RECONCILED_PHASES",
        "clamp_provider_stamp",
    ):
        assert name in contract.__all__
    assert MAX_IDEMPOTENCY_KEYS_PER_SESSION == 256
    assert MAX_LINES_TOTAL == 256
    # Round 31 (SA_swe2_review_1 item 9): the shared list page cap
    # shrank from 256 to 64 - a full page of ``meta``-carrying rows
    # overflowed a single wire frame.
    assert MAX_LIST_PAGE == 64


def test_snapshot_reconciled_phases() -> None:
    """Round 29 (SA_quant_review_1 slice-A item 7; ``ANSWERING``
    added round 31, SA_swe2_review_1 item 2): the formal
    "dial-or-answer-commanded" set for ``state_snapshot``
    reconciliation is pinned verbatim - ``ANSWERING`` belongs
    because the ``line.answer`` command already reached the
    provider, ``RINGING`` is excluded because no lease may carry
    the reserved member at all, and ``ACQUIRED``/``ENDED`` can
    never be reconciled against a provider-side call view."""
    assert SNAPSHOT_RECONCILED_PHASES == frozenset(
        {
            CallPhase.DIALING,
            CallPhase.ANSWERING,
            CallPhase.ALERTING,
            CallPhase.ACTIVE,
            CallPhase.TERMINATING,
        }
    )
    for excluded in (CallPhase.ACQUIRED, CallPhase.RINGING, CallPhase.ENDED):
        assert excluded not in SNAPSHOT_RECONCILED_PHASES


def test_clamp_provider_stamp() -> None:
    """Round 29 (SA_quant_review_1 slice-A item 4): the canonical
    advisory-stamp clamp - a stamp deeper than MAX_CLOCK_SKEW_SEC
    into the FUTURE normalizes to ``now``; a stamp inside the skew
    window and every PAST stamp returns verbatim; naive inputs are
    rejected per ``ensure_aware_utc``."""
    far_future = NOW_UTC + timedelta(seconds=MAX_CLOCK_SKEW_SEC + 0.5)
    assert contract.clamp_provider_stamp(far_future, NOW_UTC) == NOW_UTC
    # The rule is strictly-greater: exactly MAX_CLOCK_SKEW_SEC out
    # is still a verbatim dedupe key.
    at_boundary = NOW_UTC + timedelta(seconds=MAX_CLOCK_SKEW_SEC)
    assert contract.clamp_provider_stamp(at_boundary, NOW_UTC) == at_boundary
    within = NOW_UTC + timedelta(seconds=MAX_CLOCK_SKEW_SEC - 1)
    assert contract.clamp_provider_stamp(within, NOW_UTC) == within
    past = NOW_UTC - timedelta(hours=3)
    assert contract.clamp_provider_stamp(past, NOW_UTC) == past
    # Non-UTC aware input normalizes to UTC per ensure_aware_utc.
    plus3 = timezone(timedelta(hours=3))
    foreign = datetime(2026, 11, 13, 15, 0, 30, tzinfo=plus3)  # 12:00:30Z
    assert contract.clamp_provider_stamp(foreign, NOW_UTC) == NOW_UTC + timedelta(seconds=30)
    # Naive stamps and naive `now` are rejected - rule 6 fail-closed.
    with pytest.raises(ValueError, match="naive"):
        contract.clamp_provider_stamp(datetime(2026, 11, 13, 12, 0, 0), NOW_UTC)
    with pytest.raises(ValueError, match="naive"):
        contract.clamp_provider_stamp(NOW_UTC, datetime(2026, 11, 13, 12, 0, 0))


def test_provider_binding_heartbeat_interval_ms_upper_bound() -> None:
    """Round 29 (SA_quant_review_1 slice-A item 8):
    ``heartbeat_interval_ms`` carries ``le=86_400_000`` - the exact
    millisecond counterpart of ``heartbeat_interval_sec <= 86400``
    so a hydrated binding cannot carry a cadence the config model
    would reject."""
    assert _binding(heartbeat_interval_ms=86_400_000).heartbeat_interval_ms == 86_400_000
    with pytest.raises(ValidationError):
        _binding(heartbeat_interval_ms=86_400_001)
    # The converter can never stamp beyond the field bound.
    assert heartbeat_interval_ms_from_sec(86400.0) == 86_400_000


def test_checked_id_length_boundaries() -> None:
    """Round 29 (SA_quant_review_1 slice-C item 11): every
    checked-id carrier accepts exactly its maximum length and
    rejects max+1 - the off-by-one is pinned per carrier, not just
    on ``line_id``."""
    at_max = "a" * contract._ID_MAX_LEN
    over = "a" * (contract._ID_MAX_LEN + 1)

    def line_summary(raw: str) -> LineSummary:
        return LineSummary(
            line_id=LineId(raw),
            caps=(),
            state=LineState.FREE,
            liveness=LivenessState.UNBOUND,
        )

    def lease(raw: str) -> CallLease:
        return CallLease(
            lease_id=LeaseId(raw),
            line_id=LineId("line-main"),
            owner_agent_id="agent-1",
            session_id=SessionId("sess-1"),
            intent=Intent.DIAL_OUT,
            created_at=NOW_UTC,
            grant_expires_at=NOW_UTC + timedelta(seconds=5),
        )

    def ticket_session(raw: str) -> QueueTicket:
        return QueueTicket(
            ticket_id=TicketId("t-1"),
            seq=1,
            session_id=SessionId(raw),
            intent=Intent.DIAL_OUT,
            enqueued_at=NOW_UTC,
        )

    def claim(raw: str) -> PendingClaim:
        return PendingClaim(
            claim_id=ClaimId(raw),
            line_id=LineId("line-main"),
            requester_agent_id="agent-1",
            session_id=SessionId("sess-1"),
            created_at=NOW_UTC,
        )

    carriers: tuple[Callable[[str], str], ...] = (
        lambda raw: line_summary(raw).line_id,
        lambda raw: lease(raw).lease_id,
        lambda raw: ticket_session(raw).session_id,
        lambda raw: _ticket(raw, 1).ticket_id,
        lambda raw: claim(raw).claim_id,
        lambda raw: _assignment(assignment_id=contract.AssignmentId(raw)).assignment_id,
        lambda raw: RecordAndTelegramFallback(channel=raw, announcement_ref="rec-note").channel,
        lambda raw: AnswerAndAnnounceFallback(announcement_ref=raw).announcement_ref,
        lambda raw: AgentAudience(agent_id=raw).agent_id,
    )
    for carrier in carriers:
        assert len(carrier(at_max)) == contract._ID_MAX_LEN
        with pytest.raises(ValidationError):
            carrier(over)

    # Token carriers ride MAX_TOKEN_LEN (256), not _ID_MAX_LEN.
    tok_max = "t" * MAX_TOKEN_LEN
    tok_over = "t" * (MAX_TOKEN_LEN + 1)
    event = IncomingCallEvent(
        line_id=LineId("line-main"),
        call_token=CallToken(RedactedString(tok_max)),
        caller_id=RedactedString(_RAW_NUMBER),
        expires_at=NOW_UTC,
    )
    assert len(event.call_token) == MAX_TOKEN_LEN
    with pytest.raises(ValidationError):
        IncomingCallEvent(
            line_id=LineId("line-main"),
            call_token=CallToken(RedactedString(tok_over)),
            caller_id=RedactedString(_RAW_NUMBER),
            expires_at=NOW_UTC,
        )
    # Caller-number carriers ride MAX_CALLER_ID_LEN (64).
    assert len(RedactedString("c" * MAX_CALLER_ID_LEN)) == MAX_CALLER_ID_LEN
    assert (
        IncomingCallEvent(
            line_id=LineId("line-main"),
            call_token=CallToken(RedactedString("tok")),
            caller_id=RedactedString("c" * MAX_CALLER_ID_LEN),
            expires_at=NOW_UTC,
        ).caller_id
        is not None
    )
    with pytest.raises(ValidationError):
        IncomingCallEvent(
            line_id=LineId("line-main"),
            call_token=CallToken(RedactedString("tok")),
            caller_id=RedactedString("c" * (MAX_CALLER_ID_LEN + 1)),
            expires_at=NOW_UTC,
        )


def test_aggregate_limit_consistency() -> None:
    """Round 29 (SA_quant_review_1 slice-C item 6): pin the actual
    relationship among the aggregate caps. Deliberately NOT a
    product invariant: the per-session caps bound ONE session's
    occupancy while the global caps bound aggregate occupancy, so
    the global caps are ALWAYS the binding constraint (per-session
    x max-sessions would overshoot each global bound). No
    ``MAX_LEASES_TOTAL`` exists by design - live leases are bounded
    implicitly by sessions x ``MAX_LEASES_PER_SESSION``."""
    assert MAX_TICKETS_PER_SESSION * MAX_SESSIONS_TOTAL > MAX_QUEUE_TICKETS_TOTAL
    assert MAX_QUEUE_TICKETS_TOTAL == 1024
    assert MAX_TICKETS_PER_SESSION * MAX_SESSIONS_TOTAL == 16 * 512 == 8192
    # The dedup-table bound joins the same "no global cap" family:
    # idempotency keys are session-scoped, so the per-session cap
    # alone bounds each table and totals follow the session caps.
    # Pin the exported values themselves (PR #74 re-review) - the
    # earlier "> 0" assertion was vacuous.
    assert MAX_IDEMPOTENCY_KEYS_PER_SESSION == 256
    assert MAX_SESSIONS_TOTAL == 512
    assert "MAX_LEASES_TOTAL" not in contract.__all__


def test_tick_backwards_now_hard_invariant() -> None:
    """Round 30 (SA_carbon_review_19 slice-C items 5/19): the
    tick-monotonicity clause is pinned BEHAVIORALLY instead of by
    prose substring - the ``now`` parameter is typed
    ``contract.AwareDatetime``, so a naive stamp is rejected by the
    shared ``ensure_aware_utc`` validator before it can ever reach
    the kernel, and the docstring still carries the
    ``INVALID_ARGUMENT`` verdict for a backwards stamp."""
    hints = get_type_hints(LineManager.tick, include_extras=True)
    assert hints["now"] == contract.AwareDatetime
    # The shared validator behind the annotation: naive input is a
    # defect, aware non-UTC input normalizes to UTC.
    with pytest.raises(ValueError):
        ensure_aware_utc(datetime(2026, 11, 13, 12, 0, 0))
    doc = LineManager.tick.__doc__ or ""
    assert "INVALID_ARGUMENT" in doc
    assert "non-decreasing" in doc


# ---------------------------------------------------------------------------
# Round 30 (SA_carbon_review_19) contract tests - Batch 1 additions:
# full liveness matrix, enum wire dictionaries, phase/owner invariants,
# new constants and models, pagination and endpoint bounds.
# ---------------------------------------------------------------------------

# Pinned legal (old_state, new_state) pairs on event.line_health_changed
# (round 30, SA_carbon_review_19 slice-C item 4): the complete 5x5
# matrix is exercised by test_liveness_transition_matrix_full_25_pairs
# - every pair NOT in this set MUST be a ValidationError.
_LEGAL_HEALTH_TRANSITIONS: frozenset[tuple[LivenessState, LivenessState]] = frozenset(
    {
        # First provider attach on a provider-less configured line.
        (LivenessState.UNBOUND, LivenessState.REACHABLE),
        # Heartbeat ladder entry + exits.
        (LivenessState.REACHABLE, LivenessState.DEGRADED),
        (LivenessState.REACHABLE, LivenessState.WS_DISCONNECTED),
        (LivenessState.REACHABLE, LivenessState.UNREACHABLE),
        (LivenessState.DEGRADED, LivenessState.REACHABLE),
        (LivenessState.DEGRADED, LivenessState.UNREACHABLE),
        (LivenessState.DEGRADED, LivenessState.WS_DISCONNECTED),
        # Orphan-grace recovery or expiry.
        (LivenessState.WS_DISCONNECTED, LivenessState.REACHABLE),
        (LivenessState.WS_DISCONNECTED, LivenessState.UNREACHABLE),
        # Reregister recovery reports the new binding's rung.
        (LivenessState.UNREACHABLE, LivenessState.REACHABLE),
    }
)


def test_liveness_transition_matrix_full_25_pairs() -> None:
    """Round 30 (SA_carbon_review_19 slice-C item 4): the COMPLETE
    5x5 liveness transition matrix is pinned - ten legal pairs,
    fifteen rejections including self-loops, every ``-> unbound``
    endpoint, every entry into ``degraded`` except from
    ``reachable``, and the new terminal rule that
    ``unreachable -> ws_disconnected`` can never re-arm the orphan
    grace."""
    line_id = LineId("line-main")
    for old in LivenessState:
        for new in LivenessState:
            if (old, new) in _LEGAL_HEALTH_TRANSITIONS:
                ev = LineHealthChangedEvent(line_id=line_id, old_state=old, new_state=new)
                assert ev.old_state is old and ev.new_state is new
            else:
                with pytest.raises(ValidationError):
                    LineHealthChangedEvent(line_id=line_id, old_state=old, new_state=new)
    # The round-30 terminal rule is named explicitly so a regression
    # message is unambiguous.
    with pytest.raises(ValidationError):
        LineHealthChangedEvent(
            line_id=line_id,
            old_state=LivenessState.UNREACHABLE,
            new_state=LivenessState.WS_DISCONNECTED,
        )


def test_ws_disconnected_binding_requires_orphan_deadline() -> None:
    """Round 30 (SA_carbon_review_19 slice-C item 4): the
    ``orphan_deadline_at`` <-> ``ws_disconnected`` invariant is
    BIDIRECTIONAL - a dead-socket binding without the deadline could
    neither be reaped nor recovered."""
    with pytest.raises(ValidationError):
        _binding(liveness=LivenessState.WS_DISCONNECTED)
    orphan = _binding(liveness=LivenessState.WS_DISCONNECTED, orphan_deadline_at=NOW_UTC)
    assert orphan.orphan_deadline_at == NOW_UTC
    # ...and UNBOUND is never a durable binding state - it is the
    # derived listing projection for a line with no binding row.
    with pytest.raises(ValidationError):
        _binding(liveness=LivenessState.UNBOUND)


def test_error_code_wire_values_pinned() -> None:
    """Round 30 (SA_carbon_review_19 slice-C item 8): the numeric
    JSON-RPC codes are pinned VERBATIM - set-membership alone would
    not catch two codes being swapped."""
    assert ERROR_CODE_WIRE == {
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


def test_closed_vocabulary_value_dicts_pinned() -> None:
    """Round 30 (SA_carbon_review_19 slice-C item 7): the closed
    enum vocabularies are pinned as full value dictionaries so a
    renamed or re-cased member cannot drift silently."""
    assert {m.name: m.value for m in ProviderDisconnectReason} == {
        "NORMAL": "normal",
        "BUSY": "busy",
        "NO_ANSWER": "no_answer",
        "REJECTED": "rejected",
        "NETWORK_ERROR": "network_error",
        "DONGLE_LOST": "dongle_lost",
        "STACK_RESTART": "stack_restart",
    }
    assert {m.name: m.value for m in CallEndReason} == {
        "NORMAL": "normal",
        "BUSY": "busy",
        "NO_ANSWER": "no_answer",
        "REJECTED": "rejected",
        "NETWORK_ERROR": "network_error",
        "DONGLE_LOST": "dongle_lost",
        "STACK_RESTART": "stack_restart",
        "AGENT_HANGUP": "agent_hangup",
        "PREEMPTED_BY_INCOMING": "preempted_by_incoming",
        "GRANT_TTL_EXPIRED": "grant_ttl_expired",
        "ACCEPT_WINDOW_EXPIRED": "accept_window_expired",
        "AGENT_DISCONNECTED": "agent_disconnected",
        "PROVIDER_LOST": "provider_lost",
        "EXTERNAL_CALL": "external_call",
        "MAX_CALL_DURATION": "max_call_duration",
    }
    assert {m.name: m.value for m in ErrorReason} == {
        "ACCEPT_WINDOW_EXPIRED": "ACCEPT_WINDOW_EXPIRED",
        "IDEMPOTENCY_CONFLICT": "IDEMPOTENCY_CONFLICT",
        "QUEUE_FULL": "QUEUE_FULL",
        "TICKET_NOT_FOUND": "TICKET_NOT_FOUND",
        "INVALID_CALL_STATE": "INVALID_CALL_STATE",
        "LINE_NOT_CLAIMABLE": "LINE_NOT_CLAIMABLE",
        "MEDIA_AUTH_FAILED": "MEDIA_AUTH_FAILED",
        "UNKNOWN_COUNTRY_CODE": "UNKNOWN_COUNTRY_CODE",
        "COUNTRY_MISMATCH": "COUNTRY_MISMATCH",
        "INVALID_NUMBER": "INVALID_NUMBER",
        "INVALID_FORMAT_NOT_E164": "INVALID_FORMAT_NOT_E164",
        "UNSUPPORTED_NUMBER_TYPE": "UNSUPPORTED_NUMBER_TYPE",
        "LINE_POLICY_REJECT": "LINE_POLICY_REJECT",
        "GRANT_TTL_EXPIRED": "GRANT_TTL_EXPIRED",
        "SESSION_LIMIT_EXCEEDED": "SESSION_LIMIT_EXCEEDED",
        "CLAIM_NOT_FOUND": "CLAIM_NOT_FOUND",
        "INVALID_DTMF": "INVALID_DTMF",
        "CURSOR_MISMATCH": "CURSOR_MISMATCH",
    }
    assert {m.name: m.value for m in ProviderCallState} == {
        "RINGING": "ringing",
        "DIALING": "dialing",
        "ALERTING": "alerting",
        "ACTIVE": "active",
        "DISCONNECTED": "disconnected",
    }


def _lease_base() -> dict[str, Any]:
    return {
        "lease_id": LeaseId("l-1"),
        "line_id": LineId("line-main"),
        "owner_agent_id": "agent-1",
        "session_id": SessionId("sess-1"),
        "intent": Intent.DIAL_OUT,
        "created_at": NOW_UTC,
    }


def test_call_lease_phase_cross_field_invariants() -> None:
    """Round 30 (SA_carbon_review_19 slice-C item 14 + slice-A item
    16): the phase/field coherence rules are model-enforced -
    ``phase="ringing"`` is reserved for wire compat and rejected on
    every CallLease, ``answered_at``/``"confirmed"`` require an
    established call, and ``grant_expires_at`` is consumed once the
    call is active."""
    base = _lease_base()
    # RINGING can never be a lease phase - inbound-ring state lives
    # on LineState.RINGING_LOCKED and the line's ring slot.
    with pytest.raises(ValidationError):
        CallLease(**base, phase=CallPhase.RINGING)
    # Pre-established phases forbid answer facts - round 31
    # (SA_swe2_review_1 item 2) adds ANSWERING, the answer-in-flight
    # phase that precedes any confirmed establishment.
    for early in (
        CallPhase.ACQUIRED,
        CallPhase.DIALING,
        CallPhase.ANSWERING,
        CallPhase.ALERTING,
    ):
        with pytest.raises(ValidationError):
            CallLease(**base, phase=early, answered_at=NOW_UTC)
        with pytest.raises(ValidationError):
            CallLease(**base, phase=early, answer_confidence="confirmed")
    # The grant TTL rides ACQUIRED and ONLY ACQUIRED - REQUIRED
    # there (round 31 item 37); round 32 (SA_swe2_review_2 item 21)
    # removed the DIALING/ALERTING tolerance so a consumed deadline
    # can never linger as a field a reader might mis-enforce.
    CallLease(**base, grant_expires_at=NOW_UTC + timedelta(seconds=5))
    for post in (
        CallPhase.DIALING,
        CallPhase.ANSWERING,
        CallPhase.ALERTING,
        CallPhase.ACTIVE,
        CallPhase.TERMINATING,
        CallPhase.ENDED,
    ):
        with pytest.raises(ValidationError):
            CallLease(**base, phase=post, grant_expires_at=NOW_UTC + timedelta(seconds=5))
    for late in (CallPhase.ACTIVE, CallPhase.TERMINATING, CallPhase.ENDED):
        # ...and answer facts are legal once the call is established.
        # Round 33: a call-bearing phase carries media epochs >= 1
        # (the media pair the call leg rides; per-role since round
        # 35); an ENDED lease keeps the epochs it died with.
        CallLease(
            **base,
            phase=late,
            answered_at=NOW_UTC,
            answer_confidence="confirmed",
            agent_media_epoch=1,
            provider_media_epoch=1,
        )
    # Round 31 (SA_swe2_review_1 item 37): the ACQUIRED deadline is
    # REQUIRED (an undialed lease without one would park the line
    # forever) and must sit at-or-after created_at.
    with pytest.raises(ValidationError):
        CallLease(**base)  # ACQUIRED without grant_expires_at
    with pytest.raises(ValidationError):
        CallLease(**base, grant_expires_at=NOW_UTC - timedelta(seconds=1))
    # model_validate path honors the same rules (hydration cannot
    # smuggle a reserved phase either).
    with pytest.raises(ValidationError):
        CallLease.model_validate({**base, "phase": "ringing"})


def test_owner_identity_system_boundary() -> None:
    """Round 30 (SA_carbon_review_19 slice-C item 13): ``system:*``
    is legal ONLY on socket-bound owner fields
    (:class:`CallLease.owner_agent_id` - ``system:fallback_worker``
    legitimately owns leases) and is rejected on every DURABLE owner
    or durable-event owner field."""
    # Socket-bound: the fallback worker owns leases legitimately.
    lease = CallLease(
        **{
            **_lease_base(),
            "owner_agent_id": SYSTEM_FALLBACK_WORKER,
            "grant_expires_at": NOW_UTC + timedelta(seconds=5),
        }
    )
    assert lease.owner_agent_id == SYSTEM_FALLBACK_WORKER
    # Durable carriers reject the same identity.
    with pytest.raises(ValidationError):
        _assignment(mode=AssignmentMode.EXCLUSIVE, owner_agent_id=SYSTEM_FALLBACK_WORKER)
    with pytest.raises(ValidationError):
        AdminLineView(
            line_id=LineId("line-main"),
            caps=(),
            state=LineState.FREE,
            liveness=LivenessState.UNBOUND,
            owner_agent_id=SYSTEM_FALLBACK_WORKER,
        )
    with pytest.raises(ValidationError):
        PendingClaim(
            claim_id=ClaimId("claim-1"),
            line_id=LineId("line-main"),
            requester_agent_id=SYSTEM_FALLBACK_WORKER,
            session_id=SessionId("sess-1"),
            created_at=NOW_UTC,
        )
    with pytest.raises(ValidationError):
        AssignmentRevokedEvent(
            assignment_id=contract.AssignmentId("asg-1"),
            line_id=LineId("line-main"),
            owner_agent_id=SYSTEM_FALLBACK_WORKER,
        )
    with pytest.raises(ValidationError):
        IdleAlertEvent(
            assignment_id=contract.AssignmentId("asg-1"),
            line_id=LineId("line-main"),
            owner_agent_id=SYSTEM_FALLBACK_WORKER,
            idle_since=NOW_UTC,
        )
    # The agent-id grammar bounds the FULL identifier including the
    # "system:" prefix - an oversized suffix can no longer ride an
    # otherwise-well-formed prefix.
    with pytest.raises(ValidationError):
        AgentAudience(agent_id="system:" + "x" * contract._ID_MAX_LEN)


def test_round30_constants_and_models_exported() -> None:
    """Round 30: the new bound constants and models join the public
    contract surface."""
    for name in (
        "MAX_HEARTBEAT_INTERVAL_MS",
        "OUTBOX_HARD_CAP",
        "PendingRing",
        "ProviderRegisterResult",
    ):
        assert name in contract.__all__
    assert MAX_HEARTBEAT_INTERVAL_MS == 86_400_000
    # Round 31 (SA_swe2_review_1 item 16): the hard cap grew from
    # the 5120 formula value to a flat 8192 - the worst-case
    # accounting adds missed-call fanout, queue drain, live-lease
    # teardown and the admin cascade on top of the soft cap.
    assert OUTBOX_HARD_CAP == 8192
    assert OUTBOX_HARD_CAP > OUTBOX_SOFT_CAP


def test_heartbeat_interval_ms_guards() -> None:
    """Round 30 (SA_carbon_review_19 slice-A item 15): the sec -> ms
    converter is fail-closed on non-real, non-finite, non-positive
    and overflowing inputs - a hydration path bypassing the config
    model can never stamp a zero/insane cadence."""
    for bad in (
        True,
        "5.0",
        float("nan"),
        float("inf"),
        float("-inf"),
        0.0,
        -1.0,
        86400.001,
        1e12,
    ):
        with pytest.raises(ValueError):
            heartbeat_interval_ms_from_sec(bad)  # type: ignore[arg-type]
    # The upper boundary itself is still legal.
    assert heartbeat_interval_ms_from_sec(86400.0) == MAX_HEARTBEAT_INTERVAL_MS


def test_pending_ring_and_register_result() -> None:
    """Round 30 (SA_carbon_review_19 slice-A item 11): the register
    ingress returns ProviderRegisterResult - the binding plus the
    rings a state_snapshot re-minted through the ingest_ring path;
    PendingRing carries the kernel-minted token, optional caller_id
    and the observation stamp."""
    ring = PendingRing(
        call_token=CallToken(RedactedString("tok-snap")),
        caller_id=RedactedString(_RAW_NUMBER),
        observed_at=NOW_UTC,
    )
    assert ring.caller_id is not None
    result = ProviderRegisterResult(binding=_binding(), pending_rings=(ring,))
    assert result.pending_rings == (ring,)
    # Bare-binding result: the common no-snapshot case.
    assert ProviderRegisterResult(binding=_binding()).pending_rings == ()
    # Frozen like every contract DTO.
    with pytest.raises(ValidationError):
        ring.call_token = CallToken(RedactedString("other"))  # type: ignore[misc]
    # call_token stays a RedactedString container - masked in logs,
    # raw on the wire dump.
    safe = ring.safe_dump()
    assert "tok-snap" not in repr(safe)
    assert ring.model_dump(mode="json")["call_token"] == "tok-snap"


def test_admin_listing_pagination_signatures() -> None:
    """Round 30 (SA_carbon_review_19 slice-B item 1): every admin
    listing method carries the shared paged window -
    ``offset`` >= 0 default 0 and ``limit`` in 1..MAX_LIST_PAGE
    default MAX_LIST_PAGE - pinned on the ABC itself so a fake or
    implementation cannot silently drop the paging contract."""
    for method_name in (
        "list_pending_claims",
        "list_assignments",
        "list_provider_bindings",
        "list_lines",
        "list_leases",
        "list_tickets",
    ):
        method = getattr(LineAdminKernel, method_name)
        params = inspect.signature(method).parameters
        assert params["offset"].kind is inspect.Parameter.KEYWORD_ONLY
        assert params["offset"].default == 0
        assert params["limit"].default == MAX_LIST_PAGE
        hints = get_type_hints(method, include_extras=True)
        offset_meta = get_args(hints["offset"])[1].metadata
        limit_meta = get_args(hints["limit"])[1].metadata
        assert any(getattr(m, "ge", None) == 0 for m in offset_meta)
        assert any(getattr(m, "ge", None) == 1 for m in limit_meta)
        assert any(getattr(m, "le", None) == MAX_LIST_PAGE for m in limit_meta)


def test_media_endpoint_bounds_all_carriers() -> None:
    """Round 30 (SA_carbon_review_19 slice-C items 11-13): every
    ``*media_endpoint`` field across the contract carries the same
    ``1..MAX_ENDPOINT_LEN`` bound - pinned on the pydantic field
    metadata so a new or weakened bound fails the shape check."""
    carriers: dict[type[contract.ContractModel], tuple[str, ...]] = {
        DialResult: ("media_endpoint",),
        AcceptResult: ("media_endpoint",),
        MediaReattachResult: ("media_endpoint",),
        ProviderMediaReattachResult: ("media_endpoint",),
        ProviderDialCommand: ("media_endpoint",),
        ProviderAnswerCommand: ("media_endpoint",),
        ProviderReattachMediaCommand: ("media_endpoint",),
        MediaCredentials: ("agent_media_endpoint", "provider_media_endpoint"),
    }
    for model, fields in carriers.items():
        for field_name in fields:
            meta = model.__pydantic_fields__[field_name].metadata
            # Round 31 (SA_swe2_review_1 item 31): the length bound
            # now rides ``_CheckedMediaEndpoint`` - the shared
            # validator enforces non-empty + 1..MAX_ENDPOINT_LEN +
            # the unix://|wss:// scheme vocabulary, and the Field
            # keeps the schema-visible max_length.
            assert any(getattr(m, "max_length", None) == MAX_ENDPOINT_LEN for m in meta)
            annotation = model.__pydantic_fields__[field_name].annotation
            assert annotation is str
            # The checked alias must be wired in: empty and
            # scheme-less values are rejected through the validator.
    for bad in ("", "x" * (MAX_ENDPOINT_LEN + 1), "/run/m.sock", "tcp://h:1"):
        with pytest.raises(ValidationError):
            DialResult(
                lease_id=LeaseId("l-1"),
                media_endpoint=bad,
                media_token=MediaToken(RedactedString("mt")),
            )
    for good in ("unix:///run/m.sock", "wss://media.internal:7443/phonebridge"):
        DialResult(
            lease_id=LeaseId("l-1"),
            media_endpoint=good,
            media_token=MediaToken(RedactedString("mt")),
        )


def test_queue_ticket_seq_and_position_bounds() -> None:
    """Round 30 (SA_carbon_review_19 slice-C items 11-13): the
    queue-ordering numbers are bounded - ``seq`` is a monotonic
    counter minted at acquire ingress (``ge=0``) and ``position``
    is a 1-based FIFO rank (``ge=1``)."""
    _ticket("t-0", 0)  # seq=0 is the legal first counter value
    with pytest.raises(ValidationError):
        _ticket("t-neg", -1)
    AcquireQueued(ticket_id=TicketId("t-1"), position=1)
    with pytest.raises(ValidationError):
        AcquireQueued(ticket_id=TicketId("t-1"), position=0)


def test_dtmf_digit_boundaries() -> None:
    """Round 30 (SA_carbon_review_19 slice-C items 11-13):
    ``ProviderSendDtmfCommand.digits`` rides DTMF_PATTERN - the full
    32-digit maximum is legal, one more is not, and characters
    outside the DTMF alphabet are rejected."""
    assert MAX_DTMF_DIGITS == 32
    base: dict[str, Any] = {
        "line_id": LineId("line-main"),
        "assignment_id": contract.AssignmentId("bind-1"),
        "lease_id": LeaseId("l-1"),
    }
    ProviderSendDtmfCommand(**base, digits="1" * MAX_DTMF_DIGITS)
    with pytest.raises(ValidationError):
        ProviderSendDtmfCommand(**base, digits="1" * (MAX_DTMF_DIGITS + 1))
    for bad in ("x", "12e3", "12 3", "12-3"):
        with pytest.raises(ValidationError):
            ProviderSendDtmfCommand(**base, digits=bad)


def test_dtmf_pause_budget_covers_max_workload() -> None:
    """Round 34 (iteration-2 audit item 17): the media-fault
    threshold ``MAX_DTMF_STREAM_PAUSE_SEC`` is the same 30 s bound
    the ``0x05`` DTMF-START pause budget ``DTMF_PAUSE_BUDGET_MS``
    = 30 000 ms documents (api-contract.md media framing) - sized
    so the largest legal ``call.send_dtmf`` workload
    (``MAX_DTMF_DIGITS`` = 32 digits incl. ``,``/``w``/``W`` pause
    chars) fits inside it, and a pause observed beyond it reads as
    a detached role, never silence."""
    assert MAX_DTMF_STREAM_PAUSE_SEC == 30.0
    assert MAX_DTMF_STREAM_PAUSE_SEC * 1000 == 30_000
    assert MAX_DTMF_DIGITS == 32


def test_time_knob_positivity_edges() -> None:
    """Round 30 (SA_carbon_review_19 slice-C items 11-13): the
    time-knob floors are pinned on both models - zero and negative
    values are INVALID_ARGUMENT-class violations, and the documented
    defaults stay put."""
    with pytest.raises(ValidationError):
        LineTimeouts(orphan_grace_sec=0.0)
    with pytest.raises(ValidationError):
        TimeoutConfig(orphan_grace_sec=-0.5)
    # accept_window_sec lives on the durable record: default 30 s,
    # strictly positive, day-capped.
    assert _assignment().accept_window_sec == 30.0
    with pytest.raises(ValidationError):
        _assignment(accept_window_sec=0.0)
    with pytest.raises(ValidationError):
        _assignment(accept_window_sec=-1.0)
    # fallback_accept_ttl_sec: default 10 s on the effective model.
    assert LineTimeouts().fallback_accept_ttl_sec == 10.0
    with pytest.raises(ValidationError):
        LineTimeouts(fallback_accept_ttl_sec=0.0)
    # heartbeat_down_after is ge=1 AND must exceed degraded_after -
    # a zero/negative is unrepresentable, 1 is still a ladder breach
    # against the default degraded=2.
    with pytest.raises(ValidationError):
        LineTimeouts(heartbeat_down_after=0)
    with pytest.raises(ValidationError):
        LineTimeouts(heartbeat_down_after=1)


def test_admin_line_view_provider_meta_owned_and_bounded() -> None:
    """Round 30 (SA_carbon_review_19 slice-C items 11-13):
    ``AdminLineView.provider_meta`` shares the ``_bounded_meta``
    contract - entry/key/value caps enforced and the model owns a
    deep copy of the caller's map."""
    # Round 31 (SA_swe2_review_1 item 35): provider_meta is provider
    # metadata - a UNBOUND row has no provider, so the invariant
    # pins the meta-bearing cases to a live binding.
    view = AdminLineView(
        line_id=LineId("line-main"),
        caps=[Intent.DIAL_OUT],
        state=LineState.FREE,
        liveness=LivenessState.REACHABLE,
        provider_meta={"model": "gt-1"},
    )
    assert view.provider_meta == {"model": "gt-1"}
    base: dict[str, Any] = {
        "line_id": LineId("line-main"),
        "caps": [Intent.DIAL_OUT],
        "state": LineState.FREE,
        "liveness": LivenessState.REACHABLE,
    }
    with pytest.raises(ValidationError):
        AdminLineView(**base, provider_meta={"k": "v" * (MAX_PROVIDER_META_VALUE_LEN + 1)})
    with pytest.raises(ValidationError):
        AdminLineView(
            **base,
            provider_meta={f"k{i}": "v" for i in range(MAX_PROVIDER_META_ENTRIES + 1)},
        )
    with pytest.raises(ValidationError):
        AdminLineView(**base, provider_meta={"k" * (MAX_PROVIDER_META_KEY_LEN + 1): "v"})
    # Round 31 (SA_swe2_review_1 item 35): provider_meta on a
    # liveness=UNBOUND row is a derived-state bug - the model is
    # authoritative and rejects the inconsistent pair.
    with pytest.raises(ValidationError):
        AdminLineView(
            **{**base, "liveness": LivenessState.UNBOUND, "caps": ()},
            provider_meta={"m": "v"},
        )
    unbound_ok = AdminLineView(**{**base, "liveness": LivenessState.UNBOUND, "caps": ()})
    assert unbound_ok.provider_meta == {}
    # Owned copy (round 30 deep-copy hardening): mutating the source
    # map - including a NESTED structure passed through - cannot
    # corrupt the stored row.
    source = {"model": "gt-1"}
    view2 = AdminLineView(**base, provider_meta=source)
    source["model"] = "tampered"
    assert view2.provider_meta == {"model": "gt-1"}


def test_apply_patch_error_detail_join_and_truncation() -> None:
    """Round 30 (SA_carbon_review_19 slice-C item 10): the
    apply_patch failure detail joins ALL field errors with ``"; "``
    in ``field: msg`` form and is truncated to ``MAX_DETAIL_LEN``
    with an ellipsis tail - a many-field failure can never overflow
    the bounded ``ErrorData.detail`` or hide behind a first-error
    projection."""
    base = LineTimeouts()

    class _FaultyPatch:
        """Duck-typed patch injecting several invalid merged values
        (TimeoutConfig itself could never produce this - the point
        is the JOIN/TRUNCATION contract on the failure path)."""

        def __init__(self, values: dict[str, Any]) -> None:
            self._values = values

        def model_dump(self, *, exclude_none: bool = True) -> dict[str, Any]:
            return dict(self._values)

    with pytest.raises(PhonebridgeError) as excinfo:
        base.apply_patch(
            _FaultyPatch({"grant_ttl_sec": -1.0, "orphan_grace_sec": -2.0})  # type: ignore[arg-type]
        )
    assert excinfo.value.code is ErrorCode.INVALID_ARGUMENT
    detail = excinfo.value.data.detail or ""
    assert "; " in detail
    assert "grant_ttl_sec" in detail and "orphan_grace_sec" in detail
    # Enough simultaneous failures overflow MAX_DETAIL_LEN -> the
    # joined text is truncated to the cap with a "..." tail.
    many = {
        "grant_ttl_sec": -1.0,
        "orphan_grace_sec": -1.0,
        "heartbeat_interval_sec": -1.0,
        "heartbeat_degraded_after": 0,
        "heartbeat_down_after": 0,
        "fallback_accept_ttl_sec": -1.0,
        "ring_drain_ttl_sec": -1.0,
        "max_call_duration_sec": -1.0,
    }
    with pytest.raises(PhonebridgeError) as excinfo2:
        base.apply_patch(_FaultyPatch(many))  # type: ignore[arg-type]
    detail2 = excinfo2.value.data.detail or ""
    assert len(detail2) <= MAX_DETAIL_LEN
    assert detail2.endswith("...")


def test_redacted_string_bool_and_type_edges() -> None:
    """Round 30 (SA_carbon_review_19 slice-C item 20): the container's
    truthiness mirrors the RAW value (an empty secret is falsy, a
    non-empty one truthy) and non-str construction fails closed -
    an int/None/bytes can never become a token."""
    assert bool(RedactedString("tok")) is True
    assert bool(RedactedString("")) is False
    for bad in (123, None, b"tok", 4.2):
        with pytest.raises(TypeError):
            RedactedString(bad)  # type: ignore[arg-type]


def test_line_state_changed_event_no_self_loop() -> None:
    """Round 30 (SA_carbon_review_19 slice-C item 21): the
    occupancy-axis event shares the health-axis rule - a
    ``X -> X`` "transition" is a defect, not a notification, on
    every LineState; legal transitions construct cleanly."""
    for state in LineState:
        with pytest.raises(ValidationError):
            LineStateChangedEvent(line_id=LineId("line-main"), old_state=state, new_state=state)
    ev = LineStateChangedEvent(
        line_id=LineId("line-main"),
        old_state=LineState.FREE,
        new_state=LineState.BUSY,
        reason=CallEndReason.EXTERNAL_CALL,
    )
    assert ev.reason is CallEndReason.EXTERNAL_CALL


def test_caller_id_bounds_on_ring_and_fallback() -> None:
    """Round 30 (SA_carbon_review_19 slice-C item 12):
    ``caller_id`` carries the same ``MAX_CALLER_ID_LEN`` cap on the
    ring-ingress model and the fallback event - empty and overlong
    values are rejected on both carriers."""
    assert MAX_CALLER_ID_LEN == 64
    overlong = "1" * (MAX_CALLER_ID_LEN + 1)
    for bad in (overlong, ""):
        with pytest.raises(ValidationError):
            IncomingRingIngress(
                line_id=LineId("line-main"),
                assignment_id=contract.AssignmentId("bind-1"),
                observed_at=NOW_UTC,
                caller_id=RedactedString(bad),
            )
        with pytest.raises(ValidationError):
            FallbackTriggeredEvent(
                line_id=LineId("line-main"),
                call_token=CallToken(RedactedString("tok-1")),
                caller_id=RedactedString(bad),
                # Round 34 (iteration-2 audit item 25): a WORKER
                # policy is required here - a core policy is itself
                # unconstructible on this model, so NoPickupFallback
                # made the caller_id check a false-positive guard
                # (ValidationError fired on the policy regardless).
                policy=RecordAndTelegramFallback(channel="ops-alerts", announcement_ref="rec-note"),
                expires_at=NOW_UTC,
            )
    # Boundary itself is legal.
    at_cap = RedactedString("1" * MAX_CALLER_ID_LEN)
    ring = IncomingRingIngress(
        line_id=LineId("line-main"),
        assignment_id=contract.AssignmentId("bind-1"),
        observed_at=NOW_UTC,
        caller_id=at_cap,
    )
    assert ring.caller_id is not None
    assert ring.caller_id.expose() == "1" * MAX_CALLER_ID_LEN


def test_dedup_ring_documented_semantics() -> None:
    """Round 30 (SA_carbon_review_19 slice-A item 12): the closed-
    resource rings are documented as INSERTION-ORDER FIFO tombstone
    rings on the kernel - a duplicate hit marks the entry, it does
    NOT refresh eviction position. The behavioral pin lives on the
    implementation tests; here the contract surface pins that the
    documented model exists and stays bounded."""
    assert MAX_CLOSED_ASSIGNMENTS == 1024
    assert MAX_CLOSED_LEASES == 1024
    doc = LineManager.drain_events.__doc__ or ""
    assert "drain" in doc


# ---------------------------------------------------------------------------
# Round 31 (architect review - SA_swe2_review_1): batch-1 pins.
# ---------------------------------------------------------------------------


def _doc_rpc_table(section_header: str, name_re: str) -> dict[str, dict[str, bool]]:
    """Parse a ``| `method` | `{params}` |`` wire table inside the
    named api-contract.md section: wire method name ->
    {param: is_optional}. Same optionality rule as
    :func:`_doc_line_command_table` - the trailing ``?`` is kept.
    ``name[]`` plural markers are normalized to the bare param
    name."""
    text = API_CONTRACT_MD.read_text(encoding="utf-8")
    section = text.split(section_header, 1)[1]
    section = section.split("\n## ", 1)[0]
    table: dict[str, dict[str, bool]] = {}
    for m in re.finditer(rf"^\|\s*`({name_re})`\s*\|\s*`\{{([^}}]*)\}}`", section, flags=re.M):
        params: dict[str, bool] = {}
        for p in m.group(2).split(","):
            p = p.strip()
            if p:
                params[p.rstrip("?").removesuffix("[]")] = p.endswith("?")
        table[m.group(1)] = params
    return table


def _signature_params(fn: Callable[..., Any]) -> dict[str, bool]:
    """{param: has_default} for a kernel-interface method signature
    (``self`` excluded) - has_default mirrors the wire-table ``?``
    optionality marker."""
    sig = inspect.signature(fn)
    return {
        name: p.default is not inspect.Parameter.empty
        for name, p in sig.parameters.items()
        if name != "self"
    }


def test_provider_wire_method_drift() -> None:
    """Round 31 (SA_swe2_review_1 item 36): the ``provider.*`` wire
    table must not drift from the provider-fact models and the
    ingest signatures - the same param-name/optionality parity the
    ``line.*`` drift test pins on the down-channel.

    Exclusions are explicit and reviewed: ``token`` is consumed by
    the auth layer before the fact reaches the kernel; ``channel_seq``
    and ``reregister`` are adapter-distilled fields that never ride the
    wire as named params. ``provider_call_id`` rides the shared
    ``provider.call_state`` params cell from the round-31 fix round
    (PR #78 warning 2) - the same wire row distills into
    ``IncomingRingIngress`` on ``state:"ringing"`` and into
    ``ProviderCallStateUpdate`` otherwise, so the test pins BOTH
    models against the one cell."""
    table = _doc_rpc_table("## Manager namespace", r"provider\.[a-z_]+")
    expected_rows = {
        "provider.register",
        "provider.reregister",
        "provider.ping",
        "provider.capability_update",
        "provider.call_state",
        "provider.audio_ready",
        "provider.audio_lost",
        "provider.reattach_media",
    }
    assert set(table) == expected_rows

    # provider.reregister now carries an explicit params cell
    # (round 32, SA_swe2_review_2 item 23 - the earlier "same as
    # register" prose cell could never be drift-checked); the
    # distilled flag pair stays in place.
    reg_fields = ProviderRegistration.__pydantic_fields__
    assert not reg_fields["reregister"].is_required()
    assert not reg_fields["state_snapshot"].is_required()

    model_map: dict[str, tuple[type[contract.ContractModel], set[str]]] = {
        # wire method -> (fact model, model fields NOT on the wire
        # for that method: adapter-distilled or batch-2 pending)
        "provider.register": (
            ProviderRegistration,
            {"channel_seq", "reregister", "state_snapshot"},
        ),
        "provider.reregister": (ProviderRegistration, {"channel_seq", "reregister"}),
        "provider.ping": (ProviderLiveness, {"channel_seq"}),
        "provider.capability_update": (ProviderCapabilityUpdate, set()),
        "provider.call_state": (ProviderCallStateUpdate, set()),
        "provider.reattach_media": (ProviderMediaReattachRequest, set()),
    }
    # Adapter-NORMALIZED params: the wire marks them optional but
    # the kernel model requires a value - the adapter stamps kernel
    # receipt time when the provider omits the advisory stamp.
    adapter_normalized: dict[str, set[str]] = {
        "provider.ping": {"checked_at"},
        "provider.call_state": {"observed_at"},
    }
    for wire, (model, internal) in model_map.items():
        fields = model.__pydantic_fields__
        model_params = set(fields) - internal
        doc_params = table[wire]
        assert model_params == set(doc_params) - {"token"}, (
            f"{model.__name__} drift vs {wire}: "
            f"missing={set(doc_params) - model_params - {'token'}}, "
            f"extra={model_params - set(doc_params)}"
        )
        model_optional = {f for f in model_params if not fields[f].is_required()}
        doc_optional = {
            p for p, optional in doc_params.items() if optional
        } - adapter_normalized.get(wire, set())
        assert model_optional == doc_optional, (
            f"{model.__name__} optionality drift vs {wire}: "
            f"doc-optional-only={doc_optional - model_optional}, "
            f"model-optional-only={model_optional - doc_optional}"
        )

    # The shared ``provider.call_state`` cell also carries the ring
    # ingress (round-31 fix round, PR #78 warning 2): every
    # ``IncomingRingIngress`` field MUST ride the same params cell,
    # with the provider-supplied optionals marked ``?``.
    ring_fields = set(IncomingRingIngress.__pydantic_fields__)
    assert ring_fields <= set(table["provider.call_state"]), (
        "IncomingRingIngress drift vs provider.call_state params cell: "
        f"missing={ring_fields - set(table['provider.call_state'])}"
    )
    doc_optional_cs = {p for p, optional in table["provider.call_state"].items() if optional}
    assert {"caller_id", "provider_call_id"} <= doc_optional_cs

    # audio_ready / audio_lost carry no fact model - they ride the
    # ingest signatures; compare names AND optionality (audio_lost's
    # ``lease_id?`` is the line-scoped loss case).
    sig_map = {
        "provider.audio_ready": contract.LineManager.ingest_audio_ready,
        "provider.audio_lost": contract.LineManager.ingest_audio_lost,
    }
    for wire, fn in sig_map.items():
        sig_params = _signature_params(fn)
        doc_params = table[wire]
        assert set(sig_params) == set(doc_params), (
            f"{fn.__name__} drift vs {wire}: "
            f"missing={set(doc_params) - set(sig_params)}, "
            f"extra={set(sig_params) - set(doc_params)}"
        )
        doc_optional = {p for p, optional in doc_params.items() if optional}
        sig_optional = {p for p, has_default in sig_params.items() if has_default}
        assert doc_optional == sig_optional, f"{fn.__name__} optionality drift vs {wire}"


def test_agent_call_wire_method_drift() -> None:
    """Round 31 (SA_swe2_review_1 item 36): the ``agent.*``/``call.*``
    wire table must not drift from the LineManager agent surface -
    every wire param resolves to a kernel signature param with the
    same optionality, barring the explicit auth/adapter-owned set.

    ``token`` (register_intent) and ``idempotency_key`` on
    dial/accept/claim_exclusive are adapter-consumed: the key rides
    the adapter-side dedup store whose partitioning and replay
    semantics batch 2 pins in the wire doc (item 13); the kernel
    dedups only ``call.acquire``'s key today."""
    table = _doc_rpc_table("## APICallService —", r"(?:agent|call)\.[a-z_]+")
    adapter_owned: dict[str, set[str]] = {
        "agent.register_intent": {"token"},
        "call.dial": {"idempotency_key"},
        "call.accept": {"idempotency_key"},
        "call.claim_exclusive": {"idempotency_key"},
        # Round 33: ``cursor`` is adapter-owned pagination input -
        # the kernel signature keeps offset/limit only, the adapter
        # translates the opaque token into the kernel window.
        "call.get_lines": {"cursor"},
    }
    method_map: dict[str, Callable[..., Any]] = {
        "agent.register_intent": contract.LineManager.register_intent,
        "call.acquire": contract.LineManager.acquire,
        "call.cancel_acquire": contract.LineManager.cancel_acquire,
        "call.dial": contract.LineManager.dial,
        "call.accept": contract.LineManager.accept,
        "call.hangup": contract.LineManager.hangup,
        "call.send_dtmf": contract.LineManager.send_dtmf,
        "call.reattach_media": contract.LineManager.reattach_media,
        "call.claim_exclusive": contract.LineManager.claim_exclusive,
        "call.get_lines": contract.LineManager.get_lines,
    }
    # call.release splits into the two kernel entry points by its
    # XOR target - pin the union by name; optionality is a wire-only
    # concept here (each kernel method takes its target positionally).
    release_union = set(_signature_params(contract.LineManager.release)) | set(
        _signature_params(contract.LineManager.release_assignment)
    )
    assert release_union == set(table["call.release"]), (
        "call.release drift: "
        f"missing={set(table['call.release']) - release_union}, "
        f"extra={release_union - set(table['call.release'])}"
    )
    assert set(table) == set(method_map) | {"call.release"}

    for wire, fn in method_map.items():
        exempt = adapter_owned.get(wire, set())
        sig_params = _signature_params(fn)
        doc_params = table[wire]
        assert set(sig_params) == set(doc_params) - exempt, (
            f"{fn.__name__} drift vs {wire}: "
            f"missing={set(doc_params) - exempt - set(sig_params)}, "
            f"extra={set(sig_params) - set(doc_params)}"
        )
        doc_optional = {p for p, optional in doc_params.items() if optional}
        sig_optional = {p for p, has_default in sig_params.items() if has_default}
        assert doc_optional - exempt == sig_optional, (
            f"{fn.__name__} optionality drift vs {wire}: "
            f"doc-optional-only={(doc_optional - exempt) - sig_optional}, "
            f"kernel-optional-only={sig_optional - doc_optional}"
        )


def test_admin_wire_method_drift() -> None:
    """Round 31 (SA_swe2_review_1 item 36): the ``admin.*`` wire table
    must not drift from the LineAdminKernel surface - every wire
    method resolves to a kernel entry point (or an explicitly
    adapter-owned method) and every param name/optionality matches.

    Round 32 (SA_swe2_review_2 item 23) lifted the round-31
    pending-param exemption: the hardware-transport params now ride
    the wire-table cells explicitly, so the parity check covers them
    like every other param."""
    table = _doc_rpc_table("## Admin surface", r"admin\.[a-z_]+")

    method_map: dict[str, Callable[..., Any]] = {
        "admin.provision_line": contract.LineAdminKernel.provision_line,
        "admin.configure_line": contract.LineAdminKernel.configure_line,
        "admin.remove_line": contract.LineAdminKernel.remove_line,
        "admin.assign_line": contract.LineAdminKernel.assign_line,
        "admin.approve_claim": contract.LineAdminKernel.approve_claim,
        "admin.reject_claim": contract.LineAdminKernel.reject_claim,
        "admin.revoke_assignment": contract.LineAdminKernel.revoke_assignment,
        "admin.get_line": contract.LineAdminKernel.get_line,
        "admin.list_lines": contract.LineAdminKernel.list_lines,
        "admin.list_leases": contract.LineAdminKernel.list_leases,
        "admin.list_tickets": contract.LineAdminKernel.list_tickets,
        "admin.list_pending_claims": contract.LineAdminKernel.list_pending_claims,
        "admin.list_assignments": contract.LineAdminKernel.list_assignments,
        "admin.list_provider_bindings": contract.LineAdminKernel.list_provider_bindings,
    }
    # Adapter-owned admin calls never reach the kernel: the handshake
    # auth, key management, and the HCI pair window (its params ride
    # the typed PairWindowRequest model instead).
    adapter_owned = {
        "admin.auth",
        "admin.pair_window",
        "admin.issue_key",
        "admin.revoke_key",
        "admin.list_keys",
    }
    assert set(table) == set(method_map) | adapter_owned

    # ``admin.list_keys`` (round-31 fix round, PR #78 warning 3) is a
    # normative table row: pin the redacted metadata shape so the row
    # can never drift into returning key material. Round 32 adds the
    # shared ``offset?``/``limit?`` window params.
    section = API_CONTRACT_MD.read_text(encoding="utf-8").split("## Admin surface", 1)[1]
    list_keys_row = next(ln for ln in section.splitlines() if ln.startswith("| `admin.list_keys`"))
    for field in ("key_id", "scope", "subject_id", "created_at", "last_used_at?", "revoked"):
        assert field in list_keys_row, f"admin.list_keys row misses {field}"
    assert table["admin.list_keys"] == {
        "subject_id": True,
        "offset": True,
        "limit": True,
        # Round 33: adapter-owned cursor input joins the shared
        # pagination window like every other listing call.
        "cursor": True,
    }

    # Adapter-owned param columns stay drift-checked too (round 32,
    # SA_swe2_review_2 item 23): no kernel signature exists for them,
    # so pin the exact wire param sets.
    assert table["admin.auth"] == {"api_key": False}
    assert table["admin.issue_key"] == {
        "agent_id": True,
        "scopes": False,
        "line_id": True,
        "expires_at": True,
        # Round 35 (iteration-3 audit item 21): the admin-axis
        # subject parameter - REQUIRED on an admin-scope key so
        # admin.list_keys can report a non-empty bound subject.
        "subject_id": True,
    }
    assert table["admin.revoke_key"] == {"key_id": False}

    pair_req = set(PairWindowRequest.__pydantic_fields__)
    assert pair_req == set(table["admin.pair_window"]), (
        "admin.pair_window drift vs PairWindowRequest: "
        f"missing={set(table['admin.pair_window']) - pair_req}, "
        f"extra={pair_req - set(table['admin.pair_window'])}"
    )

    # Round 33: ``cursor`` is adapter-owned pagination input on the
    # listing calls - the kernel signatures keep offset/limit only
    # while the wire accepts the opaque token INSTEAD of offset.
    adapter_owned_params: dict[str, set[str]] = {
        wire: {"cursor"}
        for wire in (
            "admin.list_lines",
            "admin.list_leases",
            "admin.list_tickets",
            "admin.list_pending_claims",
            "admin.list_assignments",
            "admin.list_provider_bindings",
        )
    }
    for wire, fn in method_map.items():
        exempt = adapter_owned_params.get(wire, set())
        sig_params = _signature_params(fn)
        doc_params = table[wire]
        assert set(sig_params) == set(doc_params) - exempt, (
            f"{fn.__name__} drift vs {wire}: "
            f"missing={set(doc_params) - exempt - set(sig_params)}, "
            f"extra={set(sig_params) - set(doc_params)}"
        )
        doc_optional = {p for p, optional in doc_params.items() if optional}
        sig_optional = {p for p, has_default in sig_params.items() if has_default}
        assert doc_optional - exempt == sig_optional, (
            f"{fn.__name__} optionality drift vs {wire}: "
            f"doc-optional-only={(doc_optional - exempt) - sig_optional}, "
            f"kernel-optional-only={sig_optional - doc_optional}"
        )


def test_secure_compare_auth_helper() -> None:
    """Round 31 (SA_swe2_review_1 item 30): ``secure_compare`` is the
    dedicated AUTH-decision helper - constant-time, length-tolerant,
    and fail-closed on non-encodable input. Container
    ``__eq__``/``__hash__`` on RedactedString stay model
    conveniences, never an auth verdict."""
    assert secure_compare(RedactedString("tok-1"), RedactedString("tok-1"))
    assert not secure_compare(RedactedString("tok-1"), RedactedString("tok-2"))
    assert not secure_compare(RedactedString("tok-1"), RedactedString(""))
    # Length-mismatched prefixes must fail too (compare_digest is
    # length-tolerant, not prefix-tolerant).
    assert not secure_compare(RedactedString("tok-12"), RedactedString("tok-1"))
    # Unpaired surrogates can never reach hmac - fail closed.
    assert not secure_compare(RedactedString("\ud800"), RedactedString("x"))
    assert not secure_compare(RedactedString("x"), RedactedString("\ud800"))


def test_media_overflow_event_shape() -> None:
    """Round 31 (SA_swe2_review_1 item 8): the reserved
    ``event.media_overflow`` payload is exactly ``{lease_id,
    line_id}`` - bounded identifiers only, no counters that could
    invite unbounded per-frame emission bookkeeping."""
    payload = MediaOverflowEvent(lease_id=LeaseId("l-1"), line_id=LineId("line-main"))
    assert set(MediaOverflowEvent.__pydantic_fields__) == {"lease_id", "line_id"}
    assert payload.lease_id == "l-1"


def test_closed_ticket_ring_and_ring_dedup_bounds() -> None:
    """Round 31 (SA_swe2_review_1 items 20/24): the tombstone rings
    and the best-effort ring-dedup horizon are bounded, exported
    contract constants - a drift in either unbounds kernel memory
    or silently re-keys ring dedup."""
    assert MAX_CLOSED_TICKETS == 1024
    assert MAX_CLOSED_TICKETS <= contract.MAX_QUEUE_TICKETS_TOTAL
    assert RING_DEDUP_MIN_GAP_SEC == 30.0
    assert RING_DEDUP_MIN_GAP_SEC > 0


# ---------------------------------------------------------------------------
# Round 33 (iteration-1 audit exchange, 0.0.28): provider command
# acknowledgement facts, answer-pending rings, wake audience, durable
# provider-stamp watermarks, positive-evidence correlation, cursor
# pagination pins, media epochs, and the misc hardening fixes
# (E.164 ASCII digits, region membership, RedactedString eq/hash).
# ---------------------------------------------------------------------------


def _dial_ack(**overrides: Any) -> contract.ProviderCommandAck:
    kwargs: dict[str, Any] = {
        "line_id": LineId("line-main"),
        "assignment_id": contract.AssignmentId("bind-1"),
        "command": "dial",
        "lease_id": LeaseId("l-1"),
        "accepted": True,
    }
    kwargs.update(overrides)
    return contract.ProviderCommandAck(**kwargs)


def test_round33_command_ack_kind_vocabulary() -> None:
    """GWT: ``CommandAckKind`` is exactly ``"dial" | "answer"`` - the
    two handshakes that carry a bounded ``{accepted}`` reply. A third
    spelling is rejected at the model boundary."""
    assert "CommandAckKind" in contract.__all__
    assert set(get_args(contract.CommandAckKind)) == {"dial", "answer"}
    _dial_ack(command="dial")
    _dial_ack(command="answer", call_token=CallToken(RedactedString("tok-1")))
    with pytest.raises(ValidationError):
        _dial_ack(command="hangup")


def test_round33_provider_command_ack_shape_coherence() -> None:
    """GWT: the distilled acknowledgement obeys its command-specific
    shape rules (PR #82 review) - ``line.dial`` acks are
    ``{accepted, provider_call_id?, reason?}`` while ``line.answer``
    acks are ``{accepted, reason?}`` only: ``provider_call_id`` is
    legal ONLY on an accepted dial (the answer path has no
    provider-call-handle because ring correlation stays
    ``call_token``/``line_id``), ``call_token`` is answer-only
    (the ring-scoped reject needs it), a refused command mints no
    provider_call_id, and an accepted reply carries no reason."""
    # Legal shapes: accepted dial (+optional provider_call_id),
    # accepted/refused answer, refused dial with bounded reason.
    _dial_ack()
    _dial_ack(provider_call_id="/ofono/voicecall01")
    _dial_ack(accepted=False, reason="modem busy")
    _dial_ack(command="answer", call_token=CallToken(RedactedString("tok-1")))
    _dial_ack(
        command="answer",
        call_token=CallToken(RedactedString("tok-1")),
        accepted=False,
        reason="no media path",
    )
    # call_token is ring-scoped - never on a dial ack.
    with pytest.raises(ValidationError):
        _dial_ack(call_token=CallToken(RedactedString("tok-1")))
    # An answer ack MUST echo the ring token (the reject needs it).
    with pytest.raises(ValidationError):
        _dial_ack(command="answer")
    # reason is the refusal detail - never on an accepted reply.
    with pytest.raises(ValidationError):
        _dial_ack(reason="but it worked")
    # A refused command mints no provider handle.
    with pytest.raises(ValidationError):
        _dial_ack(accepted=False, provider_call_id="/ofono/voicecall01")
    # provider_call_id is DIAL-ONLY (PR #82 review): an answer ack
    # carrying any provider handle is a contract violation -
    # accepted AND refused alike.
    with pytest.raises(ValidationError):
        _dial_ack(
            command="answer",
            call_token=CallToken(RedactedString("tok-1")),
            provider_call_id="/ofono/voicecall01",
        )
    with pytest.raises(ValidationError):
        _dial_ack(
            command="answer",
            call_token=CallToken(RedactedString("tok-1")),
            accepted=False,
            provider_call_id="/ofono/voicecall01",
        )
    # reason rides the shared MAX_REASON_LEN bound.
    _dial_ack(accepted=False, reason="x" * MAX_REASON_LEN)
    with pytest.raises(ValidationError):
        _dial_ack(accepted=False, reason="x" * (MAX_REASON_LEN + 1))


def test_round33_ingest_command_ack_signature_pinned() -> None:
    """GWT drift pin: ``LineManager.ingest_command_ack`` is an
    abstract coroutine taking ONE ``ProviderCommandAck`` - the typed
    kernel-fact ingress; adapters distill, never pass raw frames."""
    method = LineManager.ingest_command_ack
    assert getattr(method, "__isabstractmethod__", False)
    assert inspect.iscoroutinefunction(method)
    params = inspect.signature(method).parameters
    assert list(params) == ["self", "ack"]
    hints = get_type_hints(method)
    assert hints["ack"] is contract.ProviderCommandAck
    assert hints["return"] is type(None)


def test_round33_call_lease_pending_command_coherence() -> None:
    """GWT: ``pending_command``/``provider_command_ack_deadline_at``
    are an atomic pair with phase coherence - the marker SURVIVES
    the forward phase advances a provider fact may report before the
    ack (round 34, iteration-2 audit item 1): ``"dial"`` rides
    DIALING/ALERTING/ACTIVE, ``"answer"`` rides ANSWERING/ACTIVE, and
    the deadline is armed exactly while the marker is set."""
    deadline = NOW_UTC + timedelta(seconds=2)
    dial_pending = CallLease(
        **_lease_base(),
        phase=CallPhase.DIALING,
        pending_command="dial",
        provider_command_ack_deadline_at=deadline,
        agent_media_epoch=1,
        provider_media_epoch=1,
    )
    assert dial_pending.pending_command == "dial"
    answer_pending = CallLease(
        **_lease_base(),
        phase=CallPhase.ANSWERING,
        pending_command="answer",
        provider_command_ack_deadline_at=deadline,
        agent_media_epoch=1,
        provider_media_epoch=1,
    )
    assert answer_pending.pending_command == "answer"
    # Phase coherence (round 34 widened window): a qualified
    # alerting/active fact may legitimately arrive BEFORE the
    # command acknowledgement - the handshake stays armed so the
    # ack can still record provider_call_id; the marker dies only
    # with the ack or the teardown.
    for phase in (CallPhase.ALERTING, CallPhase.ACTIVE):
        still_pending = CallLease(
            **_lease_base(),
            phase=phase,
            pending_command="dial",
            provider_command_ack_deadline_at=deadline,
            agent_media_epoch=1,
            provider_media_epoch=1,
        )
        assert still_pending.pending_command == "dial"
    assert (
        CallLease(
            **_lease_base(),
            phase=CallPhase.ACTIVE,
            pending_command="answer",
            provider_command_ack_deadline_at=deadline,
            agent_media_epoch=1,
            provider_media_epoch=1,
        ).pending_command
        == "answer"
    )
    # But the marker can never ride a phase the handshake cannot
    # occupy: ACQUIRED (no command was ever queued), TERMINATING and
    # ENDED (teardown clears the pair atomically).
    for bad_phase, kind in (
        (CallPhase.TERMINATING, "dial"),
        (CallPhase.TERMINATING, "answer"),
        (CallPhase.ENDED, "dial"),
        (CallPhase.ENDED, "answer"),
    ):
        with pytest.raises(ValidationError):
            CallLease(
                **_lease_base(),
                phase=bad_phase,
                pending_command=kind,
                provider_command_ack_deadline_at=deadline,
                agent_media_epoch=1,
                provider_media_epoch=1,
            )
    # ACQUIRED + pending marker: epoch 0 and the live grant deadline
    # are set so ONLY the pending_command phase rule can fire (an
    # epoch-1 record would raise on the unrelated media rule).
    with pytest.raises(ValidationError):
        CallLease(
            **_lease_base(),
            phase=CallPhase.ACQUIRED,
            pending_command="dial",
            provider_command_ack_deadline_at=deadline,
            grant_expires_at=NOW_UTC + timedelta(seconds=5),
            agent_media_epoch=0,
            provider_media_epoch=0,
        )
    # The marker and its deadline are set/cleared TOGETHER - a
    # deadline with nothing pending is a phantom tick entry, a
    # pending command with no deadline could wedge forever.
    with pytest.raises(ValidationError):
        CallLease(
            **_lease_base(),
            phase=CallPhase.DIALING,
            pending_command="dial",
            agent_media_epoch=1,
            provider_media_epoch=1,
        )
    with pytest.raises(ValidationError):
        CallLease(
            **_lease_base(),
            phase=CallPhase.DIALING,
            provider_command_ack_deadline_at=deadline,
            agent_media_epoch=1,
            provider_media_epoch=1,
        )
    # ...and a deadline can never precede the lease it arms on.
    with pytest.raises(ValidationError):
        CallLease(
            **_lease_base(),
            phase=CallPhase.DIALING,
            pending_command="dial",
            provider_command_ack_deadline_at=NOW_UTC - timedelta(seconds=1),
            agent_media_epoch=1,
            provider_media_epoch=1,
        )


def test_round33_call_lease_evidence_and_media_epoch() -> None:
    """GWT: ``provider_call_id``/media-epoch evidence rules -
    an ACQUIRED (pre-dial) lease carries neither, every call-bearing
    phase owns a media pair (both role epochs >= 1 - per-role since
    round 35, iteration-3 audit item 1), and ENDED keeps the epochs
    it died with."""
    # ACQUIRED: no provider handle, epoch 0.
    acquired = CallLease(**_lease_base(), grant_expires_at=NOW_UTC + timedelta(seconds=5))
    assert acquired.agent_media_epoch == 0 and acquired.provider_media_epoch == 0
    assert acquired.provider_call_id is None
    with pytest.raises(ValidationError):
        CallLease(
            **_lease_base(),
            grant_expires_at=NOW_UTC + timedelta(seconds=5),
            provider_call_id="/ofono/voicecall01",
        )
    with pytest.raises(ValidationError):
        CallLease(
            **_lease_base(),
            grant_expires_at=NOW_UTC + timedelta(seconds=5),
            agent_media_epoch=1,
            provider_media_epoch=1,
        )
    # Call-bearing phases require the pair epoch.
    for phase in (
        CallPhase.DIALING,
        CallPhase.ANSWERING,
        CallPhase.ALERTING,
        CallPhase.ACTIVE,
        CallPhase.TERMINATING,
    ):
        with pytest.raises(ValidationError):
            CallLease(**_lease_base(), phase=phase)
        CallLease(**_lease_base(), phase=phase, agent_media_epoch=1, provider_media_epoch=1)
    # ENDED keeps whatever epoch it died with - both 0 (released
    # pre-dial) and a live epoch are legal.
    CallLease(**_lease_base(), phase=CallPhase.ENDED)
    CallLease(**_lease_base(), phase=CallPhase.ENDED, agent_media_epoch=3, provider_media_epoch=3)
    # A recorded provider handle rides a call-bearing lease as
    # durable correlation evidence.
    lease = CallLease(
        **_lease_base(),
        phase=CallPhase.ACTIVE,
        agent_media_epoch=1,
        provider_media_epoch=1,
        provider_call_id="/ofono/voicecall01",
    )
    assert lease.provider_call_id == "/ofono/voicecall01"
    # Negative epochs are impossible.
    with pytest.raises(ValidationError):
        CallLease(
            **_lease_base(),
            phase=CallPhase.ACTIVE,
            agent_media_epoch=-1,
            provider_media_epoch=-1,
        )


def test_round33_wake_audience_scope() -> None:
    """GWT: ``WakeAudience`` is the zero-live-member fan-out marker -
    legal ONLY on the caller-PII events ``incoming_call``/
    ``missed_call`` (the wake-hook envelope), never on
    session-scoped or broadcast-shaped events."""
    assert "WakeAudience" in contract.__all__
    wake = contract.WakeAudience()
    assert wake.scope == "wake"
    # It is a member of the audience union - an EventEnvelope accepts
    # it through the discriminated EventAudience annotation. On
    # ``incoming_call`` the wake envelope carries ONLY the tokenless
    # ``IncomingCallWakeEvent`` projection (PR #82 review).
    EventEnvelope[IncomingCallWakeEvent](
        name=EventName.INCOMING_CALL,
        audience=wake,
        emitted_at=NOW_UTC,
        payload=IncomingCallWakeEvent(
            line_id=LineId("line-main"),
            # Round 34 (iteration-2 audit item 16): the wake
            # caller_id is the MASKED projection string, never a
            # RedactedString - a raw-bearing container would leak
            # PII on the unauthenticated hook leg.
            caller_id="+373****45",
            expires_at=NOW_UTC,
        ),
    )
    # On ``missed_call`` the wake envelope carries ONLY the
    # tokenless ``MissedCallWakeEvent`` projection (round 35,
    # iteration-3 audit item 7) - the session-scoped
    # ``MissedCallEvent`` shape is rejected on a wake audience.
    EventEnvelope[MissedCallWakeEvent](
        name=EventName.MISSED_CALL,
        audience=wake,
        emitted_at=NOW_UTC,
        payload=MissedCallWakeEvent(
            line_id=LineId("line-main"),
            caller_id="+373****45",
            reason=CallEndReason.NO_ANSWER,
        ),
    )
    with pytest.raises(ValidationError):
        EventEnvelope[MissedCallEvent](
            name=EventName.MISSED_CALL,
            audience=wake,
            emitted_at=NOW_UTC,
            payload=MissedCallEvent(line_id=LineId("line-main"), reason=CallEndReason.NO_ANSWER),
        )
    # ...but a wake envelope can never carry a session lease fact or
    # a broadcast-shaped line event.
    with pytest.raises(ValidationError):
        EventEnvelope[CallStateChangedEvent](
            name=EventName.CALL_STATE_CHANGED,
            audience=wake,
            emitted_at=NOW_UTC,
            payload=CallStateChangedEvent(
                lease_id=LeaseId("l-1"), line_id=LineId("line-main"), state="active"
            ),
        )
    with pytest.raises(ValidationError):
        EventEnvelope[LineStateChangedEvent](
            name=EventName.LINE_STATE_CHANGED,
            audience=wake,
            emitted_at=NOW_UTC,
            payload=LineStateChangedEvent(
                line_id=LineId("line-main"),
                old_state=LineState.FREE,
                new_state=LineState.BUSY,
            ),
        )
    # The scope table pins the exact wake-carrying pair - caller-PII
    # events only.
    wake_scoped = {name for name, scopes in EVENT_AUDIENCE_SCOPES.items() if "wake" in scopes}
    assert wake_scoped == {EventName.INCOMING_CALL, EventName.MISSED_CALL}
    # Round-trip through the union adapter - a serialized wake
    # audience re-validates as WakeAudience.
    env_adapter = TypeAdapter(EventAudience)
    assert isinstance(env_adapter.validate_python({"scope": "wake"}), contract.WakeAudience)


def test_round33_incoming_call_wake_payload_split() -> None:
    """GWT (PR #82 review): the ``incoming_call`` payload splits by
    audience - ``IncomingCallWakeEvent`` is the TOKENLESS
    ``{line_id, caller_id?, expires_at}`` projection, the ONLY
    spelling legal on a ``wake`` envelope, while live
    ``agent``/``session``/``fanout`` audiences keep the token-bearing
    ``IncomingCallEvent``. The split is structural: the wake model
    has no ``call_token`` field at all, and ``EventEnvelope``
    rejects every crossed (audience, payload) pairing."""
    assert "IncomingCallWakeEvent" in contract.__all__
    # The token field does not EXIST on the wake model - no
    # serialization path can emit a live ring token to a hook.
    assert "call_token" not in IncomingCallWakeEvent.__pydantic_fields__
    assert set(IncomingCallWakeEvent.__pydantic_fields__) == {
        "line_id",
        "caller_id",
        "expires_at",
    }
    wake_payload = IncomingCallWakeEvent(
        line_id=LineId("line-main"),
        expires_at=NOW_UTC,
    )
    live_payload = IncomingCallEvent(
        line_id=LineId("line-main"),
        call_token=CallToken(RedactedString("tok-1")),
        expires_at=NOW_UTC,
    )
    # Legal: wake audience + tokenless projection.
    EventEnvelope[IncomingCallWakeEvent](
        name=EventName.INCOMING_CALL,
        audience=contract.WakeAudience(),
        emitted_at=NOW_UTC,
        payload=wake_payload,
    )
    # Legal: every live audience keeps the token-bearing spelling.
    for live_audience in (
        AgentAudience(agent_id="agent-1"),
        SessionAudience(session_id=SessionId("sess-1")),
        contract.FanoutAudience(members=(SessionAudience(session_id=SessionId("sess-1")),)),
    ):
        EventEnvelope[IncomingCallEvent](
            name=EventName.INCOMING_CALL,
            audience=live_audience,
            emitted_at=NOW_UTC,
            payload=live_payload,
        )
    # A token-bearing payload on a wake envelope leaks a live
    # credential to an unauthenticated hook - unconstructible.
    with pytest.raises(ValidationError):
        EventEnvelope[IncomingCallEvent](
            name=EventName.INCOMING_CALL,
            audience=contract.WakeAudience(),
            emitted_at=NOW_UTC,
            payload=live_payload,
        )
    # ...and the tokenless projection can never ride a live
    # audience - a session needs the token to accept.
    for live_audience in (
        AgentAudience(agent_id="agent-1"),
        SessionAudience(session_id=SessionId("sess-1")),
        contract.FanoutAudience(members=(SessionAudience(session_id=SessionId("sess-1")),)),
    ):
        with pytest.raises(ValidationError):
            EventEnvelope[IncomingCallWakeEvent](
                name=EventName.INCOMING_CALL,
                audience=live_audience,
                emitted_at=NOW_UTC,
                payload=wake_payload,
            )


def test_round33_provider_binding_watermark_fields() -> None:
    """GWT: the two durable provider-stamp watermarks are typed,
    optional and aware-datetime - persisted verbatim on the binding
    row so reregister/hydration inherit the line-scoped ordering
    domain."""
    binding = _binding(
        liveness_stamp_watermark=NOW_UTC,
        call_stamp_watermark=NOW_UTC + timedelta(seconds=1),
    )
    assert binding.liveness_stamp_watermark == NOW_UTC
    assert binding.call_stamp_watermark == NOW_UTC + timedelta(seconds=1)
    # Both default to None on a first-attach binding.
    plain = _binding()
    assert plain.liveness_stamp_watermark is None
    assert plain.call_stamp_watermark is None
    # Naive stamps are rejected like every other domain datetime.
    with pytest.raises(ValidationError):
        _binding(liveness_stamp_watermark=datetime(2026, 11, 13, 12, 0, 0))
    # The persisted-row invariant stands: the channel-local
    # orphan_deadline_at exists only on WS_DISCONNECTED bindings and
    # is NOT part of the durable watermark pair.
    assert "orphan_deadline_at" in ProviderBinding.__pydantic_fields__


def test_round33_command_ack_timeout_knob() -> None:
    """GWT: ``provider_command_ack_sec`` is the bounded
    ``{accepted}``-handshake window - default 2.0 s, positive,
    60 s-capped - and a TimeoutConfig patch merges it through
    ``apply_patch`` like every other knob."""
    assert LineTimeouts().provider_command_ack_sec == 2.0
    with pytest.raises(ValidationError):
        LineTimeouts(provider_command_ack_sec=0.0)
    with pytest.raises(ValidationError):
        LineTimeouts(provider_command_ack_sec=60.1)
    with pytest.raises(ValidationError):
        TimeoutConfig(provider_command_ack_sec=-1.0)
    # An all-None patch keeps the effective value; a set field is
    # merged atomically through the same full-revalidation path.
    timeouts = LineTimeouts()
    assert timeouts.apply_patch(TimeoutConfig()).provider_command_ack_sec == 2.0
    patched = timeouts.apply_patch(TimeoutConfig(provider_command_ack_sec=5.0))
    assert patched.provider_command_ack_sec == 5.0
    assert patched.fallback_accept_ttl_sec == timeouts.fallback_accept_ttl_sec


def test_round33_e164_ascii_digit_gate() -> None:
    """GWT: ``E164_PATTERN`` is ASCII-``[0-9]`` - Python ``\\d``
    accepts Unicode digits (e.g. ARABIC-INDIC digits U+0660..0669),
    and a Unicode-digit number must never pass the shape gate."""
    assert contract.E164_PATTERN.fullmatch(_RAW_NUMBER) is not None
    unicode_digits = "+373" + "١٢٣٤٥٦٧"  # Arabic-Indic digits
    assert contract.E164_PATTERN.fullmatch(unicode_digits) is None
    # The same reject propagates through the typed helper as
    # INVALID_FORMAT_NOT_E164.
    assert _e164_reason(unicode_digits, "MD") is ErrorReason.INVALID_FORMAT_NOT_E164


def test_round33_region_membership_on_line_country() -> None:
    """GWT: ``_check_country_alpha2`` carriers reject an
    alpha-2-SHAPED but unassigned region - ``"ZZ"`` is not a known
    region and must never silently feed the dial
    normalization default."""
    assert "ZZ" not in contract._ISO_REGION_CODES
    assert "AA" not in contract._ISO_REGION_CODES
    assert "MD" in contract._ISO_REGION_CODES
    provisioned = _assignment(line_home_country="MD")
    assert provisioned.line_home_country == "MD"
    for bad_region in ("ZZ", "AA"):
        with pytest.raises(ValidationError):
            _assignment(line_home_country=bad_region)


def test_round33_line_country_accepts_telephony_extensions() -> None:
    """GWT (PR #82 review): ``line_home_country`` accepts the
    PHONENUMBERS-SUPPORTED region vocabulary - the ISO 3166-1-style
    codes PLUS telephony extensions like ``"XK"`` (Kosovo),
    ``"AC"`` (Ascension) and ``"TA"`` (Tristan da Cunha), which are
    not strict ISO 3166-1 entries but ARE libphonenumber regions -
    while unassigned codes stay rejected."""
    for ext_region in ("XK", "AC", "TA"):
        assert ext_region in contract._ISO_REGION_CODES
        provisioned = _assignment(line_home_country=ext_region)
        assert provisioned.line_home_country == ext_region
    # The reject coverage is unchanged: an alpha-2-shaped code the
    # vocabulary does not know never passes.
    for bad_region in ("ZZ", "AA"):
        with pytest.raises(ValidationError):
            _assignment(line_home_country=bad_region)


def test_round33_redacted_string_eq_hash_consistency() -> None:
    """GWT: RedactedString eq/hash agree on BOTH paths - encodable
    values use constant-time comparison, unencodable surrogates fall
    back to ordinary string equality, and equal-content instances
    behave identically as dict keys/set members."""
    a, b = RedactedString("secret-value"), RedactedString("secret-value")
    assert a == b and hash(a) == hash(b)
    assert {a: 1}[b] == 1
    assert len({a, b}) == 1
    # The unencodable path no longer falls back to identity: two
    # same-content surrogate strings compare equal and hash equal.
    s1, s2 = RedactedString("\ud800"), RedactedString("\ud800")
    assert s1 == s2 and hash(s1) == hash(s2)
    assert len({s1, s2}) == 1
    assert s1 != RedactedString("x")


def test_round33_cursor_pagination_doc_pin() -> None:
    """GWT drift pin: the shared listing window documents
    cursor-as-request-input - ``cursor`` is accepted INSTEAD of
    ``offset`` (mutually exclusive), malformed/foreign cursors fail
    closed INVALID_ARGUMENT, and the kernel surface keeps
    offset/limit only (the adapter translates)."""
    doc = LineManager.get_lines.__doc__ or ""
    assert "cursor" in doc
    assert "INVALID_ARGUMENT" in doc
    # The kernel listing surface itself stays offset/limit-only -
    # cursor never leaks into kernel signatures (the adapter owns
    # the opaque token).
    get_lines_params = inspect.signature(LineManager.get_lines).parameters
    assert "cursor" not in get_lines_params
    assert "offset" in get_lines_params and "limit" in get_lines_params


def test_round33_answer_pending_ring_doc_pin() -> None:
    """GWT drift pin: once ``call.accept`` burns the ring token the
    ring sits ``answer_pending`` - fallback MUST NOT run while the
    ``line.answer`` handshake is in flight, and a burned ring never
    re-enters the fallback decision."""
    doc = LineManager.decide_fallback.__doc__ or ""
    assert "answer_pending" in doc
    assert "INVALID_STATE" in doc
    tick_doc = LineManager.tick.__doc__ or ""
    assert "provider_command_ack_sec" in tick_doc


def test_round33_snapshot_gate_doc_pin() -> None:
    """GWT drift pin: snapshot reconciliation is gated on the armed
    command-ack handshake - a lease whose ``pending_command`` is
    still inside ``provider_command_ack_sec`` is excluded from
    omission-based reconciliation."""
    registration_src = inspect.getsource(ProviderRegistration)
    liveness_src = inspect.getsource(ProviderLiveness)
    for src in (registration_src, liveness_src):
        assert "pending_command" in src
        assert "provider_command_ack_sec" in src


# ---------------------------------------------------------------------------
# Round 34 (iteration-2 architecture audit): executable pins for the new
# contract guards - every negative case below fails if the guard it pins is
# removed or weakened back to the pre-review shape.
# ---------------------------------------------------------------------------


def test_round34_masked_caller_shape_pinned() -> None:
    """``IncomingCallWakeEvent.caller_id`` carries the MASKED
    ``RedactedString`` rendering (item 16). The guard pins the
    ``_mask`` GEOMETRY (``.{0,4}****.{0,2}`` fullmatch), not merely
    the marker's presence: a raw number, a raw number with a
    ``****`` glued on, or a buried marker can never validate."""
    base: dict[str, Any] = {"line_id": LineId("line-main"), "expires_at": NOW_UTC}
    # No caller at all stays legal (withheld identity).
    assert IncomingCallWakeEvent(**base).caller_id is None
    # Every shape _mask can actually produce validates: 0-4 visible
    # chars, the **** marker, 0-2 visible chars.
    for masked in ("****", "****45", "t****42", "+373****45", "abcd****56"):
        assert IncomingCallWakeEvent(**base, caller_id=masked).caller_id == masked
    # The projection of a genuine RedactedString round-trips - the
    # pinned shape is tight enough to reject forgeries but never so
    # tight it rejects a real rendering.
    for raw in (_RAW_NUMBER, "+37322212345", "12345", "x"):
        rendered = str(RedactedString(raw))
        assert IncomingCallWakeEvent(**base, caller_id=rendered).caller_id == rendered
    # Negative set: full raw caller strings, leading/trailing mask
    # glue, oversized windows on either side of the marker, a buried
    # marker, the empty string and a control-text carrier whose mask
    # sits inside an otherwise shape-legal frame.
    for bad in (
        "+37362112345",  # full raw number - no marker at all
        "****+37362112345",  # leading mask glued to a raw number
        "+37362112345****",  # trailing mask glued to a raw number
        "12345****67",  # >4 visible chars before the marker
        "ab****123",  # >2 visible chars after the marker
        "mask****middle12345",  # marker buried mid-string
        "**",  # too few marker chars
        "",
        "\t****",  # shape-legal window carrying control text
    ):
        with pytest.raises(ValidationError):
            IncomingCallWakeEvent(**base, caller_id=bad)


def test_round34_fanout_members_are_unique() -> None:
    """``FanoutAudience._members_are_unique`` (item 24): one
    recipient rides the member list exactly once - a duplicate
    would double-deliver and double-count the fan-out bound."""
    # Distinct recipients and the agent-vs-session cross-scope pair
    # (same name string, different routing key) both construct.
    FanoutAudience(
        members=(
            AgentAudience(agent_id="agent-1"),
            SessionAudience(session_id=SessionId("agent-1")),
            AgentAudience(agent_id="agent-2"),
        )
    )
    for members in (
        (AgentAudience(agent_id="agent-1"), AgentAudience(agent_id="agent-1")),
        (
            SessionAudience(session_id=SessionId("sess-1")),
            SessionAudience(session_id=SessionId("sess-1")),
        ),
        (
            AgentAudience(agent_id="agent-1"),
            SessionAudience(session_id=SessionId("sess-1")),
            AgentAudience(agent_id="agent-1"),
        ),
    ):
        with pytest.raises(ValidationError):
            FanoutAudience(members=members)


def test_round34_fallback_triggered_worker_binding() -> None:
    """``fallback_triggered`` routes ONLY to the reserved
    ``system:fallback_worker`` identity (item 24): the envelope
    carries a live ``fallback_call_token`` credential, so the
    binding is structural, not just an ``agent`` scope entry."""
    payload = FallbackTriggeredEvent(
        line_id=LineId("line-main"),
        call_token=CallToken(RedactedString("fb-tok")),
        policy=RecordAndTelegramFallback(channel="ops-alerts"),
        expires_at=NOW_UTC,
    )
    EventEnvelope[FallbackTriggeredEvent](
        name=EventName.FALLBACK_TRIGGERED,
        audience=AgentAudience(agent_id=SYSTEM_FALLBACK_WORKER),
        emitted_at=NOW_UTC,
        payload=payload,
    )
    for bad_audience in (
        # An ordinary agent audience is structurally rejected even
        # though "agent" is the event's only legal scope literal.
        AgentAudience(agent_id="agent-1"),
        # A RESERVED system id that is not the worker fails the same
        # guard - the check names the identity, not the namespace.
        AgentAudience(agent_id="system:not_the_worker"),
        # A session envelope could never be a worker dispatch.
        SessionAudience(session_id=SessionId("sess-1")),
    ):
        with pytest.raises(ValidationError):
            EventEnvelope[FallbackTriggeredEvent](
                name=EventName.FALLBACK_TRIGGERED,
                audience=bad_audience,
                emitted_at=NOW_UTC,
                payload=payload,
            )


def test_round34_wake_hooks_bounds() -> None:
    """``wake_hooks`` (item 2): bounded at
    ``MAX_WAKE_HOOKS_PER_LINE`` with every member through the strict
    ``_check_id`` grammar - on the durable assignment AND on the
    ``AdminLineView`` mirror."""
    full_set = frozenset(f"hook-{i}" for i in range(MAX_WAKE_HOOKS_PER_LINE))
    assignment = _assignment(wake_hooks=full_set)
    assert assignment.wake_hooks == full_set
    with pytest.raises(ValidationError):
        _assignment(wake_hooks=frozenset(f"hook-{i}" for i in range(MAX_WAKE_HOOKS_PER_LINE + 1)))
    # Every member obeys the domain-id grammar (non-empty, <=128
    # chars, [A-Za-z0-9][A-Za-z0-9._-]*, no ':' separator).
    for bad_hook in ("", "has space", "hook/name", "-lead", "x" * 129):
        with pytest.raises(ValidationError):
            _assignment(wake_hooks=frozenset({bad_hook}))
    # The AdminLineView mirror applies the same bound/grammar - the
    # view can never widen what the store held.
    view = AdminLineView(
        line_id=LineId("line-main"),
        caps=[Intent.DIAL_OUT],
        state=LineState.FREE,
        liveness=LivenessState.REACHABLE,
        wake_hooks=full_set,
    )
    assert view.wake_hooks == full_set
    with pytest.raises(ValidationError):
        AdminLineView(
            line_id=LineId("line-main"),
            caps=[Intent.DIAL_OUT],
            state=LineState.FREE,
            liveness=LivenessState.REACHABLE,
            wake_hooks=frozenset(f"hook-{i}" for i in range(MAX_WAKE_HOOKS_PER_LINE + 1)),
        )
    with pytest.raises(ValidationError):
        AdminLineView(
            line_id=LineId("line-main"),
            caps=[Intent.DIAL_OUT],
            state=LineState.FREE,
            liveness=LivenessState.REACHABLE,
            wake_hooks=frozenset({"bad hook"}),
        )


def test_round34_admin_line_view_orphan_coherence() -> None:
    """``AdminLineView.orphan_deadline_at`` (item 7): the mirror is
    set EXACTLY while ``liveness`` is ``ws_disconnected`` - set on
    any other rung, or absent on the orphan rung, is incoherent."""

    def _view(liveness: LivenessState, deadline: datetime | None) -> AdminLineView:
        bound = liveness is not LivenessState.UNBOUND
        return AdminLineView(
            line_id=LineId("line-main"),
            caps=[Intent.DIAL_OUT] if bound else (),
            state=LineState.FREE,
            liveness=liveness,
            orphan_deadline_at=deadline,
        )

    # The coherent pairs construct.
    assert _view(LivenessState.WS_DISCONNECTED, NOW_UTC).orphan_deadline_at == NOW_UTC
    assert _view(LivenessState.REACHABLE, None).orphan_deadline_at is None
    assert _view(LivenessState.UNBOUND, None).orphan_deadline_at is None
    # Orphan rung without a deadline, and a deadline on any other
    # rung, are both rejected.
    for liveness, deadline in (
        (LivenessState.WS_DISCONNECTED, None),
        (LivenessState.REACHABLE, NOW_UTC),
        (LivenessState.DEGRADED, NOW_UTC),
        (LivenessState.UNREACHABLE, NOW_UTC),
        (LivenessState.UNBOUND, NOW_UTC),
    ):
        with pytest.raises(ValidationError):
            _view(liveness, deadline)


def test_round34_media_credentials_epoch_bound() -> None:
    """``MediaCredentials`` media epochs (item 17; per-role split in
    round 35, iteration-3 audit item 1): a minted pair always stamps
    the per-role lease epochs it was issued under - each ``>= 1``,
    required, and a pair can never carry the pre-dial ``0``."""
    kwargs: dict[str, Any] = {
        "agent_media_endpoint": "unix:///run/phonebridge/media.sock",
        "provider_media_endpoint": "wss://media.internal/provider",
        "agent_media_token": MediaToken(RedactedString("a-tok")),
        "provider_media_token": MediaToken(RedactedString("p-tok")),
    }
    creds = MediaCredentials(**kwargs, agent_media_epoch=1, provider_media_epoch=1)
    assert creds.agent_media_epoch == 1 and creds.provider_media_epoch == 1
    creds9 = MediaCredentials(**kwargs, agent_media_epoch=9, provider_media_epoch=9)
    assert creds9.agent_media_epoch == 9 and creds9.provider_media_epoch == 9
    for bad_epoch in (0, -1):
        with pytest.raises(ValidationError):
            MediaCredentials(**kwargs, agent_media_epoch=bad_epoch)
        with pytest.raises(ValidationError):
            MediaCredentials(**kwargs, provider_media_epoch=bad_epoch)
    # Per-role independence (round 35, iteration-3 audit item 1):
    # the two epochs are separate counters - a pair whose provider
    # half was reissued carries the split values and still
    # validates, while the sibling epoch simply did not move.
    split = MediaCredentials(**kwargs, agent_media_epoch=1, provider_media_epoch=4)
    assert split.agent_media_epoch == 1 and split.provider_media_epoch == 4
    # Both fields are required - a pair minted without its epochs
    # is a pre-round-35 shape and must not hydrate.
    with pytest.raises(ValidationError):
        MediaCredentials(**kwargs)
    with pytest.raises(ValidationError):
        MediaCredentials(**kwargs, agent_media_epoch=1)
    with pytest.raises(ValidationError):
        MediaCredentials(**kwargs, provider_media_epoch=1)


def test_round34_forward_via_line_error_tail() -> None:
    """The ``<CODE>:<REASON>`` message tail (item 24): a rejected
    forward dial target surfaces the ErrorCode value AND the typed
    ErrorReason member in the validator message, so the adapter can
    recover the branch without re-running the check."""
    with pytest.raises(ValidationError) as e164_err:
        ForwardViaLineFallback(
            line_id=LineId("line-b"),
            target_number=RedactedString("79099999"),
            target_country="MD",
        )
    assert "INVALID_ARGUMENT:INVALID_FORMAT_NOT_E164" in str(e164_err.value)
    with pytest.raises(ValidationError) as mismatch_err:
        ForwardViaLineFallback(
            line_id=LineId("line-b"),
            target_number=RedactedString("+12015550123"),
            target_country="MD",
        )
    assert "INVALID_ARGUMENT:COUNTRY_MISMATCH" in str(mismatch_err.value)
    # The typed reason stays reachable through the cause chain too.
    inner = e164_err.value.errors()[0].get("ctx", {}).get("error")
    assert isinstance(inner, ValueError)
    cause = inner.__cause__
    assert isinstance(cause, PhonebridgeError)
    assert cause.data.reason is ErrorReason.INVALID_FORMAT_NOT_E164


def test_round34_liveness_watermark_safety_constants() -> None:
    """New round-34 constants pin: the claimable-vs-servable split
    (item 4), the flat unmatched-call safety net (item 13), the
    per-line call-stamp watermark bound (item 11), the wake-hook
    bound (item 2) and the DTMF pause budget (item 17)."""
    assert SERVABLE_LIVENESS_STATES == frozenset({LivenessState.REACHABLE, LivenessState.DEGRADED})
    assert CLAIM_VISIBLE_LIVENESS_STATES == frozenset(
        {
            LivenessState.REACHABLE,
            LivenessState.DEGRADED,
            LivenessState.WS_DISCONNECTED,
        }
    )
    # ws_disconnected is the ONLY claim-visible-but-never-servable
    # rung; the dead/absent rungs fail both axes.
    assert CLAIM_VISIBLE_LIVENESS_STATES - SERVABLE_LIVENESS_STATES == frozenset(
        {LivenessState.WS_DISCONNECTED}
    )
    for dead_rung in (LivenessState.UNREACHABLE, LivenessState.UNBOUND):
        assert dead_rung not in SERVABLE_LIVENESS_STATES
        assert dead_rung not in CLAIM_VISIBLE_LIVENESS_STATES
    assert UNMATCHED_CALL_SAFETY_SEC == 3600.0
    assert MAX_CALL_STAMP_WATERMARKS_PER_LINE == 64
    assert MAX_WAKE_HOOKS_PER_LINE == 16
    assert MEDIA_PAIR_TTL_SEC == 30
    assert MAX_DTMF_STREAM_PAUSE_SEC == 30.0
    # Signature pin (item 12): note_provider_liveness hands the
    # promoted pending-ring tokens back to the provider.ping reply.
    return_annotation = inspect.signature(LineManager.note_provider_liveness).return_annotation
    assert "PendingRing" in str(return_annotation)


def test_round35_call_lease_per_role_media_epochs() -> None:
    """GWT (round 35, iteration-3 audit item 1): the media epochs are
    PER-ROLE counters, not one lease-wide scalar - a single-role
    reissue advances only the addressed role so the sibling's live
    socket survives under its own epoch. The coherence rule is
    symmetric: ``ACQUIRED`` carries BOTH epochs at 0 and every
    call-bearing phase requires BOTH at >= 1, but the two values
    are independent - a provider-role re-pair leaves
    ``agent_media_epoch`` untouched."""
    # Asymmetric pair: the provider half was re-paired three times
    # (reregister/reattach) while the agent socket stayed on epoch 1.
    split = CallLease(
        **_lease_base(),
        phase=CallPhase.ACTIVE,
        agent_media_epoch=1,
        provider_media_epoch=4,
    )
    assert split.agent_media_epoch == 1 and split.provider_media_epoch == 4
    # Each role's zero independently rejects on a call-bearing
    # phase - a half-pair can never ride a live call.
    for zero_role in (
        {"agent_media_epoch": 0, "provider_media_epoch": 2},
        {"agent_media_epoch": 2, "provider_media_epoch": 0},
    ):
        with pytest.raises(ValidationError):
            CallLease(**_lease_base(), phase=CallPhase.ACTIVE, **zero_role)
    # ACQUIRED is all-or-nothing at zero: a single stamped role
    # already counts as an allocated pair and is rejected.
    with pytest.raises(ValidationError):
        CallLease(
            **_lease_base(),
            phase=CallPhase.ACQUIRED,
            grant_expires_at=NOW_UTC + timedelta(seconds=5),
            agent_media_epoch=1,
            provider_media_epoch=0,
        )
    # ENDED keeps whatever pair it died with - a split pair included.
    CallLease(
        **_lease_base(),
        phase=CallPhase.ENDED,
        agent_media_epoch=2,
        provider_media_epoch=7,
    )


def test_round35_missed_call_wake_payload_split() -> None:
    """GWT (round 35, iteration-3 audit item 7): the ``missed_call``
    payload splits by audience exactly like ``incoming_call`` did in
    round 34 - ``MissedCallWakeEvent`` is the TOKENLESS
    ``{line_id, caller_id?, reason}`` projection, the ONLY spelling
    legal on a ``wake`` envelope, while the authenticated audiences
    keep the ``RedactedString``-carrying ``MissedCallEvent``."""
    assert "MissedCallWakeEvent" in contract.__all__
    # Tokenless by construction: no lease token field can ever leak
    # onto the unauthenticated hook leg.
    assert "call_token" not in MissedCallWakeEvent.__pydantic_fields__
    assert "lease_id" not in MissedCallWakeEvent.__pydantic_fields__
    # The masked caller is a plain str pinned to the masked
    # projection shape - a raw E.164 or a RedactedString-shaped dump
    # can never validate in.
    wake = MissedCallWakeEvent(
        line_id=LineId("line-main"),
        caller_id="+373****45",
        reason=CallEndReason.NO_ANSWER,
    )
    assert wake.caller_id == "+373****45"
    with pytest.raises(ValidationError):
        MissedCallWakeEvent(
            line_id=LineId("line-main"),
            caller_id=_RAW_NUMBER,  # raw, unmasked
            reason=CallEndReason.NO_ANSWER,
        )
    # caller_id is optional - an unknown-caller ring still reports.
    MissedCallWakeEvent(line_id=LineId("line-main"), reason=CallEndReason.NO_ANSWER)
    # The ring-vs-lease fact rule mirrors MissedCallEvent:
    # max_call_duration is an established-lease end, never a missed
    # call.
    with pytest.raises(ValidationError):
        MissedCallWakeEvent(
            line_id=LineId("line-main"),
            reason=CallEndReason.MAX_CALL_DURATION,
        )
    # Frozen like every contract DTO.
    with pytest.raises(ValidationError):
        wake.reason = CallEndReason.NORMAL  # type: ignore[misc]


def test_round35_register_result_auto_created_assignment() -> None:
    """GWT (round 35, iteration-3 audit item 4): the register result
    exposes the ``LineAssignment`` record the register mutation
    auto-created for a previously unconfigured ``line_id`` so the
    adapter's write-through-failure compensation can target
    ``restore_line(line_id, None)``. ``None`` is the common
    already-configured-line case and carries no rollback
    privilege."""
    result = ProviderRegisterResult(binding=_binding())
    assert result.auto_created_assignment is None
    created = _assignment()
    with_assignment = ProviderRegisterResult(binding=_binding(), auto_created_assignment=created)
    assert with_assignment.auto_created_assignment == created
    # The field is typed - an arbitrary value can never validate in.
    with pytest.raises(ValidationError):
        ProviderRegisterResult(
            binding=_binding(),
            auto_created_assignment={"line_id": "line-main"},
        )
