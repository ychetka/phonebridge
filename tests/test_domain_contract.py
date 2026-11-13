"""Contract tests for phonebridge.domain.contract (roadmap item_03, issue #7).

Each GWT id rides in the parametrized case id so the pytest nodeid
renders as ``test_name[ITEM-03.GWT-N]`` (same convention as the
tooling-baseline tests).
"""

import re
from collections.abc import Collection
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

import phonebridge.domain.contract as contract
from phonebridge.domain.contract import (
    AcceptResult,
    AcquireGranted,
    AcquireQueued,
    AcquireRefused,
    AcquireResult,
    AnswerAndAnnounceFallback,
    AssignmentMode,
    AssignmentRevokedEvent,
    AudioReadyEvent,
    CallLease,
    CallPhase,
    CallStateChangedEvent,
    CallToken,
    DialResult,
    ErrorCode,
    ErrorData,
    ErrorReason,
    EventEnvelope,
    EventName,
    FallbackDecision,
    FallbackTriggeredEvent,
    ForwardViaLineFallback,
    IncomingCallEvent,
    IncomingRingIngress,
    Intent,
    LeaseId,
    LineAssignment,
    LineId,
    LineManager,
    LineState,
    LineStateChangedEvent,
    LineSummary,
    LivenessState,
    MediaToken,
    MissedCallEvent,
    NoPickupFallback,
    PhonebridgeError,
    ProviderCallState,
    ProviderCallStateUpdate,
    ProviderLiveness,
    ProviderRegistration,
    QueueReadyEvent,
    QueueTicket,
    QueueUpdatedEvent,
    RecordAndTelegramFallback,
    RedactedString,
    RejectFallback,
    SessionId,
    TicketId,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
API_CONTRACT_MD = REPO_ROOT / "docs" / "api-contract.md"

NOW_UTC = datetime(2026, 11, 13, 12, 0, 0, tzinfo=timezone.utc)
_RAW_NUMBER = "+37379012345"


def _assignment(**overrides: Any) -> LineAssignment:
    kwargs: dict[str, Any] = {
        "assignment_id": contract.AssignmentId("asg-1"),
        "line_id": LineId("line-main"),
        "owner_agent_id": "agent-1",
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
        caller=RedactedString(_RAW_NUMBER),
        expires_at=NOW_UTC,
    )
    caller = event.caller
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
    assert wire_dict["caller"] == _RAW_NUMBER

    # Log dumps are masked in both helper spellings.
    safe = event.safe_dump()
    assert safe["caller"] == "+373****45"
    assert _RAW_NUMBER not in repr(safe)
    log_dict = event.to_log_dict()
    assert _RAW_NUMBER not in repr(log_dict)


@pytest.mark.parametrize("_gwt", [pytest.param(None, id="ITEM-03.GWT-2")])
def test_validation_error_hides_pii_input(_gwt: object) -> None:
    """hide_input_in_errors: a rejected payload must not echo the raw
    caller number into the ValidationError text."""
    with pytest.raises(ValidationError) as exc_info:
        IncomingCallEvent.model_validate(
            {
                "line_id": "line-main",
                "call_token": "tok-abc",
                "caller": {"number": _RAW_NUMBER},  # invalid type, carries PII
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
        intent=Intent.DIAL_OUT,
        created_at=NOW_UTC,
    )
    with pytest.raises(ValidationError):
        CallLease.model_validate(
            {
                "lease_id": "lease-1",
                "line_id": "line-main",
                "owner_agent_id": "agent-1",
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
        {"granted": True, "line_id": "line-main", "lease_id": "lease-9"}
    )
    assert isinstance(granted, AcquireGranted)
    assert granted.lease_id == LeaseId("lease-9")

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
    assert refused.free_lines == []


def test_call_lease_answer_defaults_provisional() -> None:
    """'active' is PROVISIONAL: default confidence is provisional and
    answered_at is None until an answer is observed."""
    lease = CallLease(
        lease_id=LeaseId("lease-1"),
        line_id=LineId("line-main"),
        owner_agent_id="agent-1",
        intent=Intent.DIAL_OUT,
        created_at=NOW_UTC,
    )
    assert lease.answered_at is None
    assert lease.answer_confidence == "provisional"
    assert lease.phase == CallPhase.DIALING


def test_fallback_policy_union_and_assignment_default() -> None:
    adapter = TypeAdapter(contract.FallbackPolicy)
    assert isinstance(adapter.validate_python({"policy": "no_pickup"}), NoPickupFallback)
    assert isinstance(adapter.validate_python({"policy": "reject"}), RejectFallback)
    fwd = adapter.validate_python({"policy": "forward_via_line", "line_id": "line-b"})
    assert isinstance(fwd, ForwardViaLineFallback)
    assert fwd.line_id == LineId("line-b")
    rec = adapter.validate_python({"policy": "record_and_telegram", "channel": "@ops"})
    assert isinstance(rec, RecordAndTelegramFallback)
    assert rec.channel == "@ops"
    assert isinstance(
        adapter.validate_python({"policy": "answer_and_announce"}),
        AnswerAndAnnounceFallback,
    )
    with pytest.raises(ValidationError):
        adapter.validate_python({"policy": "spill_to_pool"})  # never existed

    # Default is no_pickup (carrier voicemail).
    assert _assignment().fallback_policy.policy == "no_pickup"
    # Round-15 allowlist default must include the NANP composite type
    # or ordinary US/CA mobiles are rejected as UNSUPPORTED_NUMBER_TYPE.
    assert "FIXED_LINE_OR_MOBILE" in _assignment().allowed_number_types
    assert {"FIXED_LINE", "MOBILE", "VOIP", "TOLL_FREE"} <= _assignment().allowed_number_types


def test_event_envelope_and_payloads() -> None:
    payload = IncomingCallEvent(
        line_id=LineId("line-main"),
        call_token=CallToken("tok-1"),
        caller=RedactedString(_RAW_NUMBER),
        expires_at=NOW_UTC,
    )
    envelope = EventEnvelope[IncomingCallEvent](
        name=EventName.INCOMING_CALL, emitted_at=NOW_UTC, payload=payload
    )
    assert envelope.payload.call_token == CallToken("tok-1")

    csc = CallStateChangedEvent(
        lease_id=LeaseId("l-1"),
        line_id=LineId("line-main"),
        phase=CallPhase.ACTIVE,
        reason="provider active",
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
        caller=RedactedString(_RAW_NUMBER),
        policy=NoPickupFallback(),
    )
    lsc = LineStateChangedEvent(
        line_id=LineId("line-main"),
        old_state=LineState.FREE,
        new_state=LineState.BUSY,
    )
    mc = MissedCallEvent(line_id=LineId("line-main"), reason="accept_window_expired")
    for ev in (csc, qr, qu, ar, are, ft, lsc, mc):
        assert ev.model_dump_json()


def test_ingress_payloads_validate() -> None:
    ProviderCallStateUpdate(
        line_id=LineId("line-main"),
        assignment_id=contract.AssignmentId("asg-1"),
        call_token=CallToken("tok-1"),
        state=ProviderCallState.RINGING,
        observed_at=NOW_UTC,
    )
    ProviderLiveness(
        line_id=LineId("line-main"),
        state=LivenessState.UNREACHABLE,
        checked_at=NOW_UTC,
    )
    IncomingRingIngress(
        line_id=LineId("line-main"),
        assignment_id=contract.AssignmentId("asg-1"),
        call_token=CallToken("tok-1"),
        caller=RedactedString(_RAW_NUMBER),
        observed_at=NOW_UTC,
    )
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
    ) -> AcquireResult:
        return AcquireGranted(line_id=LineId("line-main"), lease_id=LeaseId("l-1"))

    async def cancel_acquire(self, session_id: SessionId, ticket_id: TicketId) -> None:
        return None

    async def release(self, session_id: SessionId, lease_id: LeaseId) -> None:
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
        return contract.ClaimResult(claim_id="claim-1")

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

    async def register_provider(self, registration: ProviderRegistration) -> LineAssignment:
        return _assignment()

    async def note_provider_liveness(self, liveness: ProviderLiveness) -> None:
        return None

    async def ingest_ring(self, ring: IncomingRingIngress) -> IncomingCallEvent:
        return IncomingCallEvent(
            line_id=ring.line_id,
            call_token=ring.call_token,
            caller=ring.caller,
            expires_at=NOW_UTC,
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


def test_line_manager_is_abstract() -> None:
    with pytest.raises(TypeError):
        LineManager()  # type: ignore[abstract]


async def test_line_manager_async_surface() -> None:
    """Non-e2e async exercise: EVERY abstract method is awaitable and
    returns its contracted result type (all 18 methods)."""
    assert set(LineManager.__abstractmethods__) == {
        "register_intent",
        "acquire",
        "cancel_acquire",
        "release",
        "dial",
        "accept",
        "hangup",
        "send_dtmf",
        "claim_exclusive",
        "get_lines",
        "register_provider",
        "note_provider_liveness",
        "ingest_ring",
        "ingest_call_state",
        "ingest_audio_ready",
        "decide_fallback",
        "drop_session",
        "tick",
    }
    mgr = _FakeLineManager()

    # --- agent-facing surface ---
    session = await mgr.register_intent("agent-1", {Intent.DIAL_OUT})
    result = await mgr.acquire(session, Intent.DIAL_OUT)
    assert isinstance(result, AcquireGranted)
    assert isinstance(result.lease_id, str)
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
    assignment = await mgr.register_provider(
        ProviderRegistration(
            line_id=LineId("line-main"),
            token=RedactedString("line-secret-token-value"),
            caps=[Intent.DIAL_OUT],
        )
    )
    assert isinstance(assignment, LineAssignment)
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
            call_token=CallToken("tok-2"),
            observed_at=NOW_UTC,
        )
    )
    assert isinstance(ring_event, IncomingCallEvent)
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

    # --- lifecycle hook ---
    await mgr.tick(NOW_UTC)
