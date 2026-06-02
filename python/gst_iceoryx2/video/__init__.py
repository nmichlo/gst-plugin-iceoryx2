"""GStreamer-free Python SDK for the native ``/v2`` iceoryx2 video format.

This subpackage is the consumer/producer half of the wire-format contract that the
``iceoryx2sink``/``iceoryx2src`` elements speak — implemented purely in ``ctypes`` +
``iceoryx2`` (+ lazily ``numpy``). It deliberately does **not** import the compiled GStreamer
plugin (``gst_iceoryx2._native``), so it can be imported and used to subscribe to (or publish)
frames with **no GStreamer runtime installed** — only the elements themselves need GStreamer.

- :class:`VideoFrameHeader` — the fixed user-header (byte-for-byte mirror of the Rust struct).
- :class:`VideoFrameSample` — a received frame (detached header + copied pixels/aux).
- :class:`Iox2VideoFramePublisher` / :class:`Iox2VideoFrameSubscriber` — the transport.
- :class:`Iceoryx2SinkConfig` — element properties for an ``iceoryx2sink``.
- :func:`parse_aux`, :func:`format_to_numpy`, :func:`header_pixels_to_numpy` — decode helpers.
"""

from __future__ import annotations

from gst_iceoryx2.video._header import (
    FORMAT_LEN,
    MAX_PLANES,
    VIDEO_FRAME_HEADER_TYPE_NAME,
    VideoFrameHeader,
    format_to_numpy,
    header_pixels_to_numpy,
    parse_aux,
)
from gst_iceoryx2.video._transport import (
    VIDEO_BORROWED_MAX,
    VIDEO_BUFFER_SIZE,
    VIDEO_HISTORY_SIZE,
    VIDEO_SAFE_OVERFLOW,
    Iceoryx2SinkConfig,
    Iox2VideoFramePublisher,
    Iox2VideoFrameSubscriber,
    VideoFrameSample,
)

__all__ = [
    "FORMAT_LEN",
    "MAX_PLANES",
    "VIDEO_FRAME_HEADER_TYPE_NAME",
    "VIDEO_BORROWED_MAX",
    "VIDEO_BUFFER_SIZE",
    "VIDEO_HISTORY_SIZE",
    "VIDEO_SAFE_OVERFLOW",
    "Iceoryx2SinkConfig",
    "Iox2VideoFramePublisher",
    "Iox2VideoFrameSubscriber",
    "VideoFrameHeader",
    "VideoFrameSample",
    "format_to_numpy",
    "header_pixels_to_numpy",
    "parse_aux",
]
