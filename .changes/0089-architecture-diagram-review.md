---
type: docs
scope: docs
user_visible: false
breaking: false
user_summary: "-"
dev_summary: >-
  Issue #89 architecture-diagram review implemented (docs/diagram
  fixes only): new canonical Mermaid source
  docs/architecture-block-diagram.mmd with refreshed SVG/PNG in a
  light palette — FallbackWorker/InternalService/EventBus edges,
  line-bundle supervisor over LineProvider + vhci-feeder with
  bundle IPC, kernel->MediaRouter control-plane pairing edge,
  ordered writer task in front of SQLite, persistent volumes,
  BlueZ Agent1/admin.pair_window, corrected media path
  (MediaRouter->LineProvider->PipeWire->bluetoothd->phone) and a
  transport_profile-labelled HCI handshake (HELLO/HELLO_ACK);
  new ADR-0004 under docs/adr fixes the production hci-agent runtime as Rust +
  libusb (Bumble/pyusb demoted to protocol reference and dev
  tooling, narrow audited IsoPump FFI boundary); hci-agent.md
  gains the Rust runtime subsection, a feeder-side ingress
  jitter/playout/PLC buffer for remote media UDP, and explicit
  per-line channel-pair provisioning; design doc clarifies
  MediaRouter placement (in-process dedicated media worker, no
  kernel lock, no SQLite I/O), bounded-best-effort wake delivery,
  phonebridge doctor session-stack checks (XDG_RUNTIME_DIR,
  session D-Bus, PipeWire/WirePlumber), provider-owned DTMF
  mechanism choice (verified SendTones/AT+VTS vs in-band
  synthesis), and a post-MVP cellular health telemetry note;
  api-contract.md adds the DTMF-ownership clarification and pins
  WS binary PCM as the V1 media baseline.
why: >-
  The issue #89 architecture review found topological gaps on the
  block diagram and flagged pyusb isochronous SCO as a production
  risk; the accepted resolutions correct the diagram to match the
  normative docs, pin the agent runtime decision in an ADR, and
  disambiguate DTMF ownership, wake guarantees and doctor
  pre-flight scope.
refs: issue #89
---

## Spec impact
- modifies: docs/specs/line-manager.md#requirement-line-lifecycle-admin-operations-round-28
