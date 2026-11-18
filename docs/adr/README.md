# Architecture Decision Records

This directory holds the project's ADRs — numbered, immutable
records of decisions that are too consequential to live only in
prose inside a design document (cross-module refactors, new
features, breaking contract changes; see `.changes/README.md` for
the rule that requires one).

## Numbering

- ADRs are numbered `NNNN-slug.md` with a four-digit sequence —
  `0004`, `0005`, ... — monotonically increasing, never reused.
- An ADR's status line records `accepted` / `superseded` and links
  the issue or review that motivated it; when a later decision
  replaces an earlier one, the old file stays and points at its
  successor.

## Index

- `0004-hci-agent-runtime.md` — the production hci-agent runtime is
  Rust + libusb (Bumble/pyusb demoted to protocol reference and dev
  tooling; narrow audited `IsoPump` FFI boundary). Issue #89.

Earlier architectural decisions ADR-01/02/03 predate this directory:
they are formal-ish legacy design decisions recorded inside
`docs/phone-bridge-service-design.md` and are not renumbered. The
`ADR-A` / `ADR-B` / `ADR-24.x` identifiers that appear elsewhere in
the docs (e.g. `docs/api-contract.md`, `docs/hci-agent.md`) are
informal design notes from specific review rounds, not ADRs in this
sequence; they stay inline where they were written.
