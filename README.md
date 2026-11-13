# phonebridge

Standalone Bluetooth telephony bridge service (oFono/PipeWire over
D-Bus). AI agents place and receive phone calls and stream call audio
through a JSON-RPC + packetized-PCM agent API (UDS/WSS/MCP). SMS is
out of scope (not exposed by the stack).

Architecture: every BT dongle sits behind a dumb `hci-agent`
(USB↔HCI relay) that feeds a kernel `vhci` adapter inside a single
Docker container over a pluggable tunnel profile (wireguard by
default). See `docs/phone-bridge-service-design.md` for the
consolidated spec, `docs/api-contract.md` for the wire contract, and
`docs/hci-agent.md` for the agent config/protocol.

phonebridge is extracted from devin-telegram, which becomes a client
of this service.

**Status:** early implementation — domain contract + tooling baseline
merged; see `.devin/roadmap.md` for the milestone plan.
