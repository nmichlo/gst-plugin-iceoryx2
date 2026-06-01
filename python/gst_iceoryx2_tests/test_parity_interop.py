"""Integration tests for the shm/unixfd **drop-in parity** features:

* **Full caps fidelity** — colorimetry/framerate/PAR survive sink→src (carried in the aux blob, not
  the fixed header). Mirrors `unixfd`'s `CAPS` command.
* **Arbitrary GstMeta passthrough** — a serialisable meta added on the producer reappears on the
  consumer. Mirrors `unixfd` serialising metas onto its socket.
* **Control-plane parity** — `num-clients` + `client-connected`/`client-disconnected` signals, and
  end-of-stream propagation. Mirrors `shmsink`/`unixfdsink`.

Each test runs a producer and a consumer pipeline concurrently over a private service. Requires
GStreamer (+ `videotestsrc`/`appsrc` from the base/good plugins) and iceoryx2 shared memory. Run via
`make test-integration`.
"""

from __future__ import annotations

import os
import time
from contextlib import contextmanager

import pytest

pytestmark = pytest.mark.integration

_COUNTER = 0


def _unique_service() -> str:
    global _COUNTER
    _COUNTER += 1
    return f"video/test_{os.getpid()}_{_COUNTER}/frame/v2"


@contextmanager
def _pipeline(gst, desc: str):
    """Launch a pipeline and guarantee it is torn down, even if the test body raises — otherwise a
    leaked PLAYING pipeline holds iceoryx2 resources and starves later tests."""
    pipeline = gst.parse_launch(desc)
    try:
        yield pipeline
    finally:
        pipeline.set_state(gst.State.NULL)


def _pull(out, gst, timeout=0.5):
    return out.emit("try-pull-sample", int(timeout * gst.SECOND))


def test_caps_fidelity_framerate_survives(gst):
    """Framerate is not in the fixed header — it only round-trips because the aux blob carries the
    full caps string. The source must therefore negotiate caps that still carry the framerate."""
    service = _unique_service()
    consumer_desc = (
        f"iceoryx2src name=src service={service} safe-overflow=true ! "
        f"appsink name=out sync=false max-buffers=10 drop=false"
    )
    producer_desc = (
        f"videotestsrc num-buffers=5 ! videoconvert ! "
        f"video/x-raw,format=BGR,width=64,height=64,framerate=30/1 ! "
        f"iceoryx2sink service={service} sync=false safe-overflow=true"
    )
    with _pipeline(gst, consumer_desc) as consumer, _pipeline(gst, producer_desc) as producer:
        out = consumer.get_by_name("out")
        consumer.set_state(gst.State.PLAYING)
        time.sleep(0.3)
        producer.set_state(gst.State.PLAYING)

        caps = None
        deadline = time.monotonic() + 15.0
        while caps is None and time.monotonic() < deadline:
            sample = _pull(out, gst)
            if sample is not None:
                caps = sample.get_caps()

    assert caps is not None, "no frame received through iceoryx2src"
    s = caps.get_structure(0)
    # GstFraction can't be marshalled through get_value(); use the typed accessor.
    ok, num, denom = s.get_fraction("framerate")
    assert ok, f"framerate lost — caps only carried geometry: {caps.to_string()}"
    assert (num, denom) == (30, 1)


def test_meta_passthrough_reference_timestamp(gst):
    """A serialisable `GstReferenceTimestampMeta` added on the producer reappears on the consumer,
    proving arbitrary-meta passthrough via the aux blob (parity with unixfd)."""
    service = _unique_service()
    width, height, n = 16, 16, 4
    ref_caps = gst.Caps.from_string("timestamp/x-iox2-test")

    consumer_desc = (
        f"iceoryx2src name=src service={service} safe-overflow=true ! "
        f"appsink name=out sync=false max-buffers=10 drop=false"
    )
    producer_desc = (
        f"appsrc name=in is-live=true do-timestamp=false format=time "
        f"caps=video/x-raw,format=BGR,width={width},height={height},framerate=30/1 ! "
        f"iceoryx2sink service={service} sync=false safe-overflow=true"
    )
    got_timestamps = []
    sent_timestamps = []
    with _pipeline(gst, consumer_desc) as consumer, _pipeline(gst, producer_desc) as producer:
        out = consumer.get_by_name("out")
        consumer.set_state(gst.State.PLAYING)
        time.sleep(0.3)

        inp = producer.get_by_name("in")
        producer.set_state(gst.State.PLAYING)

        for i in range(n):
            buf = gst.Buffer.new_wrapped(bytes(width * height * 3))
            ts = (i + 1) * 1000
            buf.add_reference_timestamp_meta(ref_caps, ts, gst.CLOCK_TIME_NONE)
            sent_timestamps.append(ts)
            assert inp.emit("push-buffer", buf) == gst.FlowReturn.OK
        inp.emit("end-of-stream")

        deadline = time.monotonic() + 15.0
        while len(got_timestamps) < n and time.monotonic() < deadline:
            sample = _pull(out, gst)
            if sample is None:
                continue
            meta = sample.get_buffer().get_reference_timestamp_meta()
            assert meta is not None, "ReferenceTimestampMeta did not survive the round-trip"
            got_timestamps.append(meta.timestamp)

    assert got_timestamps == sent_timestamps


def test_num_clients_and_signals(gst):
    """`num-clients` tracks connected subscribers and `client-connected` fires when one joins."""
    service = _unique_service()
    producer_desc = (
        f"videotestsrc is-live=true ! videoconvert ! "
        f"video/x-raw,format=BGR,width=64,height=64 ! "
        f"iceoryx2sink name=sink service={service} sync=false safe-overflow=true"
    )
    consumer_desc = f"iceoryx2src service={service} safe-overflow=true ! fakesink sync=false"
    connected_counts = []
    with _pipeline(gst, producer_desc) as producer:
        sink = producer.get_by_name("sink")
        sink.connect("client-connected", lambda _el, count: connected_counts.append(count))
        producer.set_state(gst.State.PLAYING)
        time.sleep(0.5)
        assert sink.get_property("num-clients") == 0, "no subscriber yet"

        with _pipeline(gst, consumer_desc) as consumer:
            consumer.set_state(gst.State.PLAYING)
            deadline = time.monotonic() + 5.0
            while sink.get_property("num-clients") < 1 and time.monotonic() < deadline:
                time.sleep(0.05)
            assert sink.get_property("num-clients") >= 1
            assert connected_counts and connected_counts[-1] >= 1


def test_eos_propagates_to_consumer(gst):
    """The sink publishes an EOS sentinel on end-of-stream; the source turns it back into a GStreamer
    EOS so the consumer pipeline terminates cleanly (mirrors unixfd's EOS command)."""
    service = _unique_service()
    consumer_desc = f"iceoryx2src service={service} safe-overflow=true ! fakesink sync=false"
    producer_desc = (
        f"videotestsrc num-buffers=3 ! videoconvert ! "
        f"video/x-raw,format=BGR,width=64,height=64 ! "
        f"iceoryx2sink service={service} sync=false safe-overflow=true"
    )
    with _pipeline(gst, consumer_desc) as consumer, _pipeline(gst, producer_desc) as producer:
        bus = consumer.get_bus()
        consumer.set_state(gst.State.PLAYING)
        time.sleep(0.3)
        producer.set_state(gst.State.PLAYING)
        msg = bus.timed_pop_filtered(10 * gst.SECOND, gst.MessageType.EOS)
        assert msg is not None, "consumer never received EOS from iceoryx2src"
