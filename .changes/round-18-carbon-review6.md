---
type: fix
scope: contract
user_visible: true
breaking: false
user_summary: >-
  Media socket clarified as WebSocket-over-UDS (message boundaries
  for flush/keepalive frames); hci-agent gains MEDIA_PING endpoint
  binding + WireGuard /32 peer isolation; RedactedString masking now
  covers set/frozenset containers.
dev_summary: >-
  Architect review round 18 (SA_carbon_review6): _mask_for_log
  handles set/frozenset; api-contract documents WS-over-UDS framing
  for the local media endpoint; hci-agent.md adds MEDIA_PING
  (kind=0x04) uplink endpoint binding + server-side /32 peer
  isolation; design §12 vhci question marked RESOLVED; api-contract
  header updated to rounds 1-18.
why: >-
  Raw SOCK_STREAM on the local media socket has no message
  boundaries — an 8-byte flush frame would desync the 648-byte
  parser; UDP is connectionless so the feeder cannot return uplink
  audio until it learns the agent source address; _mask_for_log
  missed set/frozenset (allowed_number_types is a frozenset).
refs: SA_carbon_review6; dev session 2026-11-13
---

## Spec impact
-
