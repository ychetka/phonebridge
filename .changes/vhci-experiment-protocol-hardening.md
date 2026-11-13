## Documentation

- `docs/hci-agent.md` §2 — protocol hardening locked by the one-dongle
  vhci end-to-end experiment (issue #29): lossless sequenced control
  channel (`ctrl_seq`, gap/overflow = fatal session reset), agent-side
  USB isochronous reassembly into complete HCI SCO packets
  (`media_seq`, one packet per datagram), feeder SCO handle gate
  (open on `0x2c`/`0x03` status 0, close before `0x05`, ≤4-frame
  unknown-handle hold), and loud media health events
  (`SCO_UNEXPECTED_HANDLE`, `SCO_RX_STARVATION`, `SCO_FRAMING_ERROR`).
- `docs/phone-bridge-service-design.md` — decision log round 13.
