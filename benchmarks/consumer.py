"""Consumer side of the benchmark — runs in a *separate process* so it genuinely competes for
nothing with the producer (mirroring streaming-service → inference-service in production).

Latency is recovered on the same host monotonic clock, drop-robustly:

  * iceoryx2 carries the frame counter in ``header.offset`` → the consumer reports
    ``(offset, recv_ns)`` and the orchestrator joins it to the producer's ``send_time[offset]``.
  * Redis has no metadata channel, so the producer frames the send timestamp into the payload and the
    consumer computes the latency directly — correct even when frames are dropped.

Stops once it has seen ``expected`` frames, or shortly after the producer sets ``done_evt``.
"""

from __future__ import annotations

import ctypes
import os
import sys
import time

# Make the sibling modules importable when this runs as a spawned subprocess.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common import BORROWED_MAX, BUFFER_SIZE, HISTORY_SIZE, STAMP, now_ns  # noqa: E402

_GRACE_NS = 750_000_000  # keep draining for 0.75 s after the producer says it is done


def run_iox2_consumer(cfg: dict, ready_evt, done_evt, result_q) -> None:
    import iceoryx2 as iox2
    from gst_iceoryx2.video import VideoFrameHeader

    node = iox2.NodeBuilder.new().create(iox2.ServiceType.Ipc)
    svc = (
        node.service_builder(iox2.ServiceName.new(cfg["service"]))
        .publish_subscribe(iox2.Slice[ctypes.c_uint8])
        .user_header(VideoFrameHeader)
        .enable_safe_overflow(True)
        .subscriber_max_buffer_size(BUFFER_SIZE)
        .subscriber_max_borrowed_samples(BORROWED_MAX)
        .history_size(HISTORY_SIZE)
        .open_or_create()
    )
    sub = svc.subscriber_builder().create()
    expected = cfg["expected"]

    ready_evt.set()
    recv: list[tuple[int, int]] = []  # (header.offset, recv_ns)
    grace_end = None
    while True:
        sample = sub.receive()
        if sample is not None:
            recv.append((int(sample.user_header().contents.offset), now_ns()))
            grace_end = None
            if len(recv) >= expected:
                break
        elif done_evt.is_set():
            if grace_end is None:
                grace_end = now_ns() + _GRACE_NS
            elif now_ns() > grace_end:
                break
        # else: busy-poll (no sleep) so the measured latency reflects the transport, not a poll
        # interval. Production wakes on the iceoryx2 event Listener instead — same or lower latency,
        # without spinning a core.

    result_q.put({"received": len(recv), "recv_offsets": recv})
    del sub, svc, node


def run_redis_consumer(cfg: dict, ready_evt, done_evt, result_q) -> None:
    import redis

    client = redis.Redis(host=cfg["host"], port=cfg["port"])
    pubsub = client.pubsub()
    pubsub.subscribe(cfg["channel"])
    # Drain the SUBSCRIBE confirmation so the server has registered us before the producer starts —
    # otherwise leading frames are lost and the order-aligned join would be skewed.
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        m = pubsub.get_message(timeout=0.1)
        if m and m.get("type") == "subscribe":
            break
    expected = cfg["expected"]

    ready_evt.set()
    latencies: list[
        float
    ] = []  # µs; computed directly from the framed send timestamp (drop-robust)
    grace_end = None
    while True:
        m = pubsub.get_message(timeout=0.05)
        if m is not None and m.get("type") == "message":
            recv = now_ns()
            send = STAMP.unpack_from(m["data"], 0)[0]
            latencies.append((recv - send) / 1000.0)
            grace_end = None
            if len(latencies) >= expected:
                break
        elif done_evt.is_set():
            if grace_end is None:
                grace_end = now_ns() + _GRACE_NS
            elif now_ns() > grace_end:
                break

    result_q.put({"received": len(latencies), "latencies_us": latencies})
    pubsub.close()
    client.close()
