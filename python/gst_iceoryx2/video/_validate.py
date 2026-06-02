"""
Format metadata + untrusted-geometry validation, expressed over the format **name** (so it stays
GStreamer-free). The pure-Python mirror of the Rust ``validate`` module
(``crates/gst-plugin-iceoryx2-video/src/validate.rs``): both the ``iceoryx2src`` element and an SDK
:class:`~gst_iceoryx2.video.VideoFrameSubscriber` bounds-check a received header identically, whether
it is consumed through a pipeline or the SDK.
"""

from __future__ import annotations

__all__ = [
    "SUPPORTED_FORMATS",
    "plane_heights",
    "validate_geometry",
]

from gst_iceoryx2.video._header import MAX_PLANES, VideoFrameHeader

# The raw video formats both ends accept/produce, in negotiation-preference order — the single source
# of truth for the supported set. Distinct from ``PACKED_FORMATS`` (the numpy-reshapeable set). Mirrors
# the Rust ``SUPPORTED_FORMATS``.
SUPPORTED_FORMATS = ("BGR", "RGB", "I420", "NV12")


def plane_heights(format_name: str, height: int) -> list[int] | None:
    """Pixel rows per plane for a supported format at ``height`` — mirrors the subsampling the
    formats advertise. ``None`` for an unrecognised format (so :func:`validate_geometry` rejects it
    rather than guessing an extent). Mirrors the Rust ``plane_heights``.
    """
    chroma = (height + 1) // 2  # 4:2:0 chroma height (ceil)
    if format_name in ("BGR", "RGB"):
        return [height]
    if format_name == "I420":
        return [height, chroma, chroma]
    if format_name == "NV12":
        return [height, chroma]
    return None


def validate_geometry(header: VideoFrameHeader, pixel_size: int) -> str | None:
    """Reject a header whose declared per-plane layout would read past the ``pixel_size``-byte payload
    (or whose plane count is impossible).

    The header is untrusted wire data — any process on the same service can publish it — so this is
    the bound that keeps a malformed/hostile publisher from making the receiver read out of the
    buffer. Returns ``None`` for a sound frame, or a reason string to drop it (the parity counterpart
    of the Rust ``validate_geometry`` returning ``Result<(), String>``: ``None`` ≙ ``Ok``, the string
    ≙ ``Err``).
    """
    n = int(header.n_planes)
    if n == 0 or n > MAX_PLANES:
        return f"n_planes {n} out of range 1..={MAX_PLANES}"
    format_name = header.format_name
    heights = plane_heights(format_name, int(header.height))
    if heights is None:
        return f"unsupported format {format_name!r}"
    if len(heights) != n:
        return f"n_planes {n} != {len(heights)} expected for {format_name!r}"
    for i, rows in enumerate(heights):
        off = int(header.plane_offsets[i])
        stride = int(header.stride[i])
        extent = off + stride * rows
        if extent > pixel_size:
            return f"plane {i} extent {extent} exceeds payload {pixel_size}"
    return None
