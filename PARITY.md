# Rust ↔ Python SDK parity

`gst-plugin-iceoryx2` ships **two** GStreamer-free SDKs over one wire contract:

- **Rust** — the `gst-plugin-iceoryx2-video` crate (`crates/gst-plugin-iceoryx2-video/`).
- **Python** — the `gst_iceoryx2.video` package (`python/gst_iceoryx2/video/`).

They deliberately share **one neutral vocabulary** so a reader moves between them name-for-name. This
file is the canonical human matrix of that equivalence; it is kept honest by an enforced drift guard
(see [Enforcement](#enforcement)) so the table below cannot silently rot.

## How it is kept in sync

- The **wire layout** (`VideoFrameHeader`) is pinned by `python/gst_iceoryx2_tests/header_layout.json`
  (`SPEC.md` §6) — the Rust `header_layout_golden.rs` emits it; Python `test_header_equivalence` checks it.
- The **API surface** (the table below) is pinned by `python/gst_iceoryx2_tests/api_manifest.json` —
  the Rust `api_manifest_golden.rs` emits it (and references every symbol, so a Rust-side rename fails
  to compile); Python `test_api_parity` asserts `gst_iceoryx2.video.__all__` + class members match it.

Regenerate both goldens after an intentional change with
`UPDATE_GOLDEN=1 cargo test -p gst-plugin-iceoryx2-video`, then update the other language + this file.

## Shared surface (same neutral name on both)

### Header + layout constants

| Concept | Rust (`gst_plugin_iceoryx2_video`) | Python (`gst_iceoryx2.video`) |
|---|---|---|
| Header struct | `VideoFrameHeader` | `VideoFrameHeader` |
| Format accessor | `VideoFrameHeader::format_name()` | `VideoFrameHeader.format_name` |
| Max planes / format len | `MAX_PLANES` / `FORMAT_LEN` | `MAX_PLANES` / `FORMAT_LEN` |
| Header type name | `HEADER_TYPE_NAME` | `HEADER_TYPE_NAME` |
| Size / align | `HEADER_SIZE` / `HEADER_ALIGN` | `HEADER_SIZE` / `HEADER_ALIGN` |
| EOS flag bit | `HEADER_FLAG_EOS` | `HEADER_FLAG_EOS` |
| Default aux tail | `DEFAULT_AUX_BYTES` | `DEFAULT_AUX_BYTES` |

### Aux-blob codec

| Concept | Rust | Python |
|---|---|---|
| Decode | `parse_aux(&[u8]) -> ParsedAux` | `parse_aux(bytes) -> ParsedAux` |
| Encode | `build_aux(caps, metas, limit)` | `build_aux(caps, metas, limit=None)` |
| Parsed result | `ParsedAux { caps, metas }` | `ParsedAux(caps, metas)` (NamedTuple) |

### Geometry validation + formats

| Concept | Rust | Python |
|---|---|---|
| Negotiation/validation set | `SUPPORTED_FORMATS` | `SUPPORTED_FORMATS` |
| Geometry check | `validate_geometry(h, n) -> Result<(), String>` | `validate_geometry(h, n) -> str \| None` |
| Plane layout | `plane_heights(fmt, h)` | `plane_heights(fmt, h)` |
| Reshape (packed) set | `PACKED_FORMATS` | `PACKED_FORMATS` |
| Channels per pixel | `format_channels(fmt) -> Option<usize>` | `format_channels(fmt) -> int \| None` |

`validate_geometry`: `Ok(())`/`None` means valid; `Err(reason)`/`reason: str` means drop it.
`SUPPORTED_FORMATS` (BGR/RGB/I420/NV12) is the negotiation set; `PACKED_FORMATS` (BGR/RGB/BGRA/RGBA) is
the single-array-reshapeable set — neither is a subset of the other, by design.

### QoS / service / config

| Concept | Rust | Python |
|---|---|---|
| Default service | `DEFAULT_SERVICE` | `DEFAULT_SERVICE` |
| Ring QoS constants | `VIDEO_BUFFER_SIZE` / `VIDEO_BORROWED_MAX` / `VIDEO_HISTORY_SIZE` / `VIDEO_SAFE_OVERFLOW` | same four |
| QoS bundle | `Qos` | `Qos` (frozen dataclass) |
| Sink config | `SinkConfig` + `gst_properties()` | `SinkConfig` + `gst_properties()` |

`SinkConfig::gst_properties()` returns `Vec<(&str, PropValue)>` in Rust and `dict[str, object]` in
Python; the Rust `PropValue` enum is the gst-free boundary the plugin maps onto gobject values.

### Transport

| Concept | Rust | Python |
|---|---|---|
| Port helpers | `create_node` / `open_video_service` / `create_notifier` / `create_listener` | same four |
| Publisher | `VideoFramePublisher` | `VideoFramePublisher` |
| Subscriber | `VideoFrameSubscriber` | `VideoFrameSubscriber` |
| Received frame | `VideoFrame` | `VideoFrame` |
| Publish params | `FrameParams` | `FrameParams` (dataclass) |
| Publish | `publish_frame(&[u8], &FrameParams)` | `publish_frame(bytes, FrameParams)` |
| Non-blocking receive | `receive()` | `receive()` |
| Blocking receive | `receive_blocking(Option<u64>)` | `receive_blocking(int \| None)` |
| Frame accessors | `header()` / `pixels()` / `pixel_size()` / `aux()` / `parse_aux()` / `is_eos()` | `.header` / `.pixels` / `.pixel_size` / `.aux` / `parse_aux()` / `is_eos()` |

## Idiomatic pair (matched role, language-native name)

| Role | Rust (feature `ndarray`) | Python |
|---|---|---|
| Reshape buffer → `(H, W, C)` array | `header_pixels_to_ndarray_view` | `header_pixels_to_numpy_view` |
| Frame → array | `VideoFrame::ndarray_view()` | `VideoFrame.numpy_view()` |

Both are **zero-copy** views over the loaned shared memory (non-contiguous when the frame has row
padding; `.to_owned()` / `.copy()` for an owned contiguous array). Names differ only because the array
library differs (`ndarray::ArrayView3<u8>` vs `numpy.ndarray`). The Rust side is behind the optional
`ndarray` cargo feature so the core's default dependency stays `iceoryx2` only; the Python reshape is
always available (numpy is a runtime dependency, imported lazily).

## Language-only

| Item | Side | Why |
|---|---|---|
| `IpcService`, `VideoPubSub` | Rust | iceoryx2 service-type aliases; Python uses `iox2.ServiceType.Ipc` inline |
| `PropValue` | Rust | the gst-free property-value enum; Python `gst_properties()` returns a plain `dict` |
| `Error`, `Result` | Rust | the SDK error type; Python raises native exceptions |
| `field_offsets` | Rust | emits the header-layout golden; Python *reads* it |
| `VideoFrameHeader::set_format` | Rust | Python assigns the ctypes `format` field directly |
| `VideoFramePublisher::with_node` / `new`, `VideoFrameSubscriber::with_node` / `new` | Rust | Python uses one constructor with an `iox2_node=` keyword |
| `close()` / `__enter__` / `__exit__` | Python | see [carve-outs](#language-idiomatic-carve-outs) |

## Memory model — unified, zero-copy borrow (both)

Both SDKs are **zero-copy on the read path**: a received `VideoFrame` *borrows* the loaned iceoryx2
sample, and `header` / `pixels` / `aux` / the array view all read shared memory directly — nothing is
copied. This carries one contract on **both** sides:

- **The frame holds an iceoryx2 loan while alive.** Its views are valid only while the frame is alive,
  and concurrent loans are capped by `subscriber-max-borrowed-samples` (default 10).
- **To retain data past the loan, copy it** — `bytes(frame.pixels)` / `frame.numpy_view().copy()` /
  `frame.ndarray_view().to_owned()`. To hold many frames at once, raise `borrowed-max` (a QoS value all
  participants must match).

Every view — the `numpy_view`/`ndarray_view` array and the bare `pixels`/`aux` buffers alike — is valid
**only while its `VideoFrame` is alive**; keep the frame reference, or copy to outlive it. The only
difference is **how that lifetime is enforced**, a language idiom not a feature: Rust binds the view to
`&self` and the borrow checker rejects use-after-free at compile time; Python documents the contract
(there is no hidden keepalive — a view that outlives its frame is a use-after-free, just as dropping the
borrow early would fail to compile in Rust).

## Language-idiomatic carve-out

One difference is intentional and **not** mirrored — a language idiom, not a feature:

- **Teardown.** Rust frees ports via RAII (`Drop`). Python exposes `close()` and the `with`
  context-manager protocol (`__enter__` / `__exit__`). These are the equivalent idioms.

## Enforcement

- `crates/gst-plugin-iceoryx2-video/tests/api_manifest_golden.rs` — emits `api_manifest.json` and
  references every shared symbol (Rust-side renames fail to compile).
- `python/gst_iceoryx2_tests/test_api_parity.py` — asserts `__all__` + class members match the golden
  (Python-side renames fail the test).
- A rename on **either** side therefore breaks the build until the manifest is regenerated, the other
  language is updated, and this table is corrected.
