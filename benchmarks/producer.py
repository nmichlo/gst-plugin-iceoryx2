"""Producer side — one GStreamer pipeline per transport, all sharing the same upstream
``videotestsrc ! videoconvert`` so only the tail (the sink) differs.

End-to-end latency is measured *without mutating the buffer* (mutating a zero-copy pool buffer would
force a copy and defeat the very path under test): a sink-pad probe records ``send_time[offset]`` for
each frame (``GstBuffer.offset`` is the frame counter that the sink also writes into the header), and
the consumer reports ``(offset, recv_time)``; the two are joined on the same host monotonic clock in
the orchestrator. Producer CPU is the whole-process user+system time across the run (the publish work
runs in the GStreamer streaming thread of this same process).
"""

from __future__ import annotations

import ctypes
import os

import psutil
from common import BORROWED_MAX
from common import BUFFER_SIZE
from common import HISTORY_SIZE
from common import PIXEL_FORMAT
from common import STAMP
from common import now_ns


def _gst():
    # Registers the iceoryx2sink plugin (and calls Gst.init) — idempotent.
    from gst_iceoryx2 import setup_gstreamer

    setup_gstreamer()

    import gi

    gi.require_version("Gst", "1.0")
    from gi.repository import Gst

    return Gst


def _pipeline_desc(transport, w, h, n, framerate, sync, *, service):
    head = (
        f"videotestsrc name=src num-buffers={n} is-live={'true' if framerate else 'false'} ! "
        f"videoconvert ! "
        f"video/x-raw,format={PIXEL_FORMAT},width={w},height={h},framerate={framerate or 30}/1 ! "
    )
    s = "true" if sync else "false"
    if transport == "fakesink":
        return head + f"fakesink name=sink sync={s}"
    if transport == "iox2-zerocopy":
        return head + (
            f"iceoryx2sink name=sink sync={s} service={service} "
            f"buffer-size={BUFFER_SIZE} borrowed-max={BORROWED_MAX} "
            f"history-size={HISTORY_SIZE} safe-overflow=true"
        )
    # one-copy and redis both terminate in an appsink that publishes from Python.
    return head + f"appsink name=sink sync={s} emit-signals=true max-buffers=2 drop=false"


class _OneCopyPublisher:
    """iceoryx2 publisher for the one-copy path: copy the frame bytes into a loaned slice."""

    def __init__(self, service, w, h):
        import iceoryx2 as iox2
        from gst_iceoryx2.video import VideoFrameHeader

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
        self._node, self._svc, self._pub = node, svc, None
        self._w, self._h = w, h

    def publish(self, data, pts, offset):
        n = len(data)
        if self._pub is None:
            self._pub = self._svc.publisher_builder().initial_max_slice_len(n).create()
        sample = self._pub.loan_slice_uninit(n)
        # The one explicit publish-side copy: Python bytes -> shared-memory slice.
        ctypes.memmove(sample.payload().as_ptr(), data, n)
        hdr = sample.user_header().contents
        hdr.pts = pts & 0xFFFFFFFFFFFFFFFF
        hdr.offset = offset & 0xFFFFFFFFFFFFFFFF
        hdr.width, hdr.height, hdr.n_planes = self._w, self._h, 1
        hdr.stride[0] = n // self._h
        hdr.format = PIXEL_FORMAT.encode()
        sample.assume_init().send()


def run_producer(
    transport,
    res,
    *,
    sync,
    n_buffers,
    framerate,
    service=None,
    redis_addr=None,
    channel=None,
):
    Gst = _gst()
    name, w, h = res

    pipeline = Gst.parse_launch(_pipeline_desc(transport, w, h, n_buffers, framerate, sync, service=service))
    sink = pipeline.get_by_name("sink")

    counter = {"n": 0, "first": 0, "last": 0, "payload": 0}
    send_times: dict[int, int] = {}  # offset -> monotonic-ns send time
    send_order: list[int] = []  # send times in emission order (for the order-aligned Redis join)

    # For the appsink paths the actual publish happens in the callback (after the appsink queue), so
    # the send time is recorded there; the probe only counts/sizes. For the in-pipeline sinks the
    # probe fires immediately before render() (sink sync=false), so it is the publish instant.
    appsink_path = transport in ("iox2-onecopy", "redis")

    def _probe(_pad, info, _u):
        buf = info.get_buffer()
        t = now_ns()
        if not appsink_path:
            send_times[buf.offset] = t
            send_order.append(t)
        counter["n"] += 1
        counter["payload"] = buf.get_size()
        if not counter["first"]:
            counter["first"] = t
        counter["last"] = t
        return Gst.PadProbeReturn.OK

    sink.get_static_pad("sink").add_probe(Gst.PadProbeType.BUFFER, _probe, None)

    copies, copies_note = 0.0, ""

    if transport == "iox2-onecopy":
        pub = _OneCopyPublisher(service, w, h)
        copies, copies_note = 2.0, "buffer→bytes + bytes→shm"

        def _on_sample(appsink):
            sample = appsink.emit("pull-sample")
            buf = sample.get_buffer()
            ok, mi = buf.map(Gst.MapFlags.READ)
            if not ok:
                return Gst.FlowReturn.ERROR
            try:
                t = now_ns()
                send_times[buf.offset] = t
                send_order.append(t)
                pub.publish(mi.data, buf.pts or 0, buf.offset)
            finally:
                buf.unmap(mi)
            return Gst.FlowReturn.OK

        sink.connect("new-sample", _on_sample)

    elif transport == "redis":
        import redis

        client = redis.Redis(host=redis_addr[0], port=redis_addr[1])
        # Redis has no free per-message metadata channel (unlike the iceoryx2 user-header), so the
        # 8-byte send timestamp must be framed into the payload — a second full-frame copy.
        copies, copies_note = 2.0, "buffer→bytes + framing (+ socket/server)"

        def _on_sample(appsink):
            sample = appsink.emit("pull-sample")
            buf = sample.get_buffer()
            ok, mi = buf.map(Gst.MapFlags.READ)
            if not ok:
                return Gst.FlowReturn.ERROR
            try:
                client.publish(channel, STAMP.pack(now_ns()) + mi.data)
            finally:
                buf.unmap(mi)
            return Gst.FlowReturn.OK

        sink.connect("new-sample", _on_sample)

    proc = psutil.Process(os.getpid())
    cpu0 = proc.cpu_times()
    pipeline.set_state(Gst.State.PLAYING)
    bus = pipeline.get_bus()
    msg = bus.timed_pop_filtered(60 * Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR)
    cpu1 = proc.cpu_times()
    pipeline.set_state(Gst.State.NULL)

    err = None
    if msg is not None and msg.type == Gst.MessageType.ERROR:
        gerr, dbg = msg.parse_error()
        err = f"{gerr.message} ({dbg})"

    if transport == "iox2-zerocopy":
        sent = max(1, sink.get_property("frames-sent"))
        copies = sink.get_property("frames-copied") / sent
        copies_note = "shm loan, no memcpy"

    cpu_s = (cpu1.user + cpu1.system) - (cpu0.user + cpu0.system)
    wall_s = (counter["last"] - counter["first"]) / 1e9 if counter["n"] > 1 else 0.0
    return {
        "frames": counter["n"],
        "wall_s": wall_s,
        "cpu_s": cpu_s,
        "payload_bytes": counter["payload"] or w * h * 3,
        "copies": copies,
        "copies_note": copies_note,
        "send_times": send_times,
        "send_order": send_order,
        "error": err,
    }
