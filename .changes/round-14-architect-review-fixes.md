---
type: fix
scope: contract
user_visible: true
breaking: true
user_summary: >-
  Provider call-state wire vocabulary changed to the canonical set
  (ringing|dialing|alerting|active|disconnected); line.dial/line.answer
  now carry per-lease media_endpoint+media_token; new provider.audio_ready
  notification; call.acquire restricted to dial_out.
dev_summary: >-
  Architect review round 14 (issue #31): per-lease media credentials
  for the provider on line.dial/line.answer; canonical
  ProviderCallState vocabulary in domain contract + api-contract;
  provider.audio_ready feeds event.audio_ready; call.acquire limited
  to dial_out; MediaRouter pair TTL + bounded queues; fallback_call_token
  mechanics for FallbackWorker; adapter_mac added to LineProviderConfig;
  HTML diagrams clarified.
why: >-
  Second architect review found wire-level edge cases that would break
  in M1-M3: provider could not auth media per lease, coarse call-state
  vocabulary lost alerting (early-media gating), fallback worker could
  not accept after accept-window expiry (STALE_TOKEN/OWNER_MISMATCH),
  and MediaRouter had no resource bounds.
refs: issue #31; SA_argon_review2; dev session 2026-11-13
---

## Spec impact
-
