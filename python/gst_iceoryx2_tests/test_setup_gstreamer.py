"""Unit tests for `setup_gstreamer()` plugin registration (no IPC, no pipeline)."""

from __future__ import annotations

import gst_iceoryx2 as q


def test_setup_gstreamer_registers_elements(gst):
    """After setup, both element factories exist and can be instantiated."""
    for name in q.ELEMENT_NAMES:
        assert gst.ElementFactory.find(name) is not None, name
        assert gst.ElementFactory.make(name, None) is not None, name


def test_setup_gstreamer_idempotent(gst):
    """Calling setup_gstreamer twice is safe (symlink + scan are idempotent)."""
    q.setup_gstreamer()
    q.setup_gstreamer(verify=True)
    assert gst.ElementFactory.find(q.SRC_ELEMENT_NAME) is not None


def test_plugin_constants_match():
    """The Python and Rust sides agree on the element/plugin names."""
    assert q.SINK_ELEMENT_NAME == q._gst_iceoryx2.SINK_ELEMENT_NAME == "iceoryx2sink"
    assert q.SRC_ELEMENT_NAME == q._gst_iceoryx2.SRC_ELEMENT_NAME == "iceoryx2src"
    assert q.ELEMENT_NAME == q._gst_iceoryx2.ELEMENT_NAME == "iceoryx2sink"
    assert q.PLUGIN_NAME == q._gst_iceoryx2.PLUGIN_NAME == "iceoryx2"


def test_element_exposes_documented_properties(gst):
    """Contract sanity (gst-inspect-level): the element exposes the documented properties."""
    element = gst.ElementFactory.make(q.ELEMENT_NAME, None)
    names = {p.name for p in element.list_properties()}
    expected = {
        "service",
        "max-bytes",
        "buffer-size",
        "borrowed-max",
        "history-size",
        "safe-overflow",
        "wait-for-connection",
        "lossless",
        "aux-bytes",
        "num-clients",
        "frames-sent",
        "frames-zero-copy",
        "frames-copied",
    }
    assert expected <= names, f"missing properties: {expected - names}"
    assert element.get_property("service") == "video/default/frame/v2"


def test_source_exposes_documented_properties(gst):
    """Contract sanity: the source exposes its documented (subscriber-side) properties."""
    element = gst.ElementFactory.make(q.SRC_ELEMENT_NAME, None)
    names = {p.name for p in element.list_properties()}
    expected = {
        "service",
        "buffer-size",
        "borrowed-max",
        "history-size",
        "safe-overflow",
        "frames-received",
    }
    assert expected <= names, f"missing properties: {expected - names}"
    assert element.get_property("service") == "video/default/frame/v2"


def test_pads_advertise_broadened_formats(gst):
    """Both pad templates advertise the same broadened format set (BGR/RGB/I420/NV12)."""
    for name in q.ELEMENT_NAMES:
        factory = gst.ElementFactory.find(name)
        joined = " ".join(t.get_caps().to_string() for t in factory.get_static_pad_templates())
        assert "video/x-raw" in joined, name
        for fmt in ("BGR", "RGB", "I420", "NV12"):
            assert fmt in joined, f"{name} missing {fmt}"
