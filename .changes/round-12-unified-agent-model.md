---
type: docs
scope: docs
user_visible: false
breaking: false
user_summary: "-"
dev_summary: >-
  Record round-12 architecture decisions: unified hci-agent model
  (kernel vhci fed by dumb USB-HCI relay agents over transport_profile
  tunnels: control-TCP + media-UDP), thin-slice per-line worker
  bundles, strict E.164+ISO dial validation with DIAL_REJECTED and
  per-line number-type allowlist, MCP adapter for the agent surface,
  live-probe results; new docs/hci-agent.md agent spec.
why: >-
  Live probes (Answer/Reject/SendTones/WirePlumber echo loop) and the
  USB-passthrough blast-radius analysis invalidated the --device and
  remote-provider deployment sketches; the wire contract now carries
  DIAL_REJECTED so the domain enum is amended in sync.
refs: issue #22, #25; dev session 2026-11-13
---

## Spec impact
-
