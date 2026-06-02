"""Integration tests for `iceoryx2src` — the zero-copy source, the inverse of `iceoryx2sink`.

A publisher pipeline (`videotestsrc ! videoconvert ! iceoryx2sink`) and a consumer pipeline
(`iceoryx2src ! appsink`) run concurrently in one process over a shared service. The frames pulled
from the consumer's `appsink` are compared byte-for-byte against a reference capture of the same
deterministic `videotestsrc` stream, proving the source reconstructs the published frames exactly
(including the header-driven caps + per-plane layout).

Requires GStreamer (+ `videotestsrc` from plugins-good) and iceoryx2 shared memory. Run via
`make test-integration`.
"""

from __future__ import annotations

import os
import time

import pytest

pytestmark = pytest.mark.integration

BUFFER_SIZE = 20
BORROWED_MAX = 20
HISTORY_SIZE = 0

_COUNTER = 0


def _unique_service() -> str:
    global _COUNTER
    _COUNTER += 1
    return f"video/test_{os.getpid()}_{_COUNTER}/frame/v2"


def _reference_frames(gst, fmt: str, width: int, height: int, n: int):
    """Capture the raw bytes of the deterministic `videotestsrc` stream straight from an appsink."""
    desc = (
        f"videotestsrc num-buffers={n} ! videoconvert ! "
        f"video/x-raw,format={fmt},width={width},height={height} ! "
        f"appsink name=out sync=false max-buffers={n + 5} drop=false"
    )
    pipeline = gst.parse_launch(desc)
    out = pipeline.get_by_name("out")
    pipeline.set_state(gst.State.PLAYING)

    frames = []
    deadline = time.monotonic() + 15.0
    while len(frames) < n and time.monotonic() < deadline:
        sample = out.emit("try-pull-sample", int(0.5 * gst.SECOND))
        if sample is None:
            continue
        buf = sample.get_buffer()
        ok, mi = buf.map(gst.MapFlags.READ)
        assert ok
        frames.append(bytes(mi.data))
        buf.unmap(mi)

    pipeline.set_state(gst.State.NULL)
    return frames


def _roundtrip_frames(gst, service: str, fmt: str, width: int, height: int, n: int):
    """Publish via `iceoryx2sink`, receive via `iceoryx2src`, return (frames, caps, src_element)."""
    # Consumer first, so its subscriber is live before the publisher starts pushing.
    consumer = gst.parse_launch(
        f"iceoryx2src name=src service={service} "
        f"buffer-size={BUFFER_SIZE} borrowed-max={BORROWED_MAX} "
        f"history-size={HISTORY_SIZE} safe-overflow=true ! "
        f"appsink name=out sync=false max-buffers={n + 5} drop=false"
    )
    out = consumer.get_by_name("out")
    src = consumer.get_by_name("src")
    consumer.set_state(gst.State.PLAYING)
    time.sleep(0.3)  # let the subscriber + listener come up

    producer = gst.parse_launch(
        f"videotestsrc num-buffers={n} ! videoconvert ! "
        f"video/x-raw,format={fmt},width={width},height={height} ! "
        f"iceoryx2sink service={service} sync=false "
        f"buffer-size={BUFFER_SIZE} borrowed-max={BORROWED_MAX} "
        f"history-size={HISTORY_SIZE} safe-overflow=true"
    )
    producer.set_state(gst.State.PLAYING)

    frames, caps = [], None
    deadline = time.monotonic() + 15.0
    while len(frames) < n and time.monotonic() < deadline:
        sample = out.emit("try-pull-sample", int(0.5 * gst.SECOND))
        if sample is None:
            continue
        if caps is None:
            caps = sample.get_caps()
        buf = sample.get_buffer()
        ok, mi = buf.map(gst.MapFlags.READ)
        assert ok
        frames.append({"bytes": bytes(mi.data), "offset": buf.offset, "pts": buf.pts})
        buf.unmap(mi)

    received = src.get_property("frames-received")
    producer.set_state(gst.State.NULL)
    consumer.set_state(gst.State.NULL)
    return frames, caps, received


def test_source_roundtrip_bgr(gst):
    """Crown jewel of the source: BGR frames survive sink→src byte-for-byte with correct caps."""
    service = _unique_service()
    ref = _reference_frames(gst, "BGR", 640, 640, 5)
    assert len(ref) == 5, "reference capture failed"

    frames, caps, received = _roundtrip_frames(gst, service, "BGR", 640, 640, 5)

    assert len(frames) == 5, "did not receive all frames through iceoryx2src"
    assert received >= 5

    s = caps.get_structure(0)
    assert s.get_name() == "video/x-raw"
    assert s.get_value("format") == "BGR"
    assert (s.get_value("width"), s.get_value("height")) == (640, 640)

    # The source reconstructs the exact published pixels.
    for got, want in zip(frames, ref):
        assert got["bytes"] == want

    offsets = [f["offset"] for f in frames]
    assert offsets == sorted(offsets)


def test_source_roundtrip_i420(gst):
    """Multi-plane (I420) round-trips correctly — exercises the broadened caps + per-plane layout."""
    service = _unique_service()
    ref = _reference_frames(gst, "I420", 640, 480, 4)
    assert len(ref) == 4

    frames, caps, _ = _roundtrip_frames(gst, service, "I420", 640, 480, 4)

    assert len(frames) == 4
    s = caps.get_structure(0)
    assert s.get_value("format") == "I420"
    assert (s.get_value("width"), s.get_value("height")) == (640, 480)
    for got, want in zip(frames, ref):
        assert got["bytes"] == want


def test_source_drops_invalid_geometry(gst):
    """A frame whose header declares a layout larger than its payload is dropped, not fatal.

    Publishes (via the SDK, which lets us set an arbitrary `stride`) one frame whose `stride[0]`
    makes the declared plane extent far exceed the pixel payload, then a valid frame. The source
    must reject the bad frame (so it never reaches downstream and `frames-received` stays at the
    valid count) while the pipeline keeps running and still delivers the good frame.
    """
    from gst_iceoryx2.video import FrameParams, VideoFramePublisher

    service = _unique_service()
    width, height = 16, 16
    pixels = bytes(width * height * 3)  # 768 bytes of BGR

    consumer = gst.parse_launch(
        f"iceoryx2src name=src service={service} ! "
        f"appsink name=out sync=false max-buffers=10 drop=false"
    )
    out = consumer.get_by_name("out")
    src = consumer.get_by_name("src")
    consumer.set_state(gst.State.PLAYING)
    time.sleep(0.3)  # let the subscriber + listener come up

    pub = VideoFramePublisher(service, max_bytes=len(pixels))
    # Bad frame: stride[0] = width*3*100, so extent = stride*height ≫ 768-byte payload → rejected.
    pub.publish_frame(
        pixels, FrameParams(width=width, height=height, format="BGR", stride0=width * 3 * 100)
    )
    # Good frame: default stride (width*3) → valid; proves the source survives and resumes.
    pub.publish_frame(pixels, FrameParams(width=width, height=height, format="BGR"))

    frames = []
    deadline = time.monotonic() + 5.0
    while len(frames) < 1 and time.monotonic() < deadline:
        sample = out.emit("try-pull-sample", int(0.5 * gst.SECOND))
        if sample is None:
            continue
        buf = sample.get_buffer()
        ok, mi = buf.map(gst.MapFlags.READ)
        assert ok
        frames.append(bytes(mi.data))
        buf.unmap(mi)

    err = consumer.get_bus().poll(gst.MessageType.ERROR, 0)
    received = src.get_property("frames-received")
    pub.close()
    consumer.set_state(gst.State.NULL)

    assert err is None, "pipeline errored on an invalid-geometry frame instead of dropping it"
    assert len(frames) == 1, (
        "the valid frame should still be delivered after the bad one is dropped"
    )
    assert received == 1, "only the valid frame counts (the malformed one is dropped pre-delivery)"
