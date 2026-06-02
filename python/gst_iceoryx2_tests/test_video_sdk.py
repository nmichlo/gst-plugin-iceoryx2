"""Unit tests for the GStreamer-free ``gst_iceoryx2.video`` SDK helpers.

Pure ctypes + numpy + struct — no GStreamer, no compiled ``.so``, no IPC. These pin the
aux-blob codec, the packed-pixel reshape, geometry validation, and the sink-config rendering;
the byte-for-byte header layout is pinned separately by ``test_header_equivalence`` and the
Rust↔Python API surface by ``test_api_parity``.

The ``build_aux`` / ``validate_geometry`` / ``plane_heights`` cases mirror the Rust unit tests in
``crates/gst-plugin-iceoryx2-video/src/{auxblob,validate}.rs``, so the ported logic is proven
identical, not merely present.
"""

from __future__ import annotations

import struct

import pytest
from gst_iceoryx2.video import (
    FrameParams,
    Qos,
    SinkConfig,
    VideoFrameHeader,
    build_aux,
    format_channels,
    header_pixels_to_numpy,
    parse_aux,
    plane_heights,
    validate_geometry,
)

# ========================================================================= #
# VideoFrameHeader helpers
# ========================================================================= #


def test_header_format_name_strips_padding():
    h = VideoFrameHeader()
    h.format = b"BGR"
    assert h.format_name == "BGR"


# ========================================================================= #
# Aux-blob codec (build_aux / parse_aux — mirrors auxblob.rs)
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
    # ParsedAux is a NamedTuple, so it still compares equal to the plain (caps, metas) tuple.
    assert parse_aux(b"") == (None, [])
    assert parse_aux(b"\x03\x00") == (None, [])  # < 4 bytes after declaring len
    # caps_len overruns the buffer -> best-effort returns nothing
    assert parse_aux(struct.pack("<I", 999)) == (None, [])


def test_build_aux_roundtrips_through_parse_aux():
    caps = "video/x-raw, format=(string)BGR, width=(int)640, height=(int)480"
    metas = [b"\x01\x02\x03", b"\x09" * 40]
    parsed = parse_aux(build_aux(caps, metas))
    assert parsed.caps == caps
    assert parsed.metas == metas


def test_build_aux_limit_drops_overflowing_metas():
    # caps(4+11) + n_metas(4) = 19; one 4+10=14-byte meta fits under 34, the second does not.
    blob = build_aux("video/x-raw", [b"\x07" * 10, b"\x08" * 10], limit=34)
    parsed = parse_aux(blob)
    assert len(parsed.metas) == 1, "only the first meta fits the reserve"
    assert parsed.metas[0] == b"\x07" * 10


# ========================================================================= #
# Pixel reshaping (format_channels / header_pixels_to_numpy)
# ========================================================================= #


def test_format_channels_packed_only():
    assert format_channels("BGR") == 3
    assert format_channels("RGBA") == 4
    # planar formats are validated but not single-array reshapeable -> not "packed"
    assert format_channels("I420") is None
    assert format_channels("nonsense") is None


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


def test_header_pixels_to_numpy_rejects_non_packed():
    h = VideoFrameHeader()
    h.width, h.height, h.n_planes = 4, 2, 3
    h.format = b"I420"
    with pytest.raises(NotImplementedError):
        header_pixels_to_numpy(h, bytes(64))


# ========================================================================= #
# Geometry validation (validate_geometry / plane_heights — mirrors validate.rs)
# ========================================================================= #


def _vh(fmt: bytes, height: int, n_planes: int, stride: list[int], offsets: list[int]):
    h = VideoFrameHeader()
    h.height, h.n_planes, h.format = height, n_planes, fmt
    for i, (s, o) in enumerate(zip(stride, offsets)):
        h.stride[i] = s
        h.plane_offsets[i] = o
    return h


def test_validate_geometry_packed_bgr_within_payload():
    # 4x2 BGR: stride 12, 2 rows -> 24 bytes.
    h = _vh(b"BGR", 2, 1, [12, 0, 0, 0], [0, 0, 0, 0])
    assert validate_geometry(h, 24) is None
    assert validate_geometry(h, 23) is not None, "one byte short must fail"


def test_validate_geometry_i420_three_planes():
    # 4x2 I420: Y 4x2=8 @0, U 2x1=2 @8, V 2x1=2 @10 -> 12 bytes.
    h = _vh(b"I420", 2, 3, [4, 2, 2, 0], [0, 8, 10, 0])
    assert validate_geometry(h, 12) is None
    assert validate_geometry(h, 11) is not None


def test_validate_geometry_rejects_bad_headers():
    # wrong plane count for the format
    assert validate_geometry(_vh(b"BGR", 2, 3, [12, 12, 12, 0], [0, 0, 0, 0]), 100_000)
    # oversized stride overruns the payload
    assert validate_geometry(_vh(b"BGR", 2, 1, [0xFFFFFFFF, 0, 0, 0], [0, 0, 0, 0]), 24)
    # unadvertised format cannot be validated -> rejected
    assert validate_geometry(_vh(b"RGBA", 2, 1, [16, 0, 0, 0], [0, 0, 0, 0]), 100_000)
    # zero planes
    assert validate_geometry(_vh(b"BGR", 2, 0, [0, 0, 0, 0], [0, 0, 0, 0]), 100_000)


def test_plane_heights():
    assert plane_heights("BGR", 480) == [480]
    assert plane_heights("I420", 480) == [480, 240, 240]
    assert plane_heights("NV12", 481) == [481, 241]  # ceil chroma
    assert plane_heights("RGBA", 480) is None  # not a SUPPORTED_FORMATS member


# ========================================================================= #
# Qos / FrameParams / SinkConfig
# ========================================================================= #


def test_qos_defaults_match_constants():
    q = Qos()
    assert (q.buffer_size, q.borrowed_max, q.history_size, q.safe_overflow) == (10, 10, 0, True)


def test_frame_params_defaults():
    p = FrameParams()
    assert (p.width, p.height, p.format, p.n_planes, p.stride0) == (0, 0, "BGR", 1, None)


def test_sink_config_gst_properties():
    cfg = SinkConfig(service="video/cam0/frame/v2", max_bytes=640 * 640 * 3)
    props = cfg.gst_properties()
    assert props["service"] == "video/cam0/frame/v2"
    assert props["max-bytes"] == 640 * 640 * 3
    assert props["buffer-size"] == 10
    assert props["borrowed-max"] == 10
    assert props["history-size"] == 0
    assert props["safe-overflow"] is True


def test_sink_config_defaults_max_bytes_zero():
    # 0 lets the element derive the slice length from the negotiated caps (no policy baked in)
    assert SinkConfig(service="video/x/frame/v2").max_bytes == 0
