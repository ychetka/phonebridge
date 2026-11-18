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

Earlier architectural decisions ADR-01/02/03 predate this directory
and live as the recorded decision history inside
`docs/phone-bridge-service-design.md`; they are not renumbered.
