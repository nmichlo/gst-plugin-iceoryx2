# Implementation TODO — `gst-plugin-iceoryx2`

Tracks the build of the `iceoryx2sink`/`iceoryx2src` GStreamer plugin. Phases are committed
independently. See `SPEC.md` for the wire-format/element contract and `README.md` for the overview.

Legend: `[x]` done · `[~]` in progress · `[ ]` pending.

---

## Phase 0 — De-risk spike (go/no-go) — ✅ DONE
- [x] Python iceoryx2 0.7 exposes `Slice[u8]` payload + `.user_header(struct)` + Notifier/Listener.
- [x] Rust `=0.7.0` `[u8]` + `#[type_name("VideoFrameHeader")]` publisher read byte-exact by Python.
- [x] One maturin cdylib loads as both a pyo3 module and a GStreamer plugin
      (via `libgst<name>.so` symlink + `GST_REGISTRY_FORK=no`).
- Spikes removed in Phase 6; findings captured in `SPEC.md` + this repo's real code.

## Phase 1 — Scaffold — ✅ DONE
- [x] `Cargo.toml` — `crate-type=["cdylib"]`; deps: `iceoryx2 =0.7.0`, `gstreamer`,
      `gstreamer-base`, `gstreamer-video`, `pyo3` (abi3-py312 → one portable wheel).
- [x] `pyproject.toml` — maturin backend, `module-name = gst_iceoryx2._gst_iceoryx2`,
      `python-source = python`, dev group (pytest, pre-commit, ruff).
- [x] `python/gst_iceoryx2/__init__.py` — `setup_gstreamer()` (no import side effects).
- [x] `python/gst_iceoryx2/py.typed`.
- [x] `src/lib.rs` — `gst::plugin_define!` registering `iceoryx2sink` + `#[pymodule] _gst_iceoryx2`.
- [x] `src/sink.rs` — no-op BaseSink skeleton (metadata + caps). `format.rs`/`allocator.rs` land in
      Phases 2/4 (created when first needed, to keep commits incremental).
- [x] `.gitignore`, `.pre-commit-config.yaml` (ruff + cargo fmt/clippy), `Makefile`,
      `.cargo/config.toml` (abi3 forward-compat).
- [x] CI skeleton: `pytest.yaml`, `pre-commit.yaml`, `pr-checks.yaml`.
- [x] `.claude/CLAUDE.md`.
- [x] `README.md` (skeleton; filled in Phase 6).
- [x] `cargo check` + `maturin develop` + `setup_gstreamer()` registers `iceoryx2sink` +
      3 unit tests green.

## Phase 2 — Format — ✅ DONE
- [x] `src/format.rs` — `VideoFrameHeader` (`GstVideoMeta` + `GstBuffer`-aligned, 104 B / align 8)
      + exported size/align/offset/type-name constants + `set_format`/`format_name` helpers.
- [x] `_gst_iceoryx2` exposes `HEADER_SIZE`, `HEADER_ALIGN`, `HEADER_TYPE_NAME`,
      `HEADER_FIELD_OFFSETS`, `MAX_PLANES`, `FORMAT_LEN`.
- [x] cargo tests: `header_size_and_align`, `header_field_offsets`, `header_byte_layout`,
      `format_string_roundtrip`, `iceoryx2_type_name_matches`.
      (`payload_len_from_video_info` deferred to Phase 3, where the `GstVideoInfo`→size helper lives.)
- [x] pytest `test_header_equivalence` green (ctypes mirror == Rust constants) — unit tests pass.

## Phase 3 — `iceoryx2sink` (copy fallback first) — ✅ DONE
- [x] `src/sink.rs` — BaseSink: 6 properties, caps→`GstVideoInfo`, `start`/`stop`/`set_caps`/`render`.
- [x] iceoryx2 publisher + event notifier wired through render (uses `ipc_threadsafe::Service` for
      Send+Sync — wire-compatible with Python `ServiceType.Ipc`).
- [x] copy fallback path: loan → fill header (buffer pts/dts/duration/offset/flags + caps layout) →
      `write_from_slice` → send → notify.
- [x] pytest integration: `test_sink_publishes_to_python_subscriber` (crown jewel, 640x640 BGR),
      `test_sink_parameterised` (1280x720), `test_sink_rejects_unsupported_caps` (I420). All green —
      real Rust→Python cross-language interop confirmed.

## Phase 4 — Zero-copy allocator — ✅ DONE
- [x] `src/pool.rs` — non-reusing `Iceoryx2BufferPool`: `acquire_buffer` loans a fresh sample,
      wraps its payload via `Memory::from_mut_slice`, registers the pointer; `release_buffer` drops
      it. (A non-reusing pool, not a `GstAllocator`, is the sound fit for one-shot iceoryx2 loans.)
- [x] `sink.rs::propose_allocation` advertises the pool; `render` detects pool buffers by payload
      pointer and sends them with no copy, else copy-fallback.
- [x] `frames-sent` / `frames-zero-copy` / `frames-copied` read-only properties.
- [x] pytest integration `test_zero_copy_path_used` — confirms 5/5 frames published zero-copy,
      0 copied.

## Phase 5 — public CI + release — ✅ DONE (unvalidated until first push)
- [x] `pytest.yaml` — a `unit-tests` job (maturin develop + cargo test/clippy/fmt + pytest unit) and a
      separate `integration-tests` job (GStreamer + iceoryx2 IPC, memlock raised), both on public
      GitHub-hosted runners (apt-get GStreamer dev libs, `uv sync`).
- [x] `release.yaml` — version stamped from tag → maturin wheel matrix (linux x86_64 + macOS arm64,
      `manylinux: off` since we link system GStreamer); PyPI publish left as an opt-in trusted-publishing
      stub (not wired to secrets).
- [x] `pre-commit.yaml` runs the repo's `.pre-commit-config.yaml` (ruff + cargo fmt/clippy).
- [x] All workflow YAML parses. (CI behaviour itself can only be confirmed once pushed to GitHub.)

## Phase 6 — Docs + contract test — ✅ DONE
- [x] `README.md` — purpose, layout, how-it-works, non-obvious details, config, running,
      consumer-migration notes.
- [x] Contract sanity tests: `test_element_exposes_documented_properties`,
      `test_sink_pad_accepts_bgr` (gst-inspect-level), plus the existing `test_header_equivalence`.
- [x] Removed the `spike/` and `spike-plugin/` reference dirs.
- [x] `SPEC.md` matches the shipped code (pool, properties, header).

## Phase 7 — `iceoryx2src` + broadened caps + sink hardening — ✅ DONE
- [x] `src/caps.rs` — shared `supported_video_caps()` (`BGR`/`RGB`/`I420`/`NV12`); both pad templates
      use it (sink caps broadened beyond `BGR`/`RGB`, source advertises the same set).
- [x] `src/source.rs` — `iceoryx2src` `PushSrc`: subscriber + event listener, data-driven caps
      (`negotiate` no-op + `set_caps` from the header), zero-copy `create` (`Memory::from_slice` over
      the received `Sample`, no `unsafe`), header→buffer timing + stride-exact `VideoMeta`,
      `unlock`/`unlock_stop`, `frames-received` counter.
- [x] `src/sink.rs` — publisher created **once** with `AllocationStrategy::PowerOfTwo` (no
      per-caps-change publisher recreate).
- [x] `lib.rs` registers both elements; `_gst_iceoryx2` exposes `SINK_ELEMENT_NAME` /
      `SRC_ELEMENT_NAME`; `setup_gstreamer()` verifies both register.
- [x] Tests: unit (both elements register, source properties, broadened pad caps); integration
      (`test_source_roundtrip_bgr` + `test_source_roundtrip_i420` byte-exact sink↔src round-trips,
      `test_sink_publishes_i420`, `test_sink_rejects_unsupported_caps` now uses `RGBA`). cargo +
      ruff clean.
- [x] `SPEC.md` (§4a/§4b/§5) + `README.md` updated for both elements.

### Not built — two-`.so` packaging fallback (deferred with rationale)
The plan's alternative packaging (a pure-GStreamer cdylib + a separate pyo3 constants module) is the
contingency for *if the single dual-purpose `.so` proves fragile*. It has not: the symlink +
`GST_REGISTRY_FORK=no` path works on macOS arm64 and both elements register. Splitting now would
double the build matrix and the maturin config for no current benefit. Revisit only if the Linux/edge
wheel build (Phase 5, validated on first push) shows the in-process load failing there.

## Phase 8 — shm/unixfd drop-in parity — ✅ DONE
Mirror the established `shmsink`/`unixfdsink` property/signal names + semantics so the pair is a
drop-in replacement, across four tiers (built after a refactor-first pass).

- [x] **P1 — refactor.** Frame/zero-copy/copied + `frames-received` counters moved to `AtomicU64` on
      the element (read-only properties no longer contend on the hot-path `state` mutex while `render`
      / `create` hold it). `LoanRegistry` rationale documented in `pool.rs` (gstreamer-rs cannot
      recover a `from_mut_slice` object → a side registry is the pragmatic choice).
- [x] **P2 — control-plane parity.** `wait-for-connection` + `num-clients` properties;
      `client-connected`/`client-disconnected` signals (carry the subscriber **count** — iceoryx2 has
      no per-client fd); EOS propagation via the `HEADER_FLAG_EOS` sentinel sample (sink emits on the
      EOS event, source returns `FlowError::Eos`).
- [x] **P3 — opt-in lossless mode.** `lossless` property: forces `safe-overflow=false` and
      back-pressures (retry-loan) on a full ring instead of dropping, in both the copy path and the
      zero-copy pool; interruptible via `unlock`. Off by default.
- [x] **P4 — full caps fidelity + GstMeta passthrough.** Repurposed the header's `_reserved` →
      `aux_size` (non-breaking; same offset/size). New `src/aux.rs` codec appends a variable-length
      **aux blob** (full caps string + serialisable metas, minus `GstVideoMeta`) after the pixels;
      pixels stay at offset 0 (zero-copy intact). Sink reserves an `aux-bytes` tail (zero-copy) or
      loans exactly (copy); source parses it, negotiates the full caps, re-attaches metas. Requires
      GStreamer **1.24+** (`gst_meta_serialize`); `gst`/`gst-base`/`gst-video` now build with `v1_24`.
- [x] **P5 — tests + mirror + docs.** The Python ctypes mirror carries the `aux_size` rename;
      `test_sink_interop` splits pixels on `aux_size`; new
      `test_sink_aux_carries_full_caps` (parses the blob) + `test_parity_interop.py` (caps fidelity,
      `ReferenceTimestampMeta` passthrough, num-clients/signals, EOS). `SPEC.md` §1/§3/§3a/§4a/§4b/§5
      + `README.md` updated. Rust `aux::tests` pin the codec. Full suite: 9 cargo + 10 unit + 12
      integration green; clippy + ruff clean.

## Phase 9 — standalone packaging + GStreamer-free SDK — ✅ DONE
- [x] Crate/wheel renamed to `gst-plugin-iceoryx2`; plugin name `iceoryx2`; import `gst_iceoryx2`.
- [x] `gst_iceoryx2.video` SDK: `VideoFrameHeader` (ctypes), `VideoFrame`,
      `VideoFramePublisher`/`VideoFrameSubscriber`, `SinkConfig`, aux/numpy helpers —
      pure-Python (ctypes + iceoryx2 + lazy numpy), never imports the compiled `.so`.
      (Names neutralised in Phase 10.)
- [x] `__init__.py` made lazy (importing the package / `.video` never loads the GStreamer-linked `.so`);
      PyGObject moved to a `[gst]` extra; `test_header_equivalence` repointed to the SDK header.

## Phase 10 — SDK standardisation + full parity + drift guard — ✅ DONE
- [x] One **neutral vocabulary** across both SDKs: Python dropped the `Iox2` prefix; `VideoFrameSample`→
      `VideoFrame`, `receive_nonblocking`→`receive`, `VIDEO_FRAME_HEADER_TYPE_NAME`→`HEADER_TYPE_NAME`,
      `Iceoryx2SinkConfig`→`SinkConfig`; Rust `ReceivedFrame`→`VideoFrame`. `publish_frame` takes a
      `FrameParams` on both.
- [x] **Full feature parity** both ways. Python gained `is_eos`, `validate_geometry`/`plane_heights`/
      `SUPPORTED_FORMATS`, `build_aux`, `ParsedAux`, `Qos`, `FrameParams`, `DEFAULT_SERVICE`,
      `HEADER_SIZE`/`HEADER_ALIGN`, `format_channels`/`PACKED_FORMATS`, and public port helpers
      (`create_node`/`open_video_service`/`create_notifier`/`create_listener`). Rust gained `SinkConfig`
      + `PropValue`, `PACKED_FORMATS`/`format_channels`, and an optional `ndarray` feature
      (`header_pixels_to_ndarray`, `VideoFrame::to_ndarray`).
- [x] **Drift guard**: `PARITY.md` matrix + `api_manifest.json` golden — Rust `api_manifest_golden.rs`
      emits it (and references every symbol, so a Rust rename fails to compile); Python `test_api_parity`
      asserts `__all__` + members match. Two carve-outs (zero-copy-borrow vs copy; RAII vs `close()`)
      documented, not faked. `SPEC.md` §7 added.

## Possible future work
- [ ] Multi-plane (`I420`/`NV12`) `to_numpy`/`to_ndarray` in the SDK (currently non-packed → raises/`Err`).
- [ ] Publish to PyPI (wire the `release.yaml` trusted-publishing stub to a real environment).
- [ ] A `dmabuf`/GPU-memory story (out of scope today — see `SPEC.md` §3a "non-closable divergences").
