<p align="center">
    <h1 align="center">🧊 gst-plugin-iceoryx2 🎞️</h1>
    <p align="center">
        <i>GStreamer iceoryx2 shared memory source/sink plugin to move frames between process with zero-copy.</i>
    </p>
</p>

<p align="center">
    <a href="https://github.com/nmichlo/gst-plugin-iceoryx2/actions/workflows/pytest.yaml" target="_blank">
        <img alt="pytest" src="https://github.com/nmichlo/gst-plugin-iceoryx2/actions/workflows/pytest.yaml/badge.svg"/>
    </a>
    <a href="https://pypi.org/project/gst-plugin-iceoryx2/" target="_blank">
        <img alt="pypi version" src="https://img.shields.io/pypi/v/gst-plugin-iceoryx2.svg"/>
    </a>
    <a href="https://pypi.org/project/gst-plugin-iceoryx2/" target="_blank">
        <img alt="python versions" src="https://img.shields.io/pypi/pyversions/gst-plugin-iceoryx2.svg"/>
    </a>
    <a href="LICENSE" target="_blank">
        <img alt="license" src="https://img.shields.io/badge/License-MIT-blue.svg"/>
    </a>
</p>

---

[GStreamer](https://gstreamer.freedesktop.org) builds media pipelines out of **elements** — small
stages you chain together (`source ! convert ! sink`). To get a frame *out* of a pipeline you
normally copy it through an `appsink`; to share it with another process you copy it again. Every copy
of a full video frame costs memory bandwidth.

This plugin removes the copies. It adds two elements — a **sink** that writes each frame straight into
shared memory, and a **source** that reads it back in another pipeline — so a frame is written once
and handed to readers by reference. It also ships a small **Python SDK** that subscribes to (or
publishes) the same frames with **no GStreamer installed at all** — handy for an inference or
recording process that just wants the pixels.

```
  Producer pipeline                  iceoryx2 shared memory               Consumer pipeline
  ┌────────────────────┐             ┌──────────────────────┐            ┌────────────────────┐
  │ … ! videoconvert ! │  zero-copy  │   one raw frame,     │  zero-copy │ iceoryx2src ! … !  │
  │      iceoryx2sink ─┼────────────▶│   no memcpy          ┼───────────▶│   autovideosink    │
  └────────────────────┘             └──────────┬───────────┘            └────────────────────┘
                                                │
                                                │  (same frame, by reference)
                                                ▼
                                     ┌──────────────────────┐
                                     │  Python SDK           │
                                     │  no GStreamer needed  │
                                     │  subscribe → numpy    │
                                     └──────────────────────┘
```

| Component | What it is |
|---|---|
| **`iceoryx2sink`** | A GStreamer element that publishes each frame from its buffer straight into shared memory. |
| **`iceoryx2src`** | The inverse element: subscribes and pushes received frames downstream into a pipeline. |
| **`gst_iceoryx2.video`** | A GStreamer-**free** Python SDK to publish/subscribe the same frames with no pipeline (ctypes + iceoryx2 + numpy). |

Supported formats: `BGR`, `RGB`, `I420`, `NV12`. The on-the-wire layout is the canonical contract —
see [`SPEC.md`](SPEC.md); this README is the tour.

---

## Install

```bash
pip install gst-plugin-iceoryx2          # the gst_iceoryx2.video SDK — subscribe/publish, no GStreamer
pip install gst-plugin-iceoryx2[gst]     # + the iceoryx2sink/iceoryx2src elements (needs host GStreamer)
```

**Linux & macOS · Python 3.12+ · GStreamer 1.24+ (only for the elements).** Prebuilt wheels ship for
x86_64 Linux and arm64 macOS; other platforms build from the sdist. The SDK alone needs only Python
and `iceoryx2==0.7.0` — never a GStreamer install. Full breakdown below.

<details>
<summary><strong>Platform &amp; version support</strong></summary>

One **abi3** wheel per platform covers CPython **3.12 and up** (3.12, 3.13, 3.14…). Where no prebuilt
wheel exists the sdist compiles the Rust against your system GStreamer (needs a Rust toolchain +
GStreamer **1.24+** dev headers).

| Platform | Arch | Prebuilt wheel | SDK only — `gst_iceoryx2.video` (no GStreamer) | Elements — `[gst]` (needs host GStreamer 1.24+) |
|---|---|:--:|:--:|:--:|
| Linux (glibc / manylinux) | x86_64 | ✅ | ✅ | ✅ |
| Linux (glibc) | aarch64 | ⚠️ sdist | ✅ | ✅ |
| Linux (musl / Alpine) | any | ⚠️ sdist | ✅ | ✅ |
| macOS (Apple Silicon) | arm64 | ✅ | ✅ | ✅ |
| macOS (Intel) | x86_64 | ⚠️ sdist | ✅ | ✅ |
| Windows | x86_64 | ❌ | ❌ | ❌ |

**Legend** — ✅ supported · ⚠️ no prebuilt wheel, builds from the sdist · ❌ unsupported.

| Axis | Support | Why |
|---|---|---|
| **Python** | CPython **3.12+** only | Built `abi3-py312`, so one wheel spans 3.12/3.13/3.14+; PyPy/GraalPy are not supported. |
| **GStreamer** | **1.24+**, linked from the host (not bundled) | The aux blob uses `gst_meta_serialize` (1.24+). The wheel shares the host's one GStreamer with the rest of your process — bundling it would double-load `libgstreamer` and crash. macOS resolves it from the Homebrew prefix. |
| **iceoryx2** | pinned **`==0.7.0`** (Rust crate + Python binding) | Wire/ABI lockstep — publisher and subscriber must run the same version. |
| **Linux glibc floor** | set by `auditwheel` from the binary's symbol usage | The Linux wheel is built against GStreamer 1.24 (Ubuntu 24.04 runner). Older-glibc or **musl** hosts build from the sdist. |

The split the matrix encodes: **the SDK needs no GStreamer on any row.** Importing it never loads the
compiled module (the load is lazy), so the wheel's unresolved GStreamer references are never reached.
Using the **elements** adds PyGObject and a GStreamer **1.24+** runtime (`apt install
gstreamer1.0-plugins-base …` or `brew install gstreamer`).

</details>

---

## Quick start

### 1 · Register the elements (once per process)

The plugin is a single library that is *both* a GStreamer plugin and a Python module, so it has to be
registered **inside your Python process** — `gst-launch-1.0` cannot load it. Always go through
`setup_gstreamer()`:

```python
from gst_iceoryx2 import setup_gstreamer
setup_gstreamer()   # registers `iceoryx2sink` + `iceoryx2src` with the host GStreamer
```

### 2 · Publish from a pipeline — the **sink**

```python
import gi; gi.require_version("Gst", "1.0")
from gi.repository import Gst
from gst_iceoryx2 import setup_gstreamer

setup_gstreamer(); Gst.init(None)

pipeline = Gst.parse_launch(
    "videotestsrc ! videoconvert ! video/x-raw,format=BGR,width=640,height=640 "
    "! iceoryx2sink service=video/cam0/frame/v2"
)
pipeline.set_state(Gst.State.PLAYING)
```

### 3 · Receive in another pipeline — the **source**

The format and size travel with each frame, so the source needs no `caps` of its own:

```python
pipeline = Gst.parse_launch(
    "iceoryx2src service=video/cam0/frame/v2 ! videoconvert ! autovideosink"
)
pipeline.set_state(Gst.State.PLAYING)
```

### 4 · Subscribe with the SDK — **no GStreamer**

A consumer process (inference, recording, …) needs neither a pipeline nor GStreamer installed:

```python
from gst_iceoryx2.video import Iox2VideoFrameSubscriber

sub = Iox2VideoFrameSubscriber("video/cam0/frame/v2")
while (sample := sub.receive_blocking(block_ms=1000)) is not None:
    frame = sample.to_numpy()          # (H, W, C) uint8
    caps, metas = sample.parse_aux()   # full caps string + any serialised metas
    print(frame.shape, "pts", sample.header.pts)
```

`receive_blocking()` parks on the iceoryx2 event listener until a frame arrives (or `block_ms`
elapses) — no polling. `receive_nonblocking()` returns `None` at once when nothing is waiting.

<details>
<summary><strong>More recipes — SDK publishing, config from code, mixing ends</strong></summary>

### Publish with the SDK — no GStreamer

```python
import numpy as np
from gst_iceoryx2.video import Iox2VideoFramePublisher

pub = Iox2VideoFramePublisher("video/cam0/frame/v2", max_bytes=640 * 640 * 3)
frame = np.zeros((640, 640, 3), dtype=np.uint8)
pub.publish_frame(frame.tobytes(), width=640, height=640, format=b"BGR", offset=0)
```

### Configure a sink from code — `Iceoryx2SinkConfig`

A small value object that renders the element's (hyphenated) properties from a service name + QoS you
choose — handy when an application owns the naming convention:

```python
from gst_iceoryx2.video import Iceoryx2SinkConfig

cfg = Iceoryx2SinkConfig(service="video/cam0/frame/v2", max_bytes=640 * 640 * 3)
sink = Gst.ElementFactory.make("iceoryx2sink")
for name, value in cfg.gst_properties().items():
    sink.set_property(name, value)
```

### Mix and match

Both ends are independent — any combination works, because they share one wire format:

| Publisher | Subscriber |
|---|---|
| `iceoryx2sink` (pipeline) | `iceoryx2src` (pipeline) |
| `iceoryx2sink` (pipeline) | `Iox2VideoFrameSubscriber` (SDK, no GStreamer) |
| `Iox2VideoFramePublisher` (SDK) | `iceoryx2src` (pipeline) |
| `Iox2VideoFramePublisher` (SDK) | `Iox2VideoFrameSubscriber` (SDK) |

</details>

---

## Architecture

Both transports — the GStreamer elements and the pure-Python SDK — converge on **one shared wire
format**, which is the whole contract. The Rust side and the GStreamer-free Python side never talk to
each other directly; they only agree on the bytes that land in iceoryx2 shared memory. That agreement
is what lets any publisher pair with any subscriber.

```
   GStreamer pipeline                            Plain Python — no GStreamer
   ─────────────────                             ───────────────────────────
   iceoryx2sink  ──┐                         ┌──  Iox2VideoFramePublisher
   iceoryx2src   ──┤                         ├──  Iox2VideoFrameSubscriber
                   │                         │
     src/sink.rs · src/source.rs             python/gst_iceoryx2/video
     src/pool.rs  (zero-copy buffer pool)    (ctypes + iceoryx2 + numpy)
                   │                         │
                   └────────────┬────────────┘
                                ▼
                  shared wire format  —  the contract
                  src/format.rs · src/aux.rs · src/caps.rs
                                │
                                ▼
                  iceoryx2 publish/subscribe + wake event
                            (shared memory)
```

The compiled `.so` is a single library with **two entry points** (`src/lib.rs`): a GStreamer plugin
*and* a Python (pyo3) module. Importing the Python package never loads it — only `setup_gstreamer()`
does — so the SDK stays usable where no GStreamer is installed.

### Modules at a glance

| Layer | Path | Responsibility |
|---|---|---|
| **Wire format** (the contract) | `src/format.rs`, `src/aux.rs`, `src/caps.rs` | The `VideoFrameHeader` struct + exported constants; the aux-blob codec (full caps + metas); the shared `BGR`/`RGB`/`I420`/`NV12` format set. |
| **Producer** | `src/sink.rs`, `src/pool.rs` | `iceoryx2sink` — publisher + event notifier, backed by a non-reusing zero-copy buffer pool (with a copy fallback). |
| **Consumer** | `src/source.rs` | `iceoryx2src` — subscriber + event listener, data-driven caps, zero-copy `create`. |
| **Bindings** | `src/lib.rs` | The dual entry point: `gst::plugin_define!` (both elements) + the `_gst_iceoryx2` pyo3 module exposing layout constants. |
| **Python** | `python/gst_iceoryx2/` | `setup_gstreamer()` (lazy `.so` load) + the GStreamer-free `gst_iceoryx2.video` SDK. |
| **Tests & benches** | `python/gst_iceoryx2_tests/`, `benchmarks/` | Header-equivalence + interop tests; iox2 zero-copy vs one-copy vs Redis benchmarks. |

---

## Reference

<details>
<summary><strong>Elements &amp; properties</strong></summary>

**`iceoryx2sink`** — emits `client-connected` / `client-disconnected` signals `(uint count)`.

| Property | Default | Meaning |
|---|---|---|
| `service` | `video/default/frame/v2` | iceoryx2 service name (pub/sub + event) |
| `max-bytes` | `0` (derive from caps) | configured max slice length |
| `buffer-size` / `borrowed-max` / `history-size` / `safe-overflow` | `10` / `10` / `0` / `true` | iceoryx2 QoS |
| `wait-for-connection` | `false` | block the stream until ≥1 subscriber connects (mirrors `shmsink`) |
| `lossless` | `false` | back-pressure instead of dropping (pair with `safe-overflow=false` on the source) |
| `aux-bytes` | `4096` | bytes reserved for the aux blob (full caps + metas); `0` disables passthrough |
| `num-clients` | — | read-only: connected subscriber count |
| `frames-sent` / `frames-zero-copy` / `frames-copied` | — | read-only debug counters |

**`iceoryx2src`** — must match the publisher's `service` + QoS so `open_or_create` is compatible.

| Property | Default | Meaning |
|---|---|---|
| `service` | `video/default/frame/v2` | iceoryx2 service name (pub/sub + event) |
| `buffer-size` / `borrowed-max` / `history-size` / `safe-overflow` | `10` / `10` / `0` / `true` | iceoryx2 QoS |
| `frames-received` | — | read-only debug counter |

</details>

<details>
<summary><strong>The wire format</strong></summary>

```
iceoryx2 publish_subscribe::<[u8]>().user_header::<VideoFrameHeader>()

  payload (slice)   ┌──────────── pixels (offset 0) ────────────┬─── aux blob (optional) ───┐
                    │  raw planes, exactly as GStreamer laid out │  u32 caps_len | caps_str  │
                    └────────────────────────────────────────────┤  u32 n_metas | metas...   │
                         split at  len - header.aux_size  ────────┘                           │
                                                                                              ┘
  user-header       VideoFrameHeader  (fixed 104 B, align 8)
                    pts dts duration offset flags · width height n_planes aux_size
                    stride[4] · plane_offsets[4] · format[16]
```

Pixels stay at offset 0, so zero-copy is never disturbed. The **aux blob** carries the fidelity the
fixed header can't — the full caps string (colorimetry / framerate / pixel-aspect-ratio) and any
serialisable `GstMeta` (except `GstVideoMeta`, which is rebuilt from the header). Each published frame
also fires an iceoryx2 **event** on the bare service name, so subscribers wake without polling. See
[`SPEC.md`](SPEC.md) §3 / §3a.

</details>

<details>
<summary><strong>Design notes &amp; limitations</strong></summary>

- **Zero-copy, both ways.** The sink advertises a non-reusing buffer pool whose buffers *are* loaned
  iceoryx2 samples, so `videoconvert` renders straight into shared memory and the sink sends with no
  `memcpy` (a copy fallback covers buffers from elsewhere). The source wraps the loaned payload
  directly in a `GstBuffer` and keeps the sample alive until that buffer is freed.
- **Data-driven caps.** The source doesn't negotiate up front; it reads format and geometry from the
  first frame's header (and the full caps from the aux blob), and re-negotiates whenever they change.
- **shm/unixfd parity.** `wait-for-connection`, `num-clients`, the connect/disconnect signals, EOS
  propagation (a sentinel sample), opt-in `lossless` back-pressure, full caps fidelity, and arbitrary
  `GstMeta` passthrough. The documented divergences: the signals carry a subscriber *count* (iceoryx2
  has no per-client fd), and GPU/dmabuf payloads are out of scope.
- **No naming policy in the element.** `service` + QoS are properties the embedding app supplies (e.g.
  via `Iceoryx2SinkConfig`); only the `video/default/frame/v2` default is baked in.
- **One lazy library.** Importing `gst_iceoryx2` (or `gst_iceoryx2.video`) never loads the compiled
  `.so` — only `setup_gstreamer()` does — so the SDK stays usable with no GStreamer runtime.

**Current limitations**

- **SDK `to_numpy` is packed-only.** It reshapes packed formats (`BGR` / `RGB` / `BGRA` / `RGBA`) to
  `(H, W, C)`; for planar `I420` / `NV12` it raises `NotImplementedError` (the elements still transport
  those byte-exact — use `sample.pixels` + the header's per-plane `stride` / `plane_offsets`).
- **No GPU/dmabuf zero-copy.** iceoryx2 shares host pages, so a GPU frame must be downloaded to host
  memory first.
- **Linux + macOS only.** Other arches/libc build from the sdist; Windows is unsupported.

</details>

---

## Why?

Streaming a camera into several consumers — a recorder, a live view, an inference model — usually
means one of two compromises: route everything through a single process (and couple them together),
or copy every frame across a socket or `appsink` (and pay the bandwidth). A 4K frame is ~25 MB; at 30
fps that copy alone is most of a memory channel.

iceoryx2 is a zero-copy inter-process transport: the producer writes a frame into a shared page and
every subscriber reads *that same page*, no copy, no serialisation. This plugin wires that transport
into GStreamer at both ends and exposes it to plain Python, so independent processes can share live
video by reference. The elements aim for **parity with GStreamer's `shmsink`/`unixfd`** (connection
signals, EOS, back-pressure, full caps) while adding the SDK path for consumers that don't speak
GStreamer at all.

---

## Develop

```bash
make develop          # build the library + install into the uv env (maturin develop)
make test             # python unit tests (header equivalence + setup); no IPC
make cargo-test       # rust layout/format tests
make test-integration # sink ↔ src + sink → Python subscriber round-trips (needs GStreamer + iceoryx2 shm)
make lint             # cargo clippy + ruff
make benchmark        # transport comparison harness
```

See [`SPEC.md`](SPEC.md) for the canonical wire-format/element contract and full lifecycle.

## Licence

MIT — see [`LICENSE`](LICENSE).
