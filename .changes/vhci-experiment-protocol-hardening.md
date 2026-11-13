---
type: docs
scope: docs
user_visible: false
breaking: false
user_summary: "-"
dev_summary: >-
  Lock hci-agent wire-protocol invariants proven by the one-dongle
  vhci end-to-end experiment: lossless sequenced control channel
  (ctrl_seq; gap/overflow = fatal session reset), agent-side USB
  isochronous reassembly into complete HCI SCO packets (media_seq,
  one packet per datagram), feeder SCO handle gate (open on
  0x2c/0x03 status 0, close before 0x05, <=4-frame unknown-handle
  hold), and media health events SCO_UNEXPECTED_HANDLE /
  SCO_RX_STARVATION / SCO_FRAMING_ERROR.
why: >-
  The live experiment showed silent-audio failure modes (media frames
  outrunning sync-connection events across split channels; raw USB
  isoc buffers not aligned to HCI SCO packet boundaries) that must be
  impossible in the production protocol.
refs: issue #29; dev session 2026-11-13
---

## Spec impact
-
