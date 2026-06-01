"""Unit tests for the GStreamer-free ``gst_iceoryx2.video`` SDK helpers.

Pure ctypes + numpy + struct — no GStreamer, no compiled ``.so``, no IPC. These pin the
aux-blob decoder, the packed-pixel reshape, and the sink-config rendering; the byte-for-byte
header layout is pinned separately by ``test_header_equivalence`` against the Rust constants.
"""

from __future__ import annotations

import struct

import numpy as np
import pytest
from gst_iceoryx2.video import (
    Iceoryx2SinkConfig,
    VideoFrameHeader,
    format_to_numpy,
    header_pixels_to_numpy,
    parse_aux,
)

# ========================================================================= #
# VideoFrameHeader helpers
# ========================================================================= #


def test_header_format_name_strips_padding():
    h = VideoFrameHeader()
    h.format = b"BGR"
    assert h.format_name == "BGR"


# ========================================================================= #
# Aux-blob decoder
# ========================================================================= #


def _encode_aux(caps: str, metas: list[bytes]) -> bytes:
    out = struct.pack("<I", len(caps)) + caps.encode()
    out += struct.pack("<I", len(metas))
    for m in metas:
        out += struct.pack("<I", len(m)) + m
    return out


def test_parse_aux_roundtrip():
    blob = _encode_aux("video/x-raw, format=(string)BGR", [b"\x01\x02", b"meta2"])
    caps, metas = parse_aux(blob)
    assert caps == "video/x-raw, format=(string)BGR"
    assert metas == [b"\x01\x02", b"meta2"]


def test_parse_aux_ignores_trailing_padding():
    blob = _encode_aux("video/x-raw", []) + b"\x00" * 32
    caps, metas = parse_aux(blob)
    assert caps == "video/x-raw"
    assert metas == []


def test_parse_aux_empty_and_truncated_are_safe():
    assert parse_aux(b"") == (None, [])
    assert parse_aux(b"\x03\x00") == (None, [])  # < 4 bytes after declaring len
    # caps_len overruns the buffer -> best-effort returns nothing
    assert parse_aux(struct.pack("<I", 999)) == (None, [])


# ========================================================================= #
# Pixel reshaping
# ========================================================================= #


def test_format_to_numpy():
    dtype, channels = format_to_numpy("BGR")
    assert dtype == np.uint8
    assert channels == 3
    with pytest.raises(NotImplementedError):
        format_to_numpy("I420")


def test_header_pixels_to_numpy_packed_no_padding():
    h = VideoFrameHeader()
    h.width, h.height, h.n_planes = 4, 2, 1
    h.stride[0] = 4 * 3
    h.format = b"BGR"
    pixels = bytes(range(4 * 2 * 3))
    arr = header_pixels_to_numpy(h, pixels)
    assert arr.shape == (2, 4, 3)
    assert arr.flags["C_CONTIGUOUS"]
    assert bytes(arr.reshape(-1)) == pixels


def test_header_pixels_to_numpy_honours_row_stride():
    # stride wider than width*channels: trailing row bytes are padding to drop
    h = VideoFrameHeader()
    h.width, h.height, h.n_planes = 2, 2, 1
    h.stride[0] = 8  # 2*3 = 6 pixel bytes + 2 padding per row
    h.format = b"BGR"
    row0 = bytes([1, 2, 3, 4, 5, 6, 0, 0])
    row1 = bytes([7, 8, 9, 10, 11, 12, 0, 0])
    arr = header_pixels_to_numpy(h, row0 + row1)
    assert arr.shape == (2, 2, 3)
    assert list(arr[0].reshape(-1)) == [1, 2, 3, 4, 5, 6]
    assert list(arr[1].reshape(-1)) == [7, 8, 9, 10, 11, 12]


# ========================================================================= #
# Iceoryx2SinkConfig (de-policied: no service-name baked in)
# ========================================================================= #


def test_sink_config_gst_properties():
    cfg = Iceoryx2SinkConfig(service="video/cam0/frame/v2", max_bytes=640 * 640 * 3)
    props = cfg.gst_properties()
    assert props["service"] == "video/cam0/frame/v2"
    assert props["max-bytes"] == 640 * 640 * 3
    assert props["buffer-size"] == 10
    assert props["borrowed-max"] == 10
    assert props["history-size"] == 0
    assert props["safe-overflow"] is True


def test_sink_config_defaults_max_bytes_zero():
    # 0 lets the element derive the slice length from the negotiated caps (no policy baked in)
    assert Iceoryx2SinkConfig(service="video/x/frame/v2").max_bytes == 0
