"""
iceoryx2 transport + sink-config for the native ``/v2`` video-frame format.

The ``/v2`` format uses a **variable-length slice** (``Slice[u8]``) payload — raw
pixels (optionally followed by an aux blob) — plus a fixed ``VideoFrameHeader``
**user-header**. This is the format the Rust ``iceoryx2sink``/``iceoryx2src`` elements
speak; the classes here are the pure-Python (GStreamer-free) half of the same contract,
for subscribing without a pipeline or publishing from a procedural/test source.

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
    "Iceoryx2SinkConfig",
    "Iox2VideoFramePublisher",
    "Iox2VideoFrameSubscriber",
    "VideoFrameSample",
    "VIDEO_BORROWED_MAX",
    "VIDEO_BUFFER_SIZE",
    "VIDEO_HISTORY_SIZE",
    "VIDEO_SAFE_OVERFLOW",
]

import ctypes
import functools
from dataclasses import dataclass
from typing import TYPE_CHECKING

import iceoryx2 as iox2

from gst_iceoryx2.video._header import (
    VideoFrameHeader,
    header_pixels_to_numpy,
    parse_aux,
)

if TYPE_CHECKING:
    import numpy as np
    import numpy.typing as npt

# Ring-buffer QoS shared by publisher + subscriber + the Rust sink defaults.
# These MUST match across all participants for ``open_or_create`` to succeed.
VIDEO_BUFFER_SIZE = 10
VIDEO_BORROWED_MAX = 10
VIDEO_HISTORY_SIZE = 0
VIDEO_SAFE_OVERFLOW = True

_SLICE_U8 = iox2.Slice[ctypes.c_uint8]


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


def _build_pubsub(node: iox2.Node, service_name: str):
    """Open/create the slice + user-header pub/sub service with the shared QoS."""
    return (
        node.service_builder(iox2.ServiceName.new(service_name))
        .publish_subscribe(_SLICE_U8)
        .user_header(VideoFrameHeader)
        .enable_safe_overflow(VIDEO_SAFE_OVERFLOW)
        .subscriber_max_buffer_size(VIDEO_BUFFER_SIZE)
        .subscriber_max_borrowed_samples(VIDEO_BORROWED_MAX)
        .history_size(VIDEO_HISTORY_SIZE)
        .open_or_create()
    )


def _build_event(node: iox2.Node, service_name: str):
    """Open/create the event service on the **bare** service name (Rust convention)."""
    return node.service_builder(iox2.ServiceName.new(service_name)).event().open_or_create()


# ========================================================================= #
# Sink config (de-policied successor to the consumer's VideoSinkConfig)
# ========================================================================= #


@dataclass(frozen=True)
class Iceoryx2SinkConfig:
    """Connection + QoS parameters for the ``iceoryx2sink`` GStreamer element.

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
# Received sample
# ========================================================================= #


class VideoFrameSample:
    """A received ``/v2`` frame: a detached ``VideoFrameHeader`` plus copied bytes.

    The header is copied out of shared memory on construction, and ``pixels`` /
    ``aux`` / ``to_numpy`` return copies, so the sample is safe to use after the
    next ``receive()`` reclaims the underlying memory.
    """

    def __init__(self, sample: iox2.Sample) -> None:
        self._sample = sample
        # detach the header from shared memory immediately
        self.header = VideoFrameHeader.from_buffer_copy(
            bytes(
                (ctypes.c_uint8 * ctypes.sizeof(VideoFrameHeader)).from_address(
                    ctypes.addressof(sample.user_header().contents)
                )
            )
        )
        p = sample.payload()
        self._raw = bytes((ctypes.c_uint8 * p.len()).from_address(p.as_ptr()))

    @property
    def pixel_size(self) -> int:
        return len(self._raw) - int(self.header.aux_size)

    @property
    def pixels(self) -> bytes:
        """The raw pixel region (a copy)."""
        return self._raw[: self.pixel_size]

    @property
    def aux(self) -> bytes:
        """The aux blob (a copy); empty when ``aux_size == 0``."""
        return self._raw[self.pixel_size :]

    def parse_aux(self) -> tuple[str | None, list[bytes]]:
        """Decode the aux blob into ``(caps_string, serialised_metas)``."""
        return parse_aux(self.aux)

    def to_numpy(self) -> "npt.NDArray[np.uint8]":
        """Return a contiguous ``(H, W, C)`` uint8 array (a copy)."""
        return header_pixels_to_numpy(self.header, self.pixels)


# ========================================================================= #
# Publisher
# ========================================================================= #


class Iox2VideoFramePublisher:
    """Publishes ``/v2`` frames (slice pixels + ``VideoFrameHeader``) and notifies.

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
        self._service = _build_pubsub(node, service_name)
        self._publisher = (
            self._service.publisher_builder()
            .initial_max_slice_len(max_bytes)
            .allocation_strategy(iox2.AllocationStrategy.PowerOfTwo)
            .create()
        )
        self._event = _build_event(node, service_name)
        self._notifier = self._event.notifier_builder().create()

    def publish_frame(
        self,
        pixels: bytes,
        *,
        width: int,
        height: int,
        format: bytes = b"BGR",
        n_planes: int = 1,
        stride0: int | None = None,
        offset: int = 0,
        pts: int = 0,
    ) -> None:
        """Loan a slice, write pixels + header, send, and fire the event."""
        n = len(pixels)
        sample = self._publisher.loan_slice_uninit(n)
        ctypes.memmove(sample.payload_ptr, pixels, n)

        header = VideoFrameHeader()
        header.pts = pts
        header.dts = 0xFFFFFFFFFFFFFFFF
        header.duration = 0xFFFFFFFFFFFFFFFF
        header.offset = offset
        header.flags = 0
        header.width = width
        header.height = height
        header.n_planes = n_planes
        header.aux_size = 0
        # for a contiguous packed buffer with no aux, the row stride is n / height
        header.stride[0] = stride0 if stride0 is not None else (n // height if height else n)
        header.format = format
        ctypes.memmove(sample.user_header_ptr, ctypes.byref(header), ctypes.sizeof(header))

        sample.assume_init().send()
        self._notifier.notify()

    def close(self) -> None:
        self._publisher = None
        self._notifier = None
        self._service = None
        self._event = None

    def __enter__(self) -> "Iox2VideoFramePublisher":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()


# ========================================================================= #
# Subscriber
# ========================================================================= #


class Iox2VideoFrameSubscriber:
    """Event-driven subscriber for the ``/v2`` slice + user-header format.

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
        self._service = _build_pubsub(node, service_name)
        self._subscriber = self._service.subscriber_builder().create()
        self._event = _build_event(node, service_name)
        self._listener = self._event.listener_builder().create()

    def receive_nonblocking(self) -> VideoFrameSample | None:
        sample = self._subscriber.receive()
        return VideoFrameSample(sample) if sample is not None else None

    def receive_blocking(self, block_ms: int | None = None) -> VideoFrameSample | None:
        """Drain a queued frame, else park on the listener until one arrives.

        Returns ``None`` when ``block_ms`` elapses with no frame; blocks
        indefinitely when ``block_ms`` is ``None``.
        """
        sample = self._subscriber.receive()
        if sample is not None:
            return VideoFrameSample(sample)
        if block_ms is None:
            self._listener.blocking_wait_one()
        else:
            self._listener.timed_wait_one(iox2.Duration.from_millis(block_ms))
        sample = self._subscriber.receive()
        return VideoFrameSample(sample) if sample is not None else None

    def close(self) -> None:
        self._subscriber = None
        self._listener = None
        self._service = None
        self._event = None

    def __enter__(self) -> "Iox2VideoFrameSubscriber":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()
