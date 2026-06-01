# gst-plugin-iceoryx2

[![pytest](https://github.com/nmichlo/gst-plugin-iceoryx2/actions/workflows/pytest.yaml/badge.svg)](https://github.com/nmichlo/gst-plugin-iceoryx2/actions/workflows/pytest.yaml)
[![PyPI](https://img.shields.io/pypi/v/gst-plugin-iceoryx2.svg)](https://pypi.org/project/gst-plugin-iceoryx2/)
[![Python](https://img.shields.io/pypi/pyversions/gst-plugin-iceoryx2.svg)](https://pypi.org/project/gst-plugin-iceoryx2/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Move raw video frames **zero-copy through [iceoryx2](https://iceoryx.io) shared memory** — straight
from one GStreamer pipeline into another, or in/out of plain Python — with no `appsink` and no Python
on the hot path.

| Component | What it is |
|---|---|
| **`iceoryx2sink`** | GStreamer element — publishes each frame straight from its `GstBuffer` into shared memory. |
| **`iceoryx2src`** | GStreamer element — the inverse: subscribes and pushes received frames downstream, wrapping the loaned payload directly in a `GstBuffer`. |
| **`gst_iceoryx2.video`** | A GStreamer-**free** Python SDK to publish/subscribe the same wire format with no pipeline (ctypes + iceoryx2 + numpy). |

The wire format is a raw pixel payload + a fixed `VideoFrameHeader` user-header (`GstVideoMeta`/`GstBuffer`
shaped), plus an optional **aux blob** carrying the full caps + serialisable metas. Formats: `BGR`, `RGB`,
`I420`, `NV12`. `SPEC.md` is the canonical contract; this README is the tour.

---

## Install

```bash
pip install gst-plugin-iceoryx2          # the gst_iceoryx2.video SDK (subscribe/publish, no GStreamer)
pip install gst-plugin-iceoryx2[gst]     # + setup_gstreamer() and the iceoryx2sink/iceoryx2src elements
```

Prebuilt **abi3** wheels (one wheel per platform covers CPython **3.12 and up** — 3.12, 3.13, 3.14…)
ship for the platforms marked ✅ below; everything else installs from the sdist, which compiles the
Rust against your system GStreamer (needs a Rust toolchain + GStreamer **1.24+** dev headers).

### Support matrix

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
| **Linux glibc floor** | set by `auditwheel` from the binary's symbol usage | The Linux wheel is built against GStreamer 1.24 (Ubuntu 24.04 runner); the exact `manylinux_*` tag is whatever `auditwheel` computes. Older-glibc or **musl** hosts build from the sdist. |

The crucial split the matrix encodes: **the `gst_iceoryx2.video` SDK needs no GStreamer at all** on
*every* row — it never loads the compiled module (the import is lazy), so the wheel's unresolved
GStreamer references are never reached; its only runtime deps are Python 3.12+ and `iceoryx2==0.7.0`.
**Using the elements** (`iceoryx2sink`/`iceoryx2src`, via the `[gst]` extra + `setup_gstreamer()`)
adds PyGObject and needs a GStreamer **1.24+** runtime on the host (`apt install
gstreamer1.0-plugins-base …`, `brew install gstreamer`).

---

## Quick start

### 1 · Register the elements (once per process)

The plugin is one dual-purpose cdylib (a GStreamer plugin *and* a Python module), so it must be
registered **in-process** — `gst-launch-1.0` cannot load it (it has no libpython). Always go through
`setup_gstreamer()`:

```python
from gst_iceoryx2 import setup_gstreamer
setup_gstreamer()   # registers `iceoryx2sink` + `iceoryx2src` with the host GStreamer
```

### 2 · Publish from a pipeline — the **sink** side

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

### 3 · Receive in another pipeline — the **src** side

Caps are recovered from the wire header, so the source needs no `caps` of its own:

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
    frame = sample.to_numpy()          # (H, W, C) uint8, contiguous copy
    caps, metas = sample.parse_aux()   # full caps string + serialised metas (if any)
    print(frame.shape, "offset", sample.header.offset, "pts", sample.header.pts)
```

`receive_nonblocking()` returns `None` immediately when the ring is empty; `receive_blocking()` parks
on the iceoryx2 event listener until a frame arrives (or `block_ms` elapses) — no polling.

### 5 · Publish with the SDK — **no GStreamer**

```python
import numpy as np
from gst_iceoryx2.video import Iox2VideoFramePublisher

pub = Iox2VideoFramePublisher("video/cam0/frame/v2", max_bytes=640 * 640 * 3)
frame = np.zeros((640, 640, 3), dtype=np.uint8)
pub.publish_frame(frame.tobytes(), width=640, height=640, format=b"BGR", offset=0)
```

### 6 · Configure a sink from code — `Iceoryx2SinkConfig`

A small value object that renders the element's (hyphenated) properties from a service name + QoS you
choose — handy when an application owns the naming convention:

```python
from gst_iceoryx2.video import Iceoryx2SinkConfig

cfg = Iceoryx2SinkConfig(service="video/cam0/frame/v2", max_bytes=640 * 640 * 3)
sink = Gst.ElementFactory.make("iceoryx2sink")
for name, value in cfg.gst_properties().items():
    sink.set_property(name, value)
# → {"service": ..., "max-bytes": ..., "buffer-size": 10, "borrowed-max": 10, ...}
```

### Mix and match

Both ends are independent — any combination works, because they share one wire format:

| Publisher | Subscriber |
|---|---|
| `iceoryx2sink` (pipeline) | `iceoryx2src` (pipeline) |
| `iceoryx2sink` (pipeline) | `Iox2VideoFrameSubscriber` (SDK, no GStreamer) |
| `Iox2VideoFramePublisher` (SDK) | `iceoryx2src` (pipeline) |
| `Iox2VideoFramePublisher` (SDK) | `Iox2VideoFrameSubscriber` (SDK) |

---

## The wire format at a glance

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

Pixels stay at offset 0 (so zero-copy is never disturbed); the **aux blob** carries the fidelity the
fixed header can't — the full caps string (colorimetry/framerate/PAR) and any serialisable `GstMeta`
(except `GstVideoMeta`, rebuilt from the header). Each published frame also fires an iceoryx2 **event**
on the bare service name, so subscribers wake without polling. See `SPEC.md` §3/§3a.

---

## Elements & properties

**`iceoryx2sink`** — signals `client-connected` / `client-disconnected` `(uint count)`.

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

---

## Design notes

- **Zero-copy, both ways.** The sink advertises a non-reusing `GstBufferPool` whose buffers *are*
  loaned iceoryx2 samples, so `videoconvert` renders straight into shared memory and the sink sends
  with no `memcpy` (a copy fallback covers buffers from elsewhere). The source wraps the loaned payload
  directly in a `GstBuffer` and keeps the sample alive until the buffer is freed.
- **Data-driven caps.** The source doesn't negotiate up front; it reads format/geometry from the first
  sample's header (and the full caps from the aux blob), and re-negotiates whenever they change.
- **shm/unixfd parity.** `wait-for-connection`, `num-clients`, the connect/disconnect signals, EOS
  propagation (a sentinel sample), opt-in `lossless` back-pressure, full caps fidelity, and arbitrary
  `GstMeta` passthrough — the documented divergences are that the signals carry a subscriber *count*
  (iceoryx2 has no per-client fd) and that GPU/dmabuf payloads are out of scope.
- **No naming policy in the element.** `service` + QoS are properties the embedding app supplies
  (e.g. via `Iceoryx2SinkConfig`); only the `video/default/frame/v2` default is baked in.
- **One lazy cdylib.** Importing `gst_iceoryx2` (or `gst_iceoryx2.video`) never loads the compiled
  `.so` — only `setup_gstreamer()` does — so the SDK stays usable with no GStreamer runtime.

### Current limitations

- **SDK `to_numpy` is packed-only.** `VideoFrameSample.to_numpy()` reshapes packed formats
  (`BGR`/`RGB`/`BGRA`/`RGBA`) to `(H, W, C)`; for planar `I420`/`NV12` it raises `NotImplementedError`
  (the elements still transport those formats byte-exact — use `sample.pixels` + the header's per-plane
  `stride`/`plane_offsets`). Plane-aware `to_numpy` is tracked in `TODO.md`.
- **No GPU/dmabuf zero-copy.** iceoryx2 shares host pages, so a GPU frame must be downloaded to host
  memory first (see `SPEC.md` §3a "non-closable divergences").
- **Linux + macOS only; prebuilt wheels for x86_64 Linux and arm64 macOS.** Other arches/libc build
  from the sdist; Windows is unsupported (the build/dev tooling is Linux/brew). See the
  [support matrix](#support-matrix).

See `SPEC.md` for the canonical wire-format/element contract and the full lifecycle.

---

## Layout

| Path | Role |
|---|---|
| `src/lib.rs` | Dual entry points: `gst::plugin_define!` (both elements) + `_gst_iceoryx2` (pyo3 module exposing the layout constants). |
| `src/format.rs` | The `VideoFrameHeader` wire struct + exported size/align/offset/type-name constants + format helpers. |
| `src/aux.rs` | The aux-blob codec (full caps string + serialisable metas). |
| `src/caps.rs` | The shared `video/x-raw` format set (`BGR`/`RGB`/`I420`/`NV12`). |
| `src/sink.rs` | `iceoryx2sink` (`BaseSink`): publisher + event notifier, zero-copy pool, copy fallback. |
| `src/source.rs` | `iceoryx2src` (`PushSrc`): subscriber + event listener, data-driven caps, zero-copy `create`. |
| `src/pool.rs` | `Iceoryx2BufferPool` — the non-reusing pool backing the sink's zero-copy path. |
| `python/gst_iceoryx2/` | `setup_gstreamer()` (lazy `.so` load) + the `gst_iceoryx2.video` SDK. |
| `python/gst_iceoryx2_tests/` | header-equivalence + setup (unit); sink/src interop + parity (`@integration`). |
| `benchmarks/` | iox2 zero-copy vs iox2 one-copy vs Redis (see `benchmarks/README.md`). |

---

## Develop

```bash
make develop          # build the cdylib + install into the uv env (maturin develop)
make test             # python unit tests (header equivalence + setup); no IPC
make cargo-test       # rust layout/format tests
make test-integration # sink ↔ src + sink → Python subscriber round-trips (needs GStreamer + iceoryx2 shm)
make lint             # cargo clippy + ruff
make benchmark        # transport comparison harness
```

## Licence

MIT — see `LICENSE`.
