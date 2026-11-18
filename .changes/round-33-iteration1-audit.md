---
# Iteration-1 architecture audit fixes (v0.0.28) - issue #81.
type: fix
scope: domain
user_visible: true
breaking: false
user_summary: "The provider command handshake is now a first-class,
  deadline-bounded kernel fact: call.dial/call.accept replies wait for
  the bounded line.dial/line.answer acknowledgement and answer
  PROVIDER_UNREACHABLE on refusal or timeout, rings carry an
  answer_pending state that never re-enters the fallback decision,
  unqualified provider call updates need positive identity evidence
  before binding to a live lease, zero-subscriber rings emit a typed
  wake-audience envelope to configured hooks, provider stamp watermarks
  are durable across restarts, and every listing accepts the opaque
  cursor input instead of offset. Review fixes additionally pin that
  the wake-audience incoming_call envelope carries a dedicated
  tokenless wake payload with no call_token, that pending-command
  teardown clears its deadline atomically and a late acknowledgement
  is stale, that a same-key retry of an in-flight request joins the
  original operation instead of re-dispatching, that an answer
  acknowledgement never carries provider_call_id, and that the
  accepted country vocabulary is the phonenumbers-supported region
  set (including telephony extensions like XK, AC and TA), not
  strict ISO 3166-1."
dev_summary: ProviderCommandAck + ingest_command_ack + pending_command/
  provider_command_ack_deadline_at + provider_command_ack_sec +
  WakeAudience + durable binding watermarks + caller_id positive
  evidence + adapter-owned cursor + media_epoch + provisioning
  validation hardening + version-tag/spec-anchor tool fixes. PR #82
  review round: IncomingCallWakeEvent tokenless wake projection +
  EventEnvelope audience/payload split enforcement, dial-only
  provider_call_id on ProviderCommandAck (answer acks reject any
  provider handle, reason stays refusal-only), atomic
  pending_command/provider_command_ack_deadline_at teardown on every
  DIALING/ANSWERING exit path with stale-ack no-resurrection
  semantics and lease- vs ring-scoped teardown hangups, in-flight
  idempotency join-before-dispatch semantics, and corrected
  phonenumbers-region vocabulary for country fields (XK/AC/TA
  accepted, ZZ/AA still rejected).
why: Iteration-1 architecture audit (gemini-4-carbon, 33 findings)
  confirmed contract drift across the ack handshake, ring
  correlation, watermark persistence, wake fan-out, pagination, and
  release tooling; the fix re-freezes the contract at 0.0.28 with
  executable GWT coverage rather than implementation-only patches.
refs: issue #81, .audit_exchange/iteration-1-v0.0.27/findings_registry.md
---

## Spec impact
- adds: docs/specs/line-manager.md#requirement-provider-command-acknowledgements-are-kernel-facts-round-33
- adds: docs/specs/line-manager.md#requirement-answer-pending-rings-suppress-the-fallback-decision-round-33
- adds: docs/specs/line-manager.md#requirement-unqualified-provider-state-attribution-needs-positive-evidence-round-33
- adds: docs/specs/line-manager.md#requirement-provider-stamp-watermarks-are-durable-and-line-scoped-round-33
- adds: docs/specs/line-manager.md#requirement-zero-subscriber-inbound-routing-uses-the-wake-audience-round-33
- adds: docs/specs/line-manager.md#requirement-listing-cursors-are-adapter-owned-and-fail-closed-round-33
- modifies: docs/specs/line-manager.md#requirement-saga-compensation-on-linedial-and-lineanswer
- modifies: docs/specs/line-manager.md#requirement-queue-sequence-is-minted-at-acquire-ingress-round-25
- modifies: docs/specs/line-manager.md#requirement-answer-compensation-is-ring-scoped-round-32-item-9
- modifies: docs/specs/line-manager.md#requirement-provider-side-stamps-are-advisory-only-round-28
- modifies: docs/specs/line-manager.md#requirement-reregister-supersedes-a-live-binding-register-does-not-round-28
- modifies: docs/specs/line-manager.md#requirement-accept-window-always-elapses-before-fallback
- modifies: docs/specs/line-manager.md#requirement-effective-timeouts-hydrate-the-kernel-dtos
- modifies: docs/specs/line-manager.md#requirement-exclusive-line-privacy-is-filtered-at-the-edge-round-25-routing-fixed-round-27
- modifies: docs/specs/line-manager.md#requirement-call-lease-phases-and-the-grant-ttl-round-31
- modifies: docs/specs/line-manager.md#requirement-tick-runs-the-orphan-cascade-atomically-round-31
- modifies: docs/specs/line-manager.md#requirement-dial-target-and-caps-validation-is-model-level
