"""GStreamer-free Python SDK for the native ``/v2`` iceoryx2 video format.

This subpackage is the consumer/producer half of the wire-format contract that the
``iceoryx2sink``/``iceoryx2src`` elements speak — implemented purely in ``ctypes`` +
``iceoryx2`` (+ lazily ``numpy``). It deliberately does **not** import the compiled GStreamer
plugin (``gst_iceoryx2._native``), so it can be imported and used to subscribe to (or publish)
frames with **no GStreamer runtime installed** — only the elements themselves need GStreamer.

It shares one neutral vocabulary with the Rust core crate ``gst-plugin-iceoryx2-video``, kept in
lockstep by ``PARITY.md`` + the ``api_manifest.json`` golden (asserted by ``test_api_parity``):

- :class:`VideoFrameHeader` — the fixed user-header (byte-for-byte mirror of the Rust struct).
- :class:`VideoFrame` — a received frame (zero-copy views over the loaned shared memory).
- :class:`VideoFramePublisher` / :class:`VideoFrameSubscriber` — the transport.
- :class:`FrameParams` — per-frame publish parameters; :class:`Qos` — pub/sub QoS.
- :class:`SinkConfig` — element properties for an ``iceoryx2sink``.
- :func:`create_node`, :func:`open_video_service`, :func:`create_notifier`, :func:`create_listener`
  — the port-construction helpers.
- :func:`build_aux`, :func:`parse_aux`, :class:`ParsedAux` — the aux-blob codec.
- :func:`validate_geometry`, :func:`plane_heights`, :data:`SUPPORTED_FORMATS` — geometry validation.
- :func:`format_channels`, :func:`header_pixels_to_numpy_view`, :data:`PACKED_FORMATS` — packed-pixel reshape.
"""

from __future__ import annotations

from gst_iceoryx2.video._codec import (
    ParsedAux,
    build_aux,
    parse_aux,
)
from gst_iceoryx2.video._header import (
    DEFAULT_AUX_BYTES,
    FORMAT_LEN,
    HEADER_ALIGN,
    HEADER_FLAG_EOS,
    HEADER_SIZE,
    HEADER_TYPE_NAME,
    MAX_PLANES,
    PACKED_FORMATS,
    VideoFrameHeader,
    format_channels,
    header_pixels_to_numpy_view,
)
from gst_iceoryx2.video._transport import (
    DEFAULT_SERVICE,
    VIDEO_BORROWED_MAX,
    VIDEO_BUFFER_SIZE,
    VIDEO_HISTORY_SIZE,
    VIDEO_SAFE_OVERFLOW,
    FrameParams,
    Qos,
    SinkConfig,
    VideoFrame,
    VideoFramePublisher,
    VideoFrameSubscriber,
    create_listener,
    create_node,
    create_notifier,
    open_video_service,
)
from gst_iceoryx2.video._validate import (
    SUPPORTED_FORMATS,
    plane_heights,
    validate_geometry,
)

__all__ = [
    # ---- header + layout constants ----
    "DEFAULT_AUX_BYTES",
    "FORMAT_LEN",
    "HEADER_ALIGN",
    "HEADER_FLAG_EOS",
    "HEADER_SIZE",
    "HEADER_TYPE_NAME",
    "MAX_PLANES",
    "VideoFrameHeader",
    # ---- packed-pixel reshape ----
    "PACKED_FORMATS",
    "format_channels",
    "header_pixels_to_numpy_view",
    # ---- aux-blob codec ----
    "ParsedAux",
    "build_aux",
    "parse_aux",
    # ---- geometry validation ----
    "SUPPORTED_FORMATS",
    "plane_heights",
    "validate_geometry",
    # ---- QoS / service ----
    "DEFAULT_SERVICE",
    "VIDEO_BORROWED_MAX",
    "VIDEO_BUFFER_SIZE",
    "VIDEO_HISTORY_SIZE",
    "VIDEO_SAFE_OVERFLOW",
    "Qos",
    "SinkConfig",
    # ---- transport ----
    "FrameParams",
    "VideoFrame",
    "VideoFramePublisher",
    "VideoFrameSubscriber",
    "create_listener",
    "create_node",
    "create_notifier",
    "open_video_service",
]
