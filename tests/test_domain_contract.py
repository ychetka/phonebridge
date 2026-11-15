"""Contract tests for phonebridge.domain.contract (roadmap item_03, issue #7).

Each GWT id rides in the parametrized case id so the pytest nodeid
renders as ``test_name[ITEM-03.GWT-N]`` (same convention as the
tooling-baseline tests).
"""

import json
import re
from collections.abc import Collection
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

import phonebridge.domain.contract as contract
from phonebridge.domain.contract import (
    CALL_PHASE_WIRE_STATES,
    DTMF_PATTERN,
    EVENT_WIRE_NAMES,
    MAX_DTMF_DIGITS,
    MAX_IDEMPOTENCY_KEY_LEN,
    MAX_PENDING_CLAIMS_PER_AGENT,
    MAX_PENDING_CLAIMS_TOTAL,
    MAX_PROVIDER_META_ENTRIES,
    MAX_PROVIDER_META_KEY_LEN,
    MAX_PROVIDER_META_VALUE_LEN,
    PROVIDER_COMMAND_WIRE_NAMES,
    AcceptResult,
    AcquireGranted,
    AcquireQueued,
    AcquireRefused,
    AcquireResult,
    AdminAudience,
    AgentAudience,
    AnswerAndAnnounceFallback,
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
    ForwardViaLineFallback,
    IdleAlertEvent,
    IncomingCallEvent,
    IncomingRingIngress,
    Intent,
    LeaseId,
    LineAdminKernel,
    LineAssignment,
    LineId,
    LineManager,
    LineState,
    LineStateChangedEvent,
    LineSummary,
    LivenessState,
    MediaCredentials,
    MediaToken,
    MissedCallEvent,
    NoPickupFallback,
    PendingClaim,
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
    ProviderRegistration,
    ProviderRevokeCommand,
    ProviderSendDtmfCommand,
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
)

REPO_ROOT = Path(__file__).resolve().parents[1]
API_CONTRACT_MD = REPO_ROOT / "docs" / "api-contract.md"

NOW_UTC = datetime(2026, 11, 13, 12, 0, 0, tzinfo=timezone.utc)
_RAW_NUMBER = "+37379012345"


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

    # Enum values match the wire spellings verbatim.
    assert {i.value for i in Intent} == {
        "dial_out",
        "receive_inbound",
        "exclusive_line",
    }
    assert {s.value for s in LineState} == {
        "FREE",
        "BUSY",
        "RESERVED",
        "RINGING_LOCKED",
        "DEGRADED",
        "DOWN",
    }
    assert {p.value for p in CallPhase} == {
        "acquired",
        "dialing",
        "ringing",
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
        call_token=CallToken("tok-abc"),
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
    reg = ProviderRegistration(
        line_id=LineId("line-main"),
        token=RedactedString("line-secret-token-value"),
        caps=[Intent.DIAL_OUT],
    )
    assert "line-secret-token-value" not in repr(reg.token)
    assert "line-secret-token-value" in reg.model_dump_json()
    assert "line-secret-token-value" not in repr(reg.safe_dump())


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
        "+37379012345": "+373****45",
    }
    for raw, masked in expected.items():
        assert str(RedactedString(raw)) == masked
        visible = len(masked) - masked.count("*")
        assert visible <= len(raw) // 2


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


@pytest.mark.parametrize("_gwt", [pytest.param(None, id="ITEM-03.GWT-3")])
def test_error_codes_match_contract_doc(_gwt: object) -> None:
    doc_codes = _doc_error_codes()
    assert doc_codes, "parsed zero error codes - doc table format changed?"
    enum_codes = {c.value for c in ErrorCode}
    assert enum_codes == doc_codes, (
        f"ErrorCode drift vs docs/api-contract.md: "
        f"missing={doc_codes - enum_codes}, extra={enum_codes - doc_codes}"
    )


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
    lease = CallLease(
        lease_id=LeaseId("lease-1"),
        line_id=LineId("line-main"),
        owner_agent_id="agent-1",
        session_id=SessionId("sess-1"),
        intent=Intent.DIAL_OUT,
        created_at=NOW_UTC,
    )
    assert lease.answered_at is None
    assert lease.answer_confidence == "provisional"
    assert lease.phase == CallPhase.ACQUIRED  # round 20: pre-dial phase
    assert lease.grant_expires_at is None
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
            "target_number": "+37379099999",
            "target_country": "MD",
        }
    )
    assert isinstance(fwd, ForwardViaLineFallback)
    assert fwd.line_id == LineId("line-b")
    # Round 23 (review-12 C2): the policy must carry the full dial
    # target - the FallbackWorker has no admin scope to query config.
    assert fwd.target_number.to_wire() == "+37379099999"
    assert fwd.target_country == "MD"
    # ...the target is PII-safe in log dumps (masked, never raw).
    assert "+37379099999" not in repr(fwd.safe_dump())
    for missing in ({}, {"target_country": "MD"}, {"target_number": "+37379099999"}):
        with pytest.raises(ValidationError):
            adapter.validate_python({"policy": "forward_via_line", "line_id": "line-b", **missing})
    rec = adapter.validate_python({"policy": "record_and_telegram", "channel": "@ops"})
    assert isinstance(rec, RecordAndTelegramFallback)
    assert rec.channel == "@ops"
    announce = adapter.validate_python(
        {"policy": "answer_and_announce", "announcement_ref": "wav:busy-msg"}
    )
    assert isinstance(announce, AnswerAndAnnounceFallback)
    assert announce.announcement_ref == "wav:busy-msg"
    # Round 22 (review-10 MEDIUM-2): the policy carries the
    # announcement selector - it cannot be empty or absent.
    with pytest.raises(ValidationError):
        adapter.validate_python({"policy": "answer_and_announce"})
    with pytest.raises(ValidationError):
        adapter.validate_python({"policy": "answer_and_announce", "announcement_ref": ""})
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


EVENT_PAYLOAD_MODELS: dict[EventName, type[contract.ContractModel]] = {
    EventName.INCOMING_CALL: IncomingCallEvent,
    EventName.CALL_STATE_CHANGED: CallStateChangedEvent,
    EventName.QUEUE_READY: QueueReadyEvent,
    EventName.QUEUE_UPDATED: QueueUpdatedEvent,
    EventName.ASSIGNMENT_REVOKED: AssignmentRevokedEvent,
    EventName.AUDIO_READY: AudioReadyEvent,
    EventName.FALLBACK_TRIGGERED: FallbackTriggeredEvent,
    EventName.LINE_STATE_CHANGED: LineStateChangedEvent,
    EventName.MISSED_CALL: MissedCallEvent,
    EventName.IDLE_ALERT: IdleAlertEvent,
}
"""Domain EventName -> payload model (round 22): the wire-parity
check below needs one authoritative model per event row."""


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
    # either direction).
    api_contract = (Path(__file__).parents[1] / "docs" / "api-contract.md").read_text(
        encoding="utf-8"
    )
    doc_event_rows = set(re.findall(r"^\|\s*`(event\.[a-z_]+)`\s*\|", api_contract, flags=re.M))
    assert doc_event_rows == set(EVENT_WIRE_NAMES.values())

    # Payload parity: every EventName has exactly one payload model,
    # and the model's field names + optionality match the wire row.
    assert set(EVENT_PAYLOAD_MODELS) == set(EventName)
    table = _doc_event_table()
    assert table, "parsed zero event.* payloads - doc table format changed?"
    assert set(table) == set(EVENT_WIRE_NAMES.values())
    for name, model in EVENT_PAYLOAD_MODELS.items():
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
        call_token=CallToken("tok-1"),
        caller_id=RedactedString(_RAW_NUMBER),
        expires_at=NOW_UTC,
    )
    envelope = EventEnvelope[IncomingCallEvent](
        name=EventName.INCOMING_CALL,
        audience=AgentAudience(agent_id="agent-1"),
        emitted_at=NOW_UTC,
        payload=payload,
    )
    assert envelope.payload.call_token == CallToken("tok-1")
    assert isinstance(envelope.audience, AgentAudience)

    csc = CallStateChangedEvent(
        lease_id=LeaseId("l-1"),
        line_id=LineId("line-main"),
        state="active",
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
        call_token=CallToken("fb-tok"),
        caller_id=RedactedString(_RAW_NUMBER),
        policy=NoPickupFallback(),
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
            old_state=LineState.FREE,
            new_state=LineState.DOWN,
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
    ProviderCallStateUpdate(
        line_id=LineId("line-main"),
        assignment_id=contract.AssignmentId("asg-1"),
        call_token=CallToken("tok-1"),
        state=ProviderCallState.RINGING,
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
        state=LivenessState.UNREACHABLE,
        checked_at=NOW_UTC,
    )
    # Round 21 (CRITICAL-3): ws_disconnected is the WS-drop fact that
    # starts the orphan-grace window - distinct from UNREACHABLE
    # (heartbeat timeout -> immediate teardown).
    ProviderLiveness(
        line_id=LineId("line-main"),
        assignment_id=contract.AssignmentId("asg-1"),
        state=LivenessState.WS_DISCONNECTED,
        checked_at=NOW_UTC,
    )
    # Round-21 review (WARNING-2): a ping may carry the same
    # state_snapshot shape as reregister - ProviderLiveness is its
    # domain carrier.
    ping = ProviderLiveness(
        line_id=LineId("line-main"),
        assignment_id=contract.AssignmentId("asg-1"),
        state=LivenessState.REACHABLE,
        checked_at=NOW_UTC,
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
        token=RedactedString("line-secret-token-value"),
        caps=[Intent.DIAL_OUT],
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
    # ...and the wire update itself carries caller_id too.
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
        call_token=CallToken("tok-1"),
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
    """Minimal concrete implementation exercising the async surface."""

    async def register_intent(
        self,
        agent_id: str,
        intents: Collection[Intent],
        *,
        profile: str | None = None,
    ) -> SessionId:
        return SessionId("sess-1")

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
            lease_id=lease_id, media_endpoint="unix:///run/m.sock", media_token=MediaToken("mt")
        )

    async def accept(self, session_id: SessionId, call_token: CallToken) -> AcceptResult:
        return AcceptResult(
            lease_id=LeaseId("l-2"),
            line_id=LineId("line-main"),
            media_endpoint="unix:///run/m.sock",
            media_token=MediaToken("mt"),
        )

    async def hangup(self, session_id: SessionId, lease_id: LeaseId) -> None:
        return None

    async def send_dtmf(self, session_id: SessionId, lease_id: LeaseId, digits: str) -> None:
        return None

    async def claim_exclusive(self, session_id: SessionId, line_id: LineId) -> contract.ClaimResult:
        return contract.ClaimResult(claim_id=ClaimId("claim-1"))

    async def get_lines(
        self, session_id: SessionId, intent: Intent | None = None
    ) -> list[LineSummary]:
        return [
            LineSummary(
                line_id=LineId("line-main"),
                caps=[Intent.DIAL_OUT],
                state=LineState.FREE,
            )
        ]

    async def register_provider(self, registration: ProviderRegistration) -> ProviderBinding:
        return ProviderBinding(
            assignment_id=contract.AssignmentId("bind-1"),
            line_id=registration.line_id,
            caps=registration.caps,
            connected_at=NOW_UTC,
            last_seen_at=NOW_UTC,
        )

    async def note_provider_liveness(self, liveness: ProviderLiveness) -> None:
        return None

    async def ingest_ring(self, ring: IncomingRingIngress) -> IncomingCallEvent:
        return IncomingCallEvent(
            line_id=ring.line_id,
            call_token=CallToken("tok-minted"),
            caller_id=ring.caller_id,
            expires_at=NOW_UTC,
        )

    async def update_provider_capabilities(
        self, update: ProviderCapabilityUpdate
    ) -> ProviderBinding:
        return ProviderBinding(
            assignment_id=update.assignment_id,
            line_id=update.line_id,
            caps=update.caps,
            profile=update.profile,
            connected_at=NOW_UTC,
            last_seen_at=NOW_UTC,
        )

    async def ingest_call_state(self, update: ProviderCallStateUpdate) -> None:
        return None

    async def ingest_audio_ready(
        self,
        line_id: LineId,
        assignment_id: contract.AssignmentId,
        lease_id: LeaseId,
    ) -> None:
        return None

    async def decide_fallback(self, line_id: LineId, call_token: CallToken) -> FallbackDecision:
        return FallbackDecision(line_id=line_id, call_token=call_token, policy=NoPickupFallback())

    async def drop_session(self, session_id: SessionId) -> None:
        return None

    async def tick(self, now: datetime) -> None:
        return None

    def drain_events(self) -> list[EventEnvelope[Any]]:
        return [
            EventEnvelope[MissedCallEvent](
                name=EventName.MISSED_CALL,
                audience=BroadcastAudience(),
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
    returns its contracted result type (all 22 methods - 20 async +
    the two sync outbox drains; round 22 adds ``release_assignment``
    and ``update_provider_capabilities``)."""
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
        "claim_exclusive",
        "get_lines",
        "register_provider",
        "update_provider_capabilities",
        "note_provider_liveness",
        "ingest_ring",
        "ingest_call_state",
        "ingest_audio_ready",
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
    assert isinstance(dial.media_token, str)
    accepted = await mgr.accept(session, CallToken("tok-1"))
    assert isinstance(accepted, AcceptResult)
    await mgr.send_dtmf(session, result.lease_id, "12#")
    await mgr.hangup(session, result.lease_id)
    await mgr.release(session, result.lease_id)
    lines = await mgr.get_lines(session)
    assert lines[0].state is LineState.FREE
    claim = await mgr.claim_exclusive(session, LineId("line-main"))
    assert claim.status == "pending_approval"

    # --- provider ingress ---
    binding = await mgr.register_provider(
        ProviderRegistration(
            line_id=LineId("line-main"),
            token=RedactedString("line-secret-token-value"),
            caps=[Intent.DIAL_OUT],
        )
    )
    assert isinstance(binding, ProviderBinding)  # round 20: distinct object space
    await mgr.note_provider_liveness(
        ProviderLiveness(
            line_id=LineId("line-main"),
            state=LivenessState.REACHABLE,
            checked_at=NOW_UTC,
        )
    )
    ring_event = await mgr.ingest_ring(
        IncomingRingIngress(
            line_id=LineId("line-main"),
            assignment_id=contract.AssignmentId("asg-1"),
            observed_at=NOW_UTC,
        )
    )
    assert isinstance(ring_event, IncomingCallEvent)
    assert isinstance(ring_event.call_token, str)  # kernel-minted (round 22)
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
            assignment_id=contract.AssignmentId("asg-1"),
            state=ProviderCallState.ACTIVE,
            observed_at=NOW_UTC,
        )
    )
    decision = await mgr.decide_fallback(LineId("line-main"), CallToken("tok-3"))
    assert isinstance(decision, FallbackDecision)
    assert decision.fallback_call_token is None  # worker tokens only on WORKER policies
    await mgr.ingest_audio_ready(
        LineId("line-main"), contract.AssignmentId("asg-1"), result.lease_id
    )
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
    non-None values."""
    assert set(CALL_PHASE_WIRE_STATES.keys()) == set(CallPhase)
    doc = API_CONTRACT_MD.read_text(encoding="utf-8")
    row = next(line for line in doc.splitlines() if "`event.call_state_changed`" in line)
    state_seg = re.search(r"`state`:([^;(]+)", row)
    assert state_seg is not None
    doc_states = set(re.findall(r"`([a-z_]+)`", state_seg.group(1)))
    mapped = {v for v in CALL_PHASE_WIRE_STATES.values() if v is not None}
    assert doc_states == mapped


def test_provider_command_union_roundtrip() -> None:
    """Round 20: all five ProviderCommand variants serialize through
    TypeAdapter(ProviderCommand) and carry PII-safely."""
    adapter = TypeAdapter(ProviderCommand)
    commands: list[ProviderCommand] = [
        ProviderDialCommand(
            line_id=LineId("line-main"),
            assignment_id=contract.AssignmentId("bind-1"),
            lease_id=LeaseId("l-1"),
            number=RedactedString(_RAW_NUMBER),
            media_endpoint="unix:///run/phonebridge/media.sock",
            media_token=MediaToken("p-tok-1"),
        ),
        ProviderAnswerCommand(
            line_id=LineId("line-main"),
            assignment_id=contract.AssignmentId("bind-1"),
            call_token=CallToken("tok-1"),
            lease_id=LeaseId("l-2"),
            media_endpoint="unix:///run/phonebridge/media.sock",
            media_token=MediaToken("p-tok-2"),
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
        return _assignment()

    async def reject_claim(self, claim_id: ClaimId, reason: str | None = None) -> None:
        return None

    async def list_pending_claims(self) -> list[PendingClaim]:
        return [
            PendingClaim(
                claim_id=ClaimId("claim-1"),
                line_id=LineId("line-main"),
                requester_agent_id="agent-1",
                session_id=SessionId("sess-1"),
                created_at=NOW_UTC,
            )
        ]

    async def list_assignments(self) -> list[LineAssignment]:
        return [_assignment()]

    async def list_provider_bindings(self) -> list[ProviderBinding]:
        return [
            ProviderBinding(
                assignment_id=contract.AssignmentId("bind-1"),
                line_id=LineId("line-main"),
                caps=(Intent.DIAL_OUT,),
                connected_at=NOW_UTC,
                last_seen_at=NOW_UTC,
            )
        ]

    async def revoke_assignment(
        self, assignment_id: contract.AssignmentId, reason: str | None = None
    ) -> None:
        return None

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
        return _assignment()


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
    await kernel.revoke_assignment(contract.AssignmentId("asg-1"), reason="admin")
    updated = await kernel.configure_line(
        LineId("line-main"),
        claimable=False,
        accept_window_sec=10.0,
        idle_revoke_after=timedelta(hours=24),
        timeouts=TimeoutConfig(grant_ttl_sec=5.0, heartbeat_down_after=3),
    )
    assert isinstance(updated, LineAssignment)


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
        "token": "line-secret-token-value",
        "caps": ["dial_out"],
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
    share one token (media.auth role spoofing fix)."""
    creds = MediaCredentials(
        media_endpoint="unix:///run/phonebridge/media.sock",
        agent_media_token=MediaToken("a-tok"),
        provider_media_token=MediaToken("p-tok"),
    )
    assert creds.agent_media_token != creds.provider_media_token
    with pytest.raises(ValidationError):
        MediaCredentials(
            media_endpoint="unix:///run/phonebridge/media.sock",
            agent_media_token=MediaToken("same"),
            provider_media_token=MediaToken("same"),
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
    by_token = ProviderHangupCommand(**base, call_token=CallToken("tok-1"))
    assert by_token.lease_id is None
    with pytest.raises(ValidationError):
        ProviderHangupCommand(**base)  # neither target
    with pytest.raises(ValidationError):
        ProviderHangupCommand(
            **base, lease_id=LeaseId("l-1"), call_token=CallToken("tok-1")
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
        def allocate(self, lease_id: LeaseId, line_id: LineId) -> MediaCredentials:
            calls.append(f"allocate:{lease_id}")
            return MediaCredentials(
                media_endpoint="unix:///run/phonebridge/media.sock",
                agent_media_token=MediaToken("a-tok"),
                provider_media_token=MediaToken("p-tok"),
            )

        def revoke(self, lease_id: LeaseId) -> None:
            calls.append(f"revoke:{lease_id}")

    allocator: contract.MediaAllocator = FakeAllocator()
    creds = allocator.allocate(LeaseId("l-1"), LineId("line-main"))
    assert creds.agent_media_token != creds.provider_media_token
    allocator.revoke(LeaseId("l-1"))
    allocator.revoke(LeaseId("l-1"))  # idempotent - safe to call twice
    allocator.revoke(LeaseId("never-allocated"))  # unknown lease: no-op
    assert calls == [
        "allocate:l-1",
        "revoke:l-1",
        "revoke:l-1",
        "revoke:never-allocated",
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
            call_token=CallToken("fb-tok"),
            policy=NoPickupFallback(),
        )
    ft = FallbackTriggeredEvent(
        line_id=LineId("line-main"),
        call_token=CallToken("fb-tok"),
        policy=NoPickupFallback(),
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
