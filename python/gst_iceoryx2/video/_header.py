"""
The native ``/v2`` video-frame wire format published by the ``iceoryx2sink`` element.

The sink publishes each frame as a split payload:

- the iceoryx2 ``Slice[u8]`` payload is **raw pixels at offset 0** (so the GStreamer
  buffer memory is published with no copy), optionally followed by a variable-length
  **aux blob** (the full caps string + serialisable metas);
- a fixed ``VideoFrameHeader`` rides as the per-sample **user-header**.

This is the canonical Python mirror of that header — a byte-for-byte ctypes copy of the
Rust ``#[repr(C)] VideoFrameHeader`` (``crates/gst-plugin-iceoryx2-video/src/header.rs``).
It is hand-written (not generated) because iceoryx2's Python binding requires a
``ctypes.Structure`` for the user-header. ``test_header_equivalence`` pins it against the
Rust-exported layout golden (size 104, ``aux_size`` @ 52), so the two can never silently drift.

Receiver split rule (uniform, also used by the Rust source)::

    pixel_region = payload[: len(payload) - header.aux_size]
    aux_blob     = payload[len(payload) - header.aux_size :]

``aux_size == 0`` means no aux blob (e.g. the EOS sentinel, or a producer that reserves none).
"""

from __future__ import annotations

__all__ = [
    "DEFAULT_AUX_BYTES",
    "FORMAT_LEN",
    "HEADER_ALIGN",
    "HEADER_FLAG_EOS",
    "HEADER_SIZE",
    "HEADER_TYPE_NAME",
    "MAX_PLANES",
    "PACKED_FORMATS",
    "VideoFrameHeader",
    "format_channels",
    "header_pixels_to_numpy_view",
]

import ctypes
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import numpy as np
    import numpy.typing as npt

# Mirrors GST_VIDEO_MAX_PLANES and the Rust ``FORMAT_LEN`` / type name.
MAX_PLANES = 4
FORMAT_LEN = 16
# iceoryx2 matches services by user-header type name; this MUST equal the Rust
# ``#[type_name("VideoFrameHeader")]`` the plugin declares (parity name with the Rust ``HEADER_TYPE_NAME``).
HEADER_TYPE_NAME = "VideoFrameHeader"
# Private sentinel bit (bit 32) marking an end-of-stream sample (carries no pixels). Mirrors the Rust
# ``HEADER_FLAG_EOS``: ``GstBufferFlags`` occupy only the low 32 bits, so bit 32 never collides.
HEADER_FLAG_EOS = 1 << 32
# Default aux-blob tail reserved after the pixels in a zero-copy sample (bytes). Mirrors the Rust
# ``DEFAULT_AUX_BYTES``.
DEFAULT_AUX_BYTES = 4096


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


# Named size/align constants (mirror the Rust ``HEADER_SIZE`` / ``HEADER_ALIGN``); pinned by the
# golden alongside the field offsets.
HEADER_SIZE = ctypes.sizeof(VideoFrameHeader)
HEADER_ALIGN = ctypes.alignment(VideoFrameHeader)


# The **packed** formats that reshape to a contiguous ``(H, W, C)`` array, as ``{name: channels}``.
# This is the *reshape* set (incl. 4-channel BGRA/RGBA) used by :func:`format_channels` and
# :func:`header_pixels_to_numpy` — deliberately distinct from ``validate.SUPPORTED_FORMATS`` (the
# negotiation/validation set, which also covers planar I420/NV12). Mirrors the Rust ``PACKED_FORMATS``.
PACKED_FORMATS: dict[str, int] = {"BGR": 3, "RGB": 3, "BGRA": 4, "RGBA": 4}


def format_channels(format_name: str) -> int | None:
    """Channels per pixel for a :data:`packed format <PACKED_FORMATS>` (pixels are always ``uint8``),
    or ``None`` for a non-packed/unrecognised format. Mirrors the Rust ``format_channels``."""
    return PACKED_FORMATS.get(format_name)


def header_pixels_to_numpy_view(header: VideoFrameHeader, pixels) -> "npt.NDArray[np.uint8]":
    """Borrow a packed pixel buffer as a **zero-copy** ``(H, W, C)`` uint8 view.

    ``pixels`` is any buffer-protocol object (a ``memoryview`` over the loaned shared memory, or
    ``bytes``); the returned array shares its memory — no copy. ``stride[0]`` row padding is expressed
    as a non-contiguous stride (so a padded frame yields a non-C-contiguous view); call ``.copy()`` /
    ``np.ascontiguousarray`` for an owned, contiguous array. Mirrors the Rust
    ``header_pixels_to_ndarray_view``; raises ``NotImplementedError`` for a non-packed format.

    The caller is responsible for keeping ``pixels`` (and whatever backs it) alive for the view's
    lifetime — e.g. :meth:`VideoFrame.numpy_view` is valid only while its frame is alive.
    """
    import numpy as np

    channels = format_channels(header.format_name)
    if channels is None:
        raise NotImplementedError(
            f"non-packed/unsupported format {header.format_name!r}; supported: {sorted(PACKED_FORMATS)}"
        )
    width, height = int(header.width), int(header.height)
    row_bytes = width * channels
    stride0 = int(header.stride[0]) or row_bytes
    if stride0 < row_bytes:
        raise ValueError(f"stride[0] {stride0} < row bytes {row_bytes}")

    needed = stride0 * height
    # `frombuffer` is a zero-copy view over `pixels`; `as_strided` reshapes to (H, W, C) honouring the
    # row stride (skipping any padding) — also zero-copy, never an allocation.
    flat = np.frombuffer(pixels, dtype=np.uint8, count=needed)
    return np.lib.stride_tricks.as_strided(
        flat, shape=(height, width, channels), strides=(stride0, channels, 1)
    )
