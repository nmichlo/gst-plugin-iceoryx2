# Project conventions for Claude

`gst-plugin-iceoryx2` is a Rust GStreamer plugin (`iceoryx2sink` / `iceoryx2src`) shipped as a
maturin wheel. It is a **Cargo workspace** of two published crates:

- **`gst-plugin-iceoryx2-video`** (`crates/gst-plugin-iceoryx2-video/`) — the GStreamer-free core: the
  `VideoFrameHeader` wire struct, the aux-blob codec, geometry validation, and the
  `VideoFramePublisher`/`VideoFrameSubscriber` SDK. No GStreamer, no pyo3. This is the one Rust
  implementation of the wire contract + transport, also published to crates.io as a standalone SDK.
- **`gst-plugin-iceoryx2`** (`crates/gst-plugin-iceoryx2/`) — the GStreamer adapter (the elements +
  zero-copy buffer pool) built on the core. A **pure** GStreamer plugin (no pyo3); maturin's cffi
  binding ships the cdylib in the wheel under `gst_iceoryx2/_native/`.

The wheel also ships a pure-Python, GStreamer-free SDK (`gst_iceoryx2.video`) mirroring the core
crate's transport — the two share **one neutral vocabulary** (`VideoFramePublisher`/
`VideoFrameSubscriber`/`VideoFrame`/`FrameParams`/`Qos`/`SinkConfig`/…), kept in lockstep by `PARITY.md`.
See `SPEC.md` for the wire-format/element contract, `PARITY.md` for the Rust↔Python API equivalence,
`TODO.md` for phase status, and `README.md` for the overview.

## Source of truth

- **`SPEC.md`** is canonical for the `VideoFrameHeader` layout, the iceoryx2 service/QoS, and the
  elements' properties + lifecycle. Change it in the same commit as any layout/behaviour change.
- The `VideoFrameHeader` layout is asserted from both Rust and Python against a committed **golden
  file** (`python/gst_iceoryx2_tests/header_layout.json`): the core crate's
  `tests/header_layout_golden.rs` emits + checks it (regenerate with `UPDATE_GOLDEN=1 cargo test -p
  gst-plugin-iceoryx2-video`), and Python's `test_header_equivalence` reads it to pin the
  `gst_iceoryx2.video` ctypes mirror. Never let the two drift.
- **`PARITY.md`** is canonical for the Rust↔Python **API surface** equivalence (the one neutral
  vocabulary). It is pinned by a second golden, `python/gst_iceoryx2_tests/api_manifest.json`: the core
  crate's `tests/api_manifest_golden.rs` emits it **and** references every public symbol (a Rust-side
  rename fails to compile), and Python's `test_api_parity` asserts `gst_iceoryx2.video.__all__` + class
  members match (regenerate with `UPDATE_GOLDEN=1 cargo test -p gst-plugin-iceoryx2-video`). A rename on
  either side breaks the build until both languages + `PARITY.md` are updated. Both SDKs are
  **zero-copy on the read path**: a received `VideoFrame` borrows the loaned sample (`pixels`/`aux`/
  `header`/`numpy_view`⇄`ndarray_view` all view shared memory) — so a frame holds an iceoryx2 loan while
  alive (capped by `borrowed-max`), its views die with it, and retaining means copying (`.copy()`/
  `bytes(...)`/`.to_owned()`). The one documented carve-out (not mirrored) is teardown: RAII `Drop` vs
  `close()`/`with`. The opt-in Rust `ndarray` feature is the parity counterpart of the Python numpy
  reshape; keep it optional so the core's default dep stays `iceoryx2`.

## Commands

- Always use `uv run python` — never bare `python`/`python3`.
- Prefer `rg`/`fd` over `grep`/`find`; one simple command per step, no chaining/echoing exit codes.
- Build: `make develop` (maturin into the uv env). Tests: `make test` (unit, no IPC) /
  `make test-integration` (real pipeline + iceoryx2). Lint: `make lint`.
- Python tests live in `python/gst_iceoryx2_tests/`; Rust tests in each crate (`cargo test`), with the
  SDK round-trip `#[ignore]`d (needs the IPC runtime — run via `make test-integration`). No import
  side effects in the package; importing `gst_iceoryx2` (or `gst_iceoryx2.video`) must never load the
  compiled plugin — `setup_gstreamer()` only locates it by path, and only when called.

## Style

- British English, declarative, no fluff. Backticks for identifiers; em dashes for inline asides.
- Keep `README.md` and `SPEC.md` accurate when components change — a new reader should orient
  themselves from them alone.

## Layering rule

The plugin cdylib links libgstreamer, so loading it needs a GStreamer runtime. Keep the
`gst_iceoryx2.video` SDK pure-Python (ctypes + iceoryx2 + lazy numpy) — it must **never** import the
compiled plugin (`gst_iceoryx2._native`); `setup_gstreamer()` finds the plugin file by *path* without
importing it, so a consumer can subscribe with no GStreamer installed. Mirror of this on the Rust
side: keep `gst-plugin-iceoryx2-video` free of any `gst`/`gstreamer` or `pyo3` dependency — GStreamer
types live only in the `gst-plugin-iceoryx2` plugin crate. The plugin elements own no naming policy:
`service`/QoS are element **properties** the embedding application supplies.
