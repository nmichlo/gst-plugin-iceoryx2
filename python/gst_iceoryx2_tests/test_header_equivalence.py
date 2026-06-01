"""The cross-language layout contract: the ``gst_iceoryx2.video`` SDK's ctypes
``VideoFrameHeader`` must match the Rust ``#[repr(C)]`` struct byte-for-byte.

This is the single pin that keeps the hand-written ctypes mirror and the Rust struct from
drifting. It loads the compiled module (for the Rust-exported layout constants) but needs no
GStreamer pipeline or IPC, so it runs in the fast unit suite.
"""

from __future__ import annotations

import ctypes

from gst_iceoryx2 import _gst_iceoryx2
from gst_iceoryx2.video import (
    FORMAT_LEN,
    MAX_PLANES,
    VIDEO_FRAME_HEADER_TYPE_NAME,
    VideoFrameHeader,
)


def test_header_size_and_align():
    assert ctypes.sizeof(VideoFrameHeader) == _gst_iceoryx2.HEADER_SIZE == 104
    assert ctypes.alignment(VideoFrameHeader) == _gst_iceoryx2.HEADER_ALIGN == 8


def test_field_offsets_match():
    rust_offsets = dict(_gst_iceoryx2.HEADER_FIELD_OFFSETS)
    py_fields = {name for name, *_ in VideoFrameHeader._fields_}
    assert set(rust_offsets) == py_fields, "field sets differ between Rust and Python"
    for name, rust_off in rust_offsets.items():
        assert getattr(VideoFrameHeader, name).offset == rust_off, f"offset mismatch: {name}"


def test_type_name_matches():
    assert VIDEO_FRAME_HEADER_TYPE_NAME == _gst_iceoryx2.HEADER_TYPE_NAME == "VideoFrameHeader"


def test_constants_match():
    assert _gst_iceoryx2.MAX_PLANES == MAX_PLANES == 4
    assert _gst_iceoryx2.FORMAT_LEN == FORMAT_LEN == 16
