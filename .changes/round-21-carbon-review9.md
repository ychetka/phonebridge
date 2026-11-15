---
type: refactor
scope: daemon
user_visible: false
breaking: false
user_summary: "-"
dev_summary: >-
  Round-21 architect review (SA_carbon_review_9): ProviderCommand
  brought to line.* wire parity (PROVIDER_COMMAND_WIRE_NAMES +
  drift test, media_endpoint/media_token on dial/answer, mask_clir
  removed), two independent per-role media tokens per lease
  (MediaCredentials), state_snapshot orphan-grace input +
  LivenessState.WS_DISCONNECTED, closed reason enums, frozen DTOs +
  EventAudience routing, ClaimId/PendingClaim + admin list reads,
  Clock/MediaAllocator ports; docs synced (SCO altsetting per BT
  Core Vol 4 Part B, deterministic external-call rule, RFC-3339
  wire-time convention, media frame lengths). Review follow-up:
  CallEndReason is now wired into the event payloads
  (call_state_changed/missed_call/line_state_changed, incl. the
  external_call member) and ProviderLiveness carries
  state_snapshot for provider.ping.
why: >-
  The review found the provider command models had drifted from the
  documented line.* wire contract (missing lease_id/media
  credentials, a nonexistent mask_clir field) and that a single
  media_token let any holder claim either role in media.auth; this
  PR makes the domain contract 1:1 with the wire spec before M1.
refs: issue #51
---

## Spec impact
- none
