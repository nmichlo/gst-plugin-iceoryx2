"""The cross-language layout contract: the ``gst_iceoryx2.video`` SDK's ctypes
``VideoFrameHeader`` must match the Rust ``#[repr(C)]`` struct byte-for-byte.

This is the single pin that keeps the hand-written ctypes mirror and the Rust struct from
drifting. The Rust side emits the canonical layout into the committed ``header_layout.json`` golden
(see ``crates/gst-plugin-iceoryx2-video/tests/header_layout_golden.rs``); this test reads that file,
so it needs **no compiled module, no GStreamer, and no Rust toolchain** — it runs in the fast unit
suite and even guards the Python mirror on a machine that cannot build the plugin.
"""

from __future__ import annotations

import ctypes
import json
from pathlib import Path

from gst_iceoryx2.video import (
    FORMAT_LEN,
    HEADER_TYPE_NAME,
    MAX_PLANES,
    VideoFrameHeader,
)

GOLDEN = json.loads((Path(__file__).parent / "header_layout.json").read_text())


def test_header_size_and_align():
    assert ctypes.sizeof(VideoFrameHeader) == GOLDEN["size"] == 104
    assert ctypes.alignment(VideoFrameHeader) == GOLDEN["align"] == 8


def test_field_offsets_match():
    rust_offsets = GOLDEN["field_offsets"]
    py_fields = {name for name, *_ in VideoFrameHeader._fields_}
    assert set(rust_offsets) == py_fields, "field sets differ between Rust and Python"
    for name, rust_off in rust_offsets.items():
        assert getattr(VideoFrameHeader, name).offset == rust_off, f"offset mismatch: {name}"


def test_type_name_matches():
    assert HEADER_TYPE_NAME == GOLDEN["type_name"] == "VideoFrameHeader"


def test_constants_match():
    assert GOLDEN["constants"]["MAX_PLANES"] == MAX_PLANES == 4
    assert GOLDEN["constants"]["FORMAT_LEN"] == FORMAT_LEN == 16
