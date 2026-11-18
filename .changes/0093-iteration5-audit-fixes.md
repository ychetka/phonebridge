---
type: docs
scope: docs
user_visible: false
breaking: false
user_summary: "-"
dev_summary: >-
  Issue #93 iteration-5 external architecture audit fixes
  implemented (docs/diagram only, 4 Major findings, all
  accepted): the internal.sock named-UID ACL model is replaced
  by a dedicated-group model — a 0600 socket zeroes POSIX
  ACL_MASK so named-UID entries are masked out at connect();
  internal.sock now lives at 0660
  gateway-uid:phonebridge-internal on a NEW dedicated mount
  /run/phonebridge-internal/ (0750, same owner/group) where
  phonebridge-internal is an empty-population group holding
  ONLY the FallbackWorker uid, and the supervisor's
  setgroups(0,NULL) reset now re-adds ONLY role-required
  groups (FallbackWorker: [phonebridge-internal]; all other
  children: zero) — admin.sock is unchanged; system:*
  dispatch is unified on the ordered writer
  (KERNEL->WRITER->internal.sock->FB, kernel keeps
  fallback_accept_ttl_sec + bounded ack-retry) and the diagram
  gains WRITER->INT; stack restart gains a cascade re-init —
  kernel reconciliation emits re-init items through the
  ordered writer (Agent1 re-registers on the fresh org.bluez,
  bundle supervisors re-attach LineProvider/vhci-feeder D-Bus
  watches, phonebridge_uplink/downlink virtual PipeWire nodes
  re-created, line_id-to-hciN map re-verified) and the diagram
  gains STACKSUP->SUP; hci-agent btusb restore is scoped to
  remote/dedicated hosts only — on the co-located loopback
  profile the agent MUST NOT re-bind btusb (a physical hciN in
  the shared init_net would be adopted by the container's
  bluetoothd), so loopback leaves btusb unbound and the
  deployment keeps the physical MAC unmanaged.
why: >-
  The iteration-5 audit found the named-UID ACL socket model
  mechanically broken (mode-0600 zeroes ACL_MASK), a dispatch
  path for system:* commands that bypassed the ordered writer
  and could race SQLite write-through, a missing re-init
  cascade when the stack supervisor restarts shared daemons,
  and a btusb-restore defect that lets a co-located agent
  register a physical hciN in the shared init_net the
  container bluetoothd can adopt; the accepted resolutions
  replace the ACL model with a dedicated group, route all
  system:* dispatch and re-init items through the ordered
  writer, and scope btusb restore to remote hosts — without
  touching implementation code.
refs: issue #93
---

## Spec impact
- adds: docs/specs/line-manager.md#requirement-iteration-5-audit-dispatch-and-reinit-round-37
