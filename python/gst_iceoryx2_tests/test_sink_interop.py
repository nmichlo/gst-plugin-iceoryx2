"""Integration tests — a real GStreamer pipeline feeding `iceoryx2sink`, read back by a real
Python iceoryx2 subscriber (the crown jewel).

Requires GStreamer (+ `videotestsrc` from plugins-good) and iceoryx2 shared memory. Run via
`make test-integration`.
"""

from __future__ import annotations

import ctypes
import os
import time

import iceoryx2 as iox2
import pytest
from gst_iceoryx2.video import VideoFrameHeader, parse_aux

pytestmark = pytest.mark.integration

# QoS — must match the iceoryx2sink defaults so open_or_create is compatible.
BUFFER_SIZE = 10
BORROWED_MAX = 10
HISTORY_SIZE = 0

_COUNTER = 0


def _unique_service() -> str:
    """A fresh service name per test, so stale shared-memory state never collides."""
    global _COUNTER
    _COUNTER += 1
    return f"video/test_{os.getpid()}_{_COUNTER}/frame/v2"


def _make_subscriber(service: str):
    node = iox2.NodeBuilder.new().create(iox2.ServiceType.Ipc)
    svc = (
        node.service_builder(iox2.ServiceName.new(service))
        .publish_subscribe(iox2.Slice[ctypes.c_uint8])
        .user_header(VideoFrameHeader)
        .enable_safe_overflow(True)
        .subscriber_max_buffer_size(BUFFER_SIZE)
        .subscriber_max_borrowed_samples(BORROWED_MAX)
        .history_size(HISTORY_SIZE)
        .open_or_create()
    )
    # keep node + service alive by returning them alongside the subscriber
    return node, svc, svc.subscriber_builder().create()


def _collect(gst, service: str, fmt: str, width: int, height: int, n: int):
    """Run `videotestsrc → iceoryx2sink` and collect `n` published frames from a subscriber."""
    node, svc, sub = _make_subscriber(service)

    desc = (
        f"videotestsrc num-buffers={n} ! videoconvert ! "
        f"video/x-raw,format={fmt},width={width},height={height} ! "
        f"iceoryx2sink name=sink service={service} "
        f"buffer-size={BUFFER_SIZE} borrowed-max={BORROWED_MAX} "
        f"history-size={HISTORY_SIZE} safe-overflow=true"
    )
    pipeline = gst.parse_launch(desc)
    pipeline.set_state(gst.State.PLAYING)

    frames = []
    deadline = time.monotonic() + 15.0
    while len(frames) < n and time.monotonic() < deadline:
        sample = sub.receive()
        if sample is None:
            time.sleep(0.005)
            continue
        h = sample.user_header().contents
        p = sample.payload()
        raw = bytes((ctypes.c_uint8 * p.len()).from_address(p.as_ptr()))
        # Payload is pixels followed by the aux blob; split on the header's aux_size.
        pixel_len = len(raw) - h.aux_size
        frames.append(
            {
                "format": bytes(h.format).split(b"\0", 1)[0].decode(),
                "width": h.width,
                "height": h.height,
                "n_planes": h.n_planes,
                "stride0": h.stride[0],
                "offset": h.offset,
                "pts": h.pts,
                "payload_len": pixel_len,
                "payload": raw[:pixel_len],
                "aux_size": h.aux_size,
                "aux": raw[pixel_len:],
            }
        )

    # Read the sink's debug counters while still PLAYING (stop() clears them).
    sink = pipeline.get_by_name("sink")
    counters = {
        "sent": sink.get_property("frames-sent"),
        "zero_copy": sink.get_property("frames-zero-copy"),
        "copied": sink.get_property("frames-copied"),
    }

    pipeline.set_state(gst.State.NULL)
    del sub, svc, node
    return frames, counters


def test_sink_publishes_to_python_subscriber(gst):
    """Crown jewel: 640x640 BGR frames arrive byte-correct with the right header metadata."""
    service = _unique_service()
    frames, _counters = _collect(gst, service, "BGR", 640, 640, 5)

    assert len(frames) == 5, "did not receive all published frames"
    for f in frames:
        assert f["format"] == "BGR"
        assert (f["width"], f["height"]) == (640, 640)
        assert f["n_planes"] == 1
        assert f["stride0"] >= 640 * 3
        # payload length matches the negotiated frame size (stride * height for packed BGR)
        assert f["payload_len"] == f["stride0"] * 640
        # videotestsrc produces non-blank frames
        assert any(b != 0 for b in f["payload"][:64])

    # frame ordering monotonic via GstBuffer.offset; pts non-decreasing
    offsets = [f["offset"] for f in frames]
    assert offsets == sorted(offsets)
    pts = [f["pts"] for f in frames]
    assert pts == sorted(pts)


def test_sink_parameterised(gst):
    """Caps-driven sizing: a different resolution yields the right stride/payload."""
    service = _unique_service()
    frames, _counters = _collect(gst, service, "BGR", 1280, 720, 3)

    assert len(frames) == 3
    for f in frames:
        assert (f["width"], f["height"]) == (1280, 720)
        assert f["stride0"] >= 1280 * 3
        assert f["payload_len"] == f["stride0"] * 720


def test_sink_aux_carries_full_caps(gst):
    """The aux blob carries the full caps string — the colorimetry/framerate/PAR fidelity the
    fixed header cannot hold (parity with unixfd's CAPS command)."""
    service = _unique_service()
    frames, _counters = _collect(gst, service, "BGR", 640, 640, 3)

    assert len(frames) == 3
    for f in frames:
        assert f["aux_size"] > 0, "default aux-bytes reserve should carry the caps"
        caps, metas = parse_aux(f["aux"])
        assert "video/x-raw" in caps
        assert "format=(string)BGR" in caps
        # framerate is not in the fixed header — it only survives via the aux caps string.
        assert "framerate" in caps


def test_zero_copy_path_used(gst):
    """Upstream renders into our pool's loaned samples → frames go out zero-copy, no memcpy."""
    service = _unique_service()
    frames, counters = _collect(gst, service, "BGR", 640, 640, 5)

    assert len(frames) == 5
    assert counters["sent"] == 5
    # videoconvert adopts the proposed iceoryx2 pool, so every frame is published without a copy.
    assert counters["zero_copy"] == 5, f"expected all frames zero-copy, got {counters}"
    assert counters["copied"] == 0, f"unexpected copy fallback, got {counters}"


def test_sink_rejects_unsupported_caps(gst):
    """A format outside the supported set (BGR/RGB/I420/NV12) fails caps negotiation cleanly.

    With fixed caps in the pipeline string, GStreamer links eagerly, so the rejection surfaces as a
    parse/link `GError`. If a version defers linking, it instead fails the state change or posts a
    bus error — accept any of these. (`RGBA` is a 4-channel format the sink does not advertise.)
    """
    from gi.repository import GLib

    service = _unique_service()
    desc = (
        f"videotestsrc num-buffers=3 ! videoconvert ! "
        f"video/x-raw,format=RGBA,width=320,height=240 ! "
        f"iceoryx2sink service={service}"
    )
    try:
        pipeline = gst.parse_launch(desc)
    except GLib.GError:
        return  # eager link refused I420 — the sink rejected it

    ret = pipeline.set_state(gst.State.PLAYING)
    bus = pipeline.get_bus()
    msg = bus.timed_pop_filtered(3 * gst.SECOND, gst.MessageType.ERROR | gst.MessageType.EOS)
    pipeline.set_state(gst.State.NULL)

    failed = ret == gst.StateChangeReturn.FAILURE
    errored = msg is not None and msg.type == gst.MessageType.ERROR
    assert failed or errored, "RGBA should not negotiate with the sink's advertised formats"


def test_sink_publishes_i420(gst):
    """Multi-plane I420 publishes with the right plane count + per-plane stride."""
    service = _unique_service()
    frames, _counters = _collect(gst, service, "I420", 640, 480, 3)

    assert len(frames) == 3
    for f in frames:
        assert f["format"] == "I420"
        assert (f["width"], f["height"]) == (640, 480)
        assert f["n_planes"] == 3
        assert f["stride0"] >= 640
        # I420 is fully planar: Y (w*h) + U + V (each w/2*h/2) = w*h*3/2.
        assert f["payload_len"] == 640 * 480 * 3 // 2
