# Project conventions for Claude

`gst-plugin-iceoryx2` is a Rust GStreamer plugin (`iceoryx2sink` / `iceoryx2src`) shipped as a
maturin wheel. One cdylib is both a GStreamer plugin and a pyo3 module. The wheel also ships a
pure-Python, GStreamer-free SDK (`gst_iceoryx2.video`) for subscribing to / publishing the wire
format without a pipeline. See `SPEC.md` for the wire-format/element contract, `TODO.md` for phase
status, and `README.md` for the overview.

## Source of truth

- **`SPEC.md`** is canonical for the `VideoFrameHeader` layout, the iceoryx2 service/QoS, and the
  elements' properties + lifecycle. Change it in the same commit as any layout/behaviour change.
- The `VideoFrameHeader` layout is asserted from both Rust (`cargo test`) and Python
  (`test_header_equivalence`, which pins the `gst_iceoryx2.video` ctypes mirror against the
  Rust-exported constants). Never let the two drift.

## Commands

- Always use `uv run python` — never bare `python`/`python3`.
- Prefer `rg`/`fd` over `grep`/`find`; one simple command per step, no chaining/echoing exit codes.
- Build: `make develop` (maturin into the uv env). Tests: `make test` (unit, no IPC) /
  `make test-integration` (real pipeline + iceoryx2). Lint: `make lint`.
- Python tests live in `python/gst_iceoryx2_tests/`. No import side effects in the package;
  importing `gst_iceoryx2` (or `gst_iceoryx2.video`) must never load the compiled `.so` —
  `setup_gstreamer()` is the only thing that touches GStreamer, and only when called.

## Style

- British English, declarative, no fluff. Backticks for identifiers; em dashes for inline asides.
- Keep `README.md` and `SPEC.md` accurate when components change — a new reader should orient
  themselves from them alone.

## Layering rule

The compiled `.so` links libgstreamer, so loading it needs a GStreamer runtime. Keep the
`gst_iceoryx2.video` SDK pure-Python (ctypes + iceoryx2 + lazy numpy) — it must **never** import
`_gst_iceoryx2`, so a consumer can subscribe with no GStreamer installed. The plugin elements own
no naming policy: `service`/QoS are element **properties** the embedding application supplies.
