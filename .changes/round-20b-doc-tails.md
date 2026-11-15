---
type: docs
scope: docs
user_visible: false
breaking: false
user_summary: >-
  Design and wire docs gain the leftover mechanics from the round-20
  hardening spec: orphan-grace deadline handling, recognized teardown
  reasons, container privilege boundary, and the stack-restart
  reconciliation protocol.
dev_summary: >-
  SA_carbon_review_changes_8 tails: design §5.4 documents
  ProviderBinding.orphan_deadline_at (set on WS drop, cleared on a
  state_snapshot-confirmed re-register, expiry in tick() ends the
  lease as provider_lost); api-contract provider.call_state lists
  recognized teardown reasons dongle_lost / stack_restart /
  provider_lost; design §9 details the container privilege boundary
  (vhci-feeder+tunnel only hold /dev/vhci + CAP_NET_ADMIN;
  pipewire/wireplumber/LineProvider/gateway run as unprivileged user
  phonebridge with shared XDG_RUNTIME_DIR and a session D-Bus + a
  policy granting UID access to org.bluez on the system bus) and the
  GetCalls() reconciliation on bus/name-owner restart.
why: >-
  Verbatim spec details that did not fit the round-20 diff: without
  them M1/M4 implementers would re-invent the orphan-grace deadline
  mechanics and the container UID/bus split.
refs: SA_carbon_review_changes_8 tasks 8-9; issue #47
---

## Spec impact
- none
