"""
iceoryx2 transport + sink-config for the native ``/v2`` video-frame format.

The ``/v2`` format uses a **variable-length slice** (``Slice[u8]``) payload — raw
pixels (optionally followed by an aux blob) — plus a fixed ``VideoFrameHeader``
**user-header**. This is the format the Rust ``iceoryx2sink``/``iceoryx2src`` elements
speak; the classes here are the pure-Python (GStreamer-free) half of the same contract,
for subscribing without a pipeline or publishing from a procedural/test source.

It shares one neutral vocabulary with the Rust ``transport`` module
(``VideoFramePublisher`` / ``VideoFrameSubscriber`` / ``VideoFrame`` / ``FrameParams`` /
``Qos`` / ``SinkConfig`` and the ``create_node`` / ``open_video_service`` / ``create_notifier`` /
``create_listener`` helpers), kept in lockstep by ``PARITY.md`` + the ``api_manifest.json`` golden.

Two notable points:

- **slice + user-header payload** — distinct from a fixed-struct service;
- **event service on the bare service name** — the Rust elements open the event service
  on the *same* name as the pub/sub service (``node.service_builder(name).event()``), so a
  Python subscriber built here wakes from the Rust sink's notifications.

These import only ``ctypes`` + ``iceoryx2`` (and lazily ``numpy`` via the header helpers) —
never the compiled GStreamer plugin (``gst_iceoryx2._native``) — so a subscriber needs no
GStreamer runtime.
"""

from __future__ import annotations

__all__ = [
    "DEFAULT_SERVICE",
    "VIDEO_BORROWED_MAX",
    "VIDEO_BUFFER_SIZE",
    "VIDEO_HISTORY_SIZE",
    "VIDEO_SAFE_OVERFLOW",
    "FrameParams",
    "Qos",
    "SinkConfig",
    "VideoFrame",
    "VideoFramePublisher",
    "VideoFrameSubscriber",
    "create_listener",
    "create_node",
    "create_notifier",
    "open_video_service",
]

import ctypes
import functools
from dataclasses import dataclass
from typing import TYPE_CHECKING

import iceoryx2 as iox2

from gst_iceoryx2.video._codec import ParsedAux, parse_aux
from gst_iceoryx2.video._header import (
    HEADER_FLAG_EOS,
    VideoFrameHeader,
    header_pixels_to_numpy_view,
)

if TYPE_CHECKING:
    import numpy as np
    import numpy.typing as npt

# Default service name (the ``/v2`` slice + user-header video format). Bakes in no naming policy — an
# application supplies the name it chose. Mirrors the Rust ``DEFAULT_SERVICE``.
DEFAULT_SERVICE = "video/default/frame/v2"

# Ring-buffer QoS shared by publisher + subscriber + the Rust sink defaults.
# These MUST match across all participants for ``open_or_create`` to succeed.
VIDEO_BUFFER_SIZE = 10
VIDEO_BORROWED_MAX = 10
VIDEO_HISTORY_SIZE = 0
VIDEO_SAFE_OVERFLOW = True

_SLICE_U8 = iox2.Slice[ctypes.c_uint8]


# ========================================================================= #
# QoS
# ========================================================================= #


@dataclass(frozen=True)
class Qos:
    """The publish/subscribe QoS shared by both ends (mirrors the Rust ``Qos``).

    Defaults are the ``VIDEO_*`` constants; every participant must agree for iceoryx2's
    ``open_or_create`` to attach to the same service rather than failing on a QoS mismatch.
    """

    buffer_size: int = VIDEO_BUFFER_SIZE
    borrowed_max: int = VIDEO_BORROWED_MAX
    history_size: int = VIDEO_HISTORY_SIZE
    safe_overflow: bool = VIDEO_SAFE_OVERFLOW


# ========================================================================= #
# Port construction helpers (public; mirror the Rust transport free functions)
# ========================================================================= #


@functools.lru_cache(maxsize=1)
def _default_ipc_node() -> iox2.Node:
    """A process-wide cached ``ipc`` node, used when the caller passes none.

    Reusing one node minimises shared-memory/peer-monitoring overhead. An application that
    manages its own node (e.g. to share one across many services, or to use a ``local`` node
    in tests) passes it explicitly to the publisher/subscriber instead.
    """
    return iox2.NodeBuilder.new().create(iox2.ServiceType.Ipc)


def _get_node(node: iox2.Node | None) -> iox2.Node:
    return node if node is not None else _default_ipc_node()


def create_node() -> iox2.Node:
    """Create a fresh ``ipc`` process node. Mirrors the Rust ``create_node``.

    Most callers can let the publisher/subscriber use the cached default node instead; pass a node
    explicitly only to share one across services or to use a ``local`` node in tests.
    """
    return iox2.NodeBuilder.new().create(iox2.ServiceType.Ipc)


def open_video_service(node: iox2.Node, service_name: str, qos: Qos = Qos()):
    """Open/create the slice + user-header pub/sub service with the given QoS. Mirrors the Rust
    ``open_video_service``."""
    return (
        node.service_builder(iox2.ServiceName.new(service_name))
        .publish_subscribe(_SLICE_U8)
        .user_header(VideoFrameHeader)
        .enable_safe_overflow(qos.safe_overflow)
        .subscriber_max_buffer_size(qos.buffer_size)
        .subscriber_max_borrowed_samples(qos.borrowed_max)
        .history_size(qos.history_size)
        .open_or_create()
    )


def _open_event(node: iox2.Node, service_name: str):
    """Open/create the event service on the **bare** service name (Rust convention)."""
    return node.service_builder(iox2.ServiceName.new(service_name)).event().open_or_create()


def create_notifier(node: iox2.Node, service_name: str):
    """Create a notifier on the bare service name (the event service the source's listener waits on).
    Mirrors the Rust ``create_notifier``."""
    return _open_event(node, service_name).notifier_builder().create()


def create_listener(node: iox2.Node, service_name: str):
    """Create a listener on the bare service name (woken by the sink's notifier). Mirrors the Rust
    ``create_listener``."""
    return _open_event(node, service_name).listener_builder().create()


# ========================================================================= #
# Sink config
# ========================================================================= #


@dataclass(frozen=True)
class SinkConfig:
    """Connection + QoS parameters for the ``iceoryx2sink`` GStreamer element (mirrors the Rust
    ``SinkConfig``).

    A plain value object: the caller supplies the ``service`` name (and optionally
    ``max_bytes`` + QoS); :meth:`gst_properties` renders them as the element's hyphenated
    GObject property names. It bakes in **no naming policy** — an application's channel
    factory owns the service-name convention and constructs this with the name it chose.

    ``max_bytes == 0`` lets the element derive the slice length from the negotiated caps.
    """

    service: str
    max_bytes: int = 0
    buffer_size: int = VIDEO_BUFFER_SIZE
    borrowed_max: int = VIDEO_BORROWED_MAX
    history_size: int = VIDEO_HISTORY_SIZE
    safe_overflow: bool = VIDEO_SAFE_OVERFLOW

    def gst_properties(self) -> dict[str, object]:
        """The ``iceoryx2sink`` property names → values (hyphenated GObject names)."""
        return {
            "service": self.service,
            "max-bytes": self.max_bytes,
            "buffer-size": self.buffer_size,
            "borrowed-max": self.borrowed_max,
            "history-size": self.history_size,
            "safe-overflow": self.safe_overflow,
        }


# ========================================================================= #
# Per-frame publish parameters
# ========================================================================= #


@dataclass
class FrameParams:
    """Per-frame parameters for :meth:`VideoFramePublisher.publish_frame` (mirrors the Rust
    ``FrameParams``).

    The defaults describe a single-plane packed ``BGR`` frame with no timestamps; set what you need.
    ``stride0 = None`` derives ``len(pixels) / height`` (packed, contiguous).
    """

    width: int = 0
    height: int = 0
    format: str = "BGR"
    n_planes: int = 1
    stride0: int | None = None
    offset: int = 0
    pts: int = 0


# ========================================================================= #
# Received frame
# ========================================================================= #


class VideoFrame:
    """A received ``/v2`` frame — a **zero-copy borrow** of the loaned iceoryx2 sample (parity name +
    semantics with the Rust ``VideoFrame``).

    ``header`` / ``pixels`` / ``aux`` / :meth:`numpy_view` all view the shared memory directly; nothing
    is copied. One uniform contract follows, the same for every view: each is valid **only while this
    frame is alive**, and the frame holds an iceoryx2 loan while alive (concurrent frames are capped by
    ``subscriber-max-borrowed-samples``, default 10). Keep the ``frame`` reference for as long as you use
    any view of it; to outlive the frame, copy the data (``bytes(frame.pixels)`` /
    ``frame.numpy_view().copy()``). Drop the frame (or :meth:`close`) to release the loan. (The Rust
    ``VideoFrame`` enforces the identical lifetime at compile time via the borrow checker; here it is a
    documented contract — there is no hidden keepalive.)
    """

    def __init__(self, sample: iox2.Sample) -> None:
        self._sample = sample
        # zero-copy header view: the ctypes struct lives in shared memory (sample kept alive by self).
        self._hdr_ptr = sample.user_header()
        self.header: VideoFrameHeader = self._hdr_ptr.contents
        # zero-copy payload view: a memoryview over the loaned slice (no bytes() copy).
        p = sample.payload()
        self._n = p.len()
        self._buf = (ctypes.c_uint8 * self._n).from_address(p.as_ptr())
        self._mv = memoryview(self._buf)

    @property
    def pixel_size(self) -> int:
        return self._n - int(self.header.aux_size)

    @property
    def pixels(self) -> memoryview:
        """The raw pixel region as a zero-copy ``memoryview`` over shared memory (valid while the frame
        is alive). ``bytes(frame.pixels)`` to copy."""
        return self._mv[: self.pixel_size]

    @property
    def aux(self) -> memoryview:
        """The aux blob as a zero-copy ``memoryview``; empty when ``aux_size == 0``."""
        return self._mv[self.pixel_size :]

    def parse_aux(self) -> ParsedAux:
        """Decode the aux blob into ``ParsedAux(caps, metas)``. Mirrors the Rust ``VideoFrame::parse_aux``."""
        return parse_aux(self.aux)

    def is_eos(self) -> bool:
        """Whether this is the end-of-stream sentinel (carries no pixels). Mirrors the Rust
        ``VideoFrame::is_eos``."""
        return bool(int(self.header.flags) & HEADER_FLAG_EOS)

    def numpy_view(self) -> "npt.NDArray[np.uint8]":
        """A **zero-copy** ``(H, W, C)`` uint8 view over the shared-memory pixels (parity counterpart of
        the Rust ``VideoFrame::ndarray_view``). Non-contiguous when the frame has row padding; call
        ``.copy()`` for an owned, contiguous array. Valid only while this frame is alive — keep the
        ``frame`` reference, or ``.copy()`` to outlive it."""
        return header_pixels_to_numpy_view(self.header, self.pixels)


# ========================================================================= #
# Publisher
# ========================================================================= #


class VideoFramePublisher:
    """Publishes ``/v2`` frames (slice pixels + ``VideoFrameHeader``) and notifies (parity name with
    the Rust ``VideoFramePublisher``).

    The Rust ``iceoryx2sink`` is the production publisher; this exists for the
    procedural/local path (dummy source, in-process tests) and writes the exact
    same wire format. ``aux_size`` is always 0 here (no caps/meta blob).
    """

    def __init__(
        self,
        service_name: str,
        *,
        max_bytes: int,
        iox2_node: iox2.Node | None = None,
    ):
        node = _get_node(iox2_node)
        self._node = node
        self._service = open_video_service(node, service_name)
        self._publisher = (
            self._service.publisher_builder()
            .initial_max_slice_len(max_bytes)
            .allocation_strategy(iox2.AllocationStrategy.PowerOfTwo)
            .create()
        )
        # Keep the event service alive alongside the notifier built from it.
        self._event = _open_event(node, service_name)
        self._notifier = self._event.notifier_builder().create()

    def publish_frame(self, pixels: bytes, params: FrameParams) -> None:
        """Loan a slice, write pixels + a header from ``params``, send, and fire the event. Mirrors the
        Rust ``VideoFramePublisher::publish_frame``."""
        n = len(pixels)
        sample = self._publisher.loan_slice_uninit(n)
        ctypes.memmove(sample.payload_ptr, pixels, n)

        header = VideoFrameHeader()
        header.pts = params.pts
        header.dts = 0xFFFFFFFFFFFFFFFF
        header.duration = 0xFFFFFFFFFFFFFFFF
        header.offset = params.offset
        header.flags = 0
        header.width = params.width
        header.height = params.height
        header.n_planes = params.n_planes
        header.aux_size = 0
        # for a contiguous packed buffer with no aux, the row stride is n / height
        if params.stride0 is not None:
            header.stride[0] = params.stride0
        else:
            header.stride[0] = n // params.height if params.height else n
        header.format = params.format.encode()
        ctypes.memmove(sample.user_header_ptr, ctypes.byref(header), ctypes.sizeof(header))

        sample.assume_init().send()
        self._notifier.notify()

    def close(self) -> None:
        self._publisher = None
        self._notifier = None
        self._service = None
        self._event = None

    def __enter__(self) -> "VideoFramePublisher":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()


# ========================================================================= #
# Subscriber
# ========================================================================= #


class VideoFrameSubscriber:
    """Event-driven subscriber for the ``/v2`` slice + user-header format (parity name with the Rust
    ``VideoFrameSubscriber``).

    Drains queued frames from the ring (the ring carries the data; the event only
    wakes the sleeper), with the listener on the **bare** service name so it wakes
    from the Rust sink's notifications.
    """

    def __init__(
        self,
        service_name: str,
        *,
        iox2_node: iox2.Node | None = None,
    ):
        node = _get_node(iox2_node)
        self._node = node
        self._service = open_video_service(node, service_name)
        self._subscriber = self._service.subscriber_builder().create()
        self._event = _open_event(node, service_name)
        self._listener = self._event.listener_builder().create()

    def receive(self) -> VideoFrame | None:
        """Return the next queued frame, or ``None`` if the ring is empty (never blocks). Mirrors the
        Rust ``VideoFrameSubscriber::receive``."""
        sample = self._subscriber.receive()
        return VideoFrame(sample) if sample is not None else None

    def receive_blocking(self, block_ms: int | None = None) -> VideoFrame | None:
        """Drain a queued frame, else park on the listener until one arrives.

        Returns ``None`` when ``block_ms`` elapses with no frame; blocks
        indefinitely when ``block_ms`` is ``None``. Mirrors the Rust
        ``VideoFrameSubscriber::receive_blocking``.
        """
        sample = self._subscriber.receive()
        if sample is not None:
            return VideoFrame(sample)
        if block_ms is None:
            self._listener.blocking_wait_one()
        else:
            self._listener.timed_wait_one(iox2.Duration.from_millis(block_ms))
        sample = self._subscriber.receive()
        return VideoFrame(sample) if sample is not None else None

    def close(self) -> None:
        self._subscriber = None
        self._listener = None
        self._service = None
        self._event = None

    def __enter__(self) -> "VideoFrameSubscriber":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()
