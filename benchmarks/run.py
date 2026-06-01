"""Run the transport benchmark and emit a Markdown + JSON report.

    make benchmark                      # all transports, all resolutions
    uv run python benchmarks/run.py --help

For each resolution it runs a ``fakesink`` baseline (upstream-only CPU), then each transport twice:
an unthrottled *throughput/CPU* pass and a rate-limited *latency* pass (so the consumer keeps up and
the latency percentiles are meaningful rather than dominated by ring-buffer drops).
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import json
import multiprocessing as mp
import os
import platform
import queue
import sys

_THIS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS)

from common import (  # noqa: E402
    REDIS_MAX_BYTES,
    RESOLUTIONS,
    Result,
    frame_nbytes,
    redis_server,
    render_markdown,
    render_text,
)
from consumer import run_iox2_consumer, run_redis_consumer  # noqa: E402
from producer import run_producer  # noqa: E402

_run_id = 0


def _unique(prefix: str) -> str:
    global _run_id
    _run_id += 1
    return f"{prefix}_{os.getpid()}_{_run_id}"


def _consumer_target(transport: str):
    return run_iox2_consumer if transport.startswith("iox2") else run_redis_consumer


def _join_latencies(transport: str, prod: dict, cons: dict) -> list[float]:
    """Pair producer send times with consumer receive times → per-frame latency in µs."""
    if transport.startswith("iox2"):
        send = prod["send_times"]
        return [(recv - send[off]) / 1000.0 for off, recv in cons["recv_offsets"] if off in send]
    # Redis frames its own send timestamp, so the consumer already has per-frame latency.
    return cons["latencies_us"]


def _pass_with_consumer(transport, res, *, sync, n_buffers, framerate, redis_addr):
    """Start the consumer process, run the producer, return (producer_dict, consumer_dict)."""
    ctx = mp.get_context("spawn")
    ready, done, q = ctx.Event(), ctx.Event(), ctx.Queue()

    if transport.startswith("iox2"):
        service = "video/" + _unique("bench") + "/frame/v2"
        cfg = {"service": service, "expected": n_buffers}
        prod_kw = {"service": service}
    else:
        channel = _unique("bench_chan")
        cfg = {
            "host": redis_addr[0],
            "port": redis_addr[1],
            "channel": channel,
            "expected": n_buffers,
        }
        prod_kw = {"redis_addr": redis_addr, "channel": channel}

    proc = ctx.Process(target=_consumer_target(transport), args=(cfg, ready, done, q))
    proc.start()
    try:
        if not ready.wait(timeout=15):
            raise RuntimeError(f"{transport} consumer did not become ready")
        prod = run_producer(
            transport, res, sync=sync, n_buffers=n_buffers, framerate=framerate, **prod_kw
        )
        done.set()
        try:
            cres = q.get(timeout=8)
        except queue.Empty:
            cres = {"received": 0, "latencies_us": [], "first_ns": 0, "last_ns": 0}
    finally:
        done.set()
        proc.join(timeout=8)
        if proc.is_alive():
            proc.terminate()
            proc.join(timeout=3)
    return prod, cres


def benchmark(
    transports, resolutions, frames, latency_frames, latency_fps, redis_addr
) -> list[Result]:
    results: list[Result] = []
    for res in resolutions:
        name, w, h = res
        print(f"\n### {name} ###", flush=True)

        if "fakesink" in transports:
            print("  fakesink (baseline) ...", flush=True)
            base = run_producer("fakesink", res, sync=False, n_buffers=frames, framerate=0)
            r = Result("fakesink", name, w, h)
            r.frames_sent = base["frames"]
            r.producer_wall_s = base["wall_s"]
            r.producer_cpu_s = base["cpu_s"]
            r.payload_bytes = base["payload_bytes"]
            results.append(r)

        for transport in transports:
            if transport == "fakesink":
                continue
            if transport == "redis" and frame_nbytes(w, h) > REDIS_MAX_BYTES:
                print(
                    f"  redis: SKIPPED ({frame_nbytes(w, h) / 1e6:.1f} MB/frame too large)",
                    flush=True,
                )
                continue
            print(f"  {transport}: throughput pass ...", flush=True)
            prod, cons = _pass_with_consumer(
                transport, res, sync=False, n_buffers=frames, framerate=0, redis_addr=redis_addr
            )
            r = Result(transport, name, w, h)
            r.frames_sent = prod["frames"]
            r.producer_wall_s = prod["wall_s"]
            r.producer_cpu_s = prod["cpu_s"]
            r.payload_bytes = prod["payload_bytes"]
            r.copies_per_frame = prod["copies"]
            r.copies_note = prod["copies_note"]
            r.frames_received = cons["received"]
            if prod["error"]:
                print(f"    ! producer error: {prod['error']}", flush=True)

            print(f"  {transport}: latency pass ({latency_fps} fps) ...", flush=True)
            # Pace at the source (is-live) with the sink publishing immediately (sync=false), so the
            # recorded send instant is the publish instant — not the sink's clock-sync wait.
            prod2, cons2 = _pass_with_consumer(
                transport,
                res,
                sync=False,
                n_buffers=latency_frames,
                framerate=latency_fps,
                redis_addr=redis_addr,
            )
            r.latencies_us = _join_latencies(transport, prod2, cons2)
            results.append(r)
            print(
                f"    sent={r.frames_sent} recv={r.frames_received} "
                f"prodFPS={r.producer_fps:.0f} CPUms/f={r.cpu_ms_per_frame:.3f} "
                f"p50={r.pct(50):.0f}us p99={r.pct(99):.0f}us",
                flush=True,
            )
    return results


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--frames", type=int, default=600, help="frames for the throughput/CPU pass")
    ap.add_argument("--latency-frames", type=int, default=240, help="frames for the latency pass")
    ap.add_argument("--latency-fps", type=int, default=60, help="rate limit for the latency pass")
    ap.add_argument(
        "--transports",
        default="fakesink,iox2-zerocopy,iox2-onecopy,redis",
        help="comma-separated subset to run",
    )
    ap.add_argument(
        "--resolutions",
        default="",
        help="comma-separated subset of resolution names, e.g. 640x640,1280x720",
    )
    ap.add_argument("--out", default=os.path.join(_THIS, "RESULTS.md"))
    ap.add_argument("--json", default=os.path.join(_THIS, "results.json"))
    args = ap.parse_args(argv)

    transports = [t.strip() for t in args.transports.split(",") if t.strip()]
    resolutions = RESOLUTIONS
    if args.resolutions:
        wanted = {x.strip() for x in args.resolutions.split(",")}
        resolutions = [r for r in RESOLUTIONS if r[0] in wanted]

    redis_ctx = redis_server() if "redis" in transports else contextlib.nullcontext((None, None))
    with redis_ctx as redis_addr:
        results = benchmark(
            transports, resolutions, args.frames, args.latency_frames, args.latency_fps, redis_addr
        )

    meta = {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cpu_count": os.cpu_count(),
        "python": platform.python_version(),
        "pixel_format": "BGR",
        "throughput_pass": f"{args.frames} frames, unthrottled (free-run source, sink sync=false)",
        "latency_pass": (
            f"{args.latency_frames} frames @ {args.latency_fps} fps "
            "(live source paced, sink sync=false so send==publish instant)"
        ),
    }

    print(render_text(results))
    md = render_markdown(results, meta)
    with open(args.out, "w") as f:
        f.write(md)
    with open(args.json, "w") as f:
        json.dump({"meta": meta, "results": [dataclasses.asdict(r) for r in results]}, f, indent=2)
    print(f"\nWrote {args.out} and {args.json}")


if __name__ == "__main__":
    main()
