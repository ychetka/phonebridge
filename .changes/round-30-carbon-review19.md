---
type: fix
scope: domain
user_visible: false
breaking: false
user_summary: "-"
dev_summary: >-
  Round-30 blind paranoid audit fixes (SA_carbon_review_19, four
  fix batches across contract/spec/design/hci docs): domain
  contract gains the ingress/hydration/rollback surface -
  ingest_media_state(lease_id, role, attached) pairs live
  lease_id + media role under a media_token, hydrate_lines and
  restore_line close the write-through persistence loop
  (config_stale + single-transaction lines+line_assignments),
  ProviderRegisterResult carries pending_rings + channel_seq so
  closed-assignment rings redeliver and socket generations are
  distinguishable; the outbox becomes a two-level bounded model
  (OUTBOX_SOFT_CAP pre-flight + OUTBOX_HARD_CAP safety bound);
  liveness hardening - unreachable is terminal at the mutation,
  unbound is a listing-only listing state, dedup keys re-scope
  to the LINE, and the advisory-stamp watermark is a monotone
  max over (line_id, fact_kind); ring-drain burned-token edges
  and snapshot ringing ambiguity resolved deterministically;
  cancel_acquire verifies ticket ownership; saga compensation
  hangs up before releasing resources; heartbeat_interval_ms
  bounded by MAX_HEARTBEAT_INTERVAL_MS; admin listing surfaces
  paginated. Wire/docs: api-contract adds the admin.* method
  table, ping gains a state field, and the media protocol pins
  0x02 flush (agent barge-in marker + watermark), 0x04 overflow,
  media.auth single-consume token, and force-close semantics;
  phone-bridge-service-design pins SSRF redirect policy,
  media-file quotas (max_recording_sec, per-line caps, ENOSPC
  isolation), the single mutation lock covering kernel call +
  drains + bus.publish on every adapter path, forward_via_line
  second-leg FSM, answer_and_announce WAV-only + drain deadline,
  record_and_telegram token storage + dead-letter, state-file
  call-id binding + teardown + staleness, dongle_lost sibling-
  liveness ordering, PII coverage for hook streams +
  DIAL_REJECTED.detail + phonebridgectl, remove_line
  INVALID_STATE on forward references, exec-hook
  latest-per-(hook,line) coalescing, host-shared vhci.lock.
  docs/hci-agent.md gets a transport overhaul: ctrl_seq init
  0/mod-2^32 + empty-payload rule, HELLO_ACK reject schema +
  HEARTBEAT_ACK, STATUS agent-only + LINK_DEGRADED, SCO_STATE
  sourced from 0x2c only, real altsetting geometry +
  SCO_ALT_UNSUPPORTED + alt-0 on pipeline death, HELLO
  timeout/preemption, symmetric session_nonce + media_seq
  rebind rule, isoc pacing + drift compensation + silence fill
  per codec, vhci flock deadline + HCI_VENDOR_PKT index
  discovery + proxied HCI_Read_Buffer_Size + frame validation +
  BD_ADDR verification + handle-matched SCO disconnect, a
  loopback transport profile with SO_PEERCRED, direct_mtls
  DTLS-CID rebind handling, weak-host bind enforcement,
  usb_path-primary matching + adapter_mac pin, bounded USB
  reset, Agent1 pairing semantics, mandatory mask_pii. Tests:
  fake-kernel conformance for the new ingress/admin surface,
  liveness 25-pair matrix, enum dicts, ERROR_CODE_WIRE pin,
  bounds, RedactedString, apply_patch, phase validators,
  pagination.
why: >-
  The carbon paranoid audit (SA_carbon_review_19, slice B2a +
  swe-2 registry) found the dominant defect class again was
  normative semantics living only in prose plus real protocol
  gaps: the persistence write-through had no documented
  compensation or batch hydration, media attach/detach had no
  kernel fact at all, the outbox had a soft cap but no hard
  bound, the media/flush/overflow wire names were asserted in
  design docs but never pinned in the api contract, and the
  hci-agent transport spec had silently drifted from the
  implementation-facing details (frame invariants, altsetting
  numbers, index discovery, pairing semantics, PII masking).
refs: issue #75, SA_carbon_review_19
---

## Spec impact
- modifies: docs/specs/line-manager.md#requirement-outbox-soft-cap-pre-flight-and-drain-discipline
- modifies: docs/specs/line-manager.md#requirement-saga-compensation-on-linedial-and-lineanswer
- modifies: docs/specs/line-manager.md#requirement-ring-drain-after-a-fallback-decision-round-25
- modifies: docs/specs/line-manager.md#requirement-media-credentials-are-split-per-role-and-reissuable-round-25
- modifies: docs/specs/line-manager.md#requirement-ring-correlation-is-by-line-not-by-token-round-25
- modifies: docs/specs/line-manager.md#requirement-queue-sequence-is-minted-at-acquire-ingress-round-25
- modifies: docs/specs/line-manager.md#requirement-effective-timeouts-hydrate-the-kernel-dtos
- modifies: docs/specs/line-manager.md#requirement-heartbeat-liveness-ladder-is-elapsed-time-and-kernel-computed-round-28
- modifies: docs/specs/line-manager.md#requirement-provider-side-stamps-are-advisory-only-round-28
- modifies: docs/specs/line-manager.md#requirement-reregister-supersedes-a-live-binding-register-does-not-round-28
- modifies: docs/specs/line-manager.md#requirement-line-lifecycle-admin-operations-round-28
- modifies: docs/specs/line-manager.md#requirement-the-0x02-flush-watermark-rides-the-agent-stream-clock-round-28
