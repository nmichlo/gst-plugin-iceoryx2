"""Integration tests — a real GStreamer pipeline feeding `iceoryx2sink`, read back by the real Python
iceoryx2 SDK (the crown jewel).

**Cross-process by necessity.** iceoryx2 (>= 0.9) does not support two separately-linked iceoryx2
instances in one process, and the GStreamer plugin (loaded into the main pytest process) and the Python
`iceoryx2` binding are exactly that. So the main process drives the **plugin pipeline**, and the Python
**SDK subscriber runs in a spawned child** (see `_xproc`) — which is also how real deployments are
structured (pipeline and SDK consumer in separate processes).

Requires GStreamer (+ `videotestsrc` from plugins-good) and iceoryx2 shared memory. Run via
`make test-integration`.
"""

from __future__ import annotations

import os

import pytest
from gst_iceoryx2.video import parse_aux
from gst_iceoryx2_tests import _xproc

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


def _collect(gst, service: str, fmt: str, width: int, height: int, n: int):
    """Run `videotestsrc → iceoryx2sink` in this process and collect the `n` published frames from a
    Python SDK subscriber running in a child process."""
    # Subscriber first (in a child), so it is attached before the sink starts publishing.
    proc, q, ready, _go = _xproc.spawn(_xproc.child_subscribe_collect, service, n)
    assert ready.wait(timeout=10.0), "subscriber child failed to start"

    desc = (
        f"videotestsrc num-buffers={n} ! videoconvert ! "
        f"video/x-raw,format={fmt},width={width},height={height} ! "
        f"iceoryx2sink name=sink service={service} "
        f"buffer-size={BUFFER_SIZE} borrowed-max={BORROWED_MAX} "
        f"history-size={HISTORY_SIZE} safe-overflow=true"
    )
    pipeline = gst.parse_launch(desc)
    pipeline.set_state(gst.State.PLAYING)
    # Wait for the publisher to finish (num-buffers → EOS), so every frame is on the ring.
    pipeline.get_bus().timed_pop_filtered(15 * gst.SECOND, gst.MessageType.EOS | gst.MessageType.ERROR)
    # Read the sink's debug counters while still PLAYING (stop() clears them).
    sink = pipeline.get_by_name("sink")
    counters = {
        "sent": sink.get_property("frames-sent"),
        "zero_copy": sink.get_property("frames-zero-copy"),
        "copied": sink.get_property("frames-copied"),
    }
    pipeline.set_state(gst.State.NULL)

    frames = q.get(timeout=15)
    proc.join(timeout=5)
    if isinstance(frames, dict):  # child returned an error payload
        raise AssertionError(f"subscriber child error: {frames.get('error')}")
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
        return  # eager link refused RGBA — the sink rejected it

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


def test_subscriber_numpy_view_is_zero_copy(gst):
    """The SDK `VideoFrameSubscriber.numpy_view()` reads frames zero-copy: it borrows the loaned shared
    memory rather than copying it (the whole point of the library). Checked in the subscriber child via
    address identity between the view and the pixel buffer."""
    service = _unique_service()
    frames, _counters = _collect(gst, service, "BGR", 64, 64, 5)

    assert frames, "no frames received from the sink"
    assert frames[0]["numpy_shape"] == (64, 64, 3)
    assert all(f["numpy_zero_copy"] for f in frames), "numpy_view copied instead of borrowing the shared-memory pixels"


def test_held_zero_copy_frame_survives_later_publishes():
    """A held zero-copy frame borrows a *stable* loan: iceoryx2 must not recycle its shared memory
    while it is alive, even as later frames are published (proves the borrow is real, not a copy). Runs
    entirely in a child (pure SDK pub+sub, no GStreamer)."""
    service = _unique_service()
    proc, q, _ready, _go = _xproc.spawn(_xproc.child_hold_test, service)
    res = q.get(timeout=20)
    proc.join(timeout=5)
    assert res == "ok", f"held zero-copy frame check failed: {res!r}"
