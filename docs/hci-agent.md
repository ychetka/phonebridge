# hci-agent — dongle-side line agent

Status: design spec (2026-11-13, unified agent model). Not yet implemented.

Every phone line in phonebridge terminates at a **hci-agent**: a small
process that runs on the machine physically owning a Bluetooth USB
dongle — whether that machine is a remote Raspberry Pi, a mini-PC in
another city, or the very host where the gateway container runs. The
model is uniform: **the container never touches USB or the host
Bluetooth subsystem; every adapter it sees is a kernel `vhci` device
fed by an agent.** Location is invisible to the telephony stack.

```
dongle host (any machine)                 container (isolated netns)
┌───────────────────────────┐             ┌────────────────────────────┐
│ USB BT dongle             │             │ vhci-feeder → /dev/vhci    │
│  └ hci-agent              │  tunnel     │        └─ hciN             │
│     owns USB (libusb,     │─control-TCP─►            │              │
│     btusb detached)       │─media-UDP──►│   bluetoothd → PipeWire    │
│     relays HCI frames     │             │   → per-line worker        │
└───────────────────────────┘             └────────────────────────────┘
     paired phone nearby
```

## §1. Responsibilities

The agent is deliberately *dumb* — it owns hardware and moves frames:

- **USB ownership** — binds the dongle in userspace via libusb/pyusb
  after detaching the kernel `btusb` driver, so no `hci0` is created on
  the agent host at all (zero interference with a host `bluetoothd`,
  which may keep serving the host's own devices). Base implementation:
  the USB transport of Google Bumble.
- **HCI relay** — forwards HCI frames between the dongle and the
  container: commands/ACL/events on the **control channel** (TCP), SCO
  audio on the **media channel** (UDP). USB carries each class on its
  own endpoint (control EP = commands, interrupt EP = events, bulk EP =
  ACL, isochronous EP = SCO), so the agent tags each frame with the H4
  type byte implied by its endpoint — no protocol interpretation
  beyond that mapping.
- **Liveness** — heartbeat over the control channel; probes the health
  of its own USB/HCI path and reports `DONGLE_DEAD` explicitly.
- **Local reset** — on command (and/or by policy), power-cycles the
  dongle's USB port (`uhubctl`/`usbreset`), re-attaches, resumes.

It does **not** run bluetoothd, does not pair phones itself, and holds
no telephony state — the container-side `bluetoothd` performs pairing
through the virtual adapter exactly as it would for a local dongle.

## §2. Wire protocol (V1)

Two channels per line, both always dialing *into* the container's
per-line listener (NAT-safe, consistent with the provider model):

### Control channel — TCP

Length-prefixed frames: `uint8 kind · uint16 BE len · payload`.

| kind | direction | payload |
|---|---|---|
| `0x01` HCI_PASSTHRU | both | HCI packet prefixed with its H4 type byte (`0x01` cmd / `0x02` ACL / `0x04` evt); the byte is synthesized from the USB endpoint class |
| `0x10` HELLO | agent→ | `{line_id, token, agent_version, dongle:{adapter_mac, usb_path}, caps}` — `token` is the single-use enrollment credential from `agent_token_file`, sent inside the authenticated tunnel only |
| `0x11` HELLO_ACK | container→ | `{accepted, line_id, heartbeat_interval_ms, server_time}` or reject+close |
| `0x12` HEARTBEAT | agent→ | `{seq, usb_ok, hci_ok}` every `heartbeat_interval_ms` |
| `0x13` RESET | container→ | `{}` — agent power-cycles the dongle, then re-enumerates |
| `0x14` STATUS | agent→ | `{state: UP|DEGRADED|DONGLE_DEAD|TUNNEL_FLAP, detail}` |
| `0x15` SHUTDOWN | both | graceful close |

Control traffic is small and latency-critical: it never shares a queue
with media. Missed N heartbeats → line `DEGRADED` → `DOWN`.

### Media channel — UDP

`uint8 kind=0x03 (SCO) · uint32 BE seq · uint32 BE ts_ms · SCO payload`
— one HCI SCO packet per datagram. Stale frames MAY be dropped; late
audio is worse than missing audio (isochronous semantics preserved on
purpose — this is why media is not TCP).

### H4 type split

`0x01` command, `0x02` ACL, `0x04` event → control-TCP.
`0x03` SCO → media-UDP. On the USB side the class is implied by the
endpoint (no literal H4 byte exists on USB); the agent adds/strips it
when translating to the wire framing, and the container's `vhci`
adapter consumes the same typed frames.

## §3. Tunnel profiles (`transport_profile`)

The wire protocol above is **invariant**; how it reaches the container
is a per-line administrative choice (geo-blocking resilience). The
admin picks *which* profile; the *parameters* of each profile are
canonical, shipped by the system, and not user-editable.

| profile | carriage | status |
|---|---|---|
| `wireguard` | system-managed `wg0` peer; auth = peer pubkey ↔ line_id | V1 default |
| `direct_mtls` | plain TCP/UDP + mutual TLS; cert CN ↔ line_id | V1 (LAN/VPN-ready nets) |
| `sstp` | reserved profile — server pack shipped by system | future |
| `openconnect` | reserved profile | future |
| `vless_xray` | reserved profile | future |

Interface-creating profiles (wireguard, sstp, openconnect, vless_xray)
give the agent an L3 path; the two channels then run inside it
unmodified. `direct_mtls` wraps the TCP control channel in TLS and the
UDP media channel in DTLS. Line identity is always bound to the tunnel
credential — an agent cannot feed a line it was not provisioned for.

## §4. Agent configuration file

Single YAML file (suggested path `/etc/phonebridge-agent/agent.yaml`,
or passed as `--config`). One file per dongle; N dongles on one host =
N agent instances.

```yaml
# phonebridge hci-agent configuration — example
line_id: "line-kitchen"            # assigned at line onboarding (admin)
agent_token_file: "/etc/phonebridge-agent/agent.token"
                                   # per-line bootstrap credential,
                                   # sent once in HELLO (see §2)

dongle:
  match:
    adapter_mac: "A8:9C:ED:C9:E0:FD"   # primary selector — BDADDR
    usb_path: "1-1.4"                  # optional secondary selector —
                                       # physical USB port path
  detach_btusb: true                 # userspace ownership (recommended)
  reset:
    on_start: false
    on_watchdog: true                # power-cycle on DONGLE_DEAD
    command: "uhubctl -l 1-1 -p 4 -a cycle"   # host-specific, optional
                                             # (port 4 of hub 1-1 = usb_path 1-1.4)

transport:
  profile: "wireguard"             # see §3 — admin chooses WHICH;
                                   # canonical params below are
                                   # system-shipped, not user-tuned
  endpoint: "phonebridge.example.net"  # gateway address
  wireguard:
    private_key_file: "/etc/phonebridge-agent/wg0.key"
    peer_public_key: "<gateway pubkey>"   # provisioned at onboarding
    allowed_ips: ["10.99.0.0/24"]
    endpoint_port: 51820
    persistent_keepalive_s: 25
  # direct_mtls:
  #   ca_file: /etc/phonebridge-agent/ca.pem
  #   cert_file: /etc/phonebridge-agent/agent.pem
  #   key_file: /etc/phonebridge-agent/agent.key

channels:                          # container-side endpoint pair for
  control: { proto: tcp, port: 7300 }   # THIS line, issued at
  media:   { proto: udp, port: 7301 }   # onboarding — canonical
                                      # allocation: one pair per line
                                      # (7300/7301 shown for line 1)

heartbeat:
  interval_ms: 2000                # canonical default
  missed_limit: 3                  # → agent reports tunnel as dead
reconnect:
  backoff_ms: [500, 30000]
  jitter_pct: 20

logging:
  level: "info"
  mask_pii: true                   # never log dialed numbers / IDs
```

Field rules:

- `line_id` + `agent_token` bind the agent to exactly one line at the
  container; mismatch → `HELLO` rejected, connection closed.
- `dongle.match.adapter_mac` is REQUIRED (hciN-style indices are
  unstable; `usb_path` may *additionally* pin the physical port so a
  second identical dongle cannot be claimed by mistake).
- Everything under `transport.<profile>` except file paths and the
  endpoint address is canonical — the system ships per-profile config
  packs; admins do not tune protocol internals.

## §5. Lifecycle

`PROVISIONING` (admin created line + issued token/keys) → `PAIRING`
(agent connected, dongle attached, adapter materialized, phone pairing
via container bluetoothd) → `READY` → `IN_CALL` → `DEGRADED`
(heartbeat/USB issues) → `FAULTED` (manual or watchdog teardown) →
back to `READY` on recovery. Container-side teardown of a line:
destroy the vhci adapter → bluetoothd emits `InterfacesRemoved` →
per-line worker FSM reaps cleanly — the same deterministic path for
every failure source, because a virtual adapter never wedges the kernel.

## §6. Security notes

- Bootstrap tokens are single-use enrollment credentials; after
  `HELLO_ACK` the tunnel credential (WG peer key or mTLS cert) is the
  line identity.
- PII hygiene: the agent must not log dialed numbers, caller IDs, or
  HCI payloads containing them (`mask_pii` default on).
- The agent binds to loopback/WAN only via the tunnel profile; it does
  not expose any local listener.
