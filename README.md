# phonebridge

Standalone Bluetooth telephony bridge service (oFono/PipeWire over
D-Bus). Agents place and receive calls, send and receive SMS, and
stream call audio through a JSON-RPC + PCM agent API exposed over a
Unix socket.

phonebridge is extracted from devin-telegram, which becomes a client
of this service.

**Status:** bootstrap - canonical repository skeleton only, no service
code yet. See `docs/phone-bridge-service-design.md` for the founding
specification.
