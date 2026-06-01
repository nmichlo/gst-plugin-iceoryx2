"""Shared benchmark infrastructure: resolutions, stats, a throwaway Redis server, result types.

The harness compares three ways of moving decoded video frames out of a GStreamer pipeline:

  * ``iox2-zerocopy`` — the ``iceoryx2sink`` plugin (frame published straight from shared memory)
  * ``iox2-onecopy``  — ``appsink`` → Python copies the frame into an iceoryx2 loaned slice
  * ``redis``         — ``appsink`` → Python publishes the frame bytes over Redis pub/sub

plus a ``fakesink`` baseline that discards the frame, isolating the upstream
(``videotestsrc ! videoconvert``) cost so the per-transport *marginal* CPU can be reported.

All timings use ``time.monotonic_ns()``, which is consistent across processes on one host, so the
producer stamps a send time into the first 8 bytes of each frame and the (separate) consumer
process reads it back to compute end-to-end latency.
"""

from __future__ import annotations

import contextlib
import dataclasses
import shutil
import socket
import struct
import subprocess
import tempfile
import time

# Resolutions to sweep. All packed BGR (n_planes == 1), the production pixel format.
Resolution = tuple[str, int, int]
RESOLUTIONS: list[Resolution] = [
    ("640x640", 640, 640),
    ("1280x720", 1280, 720),
    ("1920x1080", 1920, 1080),
    ("3840x2160", 3840, 2160),  # 4K UHD (~24.9 MB/frame)
]
PIXEL_FORMAT = "BGR"
BYTES_PER_PIXEL = 3  # BGR

# Above this frame size the Redis pub/sub comparison is skipped — multi-megabyte messages over a
# socket bus are not a meaningful comparison against shared memory (and melt the broker). 1080p BGR.
REDIS_MAX_BYTES = 1920 * 1080 * BYTES_PER_PIXEL

# iceoryx2 QoS — identical for the plugin, the one-copy publisher and every subscriber, so
# ``open_or_create`` is always compatible.
BUFFER_SIZE = 10
BORROWED_MAX = 10
HISTORY_SIZE = 0

# 8-byte little-endian monotonic-ns send time: framed into the Redis payload (iceoryx2 instead joins
# on the frame counter in its user-header, so it needs no in-band timestamp).
STAMP = struct.Struct("<Q")


def now_ns() -> int:
    return time.monotonic_ns()


def frame_nbytes(width: int, height: int) -> int:
    """Tightly-packed BGR frame size. GStreamer may pad the stride; the negotiated buffer can be
    larger, but this is the lower bound used to size the iceoryx2 slice service."""
    return width * height * BYTES_PER_PIXEL


# --------------------------------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------------------------------


@dataclasses.dataclass
class Result:
    transport: str
    resolution: str
    width: int
    height: int
    # throughput / CPU pass (unthrottled)
    frames_sent: int = 0
    frames_received: int = 0
    producer_wall_s: float = 0.0
    producer_cpu_s: float = 0.0  # user+sys CPU of the producer process over the run
    payload_bytes: int = 0  # actual published payload size (incl. any stride padding)
    copies_per_frame: float = 0.0
    copies_note: str = ""
    # latency pass (rate-limited so the consumer keeps up)
    latencies_us: list[float] = dataclasses.field(default_factory=list)

    @property
    def producer_fps(self) -> float:
        return self.frames_sent / self.producer_wall_s if self.producer_wall_s else 0.0

    @property
    def cpu_ms_per_frame(self) -> float:
        return 1e3 * self.producer_cpu_s / self.frames_sent if self.frames_sent else 0.0

    @property
    def producer_mb_s(self) -> float:
        return self.producer_fps * self.payload_bytes / 1e6

    @property
    def drop_pct(self) -> float:
        if not self.frames_sent:
            return 0.0
        return 100.0 * (1.0 - min(1.0, self.frames_received / self.frames_sent))

    def pct(self, p: float) -> float:
        if not self.latencies_us:
            return 0.0
        xs = sorted(self.latencies_us)
        k = max(0, min(len(xs) - 1, round((p / 100.0) * (len(xs) - 1))))
        return xs[k]


# --------------------------------------------------------------------------------------------------
# Throwaway Redis server (so we never touch a developer's own instance)
# --------------------------------------------------------------------------------------------------


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@contextlib.contextmanager
def redis_server():
    """Start an isolated in-memory ``redis-server`` on a free port; yield ``(host, port)``.

    Persistence is disabled so it is purely an in-memory message bus, the fairest comparison
    against the shared-memory transports.
    """
    exe = shutil.which("redis-server")
    if exe is None:
        raise RuntimeError("redis-server not found on PATH (brew install redis)")
    port = _free_port()
    workdir = tempfile.mkdtemp(prefix="gst_iceoryx2_bench_redis_")
    proc = subprocess.Popen(
        [
            exe,
            "--port",
            str(port),
            "--save",
            "",
            "--appendonly",
            "no",
            "--dir",
            workdir,
            # Never disconnect a slow pub/sub subscriber — let it buffer so back-pressure shows up as
            # latency rather than a dropped connection (the fairer comparison under load).
            "--client-output-buffer-limit",
            "pubsub 0 0 0",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        import redis

        client = redis.Redis(host="127.0.0.1", port=port)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            try:
                if client.ping():
                    break
            except redis.exceptions.ConnectionError:
                time.sleep(0.02)
        else:
            raise RuntimeError("redis-server did not come up")
        client.close()
        yield "127.0.0.1", port
    finally:
        proc.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=3)
        if proc.poll() is None:
            proc.kill()
        shutil.rmtree(workdir, ignore_errors=True)


# --------------------------------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------------------------------

_TRANSPORT_LABEL = {
    "fakesink": "fakesink (baseline)",
    "iox2-zerocopy": "iox2 zero-copy (plugin)",
    "iox2-onecopy": "iox2 one-copy (Python)",
    "redis": "redis pub/sub",
}


def render_markdown(results: list[Result], meta: dict) -> str:
    """Render the full comparison as Markdown, one table per resolution."""
    lines: list[str] = ["# iceoryx2sink transport benchmark", ""]
    for k, v in meta.items():
        lines.append(f"- **{k}**: {v}")
    lines.append("")

    by_res: dict[str, list[Result]] = {}
    for r in results:
        by_res.setdefault(r.resolution, []).append(r)

    for res, rows in by_res.items():
        baseline = next((r for r in rows if r.transport == "fakesink"), None)
        base_cpu = baseline.cpu_ms_per_frame if baseline else 0.0
        lines.append(f"## {res}")
        lines.append("")
        header = (
            "| transport | producer FPS | delivered FPS | drop % | MB/s | "
            "CPU ms/frame (total) | CPU ms/frame (marginal) | copies/frame | "
            "latency p50 / p95 / p99 (µs) |"
        )
        sep = "|" + "|".join(["---"] * 9) + "|"
        lines.append(header)
        lines.append(sep)
        for r in rows:
            marginal = max(0.0, r.cpu_ms_per_frame - base_cpu)
            if r.transport == "fakesink":
                lat = "— (no consumer)"
                delivered = "—"
                drop = "—"
                marg = "0.000 (ref)"
            else:
                lat = f"{r.pct(50):.0f} / {r.pct(95):.0f} / {r.pct(99):.0f}"
                delivered = f"{r.frames_received / max(1e-9, r.producer_wall_s):.0f}"
                drop = f"{r.drop_pct:.1f}"
                marg = f"{marginal:.3f}"
            copies = (
                f"{r.copies_per_frame:.0f} ({r.copies_note})"
                if r.copies_note
                else f"{r.copies_per_frame:.0f}"
            )
            lines.append(
                f"| {_TRANSPORT_LABEL.get(r.transport, r.transport)} "
                f"| {r.producer_fps:.0f} | {delivered} | {drop} | {r.producer_mb_s:.0f} "
                f"| {r.cpu_ms_per_frame:.3f} | {marg} | {copies} | {lat} |"
            )
        lines.append("")
    return "\n".join(lines)


def render_text(results: list[Result]) -> str:
    """Compact console summary."""
    out: list[str] = []
    by_res: dict[str, list[Result]] = {}
    for r in results:
        by_res.setdefault(r.resolution, []).append(r)
    for res, rows in by_res.items():
        baseline = next((r for r in rows if r.transport == "fakesink"), None)
        base_cpu = baseline.cpu_ms_per_frame if baseline else 0.0
        out.append(f"\n=== {res} ===")
        out.append(
            f"{'transport':<26}{'prodFPS':>9}{'delivFPS':>9}{'drop%':>7}"
            f"{'MB/s':>8}{'CPU ms/f':>10}{'marg':>8}{'p50us':>8}{'p95us':>8}{'p99us':>8}"
        )
        for r in rows:
            marginal = max(0.0, r.cpu_ms_per_frame - base_cpu)
            if r.transport == "fakesink":
                deliv = drop = p50 = p95 = p99 = "-"
                marg = "ref"
            else:
                deliv = f"{r.frames_received / max(1e-9, r.producer_wall_s):.0f}"
                drop = f"{r.drop_pct:.1f}"
                p50, p95, p99 = f"{r.pct(50):.0f}", f"{r.pct(95):.0f}", f"{r.pct(99):.0f}"
                marg = f"{marginal:.3f}"
            out.append(
                f"{_TRANSPORT_LABEL.get(r.transport, r.transport):<26}"
                f"{r.producer_fps:>9.0f}{deliv:>9}{drop:>7}{r.producer_mb_s:>8.0f}"
                f"{r.cpu_ms_per_frame:>10.3f}{marg:>8}{p50:>8}{p95:>8}{p99:>8}"
            )
    return "\n".join(out)
