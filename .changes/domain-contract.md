---
type: feat
scope: domain
user_visible: false
breaking: false
user_summary: "-"
dev_summary: >-
  Domain contract module (src/phonebridge/domain/contract.py): wire
  enums, identifier NewTypes, RedactedString, FallbackPolicy union,
  LineAssignment/CallLease/QueueTicket records, AcquireResult union,
  event + provider-ingress payloads, PhonebridgeError, and the
  abstract async LineManager kernel - types only, no implementation.
why: >-
  Roadmap item_03 (issue #7): freeze the typed domain surface ahead of
  the M1 in-memory LineManager so implementation, JSON-RPC adapters
  and tests all build against one contract.
refs: issue #7, task item_03
---

## Spec impact
-

## Contract surface summary

- Enums (verbatim wire spellings): `Intent`, `LineState`, `CallPhase`,
  `ProviderCallState` (bounded provider vocabulary), `AssignmentMode`,
  `LivenessState`, `ErrorCode`, `EventName`.
- Identifier NewTypes: `AssignmentId`, `CallToken`, `LeaseId`,
  `MediaToken`, `LineId`, `TicketId`, `SessionId` - the contract owns
  type + guarantees (globally unique, opaque, unguessable; tickets
  orderable/monotonic for FIFO), not the generation strategy.
- `RedactedString`: a `str` subclass - masked `repr`/`str`, raw value
  on wire via `.expose()`/`.to_wire()` and JSON serialization;
  `ContractModel` adds `hide_input_in_errors` + `safe_dump()`/
  `to_log_dict()`; raw `model_dump()` to logs is prohibited.
- Records: `LineAssignment` (durable), `CallLease` (ephemeral,
  `lease_id` sole identifier), `QueueTicket` (monotonic `seq` = FIFO),
  `LineSummary`, `FallbackPolicy` (5-way discriminated union),
  `AcquireResult` (granted/queued/refused tagged union), event
  payloads under a generic `EventEnvelope`, provider ingress payloads
  (`ProviderRegistration`, `ProviderLiveness`,
  `ProviderCallStateUpdate`, `IncomingRingIngress`, `FallbackDecision`).
- `PhonebridgeError(code, data)`: `code` is a verbatim `ErrorCode`;
  `data.reason` is a typed `ErrorReason` sub-code for operational
  detail, `data.free_lines` carries the exclusive-conflict offers.
- `LineManager` (ABC, all-async): register_intent, acquire,
  cancel_acquire, release, dial, accept, hangup, send_dtmf,
  claim_exclusive, get_lines + narrowed provider ingress
  (register_provider, note_provider_liveness, ingest_ring,
  ingest_call_state, decide_fallback) + ONE generic `tick(now)`
  lifecycle hook for time-driven housekeeping (accept-window expiry,
  queue grant-TTL advance, DEGRADED->DOWN). Docstrings carry the
  idempotent-or-duplicate-reject and single-event-loop atomicity
  invariants.

## Deferred error-code candidates (next contract round)

The wire document defines exactly seven codes; `ErrorCode` adds none.
Operational detail travels in `ErrorData.reason` (enum `ErrorReason`),
whose members are the leading candidates for promotion to full codes
in the next contract round:

- `ACCEPT_WINDOW_EXPIRED` (today: `STALE_TOKEN` + reason)
- `QUEUE_FULL` (today: `STALE_TOKEN`/`PhonebridgeError` + reason)
- `TICKET_NOT_FOUND` (today: `STALE_TOKEN` + reason)
- `INVALID_CALL_STATE` (today: `STALE_TOKEN` + reason)
- `LINE_NOT_CLAIMABLE` (today: `LINE_EXCLUSIVE_NOT_OWNER` + reason)
- `MEDIA_AUTH_FAILED` (today: transport-layer close; not yet an
  agent-facing code)

## Provisional 'active' assumption

`CallPhase.ACTIVE` is modeled as PROVISIONAL: the §5.5 open spike has
not proven whether oFono `State=active` equals remote answer or merely
cellular setup. `CallLease` therefore carries
`answered_at: aware datetime | None` and
`answer_confidence: Literal["provisional", "confirmed"]`
(default `"provisional"`). A later PLAN_DELTA may redefine ACTIVE
once the spike resolves.
