"""
The native ``/v2`` video-frame wire format published by the ``iceoryx2sink`` element.

The sink publishes each frame as a split payload:

- the iceoryx2 ``Slice[u8]`` payload is **raw pixels at offset 0** (so the GStreamer
  buffer memory is published with no copy), optionally followed by a variable-length
  **aux blob** (the full caps string + serialisable metas);
- a fixed ``VideoFrameHeader`` rides as the per-sample **user-header**.

This is the canonical Python mirror of that header — a byte-for-byte ctypes copy of the
Rust ``#[repr(C)] VideoFrameHeader`` (``src/format.rs``). It is hand-written (not generated
from PyO3) because iceoryx2's Python binding requires a ``ctypes.Structure`` for the
user-header. ``test_header_equivalence`` pins it against the Rust-exported layout constants
(size 104, ``aux_size`` @ 52), so the two can never silently drift.

Receiver split rule (uniform, also used by the Rust source)::

    pixel_region = payload[: len(payload) - header.aux_size]
    aux_blob     = payload[len(payload) - header.aux_size :]

``aux_size == 0`` means no aux blob (e.g. the EOS sentinel, or a producer that reserves none).
"""

from __future__ import annotations

__all__ = [
    "FORMAT_LEN",
    "MAX_PLANES",
    "VIDEO_FRAME_HEADER_TYPE_NAME",
    "VideoFrameHeader",
    "format_to_numpy",
    "header_pixels_to_numpy",
    "parse_aux",
]

import ctypes
import struct
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import numpy as np
    import numpy.typing as npt

# Mirrors GST_VIDEO_MAX_PLANES and the Rust ``FORMAT_LEN`` / type name.
MAX_PLANES = 4
FORMAT_LEN = 16
# iceoryx2 matches services by user-header type name; this MUST equal the Rust
# ``#[type_name("VideoFrameHeader")]`` the plugin declares.
VIDEO_FRAME_HEADER_TYPE_NAME = "VideoFrameHeader"


class VideoFrameHeader(ctypes.Structure):
    """``#[repr(C)]`` mirror of the Rust ``VideoFrameHeader`` (104 B, align 8).

    Fields mirror ``GstVideoMeta`` + ``GstBuffer``: ``GstClockTime`` values are
    ``u64`` nanoseconds with ``u64::MAX`` meaning *none*; ``offset`` is the
    GStreamer buffer offset (the producer's frame counter). ``format`` is the
    null-padded ``gst_video_format_to_string`` name (e.g. ``b"BGR"``).
    """

    _fields_ = [
        ("pts", ctypes.c_uint64),
        ("dts", ctypes.c_uint64),
        ("duration", ctypes.c_uint64),
        ("offset", ctypes.c_uint64),
        ("flags", ctypes.c_uint64),
        ("width", ctypes.c_uint32),
        ("height", ctypes.c_uint32),
        ("n_planes", ctypes.c_uint32),
        # Bytes of the aux blob appended after the pixels; 0 if none. The pixel
        # region is ``payload[: len - aux_size]``; aux is ``payload[len - aux_size :]``.
        ("aux_size", ctypes.c_uint32),
        ("stride", ctypes.c_uint32 * MAX_PLANES),
        ("plane_offsets", ctypes.c_uint32 * MAX_PLANES),
        ("format", ctypes.c_char * FORMAT_LEN),
    ]

    @property
    def format_name(self) -> str:
        """The GStreamer format string, e.g. ``"BGR"``."""
        return bytes(self.format).split(b"\0", 1)[0].decode()


# Packed formats we can reshape to ``(H, W, C)``. Planar formats (I420/NV12)
# are published on this channel by no current producer, so they raise rather
# than silently mis-shape — add plane-aware handling here when needed.
_PACKED_FORMATS: dict[str, int] = {"BGR": 3, "RGB": 3, "BGRA": 4, "RGBA": 4}


def format_to_numpy(format_name: str) -> tuple["np.dtype", int]:
    """Map a GStreamer packed format string to ``(dtype, channels)``."""
    import numpy as np

    channels = _PACKED_FORMATS.get(format_name)
    if channels is None:
        raise NotImplementedError(
            f"non-packed/unsupported format {format_name!r}; supported: {sorted(_PACKED_FORMATS)}"
        )
    return np.dtype(np.uint8), channels


def header_pixels_to_numpy(header: VideoFrameHeader, pixels: bytes) -> "npt.NDArray[np.uint8]":
    """Reshape a packed pixel buffer to a contiguous ``(H, W, C)`` array (a copy).

    Honours ``stride[0]`` row padding: the buffer is ``stride[0] * height``
    bytes and each row's leading ``width * channels`` bytes are the pixels.
    """
    import numpy as np

    dtype, channels = format_to_numpy(header.format_name)
    width, height = int(header.width), int(header.height)
    stride0 = int(header.stride[0]) or width * channels
    row_bytes = width * channels

    flat = np.frombuffer(pixels, dtype=dtype, count=stride0 * height)
    rows = flat.reshape(height, stride0)
    img = rows[:, :row_bytes].reshape(height, width, channels)
    # contiguous copy so the caller can keep it after the sample is reclaimed
    return np.ascontiguousarray(img)


def parse_aux(aux: bytes) -> tuple[str | None, list[bytes]]:
    """Decode the aux blob, best-effort (never raises on truncation).

    Wire format (little-endian)::

        u32 caps_len | caps_str | u32 n_metas | (u32 meta_len, meta_bytes)*

    Returns ``(caps_string_or_None, list_of_serialised_meta_blobs)``. Trailing
    zero-padding (a zero-copy producer reserves a fixed tail) is ignored because
    the meta count bounds the read.
    """
    if len(aux) < 4:
        return None, []
    pos = 0
    (caps_len,) = struct.unpack_from("<I", aux, pos)
    pos += 4
    if pos + caps_len > len(aux):
        return None, []
    caps = aux[pos : pos + caps_len].decode(errors="replace") or None
    pos += caps_len

    if pos + 4 > len(aux):
        return caps, []
    (n_metas,) = struct.unpack_from("<I", aux, pos)
    pos += 4

    metas: list[bytes] = []
    for _ in range(n_metas):
        if pos + 4 > len(aux):
            break
        (mlen,) = struct.unpack_from("<I", aux, pos)
        pos += 4
        if pos + mlen > len(aux):
            break
        metas.append(aux[pos : pos + mlen])
        pos += mlen
    return caps, metas
