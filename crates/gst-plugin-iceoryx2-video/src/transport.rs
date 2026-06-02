//! The gstreamer-free iceoryx2 transport: shared port-construction helpers (reused by the plugin's
//! `iceoryx2sink`/`iceoryx2src` elements) plus a high-level [`VideoFramePublisher`] /
//! [`VideoFrameSubscriber`] SDK that mirrors the Python `gst_iceoryx2.video` classes
//! (`Iox2VideoFramePublisher` / `Iox2VideoFrameSubscriber` / `VideoFrameSample`).
//!
//! Both ends speak the same service shape: `publish_subscribe::<[u8]>().user_header::<VideoFrameHeader>()`
//! plus a paired **event** service on the *same* name, so a subscriber parks on the event listener and
//! wakes the instant a frame is published. This is wire-identical to the Rust elements and the Python
//! SDK, so any of them interoperate.

use core::time::Duration;

use iceoryx2::port::listener::Listener;
use iceoryx2::port::notifier::Notifier;
use iceoryx2::port::publisher::Publisher;
use iceoryx2::port::subscriber::Subscriber;
use iceoryx2::prelude::*;
use iceoryx2::sample::Sample;
use iceoryx2::service::port_factory::publish_subscribe::PortFactory as PubSubFactory;

use crate::aux::{parse_aux, ParsedAux};
use crate::error::{Error, Result};
use crate::header::{VideoFrameHeader, FORMAT_LEN, HEADER_FLAG_EOS, MAX_PLANES};
use crate::qos::Qos;

/// iceoryx2 service flavour used throughout: thread-safe handles (`Send + Sync`), wire-identical to
/// `ipc::Service` (the two differ only in their local `ArcThreadSafetyPolicy`), so it interoperates
/// with a Python subscriber opened on `ServiceType.Ipc`.
pub type IpcService = iceoryx2::service::ipc_threadsafe::Service;

/// The video publish/subscribe service factory: a `Slice[u8]` payload with a [`VideoFrameHeader`]
/// user-header. Returned by [`open_video_service`] for callers that need the port builders directly
/// (e.g. the sink's shared publisher + buffer pool).
pub type VideoPubSub = PubSubFactory<IpcService, [u8], VideoFrameHeader>;

/// Create a fresh process node. Most callers can share one node across services.
pub fn create_node() -> Result<Node<IpcService>> {
    NodeBuilder::new()
        .create::<IpcService>()
        .map_err(|e| Error::new("create iceoryx2 node", e))
}

/// Open (or create) the video publish/subscribe service on `service` with the given `qos`.
pub fn open_video_service(
    node: &Node<IpcService>,
    service: &str,
    qos: &Qos,
) -> Result<VideoPubSub> {
    let service_name: ServiceName = service
        .try_into()
        .map_err(|e| Error::new("invalid service name", e))?;
    node.service_builder(&service_name)
        .publish_subscribe::<[u8]>()
        .user_header::<VideoFrameHeader>()
        .enable_safe_overflow(qos.safe_overflow)
        .subscriber_max_buffer_size(qos.buffer_size as usize)
        .subscriber_max_borrowed_samples(qos.borrowed_max as usize)
        .history_size(qos.history_size as usize)
        .open_or_create()
        .map_err(|e| Error::new("open publish/subscribe service", e))
}

/// Create a notifier on the **bare** service name (the event service the source's listener waits on).
pub fn create_notifier(node: &Node<IpcService>, service: &str) -> Result<Notifier<IpcService>> {
    let service_name: ServiceName = service
        .try_into()
        .map_err(|e| Error::new("invalid service name", e))?;
    node.service_builder(&service_name)
        .event()
        .open_or_create()
        .map_err(|e| Error::new("open event service", e))?
        .notifier_builder()
        .create()
        .map_err(|e| Error::new("create notifier", e))
}

/// Create a listener on the **bare** service name (woken by the sink's notifier).
pub fn create_listener(node: &Node<IpcService>, service: &str) -> Result<Listener<IpcService>> {
    let service_name: ServiceName = service
        .try_into()
        .map_err(|e| Error::new("invalid service name", e))?;
    node.service_builder(&service_name)
        .event()
        .open_or_create()
        .map_err(|e| Error::new("open event service", e))?
        .listener_builder()
        .create()
        .map_err(|e| Error::new("create listener", e))
}

/// Per-frame parameters for [`VideoFramePublisher::publish_frame`]. [`Default`] is a single-plane
/// packed `BGR` frame with no timestamps; set what you need.
#[derive(Debug, Clone)]
pub struct FrameParams<'a> {
    /// Pixel width.
    pub width: u32,
    /// Pixel height.
    pub height: u32,
    /// GStreamer format name, e.g. `"BGR"`.
    pub format: &'a str,
    /// Number of planes (1 for packed `BGR`/`RGB`).
    pub n_planes: u32,
    /// Row stride of plane 0; `None` derives `pixels.len() / height` (packed, contiguous).
    pub stride0: Option<u32>,
    /// `GstBuffer.offset` — frame counter / media offset.
    pub offset: u64,
    /// Presentation timestamp (ns).
    pub pts: u64,
}

impl Default for FrameParams<'_> {
    fn default() -> Self {
        Self {
            width: 0,
            height: 0,
            format: "BGR",
            n_planes: 1,
            stride0: None,
            offset: 0,
            pts: 0,
        }
    }
}

/// Publishes `/v2` frames (slice pixels + [`VideoFrameHeader`]) and fires the paired event.
///
/// The Rust `iceoryx2sink` element is the production publisher; this is the procedural/SDK path (a
/// dummy source, in-process tests) and writes the exact same wire format. `aux_size` is always 0 here
/// (no caps/meta blob — that fidelity is the element's concern).
pub struct VideoFramePublisher {
    publisher: Publisher<IpcService, [u8], VideoFrameHeader>,
    notifier: Notifier<IpcService>,
    // The node must outlive the ports created from it.
    _node: Node<IpcService>,
}

impl VideoFramePublisher {
    /// Open the service on a fresh node. `max_bytes` seeds the slice length (it grows on demand via a
    /// `PowerOfTwo` allocation strategy, so a larger later frame does not tear the publisher down).
    pub fn new(service: &str, max_bytes: usize) -> Result<Self> {
        Self::with_node(create_node()?, service, max_bytes)
    }

    /// As [`new`](Self::new) but reuses a caller-supplied node (e.g. shared across many services).
    pub fn with_node(node: Node<IpcService>, service: &str, max_bytes: usize) -> Result<Self> {
        let pubsub = open_video_service(&node, service, &Qos::default())?;
        let publisher = pubsub
            .publisher_builder()
            .initial_max_slice_len(max_bytes.max(1))
            .allocation_strategy(AllocationStrategy::PowerOfTwo)
            .create()
            .map_err(|e| Error::new("create publisher", e))?;
        let notifier = create_notifier(&node, service)?;
        Ok(Self {
            publisher,
            notifier,
            _node: node,
        })
    }

    /// Loan a slice, write `pixels` + a header from `params`, send, and fire the event.
    pub fn publish_frame(&self, pixels: &[u8], params: &FrameParams) -> Result<()> {
        let n = pixels.len();
        let mut sample = self
            .publisher
            .loan_slice_uninit(n)
            .map_err(|e| Error::new("loan slice", e))?;

        let mut header = VideoFrameHeader {
            pts: params.pts,
            dts: u64::MAX,
            duration: u64::MAX,
            offset: params.offset,
            flags: 0,
            width: params.width,
            height: params.height,
            n_planes: params.n_planes,
            aux_size: 0,
            stride: [0; MAX_PLANES],
            plane_offsets: [0; MAX_PLANES],
            format: [0; FORMAT_LEN],
        };
        // For a contiguous packed buffer with no aux, the row stride is len / height.
        header.stride[0] = params.stride0.unwrap_or(if params.height > 0 {
            (n / params.height as usize) as u32
        } else {
            n as u32
        });
        header.set_format(params.format);
        *sample.user_header_mut() = header;

        let payload = sample.payload_mut();
        for (dst, &b) in payload.iter_mut().zip(pixels.iter()) {
            dst.write(b);
        }
        let sample = unsafe { sample.assume_init() };
        sample.send().map_err(|e| Error::new("send sample", e))?;
        self.notifier.notify().map_err(|e| Error::new("notify", e))?;
        Ok(())
    }
}

/// Event-driven subscriber for the `/v2` slice + user-header format. Drains queued frames from the
/// ring (the ring carries the data; the event only wakes the sleeper). Mirrors the Python
/// `Iox2VideoFrameSubscriber`, and wakes from the Rust sink's notifications.
pub struct VideoFrameSubscriber {
    subscriber: Subscriber<IpcService, [u8], VideoFrameHeader>,
    listener: Listener<IpcService>,
    _node: Node<IpcService>,
}

impl VideoFrameSubscriber {
    /// Open the service on a fresh node.
    pub fn new(service: &str) -> Result<Self> {
        Self::with_node(create_node()?, service)
    }

    /// As [`new`](Self::new) but reuses a caller-supplied node.
    pub fn with_node(node: Node<IpcService>, service: &str) -> Result<Self> {
        let pubsub = open_video_service(&node, service, &Qos::default())?;
        let subscriber = pubsub
            .subscriber_builder()
            .create()
            .map_err(|e| Error::new("create subscriber", e))?;
        let listener = create_listener(&node, service)?;
        Ok(Self {
            subscriber,
            listener,
            _node: node,
        })
    }

    /// Return the next queued frame, or `None` if the ring is empty (never blocks).
    pub fn receive(&self) -> Result<Option<ReceivedFrame>> {
        Ok(self
            .subscriber
            .receive()
            .map_err(|e| Error::new("receive", e))?
            .map(ReceivedFrame::new))
    }

    /// Return a queued frame, else park on the listener until one arrives. Returns `None` when
    /// `block_ms` elapses with no frame; blocks indefinitely when `block_ms` is `None`.
    pub fn receive_blocking(&self, block_ms: Option<u64>) -> Result<Option<ReceivedFrame>> {
        if let Some(frame) = self.receive()? {
            return Ok(Some(frame));
        }
        match block_ms {
            None => {
                self.listener
                    .blocking_wait_one()
                    .map_err(|e| Error::new("listener wait", e))?;
            }
            Some(ms) => {
                self.listener
                    .timed_wait_one(Duration::from_millis(ms))
                    .map_err(|e| Error::new("listener wait", e))?;
            }
        }
        self.receive()
    }
}

/// A received `/v2` frame. Borrows the loaned shared-memory payload (zero-copy); the header and the
/// pixel/aux slices stay valid for the lifetime of this value. Mirrors the Python `VideoFrameSample`.
pub struct ReceivedFrame {
    sample: Sample<IpcService, [u8], VideoFrameHeader>,
}

impl ReceivedFrame {
    fn new(sample: Sample<IpcService, [u8], VideoFrameHeader>) -> Self {
        Self { sample }
    }

    /// The per-frame [`VideoFrameHeader`].
    pub fn header(&self) -> &VideoFrameHeader {
        self.sample.user_header()
    }

    /// The whole payload slice (pixels followed by the aux blob).
    pub fn payload(&self) -> &[u8] {
        self.sample.payload()
    }

    /// Length of the pixel region (`payload` minus the `aux_size` tail).
    pub fn pixel_size(&self) -> usize {
        self.payload()
            .len()
            .saturating_sub(self.header().aux_size as usize)
    }

    /// The raw pixel region.
    pub fn pixels(&self) -> &[u8] {
        &self.payload()[..self.pixel_size()]
    }

    /// The aux blob (empty when `aux_size == 0`).
    pub fn aux(&self) -> &[u8] {
        &self.payload()[self.pixel_size()..]
    }

    /// Decode the aux blob into its caps string + serialised metas.
    pub fn parse_aux(&self) -> ParsedAux {
        parse_aux(self.aux())
    }

    /// Whether this is the end-of-stream sentinel (carries no pixels).
    pub fn is_eos(&self) -> bool {
        self.header().flags & HEADER_FLAG_EOS != 0
    }
}
