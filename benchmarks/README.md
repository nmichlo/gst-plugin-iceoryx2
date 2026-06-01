# Transport benchmark

Compares three ways of moving decoded video frames out of a GStreamer pipeline to a separate
consumer process, plus a discard baseline, across resolutions from 640×640 to 4K. It exists to
quantify what the `iceoryx2sink` plugin buys over the appsink→Python→publish path it replaces, and
how both compare to a conventional message broker.

The four transports share the **same** upstream (`videotestsrc ! videoconvert`); only the sink
differs, so the measured differences are purely the transport:

| Transport | Tail of the pipeline | Publish-side copies / frame |
|---|---|---|
| `fakesink` | discards the frame | 0 — isolates the shared upstream cost |
| `iox2-zerocopy` | the `iceoryx2sink` plugin | **0** — frame published straight from its shared-memory loan |
| `iox2-onecopy` | `appsink` → Python publishes to iceoryx2 | 2 — GStreamer buffer → Python `bytes` → shared-memory slice |
| `redis` | `appsink` → Python `PUBLISH`es over Redis | 2 — buffer → `bytes` → framed message, then socket + broker |

## Layout

| File | Role |
|---|---|
| `run.py` | Orchestrator + CLI. Sweeps resolutions × transports, runs the two passes, joins latencies, writes `RESULTS.md` + `results.json`. |
| `producer.py` | Builds one GStreamer pipeline per transport, runs it to EOS, samples producer CPU. Holds the iceoryx2 / Redis publish callbacks. |
| `consumer.py` | The subscriber, run in a **separate process** (spawned). Records per-frame receive times. |
| `common.py` | Resolutions, QoS constants, the `Result` dataclass + derived metrics, a throwaway `redis-server`, and the Markdown/console renderers. |
| `VideoFrameHeader` | the ctypes header mirror, imported from the `gst_iceoryx2.video` SDK (layout pinned by `test_header_equivalence`). |
| `RESULTS.md` / `results.json` | Generated reports (committed as a reference run). |

## How it works

Each `(transport, resolution)` is measured by **two passes**, both with the consumer in its own
process so it never contends with the producer (mirroring streaming-service → inference-service):

- **Throughput / CPU pass** — free-running source, `sink sync=false`: pushes as fast as the transport
  allows. Yields producer FPS, MB/s, whole-process CPU per frame, and the delivered/drop rate.
- **Latency pass** — live source paced at a fixed FPS, `sink sync=false` so a frame is published the
  instant it reaches the sink (not after a clock-sync wait). Yields end-to-end p50/p95/p99.

**Measuring latency without perturbing zero-copy.** Writing a timestamp into a frame would force the
zero-copy pool buffer to be copied — destroying the very path under test. So nothing is written to
the buffer: a sink-pad probe records `send_time[offset]` (read-only `GstBuffer.offset`, the frame
counter the sink also writes into the header), the iceoryx2 consumer reports `(header.offset,
recv_time)`, and the two are joined on the same host monotonic clock — drop-robust. Redis has no
free metadata channel, so there the producer frames the send timestamp into the payload (its second
copy) and the consumer computes latency directly.

**Marginal CPU.** The `fakesink` baseline is the shared `videotestsrc ! videoconvert` cost; the
reported *marginal* CPU is `transport_total − baseline`, i.e. the publish cost alone.

**Non-obvious details.**
- The iceoryx2 consumer **busy-polls** (no sleep) so latency reflects the transport rather than a
  poll interval — production instead wakes on the iceoryx2 event `Listener` (same or lower latency,
  no spinning).
- `iox2-onecopy` uses `ipc_threadsafe`-compatible QoS identical to the plugin and every subscriber.
- Redis runs as an **isolated** in-memory `redis-server` on a free port (never a developer's own),
  with the pub/sub output-buffer limit removed so back-pressure surfaces as latency, not a dropped
  connection.
- Redis is **skipped above 1080p** (`REDIS_MAX_BYTES`): ~25 MB messages over a socket bus are not a
  meaningful comparison against shared memory.

## Findings (reference run — Apple M-series, 10 cores; see `RESULTS.md`)

- **Zero-copy latency is flat at ~40 µs from 640×640 to 4K** — independent of frame size. One-copy
  scales with the frame (106 µs → 1.3 ms at 4K); Redis is ~5–17 ms (100–400× the plugin).
- **Marginal publish CPU** tracks the copies: zero-copy ≈ 0 and barely grows (0.03 → 0.13 ms/frame),
  one-copy grows linearly with bytes (0.06 → **1.44 ms/frame** at 4K — a full 25 MB memcpy), Redis
  worst (up to 1.4 ms/frame at 1080p).
- **Throughput**: zero-copy matches the `fakesink` baseline (publishing is effectively free); Redis
  caps far lower and is the first to fall behind.

## Running

```bash
make develop            # build + install the plugin first
make benchmark          # full sweep → benchmarks/RESULTS.md + results.json

# subsets / tuning (ARGS is forwarded to run.py):
make benchmark ARGS="--resolutions 1920x1080,3840x2160 --transports fakesink,iox2-zerocopy,iox2-onecopy"
make benchmark ARGS="--frames 1000 --latency-frames 300 --latency-fps 60"
```

Needs `make develop`, the benchmark dependency group (`uv sync --group benchmark` — `redis`,
`psutil`), and `redis-server` on `PATH` (`brew install redis`). On macOS the `make` target sets the
GStreamer `DYLD_LIBRARY_PATH` / `GI_TYPELIB_PATH` for you.
