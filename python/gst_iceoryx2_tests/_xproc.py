"""Cross-process helpers for the integration tests.

iceoryx2 (>= 0.9) does **not** support two separately-linked iceoryx2 instances in a single process
(the GStreamer plugin's copy + the Python `iceoryx2` binding's copy share a PID and its node monitor
collides). Real deployments run the GStreamer pipeline and the Python SDK in **separate processes**, so
the integration tests that mix them do the same: the main pytest process drives the GStreamer pipeline
(the plugin), and all Python-SDK iceoryx2 work runs in a spawned child process.

These child functions are top-level + picklable so `multiprocessing` (spawn) can run them. Each imports
`gst_iceoryx2.video` lazily inside the child and reports its result back over a `Queue`; an exception is
returned as `{"error": ...}` so the parent can assert on it instead of hanging.
"""

from __future__ import annotations

import multiprocessing as mp
import time

# spawn (not fork): the child must be a *fresh* interpreter that never inherited the loaded plugin .so,
# so it hosts only the Python binding's iceoryx2 instance.
_CTX = mp.get_context("spawn")


def spawn(target, *args):
    """Start `target(q, ready, go, *args)` in a spawned child. Returns `(proc, q, ready, go)`:
    `ready` is set by the child once its ports are up; `go` lets the parent gate the child (e.g. so a
    consumer pipeline is live before the child publishes); results come back on `q`."""
    q = _CTX.Queue()
    ready = _CTX.Event()
    go = _CTX.Event()
    proc = _CTX.Process(target=target, args=(q, ready, go, *args), daemon=True)
    proc.start()
    return proc, q, ready, go


def child_subscribe_collect(q, ready, go, service: str, n: int) -> None:
    """Child: subscribe via the SDK and collect up to `n` frames as plain (picklable) dicts, including
    a per-frame zero-copy check on `numpy_view()`."""
    import numpy as np
    from gst_iceoryx2.video import VideoFrameSubscriber

    try:
        sub = VideoFrameSubscriber(service)
        ready.set()  # subscriber is attached (open_or_create) before the parent starts the sink
        frames: list[dict] = []
        deadline = time.monotonic() + 15.0
        while len(frames) < n and time.monotonic() < deadline:
            f = sub.receive_blocking(block_ms=500)
            if f is None:
                continue
            pix = bytes(f.pixels)
            rec = {
                "format": f.header.format_name,
                "width": int(f.header.width),
                "height": int(f.header.height),
                "n_planes": int(f.header.n_planes),
                "stride0": int(f.header.stride[0]),
                "offset": int(f.header.offset),
                "pts": int(f.header.pts),
                "payload_len": len(pix),
                "payload": pix,
                "aux_size": int(f.header.aux_size),
                "aux": bytes(f.aux),
            }
            try:
                arr = f.numpy_view()
                rec["numpy_shape"] = tuple(int(x) for x in arr.shape)
                # zero-copy: the view shares the pixel buffer's address (no copy on the hot path)
                rec["numpy_zero_copy"] = (
                    arr.__array_interface__["data"][0]
                    == np.frombuffer(f.pixels, dtype=np.uint8).__array_interface__["data"][0]
                )
            except NotImplementedError:
                rec["numpy_shape"] = None  # planar (I420/NV12) is not packed-reshapeable
                rec["numpy_zero_copy"] = None
            frames.append(rec)
        q.put(frames)
    except Exception as e:  # surface to the parent rather than hang
        q.put({"error": f"{type(e).__name__}: {e}"})


def child_publish(q, ready, go, service: str, max_bytes: int, specs: list) -> None:
    """Child: publish frames via the SDK. `specs` is a list of `(pixels_bytes, frame_params_kwargs)`.
    Waits for `go` so the parent's consumer pipeline can come up first."""
    from gst_iceoryx2.video import FrameParams, VideoFramePublisher

    try:
        pub = VideoFramePublisher(service, max_bytes=max_bytes)
        ready.set()
        go.wait(timeout=10.0)
        for pixels, params in specs:
            pub.publish_frame(pixels, FrameParams(**params))
        q.put("ok")
        pub.close()
    except Exception as e:
        q.put({"error": f"{type(e).__name__}: {e}"})


def child_hold_test(q, ready, go, service: str) -> None:
    """Child: the whole 'a held zero-copy frame survives later publishes' scenario in one process
    (pure SDK pub+sub, no GStreamer). Reports ``ok`` / ``stale`` / an error."""
    from gst_iceoryx2.video import FrameParams, VideoFramePublisher, VideoFrameSubscriber

    try:
        sub = VideoFrameSubscriber(service)
        pub = VideoFramePublisher(service, max_bytes=24)
        aa = bytes([0xAA] * 24)
        pub.publish_frame(aa, FrameParams(width=4, height=2, format="BGR"))
        first = sub.receive_blocking(block_ms=500)
        if first is None or bytes(first.pixels) != aa:
            q.put("no-frame")
            return
        arr = first.numpy_view()
        # flood the ring with other frames while still holding `first`
        for _ in range(20):
            pub.publish_frame(bytes([0xBB] * 24), FrameParams(width=4, height=2, format="BGR"))
        # the held frame's borrowed shared memory must be untouched (loan protected its slot)
        ok = bytes(first.pixels) == aa and int(arr[0, 0, 0]) == 0xAA
        pub.close()
        q.put("ok" if ok else "stale")
    except Exception as e:
        q.put({"error": f"{type(e).__name__}: {e}"})
