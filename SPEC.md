# `gst-plugin-iceoryx2` — protocol & element specification

This is the **canonical contract** for the `iceoryx2sink`/`iceoryx2src` GStreamer elements and the
wire format they publish. The Rust struct and the Python ctypes mirror (`gst_iceoryx2.video`) must
both agree with this document. When the layout changes, bump the format version (service-name
suffix) and update this file in the same change.

---

## 1. Transport

- **Mechanism**: [iceoryx2](https://iceoryx.io) 0.7.0 zero-copy shared-memory pub/sub
  (daemon-less). Rust crate pinned `iceoryx2 = "=0.7.0"`; Python `iceoryx2==0.7.0` (lockstep).
- **Pattern**: `publish_subscribe::<[u8]>().user_header::<VideoFrameHeader>()`.
  - **Payload** — a `[u8]` **slice** = the raw pixel plane(s) **followed by an optional aux blob**.
    The pixels are byte-for-byte as GStreamer laid them out (respecting stride/row padding), at
    offset 0. The aux blob (section 3a) is appended after them and carries the full caps + any
    serialisable `GstMeta`s. The split point is `header.aux_size`: pixels are
    `payload[.. len - aux_size]`, the aux blob is `payload[len - aux_size ..]`. With `aux_size == 0`
    the payload is pixels only.
  - **User-header** — a fixed `VideoFrameHeader` POD struct (section 3), carried per sample
    out-of-band from the pixels.
- **GStreamer 1.24+ required**: the aux blob's meta passthrough uses `gst_meta_serialize`
  (GStreamer 1.24), so the wheel requires a 1.24+ runtime.
- **Event notification**: each published frame is paired with an iceoryx2 **event notification** on
  the same service so a subscriber's `Listener` wakes without polling. The event service is opened on
  the **bare** service name (`service_builder(name).event()`), so a Python subscriber built with the
  `gst_iceoryx2.video` SDK wakes from the sink's notifications.

This split — **raw payload + out-of-band metadata** — is the universal shared-memory-video
convention (`shmsink`/`shmsrc`, `unixfdsink`/`unixfdsrc`). We follow it.

---

## 2. Service naming

- The element default is `video/default/frame/v2`; a typical convention is
  `video/{camera_id}/frame/v2`.
- The `/v2` suffix marks the **split format** (pixels + aux blob) version. Because iceoryx2 keys a
  service on its payload + user-header types, a different layout on the same name triggers a
  type-mismatch error — so bumping the suffix lets an old and a new format run in parallel and the
  cutover flips publisher + subscriber together.
- **The element bakes in no naming policy.** `service` and the QoS knobs are element **properties**;
  an embedding application owns the naming/QoS convention and supplies them (the `gst_iceoryx2.video`
  SDK's `SinkConfig` — and its Rust `SinkConfig` mirror — render the hyphenated property names from a
  service name + QoS the caller chooses). The element opens the actual iceoryx2 ports from those properties.

---

## 3. `VideoFrameHeader` (the wire struct)

Modelled on GStreamer's own `GstVideoMeta` + `GstBuffer` timing — the same fields `unixfd`
serialises — expressed as a fixed `#[repr(C)]` POD struct (iceoryx2 user-headers must be fixed-size
for zero-copy + cross-language matching). It uses GStreamer's vocabulary, not a bespoke one.

`GST_VIDEO_MAX_PLANES = 4`. `GstClockTime` is `u64` nanoseconds; the "none" sentinel is
`u64::MAX` (`GST_CLOCK_TIME_NONE`).

| Off | Field | Rust type | C / ctypes | GStreamer source | Meaning |
|----:|-------|-----------|------------|------------------|---------|
| 0   | `pts`           | `u64`      | `c_uint64`     | `GstBuffer.pts`            | presentation timestamp (ns) |
| 8   | `dts`           | `u64`      | `c_uint64`     | `GstBuffer.dts`            | decode timestamp (ns); `u64::MAX` if none |
| 16  | `duration`      | `u64`      | `c_uint64`     | `GstBuffer.duration`       | frame duration (ns); `u64::MAX` if none |
| 24  | `offset`        | `u64`      | `c_uint64`     | `GstBuffer.offset`         | frame counter / media offset; `u64::MAX` if none |
| 32  | `flags`         | `u64`      | `c_uint64`     | `GstBufferFlags`           | buffer flags (`GAP`, `CORRUPTED`, `DELTA_UNIT`, …) |
| 40  | `width`         | `u32`      | `c_uint32`     | `GstVideoInfo.width`       | pixels |
| 44  | `height`        | `u32`      | `c_uint32`     | `GstVideoInfo.height`      | pixels |
| 48  | `n_planes`      | `u32`      | `c_uint32`     | `GstVideoInfo.n_planes`    | 1 for packed BGR/RGB; >1 for I420/NV12/… |
| 52  | `aux_size`      | `u32`      | `c_uint32`     | —                          | bytes of the aux blob (section 3a) appended after the pixels; `0` if none |
| 56  | `stride`        | `[u32; 4]` | `c_uint32 * 4` | `GstVideoInfo.stride`      | per-plane bytes per row |
| 72  | `plane_offsets` | `[u32; 4]` | `c_uint32 * 4` | `GstVideoInfo.offset`      | per-plane byte offset within the payload |
| 88  | `format`        | `[u8; 16]` | `c_char * 16`  | `gst_video_format_to_string` | null-padded GStreamer format string, e.g. `"BGR"`, `"I420"`, `"NV12"` |

- **Size = 104 bytes, alignment = 8.** No tail padding (104 is a multiple of 8).
- **iceoryx2 type name** = `"VideoFrameHeader"` (Rust declares it via `#[type_name(...)]`; Python
  derives it from the ctypes class name — they must match, along with size + alignment).
- **`format`** is the GStreamer format *string*, not an enum value — version-stable and round-trips
  via `gst_video_format_from_string`. It replaces the old bespoke `channel_mode`/`channels`/
  `pixel_format` triple.
- Only planes `0..n_planes` of `stride`/`plane_offsets` are meaningful; the rest are zero.
- `aux_size` occupies the 4 bytes that previously padded the arrays to 8-byte alignment, so the
  104-byte / align-8 layout is **unchanged** from the original (caps/meta-less) format.

---

## 3a. The aux blob

The fixed header cannot hold variable-length data, so the full caps string and serialisable metas
travel in a variable-length **aux blob** appended after the pixels in the same payload slice. This
closes the two fidelity gaps with `unixfd` (which puts the same caps + metas on its control socket)
while keeping the pixels at offset 0 and zero-copy. `aux_size == 0` ⇒ no aux blob (geometry only).

Wire layout (little-endian, self-describing):

```text
  u32 caps_len            // bytes of the caps string (0 = no caps)
  u8[caps_len] caps_str   // gst_caps_to_string output, UTF-8, no NUL
  u32 n_metas             // number of serialised metas that follow
  repeated n_metas times:
      u32 meta_len        // bytes of this meta's gst_meta_serialize output
      u8[meta_len] meta   // the serialised meta
  // trailing bytes (the zero-copy reserve's slack) are ignored
```

- **Caps** — the publisher serialises the full negotiated `GstCaps` (colorimetry, framerate,
  pixel-aspect-ratio, interlace, …) every frame; the source parses it and negotiates the **full**
  caps, not just the header geometry. Per-frame (not on-change) so a late subscriber on a lossy ring
  always sees current caps.
- **Metas** — every *serialisable* `GstMeta` on the buffer is appended, **except** `GstVideoMeta`
  (its information already travels in the header and the source rebuilds it from there). Metas whose
  API has no `serialize_func`, or is unregistered on the consumer, are skipped silently — passthrough
  is best-effort, never fatal.
- **Zero-copy reserve** — the pool loans pixels + a fixed `aux-bytes` tail (default 4096) before a
  frame's blob size is known; the sink writes the blob into the head of that tail and zero-fills the
  slack (so the parser, which stops after `n_metas`, ignores it). If a frame's caps + metas overflow
  the reserve, metas are dropped to fit (caps is kept); set `aux-bytes` larger, or `0` to disable the
  aux blob entirely. The copy fallback path loans exactly pixels + blob, so it never drops anything.

### Inherent (non-closable) divergences

Unlike `unixfd`/`cudaipc`, iceoryx2 shares host shared-memory pages, so **GPU/VRAM zero-copy, dmabuf
FD passing, and truly arbitrary (non-video) payloads** are out of scope — a GPU frame must be
downloaded to host memory first, and the header is video-shaped. These are properties of the
transport, not gaps to be closed.

---

## 4. The elements

Two elements share the wire format above: `iceoryx2sink` (publisher) and `iceoryx2src` (subscriber).

### Supported caps (both elements)

`video/x-raw` with `format` in `{ BGR, RGB, I420, NV12 }` — the packed formats the current pipeline
produces plus the two common planar formats; `width`/`height`/`framerate` left open
(caps-parameterised). The header represents any raw format (it carries `n_planes` + per-plane
`stride`/`offset`), so the supported set is governed solely by what the pad templates advertise; both
elements advertise the same set via the shared `caps::supported_video_caps()`.

## 4a. The `iceoryx2sink` element

A `GstBaseSink` subclass registered as `iceoryx2sink`.

### Properties

| Property | Type | Default | Notes |
|---|---|---|---|
| `service` | string | `video/default/frame/v2` | iceoryx2 service name (set by the embedding application) |
| `max-bytes` | uint | `0` (derive from first caps) | configured max slice length; publisher + subscriber must agree |
| `buffer-size` | uint | `10` | subscriber max buffer size (QoS) |
| `borrowed-max` | uint | `10` | subscriber max borrowed samples (QoS) |
| `history-size` | uint | `0` | publisher history (QoS) |
| `safe-overflow` | bool | `true` | ring-buffer overflow policy (QoS) |
| `wait-for-connection` | bool | `false` | block the stream until ≥1 subscriber is connected (mirrors `shmsink`) |
| `lossless` | bool | `false` | back-pressure instead of dropping: forces no ring overflow and blocks until the consumer frees a slot (the source must also set `safe-overflow=false`) |
| `aux-bytes` | uint | `4096` | bytes reserved after the pixels for the aux blob (full caps + metas); `0` disables passthrough |
| `num-clients` | uint | — | read-only: subscribers currently connected |
| `frames-sent` | uint64 | — | read-only counter |
| `frames-zero-copy` | uint64 | — | read-only: frames sent without a copy |
| `frames-copied` | uint64 | — | read-only: frames sent via the copy fallback |

The `service` + QoS knobs are intended to be set from an application's sink-config (e.g. the SDK's
`SinkConfig`) rather than hand-tuned per pipeline.

### Signals & events (control-plane parity)

- **`client-connected`** / **`client-disconnected`** `(uint count)` — emitted when the connected
  subscriber count changes. Mirrors `shmsink`'s same-named signals; ours carries the **count**
  (iceoryx2 has no per-client socket fd to pass, unlike `shmsink`).
- **EOS** — on a GStreamer end-of-stream event the sink publishes a one-byte sentinel sample with the
  private `HEADER_FLAG_EOS` flag bit (bit 32 of `flags`, free since `GstBufferFlags` are 32-bit). The
  source turns it back into a GStreamer EOS. Mirrors `unixfd`'s EOS command; best-effort on a lossy
  ring.

### Lifecycle

- **`start()`** — build the iceoryx2 node, open/create the pub/sub service with the configured QoS +
  the `VideoFrameHeader` user-header type, create the publisher, and create the event **notifier**.
- **`set_caps()`** — parse `GstVideoInfo` from the negotiated caps; cache format string, dimensions,
  `n_planes`, `stride[]`, `plane_offsets[]`, and total size. The publisher is created **once**, on
  the first caps, with `initial_max_slice_len` + `AllocationStrategy::PowerOfTwo`, so a larger later
  frame grows iceoryx2's data segment internally rather than recreating the publisher port (which
  would drop in-flight samples and churn shared memory on every resolution change).
- **`render(buffer)`** — refresh `num-clients` (firing the connect/disconnect signals; blocking if
  `wait-for-connection`), obtain a slice sample, fill the pixels (zero-copy or copy fallback, section
  5), write the **aux blob** (section 3a) into the tail and set `aux_size`, populate the
  `VideoFrameHeader` from the cached `GstVideoInfo` + the buffer's `pts`/`dts`/`duration`/`offset`/
  `flags`, `send()` it, then fire the event notification. In `lossless` mode a full ring
  back-pressures (retry-loan) instead of dropping.
- **`unlock()` / `unlock_stop()`** — break `render` out of a `wait-for-connection` / lossless wait on
  flush / shutdown.
- **`stop()`** — drop publisher / notifier / service / node.

## 4b. The `iceoryx2src` element

A `GstPushSrc` subclass registered as `iceoryx2src` — the inverse of the sink, a live source that
recovers published frames.

### Properties

| Property | Type | Default | Notes |
|---|---|---|---|
| `service` | string | `video/default/frame/v2` | iceoryx2 service name (must match the publisher) |
| `buffer-size` | uint | `10` | subscriber max buffer size (QoS) |
| `borrowed-max` | uint | `10` | subscriber max borrowed samples (QoS) |
| `history-size` | uint | `0` | publisher history (QoS; must match) |
| `safe-overflow` | bool | `true` | ring-buffer overflow policy (QoS; must match — set `false` to pair with a `lossless` sink) |
| `frames-received` | uint64 | — | read-only counter |

There is no `max-bytes`: the subscriber sizing follows the service, and each received slice carries
its own length. There is no `aux-bytes` either — the source reads the aux blob's size from
`header.aux_size`.

### Lifecycle

- **`start()`** — build the iceoryx2 node, open/create the same pub/sub service (slice + user-header
  + QoS) and event service, and create the **subscriber** + event **listener**. A live source
  (`set_live(true)`, `Format::Time`); buffer timestamps come from the wire header, not the clock.
- **`create()`** — park on the `Listener` (short timeout, re-checking the `unlock` flag) until the
  publisher's notifier wakes it, then `receive()` a sample. Wrap the loaned payload **zero-copy** in
  a `GstBuffer` (`Memory::from_slice` over the received `Sample`, which is kept alive by the buffer),
  map the header's `pts`/`dts`/`duration`/`offset`/`flags` onto it, and attach a `GstVideoMeta` with
  the header's per-plane `stride`/`offset`. It then **parses the aux blob** (section 3a): negotiating
  the **full** caps (preferring the aux caps string over the header geometry) and re-attaching any
  metas via `gst_meta_deserialize`. **Caps are data-driven**: `negotiate()` is a no-op, and `create`
  calls `set_caps` whenever the full caps change. A sample with `HEADER_FLAG_EOS` set yields a
  GStreamer EOS instead of a buffer.
- **Geometry validation** — the header is untrusted wire data (any process on the same service can
  publish it), so before building a buffer `create` rejects a frame whose declared layout would read
  past the pixel region: it requires `n_planes ∈ 1..=4`, a format in the advertised set, the expected
  plane count for that format, and `plane_offset[i] + stride[i] · plane_height[i] ≤ pixel_size` for
  every plane. A frame that fails is dropped (logged, not fatal); the stream continues.
- **`unlock()` / `unlock_stop()`** — set/clear the lock-free flag that breaks `create` out of its
  receive loop on flush / state change.
- **`stop()`** — drop subscriber / listener / node.

---

## 5. Zero-copy + copy fallback

The slice payload *is* the pixel plane (offset 0, aligned), so there is no header/pixel alignment
conflict.

- **Zero-copy path** — `iceoryx2sink` advertises, via `propose_allocation`, a custom **non-reusing**
  `GstBufferPool` (`Iceoryx2BufferPool`) whose buffers are backed directly by loaned iceoryx2 slice
  samples (`Memory::from_mut_slice` wrapping the loaned payload). Upstream (`videoconvert`/
  `videoscale`) writes pixels straight into the loaned sample's shared memory; `render` recognises
  the buffer by its payload pointer (held in a loan registry), takes the sample, fills the header,
  writes the aux blob into the reserved tail, and sends it with **no pixel copy**. The pool loans
  pixels + an `aux-bytes` tail but exposes only the pixel region to upstream. The pool is
  *non-reusing* — `acquire_buffer` always loans a fresh sample and `release_buffer` drops it —
  because an iceoryx2 loan is one-shot (once sent it belongs to the subscriber, so the same shared
  memory must never be recycled to upstream).
- **Copy fallback (mandatory)** — if a buffer did not come from our pool (its pointer is not
  registered), `loan_slice_uninit(pixels + aux) → memcpy pixels → append aux blob → fill header →
  send`. Always correct; guarantees output even when allocation negotiation does not engage.
- Read-only `frames-sent` / `frames-zero-copy` / `frames-copied` properties expose the split.

The **receive** side (`iceoryx2src`) is unconditionally zero-copy: the received `Sample` borrows the
shared-memory payload and is wrapped directly by `Memory::from_slice`, with the `Sample` kept alive
by the `GstBuffer`'s memory and dropped (returning the borrow) when the buffer is freed. The `Sample`
is `Send + 'static` for the thread-safe service, so this requires no `unsafe`. There is no copy
fallback on receive because the source owns the buffer it produces.

---

## 6. Cross-language contract test

The layout in section 3 is asserted from **both** sides, pinned by a committed **golden file**
(`python/gst_iceoryx2_tests/header_layout.json`) — the plugin is no longer a Python module, so the
contract no longer relies on a runtime constant export:

- **Rust** (`cargo test`): `size_of`/`align_of`/`offset_of!` for every field; format-string
  round-trip; the declared iceoryx2 type name. A golden test in the `gst-plugin-iceoryx2-video` core
  crate (`tests/header_layout_golden.rs`) serialises the live layout (`field_offsets()` + size/align +
  constants) and asserts it equals the golden file — regenerate after an intentional layout change
  with `UPDATE_GOLDEN=1 cargo test -p gst-plugin-iceoryx2-video`.
- **Python** (`pytest`): `test_header_equivalence` reads the same golden file and asserts the
  `gst_iceoryx2.video` ctypes `VideoFrameHeader`'s `sizeof`, field offsets, type name, and constants
  match — so it needs no compiled module, no GStreamer, and no Rust toolchain. The aux-blob format is
  asserted both in Rust (`auxblob` tests) and end-to-end in Python (`test_sink_aux_carries_full_caps`
  parses the blob; `test_caps_fidelity_framerate_survives` and
  `test_meta_passthrough_reference_timestamp` prove caps + meta round-trip through
  `iceoryx2sink → iceoryx2src`).

A consumer that mirrors this struct independently (in another language or repo) runs the same
assertion against the golden file to pin the layout on its side.

---

## 7. API surface parity

The wire layout (section 6) is one contract; the **SDK API surface** is another. The Rust core crate
`gst-plugin-iceoryx2-video` and the Python `gst_iceoryx2.video` package deliberately share **one
neutral vocabulary** (`VideoFramePublisher` / `VideoFrameSubscriber` / `VideoFrame` / `FrameParams` /
`Qos` / `SinkConfig`, the `build_aux` / `parse_aux` / `validate_geometry` / `format_channels` helpers,
the `create_node` / `open_video_service` / `create_notifier` / `create_listener` port builders, and the
shared constants). [`PARITY.md`](PARITY.md) is the canonical equivalence matrix.

It is pinned the same way as the layout — by a committed golden, `api_manifest.json`:

- **Rust** (`tests/api_manifest_golden.rs`): references every shared symbol (a Rust-side rename fails
  to compile) and serialises the canonical surface to `python/gst_iceoryx2_tests/api_manifest.json`;
  regenerate with `UPDATE_GOLDEN=1 cargo test -p gst-plugin-iceoryx2-video`.
- **Python** (`test_api_parity`): reads the golden and asserts `gst_iceoryx2.video.__all__` + class
  members match it — no compiled module / GStreamer / Rust toolchain needed.

Both SDKs are **zero-copy on the read path**: a received `VideoFrame` borrows the loaned sample, and
`pixels`/`aux`/`header`/the `(H,W,C)` array view (`numpy_view` ⇄ `ndarray_view`) read shared memory
directly. The loan contract follows: a frame's views are valid only while it is alive, concurrent loans
are capped by `subscriber-max-borrowed-samples` (default 10), and retaining data means copying it
(`bytes(...)` / `.copy()` / `.to_owned()`). Only **teardown** is a documented idiomatic carve-out (Rust
RAII `Drop` ≙ Python `close()` / `with`); see `PARITY.md`. The supported-format sets are split on
purpose: `SUPPORTED_FORMATS` is the negotiation/validation set (BGR/RGB/I420/NV12); `PACKED_FORMATS` is
the single-array-reshapeable set (BGR/RGB/BGRA/RGBA) used by the numpy/`ndarray` helpers.
