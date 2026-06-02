//! The `iceoryx2sink` element — a [`gst_base::BaseSink`] that publishes raw video frames into
//! iceoryx2 shared memory, paired with an event notification so a subscriber's `Listener` wakes
//! without polling.
//!
//! Two render paths:
//! * **Zero-copy** — when the incoming buffer was allocated by our [`Iceoryx2BufferPool`] (its
//!   payload pointer is in the loan registry), the loaned sample already holds the pixels; the sink
//!   just fills the header and `send()`s it. Advertised via `propose_allocation`.
//! * **Copy fallback** — otherwise the pixels are `memcpy`'d into a freshly loaned sample. Always
//!   correct, guaranteeing output even when allocation negotiation does not engage.

use gst::glib;
use gst::prelude::*;
use gst::subclass::prelude::*;
use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, LazyLock, Mutex};

use crate::pool::{Iceoryx2BufferPool, LoanRegistry, SendSample, SharedPublisher};
use gst_plugin_iceoryx2_video::{DEFAULT_SERVICE, IpcService, VideoFrameHeader, MAX_PLANES};

static CAT: LazyLock<gst::DebugCategory> = LazyLock::new(|| {
    gst::DebugCategory::new(
        "iceoryx2sink",
        gst::DebugColorFlags::empty(),
        Some("iceoryx2 video sink"),
    )
});

const DEFAULT_BUFFER_SIZE: u32 = 10;
const DEFAULT_BORROWED_MAX: u32 = 10;
const DEFAULT_HISTORY_SIZE: u32 = 0;
const DEFAULT_SAFE_OVERFLOW: bool = true;
const DEFAULT_WAIT_FOR_CONNECTION: bool = false;
const DEFAULT_LOSSLESS: bool = false;
const DEFAULT_AUX_BYTES: u32 = gst_plugin_iceoryx2_video::DEFAULT_AUX_BYTES;
/// Smallest aux blob that holds anything: a zero-length caps string (`u32` 0) + zero metas
/// (`u32` 0). A reserved tail smaller than this carries no aux at all.
const AUX_MIN_BYTES: usize = 8;

/// How long `render` sleeps between subscriber-count polls while blocked on `wait-for-connection`.
const WAIT_POLL: std::time::Duration = std::time::Duration::from_millis(20);

mod imp {
    use super::*;
    use gst_base::subclass::prelude::*;
    use std::sync::OnceLock;

    use iceoryx2::port::notifier::Notifier;
    use iceoryx2::prelude::*;
    use iceoryx2::service::port_factory::publish_subscribe::PortFactory as PubSubFactory;

    /// User-configurable element properties (mirrors the consumer's `DataChannelFactory` config).
    #[derive(Debug, Clone)]
    pub struct Settings {
        pub service: String,
        pub max_bytes: u32,
        pub buffer_size: u32,
        pub borrowed_max: u32,
        pub history_size: u32,
        pub safe_overflow: bool,
        pub wait_for_connection: bool,
        pub lossless: bool,
        pub aux_bytes: u32,
    }

    impl Default for Settings {
        fn default() -> Self {
            Self {
                service: DEFAULT_SERVICE.to_string(),
                max_bytes: 0,
                buffer_size: DEFAULT_BUFFER_SIZE,
                borrowed_max: DEFAULT_BORROWED_MAX,
                history_size: DEFAULT_HISTORY_SIZE,
                safe_overflow: DEFAULT_SAFE_OVERFLOW,
                wait_for_connection: DEFAULT_WAIT_FOR_CONNECTION,
                lossless: DEFAULT_LOSSLESS,
                aux_bytes: DEFAULT_AUX_BYTES,
            }
        }
    }

    /// Negotiated video layout, cached from caps and copied into every header.
    #[derive(Debug, Clone)]
    pub struct CapsInfo {
        pub format: String,
        pub width: u32,
        pub height: u32,
        pub n_planes: u32,
        pub stride: [u32; MAX_PLANES],
        pub plane_offsets: [u32; MAX_PLANES],
        pub size: usize,
    }

    /// Live iceoryx2 ports, created in `start`. The publisher is (re)created in `set_caps` once the
    /// frame size is known.
    pub struct State {
        pub node: Node<IpcService>,
        pub pubsub: PubSubFactory<IpcService, [u8], VideoFrameHeader>,
        pub publisher: Option<SharedPublisher>,
        pub notifier: Notifier<IpcService>,
        pub registry: LoanRegistry,
        pub max_slice_len: usize,
        pub caps: Option<CapsInfo>,
        /// The full negotiated caps, serialised into every aux blob so the source recovers
        /// colorimetry/framerate/PAR/interlace — not just the geometry the header carries.
        pub caps_full: Option<gst::Caps>,
    }

    /// Counters live on the element (not in `State`) as atomics so the read-only properties never
    /// contend on the hot-path `state` mutex — in particular while `render` holds it.
    #[derive(Default)]
    pub struct Counters {
        pub sent: AtomicU64,
        pub zero_copy: AtomicU64,
        pub copied: AtomicU64,
    }

    #[derive(Default)]
    pub struct Iceoryx2Sink {
        pub settings: Mutex<Settings>,
        pub state: Mutex<Option<State>>,
        pub counters: Counters,
        /// Last observed connected-subscriber count, for `num-clients` + change-signal detection.
        pub clients: AtomicU64,
        /// Set by `unlock` to break `render` (or the pool's `acquire_buffer`) out of a
        /// `wait-for-connection` / lossless back-pressure wait. `Arc` so the pool shares it.
        pub unlocked: Arc<std::sync::atomic::AtomicBool>,
    }

    #[glib::object_subclass]
    impl ObjectSubclass for Iceoryx2Sink {
        const NAME: &'static str = "GstIceoryx2Sink";
        type Type = super::Iceoryx2Sink;
        type ParentType = gst_base::BaseSink;
    }

    impl ObjectImpl for Iceoryx2Sink {
        fn properties() -> &'static [glib::ParamSpec] {
            static PROPERTIES: OnceLock<Vec<glib::ParamSpec>> = OnceLock::new();
            PROPERTIES.get_or_init(|| {
                vec![
                    glib::ParamSpecString::builder("service")
                        .nick("Service")
                        .blurb("iceoryx2 service name (publish/subscribe + event)")
                        .default_value(Some(DEFAULT_SERVICE))
                        .build(),
                    glib::ParamSpecUInt::builder("max-bytes")
                        .nick("Max bytes")
                        .blurb("Max slice length; 0 derives it from the negotiated caps")
                        .default_value(0)
                        .build(),
                    glib::ParamSpecUInt::builder("buffer-size")
                        .nick("Buffer size")
                        .blurb("subscriber-max-buffer-size QoS")
                        .default_value(DEFAULT_BUFFER_SIZE)
                        .build(),
                    glib::ParamSpecUInt::builder("borrowed-max")
                        .nick("Borrowed max")
                        .blurb("subscriber-max-borrowed-samples QoS")
                        .default_value(DEFAULT_BORROWED_MAX)
                        .build(),
                    glib::ParamSpecUInt::builder("history-size")
                        .nick("History size")
                        .blurb("publisher history-size QoS")
                        .default_value(DEFAULT_HISTORY_SIZE)
                        .build(),
                    glib::ParamSpecBoolean::builder("safe-overflow")
                        .nick("Safe overflow")
                        .blurb("ring-buffer safe-overflow QoS")
                        .default_value(DEFAULT_SAFE_OVERFLOW)
                        .build(),
                    glib::ParamSpecBoolean::builder("wait-for-connection")
                        .nick("Wait for connection")
                        .blurb("Block the stream until at least one subscriber is connected")
                        .default_value(DEFAULT_WAIT_FOR_CONNECTION)
                        .build(),
                    glib::ParamSpecBoolean::builder("lossless")
                        .nick("Lossless")
                        .blurb(
                            "Back-pressure instead of dropping: forces no ring overflow and blocks \
                             until the consumer frees a slot (the source must also set \
                             safe-overflow=false to match the service QoS)",
                        )
                        .default_value(DEFAULT_LOSSLESS)
                        .build(),
                    glib::ParamSpecUInt::builder("aux-bytes")
                        .nick("Aux bytes")
                        .blurb(
                            "Bytes reserved after the pixels for the aux blob (full caps string + \
                             serialisable GstMetas). 0 disables caps/meta passthrough (header \
                             geometry only). Must exceed the caps string for full fidelity",
                        )
                        .default_value(DEFAULT_AUX_BYTES)
                        .build(),
                    glib::ParamSpecUInt::builder("num-clients")
                        .nick("Num clients")
                        .blurb("Number of subscribers currently connected")
                        .read_only()
                        .build(),
                    glib::ParamSpecUInt64::builder("frames-sent")
                        .nick("Frames sent")
                        .blurb("total frames published")
                        .read_only()
                        .build(),
                    glib::ParamSpecUInt64::builder("frames-zero-copy")
                        .nick("Frames zero-copy")
                        .blurb("frames published without a copy (from the pool)")
                        .read_only()
                        .build(),
                    glib::ParamSpecUInt64::builder("frames-copied")
                        .nick("Frames copied")
                        .blurb("frames published via the copy fallback")
                        .read_only()
                        .build(),
                ]
            })
        }

        fn signals() -> &'static [glib::subclass::Signal] {
            static SIGNALS: OnceLock<Vec<glib::subclass::Signal>> = OnceLock::new();
            SIGNALS.get_or_init(|| {
                // Mirror shmsink's `client-connected`/`client-disconnected`. iceoryx2 has no
                // per-client fd, so (unlike shmsink, which passes the socket fd) ours carries the
                // current connected-subscriber count.
                vec![
                    glib::subclass::Signal::builder("client-connected")
                        .param_types([u32::static_type()])
                        .build(),
                    glib::subclass::Signal::builder("client-disconnected")
                        .param_types([u32::static_type()])
                        .build(),
                ]
            })
        }

        fn set_property(&self, _id: usize, value: &glib::Value, pspec: &glib::ParamSpec) {
            let mut s = self.settings.lock().unwrap();
            match pspec.name() {
                "service" => s.service = value.get::<String>().expect("string"),
                "max-bytes" => s.max_bytes = value.get().expect("uint"),
                "buffer-size" => s.buffer_size = value.get().expect("uint"),
                "borrowed-max" => s.borrowed_max = value.get().expect("uint"),
                "history-size" => s.history_size = value.get().expect("uint"),
                "safe-overflow" => s.safe_overflow = value.get().expect("bool"),
                "wait-for-connection" => s.wait_for_connection = value.get().expect("bool"),
                "lossless" => s.lossless = value.get().expect("bool"),
                "aux-bytes" => s.aux_bytes = value.get().expect("uint"),
                other => unimplemented!("unknown property {other}"),
            }
        }

        fn property(&self, _id: usize, pspec: &glib::ParamSpec) -> glib::Value {
            match pspec.name() {
                "service" => self.settings.lock().unwrap().service.to_value(),
                "max-bytes" => self.settings.lock().unwrap().max_bytes.to_value(),
                "buffer-size" => self.settings.lock().unwrap().buffer_size.to_value(),
                "borrowed-max" => self.settings.lock().unwrap().borrowed_max.to_value(),
                "history-size" => self.settings.lock().unwrap().history_size.to_value(),
                "safe-overflow" => self.settings.lock().unwrap().safe_overflow.to_value(),
                "wait-for-connection" => {
                    self.settings.lock().unwrap().wait_for_connection.to_value()
                }
                "lossless" => self.settings.lock().unwrap().lossless.to_value(),
                "aux-bytes" => self.settings.lock().unwrap().aux_bytes.to_value(),
                "num-clients" => (self.clients.load(Ordering::Relaxed) as u32).to_value(),
                "frames-sent" => self.counters.sent.load(Ordering::Relaxed).to_value(),
                "frames-zero-copy" => self.counters.zero_copy.load(Ordering::Relaxed).to_value(),
                "frames-copied" => self.counters.copied.load(Ordering::Relaxed).to_value(),
                other => unimplemented!("unknown property {other}"),
            }
        }
    }

    impl GstObjectImpl for Iceoryx2Sink {}

    impl ElementImpl for Iceoryx2Sink {
        fn metadata() -> Option<&'static gst::subclass::ElementMetadata> {
            static METADATA: OnceLock<gst::subclass::ElementMetadata> = OnceLock::new();
            Some(METADATA.get_or_init(|| {
                gst::subclass::ElementMetadata::new(
                    "iceoryx2 sink",
                    "Sink/Video",
                    "Publishes raw video frames into iceoryx2 shared memory (zero-copy capable)",
                    "Nathan Michlo <nathanjmichlo@gmail.com>",
                )
            }))
        }

        fn pad_templates() -> &'static [gst::PadTemplate] {
            static PAD_TEMPLATES: OnceLock<Vec<gst::PadTemplate>> = OnceLock::new();
            PAD_TEMPLATES.get_or_init(|| {
                let caps = crate::caps::supported_video_caps();
                let sink_template = gst::PadTemplate::new(
                    "sink",
                    gst::PadDirection::Sink,
                    gst::PadPresence::Always,
                    &caps,
                )
                .expect("failed to build sink pad template");
                vec![sink_template]
            })
        }
    }

    impl BaseSinkImpl for Iceoryx2Sink {
        fn start(&self) -> Result<(), gst::ErrorMessage> {
            use gst_plugin_iceoryx2_video as iox2v;
            let settings = self.settings.lock().unwrap().clone();

            let node = iox2v::create_node().map_err(to_err_msg)?;
            // Lossless mode requires the ring to NOT overwrite unread samples, so the producer
            // back-pressures instead. (The subscriber must open with the same overflow setting.)
            let qos = iox2v::Qos {
                buffer_size: settings.buffer_size,
                borrowed_max: settings.borrowed_max,
                history_size: settings.history_size,
                safe_overflow: settings.safe_overflow && !settings.lossless,
            };
            let pubsub =
                iox2v::open_video_service(&node, &settings.service, &qos).map_err(to_err_msg)?;
            let notifier = iox2v::create_notifier(&node, &settings.service).map_err(to_err_msg)?;

            self.counters.sent.store(0, Ordering::Relaxed);
            self.counters.zero_copy.store(0, Ordering::Relaxed);
            self.counters.copied.store(0, Ordering::Relaxed);
            self.clients.store(0, Ordering::Relaxed);
            self.unlocked.store(false, Ordering::Relaxed);
            gst::info!(CAT, imp = self, "started on service {:?}", settings.service);
            *self.state.lock().unwrap() = Some(State {
                node,
                pubsub,
                publisher: None,
                notifier,
                registry: Arc::new(Mutex::new(HashMap::new())),
                max_slice_len: 0,
                caps: None,
                caps_full: None,
            });
            Ok(())
        }

        fn stop(&self) -> Result<(), gst::ErrorMessage> {
            self.state.lock().unwrap().take();
            gst::info!(
                CAT,
                imp = self,
                "stopped: {} frames sent ({} zero-copy, {} copied)",
                self.counters.sent.load(Ordering::Relaxed),
                self.counters.zero_copy.load(Ordering::Relaxed),
                self.counters.copied.load(Ordering::Relaxed),
            );
            Ok(())
        }

        // Break `render` out of a `wait-for-connection` / lossless wait on flush / shutdown.
        fn unlock(&self) -> Result<(), gst::ErrorMessage> {
            self.unlocked.store(true, Ordering::Release);
            Ok(())
        }

        fn unlock_stop(&self) -> Result<(), gst::ErrorMessage> {
            self.unlocked.store(false, Ordering::Release);
            Ok(())
        }

        // Propagate end-of-stream to subscribers as a sentinel sample (best-effort: a slow consumer
        // on a safe-overflow ring may miss it). Mirrors unixfd's COMMAND_TYPE_EOS.
        fn event(&self, event: gst::Event) -> bool {
            if event.type_() == gst::EventType::Eos {
                if let Err(e) = self.publish_eos() {
                    gst::warning!(CAT, imp = self, "could not publish EOS sentinel: {e}");
                }
            }
            self.parent_event(event)
        }

        fn set_caps(&self, caps: &gst::Caps) -> Result<(), gst::LoggableError> {
            let info = gst_video::VideoInfo::from_caps(caps)
                .map_err(|_| gst::loggable_error!(CAT, "failed to parse video info from caps"))?;

            let n_planes = info.n_planes();
            let mut stride = [0u32; MAX_PLANES];
            let mut plane_offsets = [0u32; MAX_PLANES];
            for i in 0..(n_planes as usize).min(MAX_PLANES) {
                stride[i] = info.stride()[i] as u32;
                plane_offsets[i] = info.offset()[i] as u32;
            }
            let caps_info = CapsInfo {
                format: info.format().to_str().as_str().to_string(),
                width: info.width(),
                height: info.height(),
                n_planes,
                stride,
                plane_offsets,
                size: info.size(),
            };

            let settings = self.settings.lock().unwrap().clone();
            let mut guard = self.state.lock().unwrap();
            let state = guard
                .as_mut()
                .ok_or_else(|| gst::loggable_error!(CAT, "set_caps before start"))?;

            // Create the publisher exactly once, on the first caps. A `PowerOfTwo` allocation
            // strategy lets a *larger* later frame reallocate iceoryx2's data segment internally
            // (`loan_slice_uninit` grows it) rather than us tearing down and rebuilding the
            // publisher port — which would drop in-flight samples and churn shared memory on every
            // resolution change. `max-bytes` (if set) seeds the initial size so the common case
            // never reallocates at all.
            // Seed the slice large enough for pixels + the aux-blob reserve (the zero-copy path's
            // loan size). The copy path may loan larger if a frame carries heavy metadata —
            // `PowerOfTwo` grows the data segment on demand, so this is only the common-case seed.
            let needed = (caps_info.size + settings.aux_bytes as usize)
                .max(settings.max_bytes as usize)
                .max(1);
            if state.publisher.is_none() {
                let publisher = state
                    .pubsub
                    .publisher_builder()
                    .initial_max_slice_len(needed)
                    .allocation_strategy(AllocationStrategy::PowerOfTwo)
                    .create()
                    .map_err(|e| gst::loggable_error!(CAT, "create publisher: {e}"))?;
                state.publisher = Some(Arc::new(publisher));
            }
            state.max_slice_len = needed.max(state.max_slice_len);
            gst::info!(
                CAT,
                imp = self,
                "caps: {} {}x{} {} planes, size {}",
                caps_info.format,
                caps_info.width,
                caps_info.height,
                caps_info.n_planes,
                caps_info.size
            );
            state.caps = Some(caps_info);
            state.caps_full = Some(caps.clone());
            Ok(())
        }

        /// Advertise our non-reusing zero-copy pool so upstream renders straight into shared memory.
        fn propose_allocation(
            &self,
            query: &mut gst::query::Allocation,
        ) -> Result<(), gst::LoggableError> {
            let (lossless, aux_reserve) = {
                let s = self.settings.lock().unwrap();
                (s.lossless, s.aux_bytes as usize)
            };
            {
                let guard = self.state.lock().unwrap();
                if let Some(state) = guard.as_ref() {
                    if let (Some(publisher), Some(caps_info)) = (&state.publisher, &state.caps) {
                        let size = caps_info.size as u32;
                        let pool = Iceoryx2BufferPool::new(
                            publisher.clone(),
                            caps_info.size,
                            aux_reserve,
                            state.registry.clone(),
                            lossless,
                            self.unlocked.clone(),
                        );
                        let (caps, _need_pool) = query.get_owned();
                        let mut config = pool.config();
                        config.set_params(caps.as_ref(), size, 0, 0);
                        pool.set_config(config)
                            .map_err(|_| gst::loggable_error!(CAT, "pool config failed"))?;
                        query.add_allocation_pool(Some(&pool), size, 0, 0);
                    }
                }
            }
            self.parent_propose_allocation(query)
        }

        fn render(&self, buffer: &gst::Buffer) -> Result<gst::FlowSuccess, gst::FlowError> {
            // Update num-clients + fire connect/disconnect signals, and (if wait-for-connection)
            // block until at least one subscriber is present.
            self.track_and_await_connection()?;
            let lossless = self.settings.lock().unwrap().lossless;

            let map = buffer.map_readable().map_err(|_| {
                gst::element_imp_error!(self, gst::CoreError::Failed, ["failed to map buffer"]);
                gst::FlowError::Error
            })?;
            let src = map.as_slice();
            let ptr = src.as_ptr() as usize;

            let mut guard = self.state.lock().unwrap();
            let state = guard.as_mut().ok_or(gst::FlowError::Error)?;
            let caps = state.caps.clone().ok_or_else(|| {
                gst::element_imp_error!(self, gst::CoreError::Negotiation, ["render before caps"]);
                gst::FlowError::NotNegotiated
            })?;
            let caps_full = state.caps_full.clone();
            let header = build_header(buffer, &caps);

            // Zero-copy path: the buffer came from our pool; its loaned sample already holds the
            // pixels in `[0..pixel_size]` plus a reserved tail. We fill the tail with the aux blob,
            // stamp the header, and send — no pixel copy.
            let taken: Option<SendSample> = state.registry.lock().unwrap().remove(&ptr);
            if let Some(send_sample) = taken {
                drop(map);
                let mut header = header;
                let mut sample = send_sample.0;
                let payload = sample.payload_mut();
                let total = payload.len();
                let pixel_size = caps.size.min(total);
                let tail = total - pixel_size;
                // Build the aux blob to fit the reserved tail (caps always written; metas dropped
                // if they overflow). A tail too small for even an empty blob carries none.
                let blob = if tail >= AUX_MIN_BYTES {
                    let b = aux_blob(caps_full.as_ref(), buffer, Some(tail));
                    if b.len() <= tail {
                        b
                    } else {
                        gst::warning!(
                            CAT,
                            imp = self,
                            "caps blob exceeds aux reserve; sending none"
                        );
                        Vec::new()
                    }
                } else {
                    Vec::new()
                };
                // Initialise the whole tail: aux blob first, then zero the slack so `assume_init`
                // is sound and the parser cleanly ignores the padding.
                let tail_region = &mut payload[pixel_size..];
                for (dst, &b) in tail_region.iter_mut().zip(blob.iter()) {
                    dst.write(b);
                }
                for dst in tail_region[blob.len()..].iter_mut() {
                    dst.write(0);
                }
                header.aux_size = tail as u32;
                *sample.user_header_mut() = header;
                let sample = unsafe { sample.assume_init() };
                sample.send().map_err(|e| {
                    gst::element_imp_error!(self, gst::ResourceError::Write, ["send sample: {e}"]);
                    gst::FlowError::Error
                })?;
                notify(state, self)?;
                self.counters.sent.fetch_add(1, Ordering::Relaxed);
                self.counters.zero_copy.fetch_add(1, Ordering::Relaxed);
                return Ok(gst::FlowSuccess::Ok);
            }

            // Copy fallback: loan a fresh slice sized for pixels + the full aux blob (unbounded
            // here — we control the loan, so no metadata is dropped) and memcpy both in. Clone the
            // publisher and release the `state` lock *before* the loan, so a lossless back-pressure
            // wait on a full ring never stalls other state access (mirrors the pool's discipline in
            // `pool.rs`, which snapshots and unlocks before waiting).
            let mut header = header;
            let blob = aux_blob(caps_full.as_ref(), buffer, None);
            let total = src.len() + blob.len();
            let publisher = state.publisher.clone().ok_or(gst::FlowError::Error)?;
            drop(guard);

            let mut sample = loop {
                match publisher.loan_slice_uninit(total) {
                    Ok(s) => break s,
                    // In lossless mode a failed loan means the ring is full (consumer behind):
                    // back-pressure by waiting for a slot rather than erroring out.
                    Err(e) if lossless && !self.unlocked.load(Ordering::Acquire) => {
                        gst::trace!(CAT, imp = self, "lossless: ring full, waiting ({e})");
                        std::thread::sleep(WAIT_POLL);
                    }
                    Err(_) if lossless => return Err(gst::FlowError::Flushing),
                    Err(e) => {
                        gst::element_imp_error!(
                            self,
                            gst::ResourceError::Write,
                            ["loan slice: {e}"]
                        );
                        return Err(gst::FlowError::Error);
                    }
                }
            };
            header.aux_size = blob.len() as u32;
            *sample.user_header_mut() = header;
            let payload = sample.payload_mut();
            for (dst, &b) in payload[..src.len()].iter_mut().zip(src.iter()) {
                dst.write(b);
            }
            for (dst, &b) in payload[src.len()..].iter_mut().zip(blob.iter()) {
                dst.write(b);
            }
            let sample = unsafe { sample.assume_init() };
            sample.send().map_err(|e| {
                gst::element_imp_error!(self, gst::ResourceError::Write, ["send sample: {e}"]);
                gst::FlowError::Error
            })?;
            self.notify_after_send()?;
            self.counters.sent.fetch_add(1, Ordering::Relaxed);
            self.counters.copied.fetch_add(1, Ordering::Relaxed);
            Ok(gst::FlowSuccess::Ok)
        }
    }

    impl Iceoryx2Sink {
        /// Refresh `num-clients` (firing connect/disconnect signals on change) and, when
        /// `wait-for-connection` is set, block until at least one subscriber is present.
        fn track_and_await_connection(&self) -> Result<(), gst::FlowError> {
            let wait = self.settings.lock().unwrap().wait_for_connection;
            loop {
                if self.unlocked.load(Ordering::Acquire) {
                    return Err(gst::FlowError::Flushing);
                }
                let now = self.poll_clients();
                let prev = self.clients.swap(now, Ordering::Relaxed);
                self.emit_client_changes(prev, now);
                if now >= 1 || !wait {
                    return Ok(());
                }
                std::thread::sleep(WAIT_POLL);
            }
        }

        /// Number of subscribers currently connected to the service (0 if not started).
        fn poll_clients(&self) -> u64 {
            self.state
                .lock()
                .unwrap()
                .as_ref()
                .map(|s| s.pubsub.dynamic_config().number_of_subscribers() as u64)
                .unwrap_or(0)
        }

        /// Emit `client-connected` / `client-disconnected` (carrying the new count) on a change.
        fn emit_client_changes(&self, prev: u64, now: u64) {
            use std::cmp::Ordering as Cmp;
            match now.cmp(&prev) {
                Cmp::Greater => self
                    .obj()
                    .emit_by_name::<()>("client-connected", &[&(now as u32)]),
                Cmp::Less => self
                    .obj()
                    .emit_by_name::<()>("client-disconnected", &[&(now as u32)]),
                Cmp::Equal => {}
            }
        }

        /// Fire the event notification after a copy-path send. The `state` lock was released for the
        /// loan/back-pressure wait, so re-lock briefly; a no-op if the element has since stopped (the
        /// sample is already on the ring, and the consumer's listener has its own timeout fallback).
        fn notify_after_send(&self) -> Result<(), gst::FlowError> {
            let guard = self.state.lock().unwrap();
            match guard.as_ref() {
                Some(state) => notify(state, self),
                None => Ok(()),
            }
        }

        /// Publish a zero-pixel sentinel sample with [`HEADER_FLAG_EOS`] so the source can emit EOS.
        fn publish_eos(&self) -> Result<(), String> {
            let mut guard = self.state.lock().unwrap();
            let state = match guard.as_mut() {
                Some(s) => s,
                None => return Ok(()),
            };
            let publisher = match state.publisher.clone() {
                Some(p) => p,
                None => return Ok(()), // never negotiated caps → nothing was ever published
            };
            let mut sample = publisher.loan_slice_uninit(1).map_err(|e| e.to_string())?;
            *sample.user_header_mut() = VideoFrameHeader {
                flags: gst_plugin_iceoryx2_video::HEADER_FLAG_EOS,
                ..Default::default()
            };
            sample
                .write_from_slice(&[0u8])
                .send()
                .map_err(|e| e.to_string())?;
            let _ = state.notifier.notify();
            Ok(())
        }
    }

    /// Fire the event notification so a subscriber's listener wakes.
    fn notify(state: &State, imp: &Iceoryx2Sink) -> Result<(), gst::FlowError> {
        state.notifier.notify().map_err(|e| {
            gst::element_imp_error!(imp, gst::ResourceError::Write, ["notify: {e}"]);
            gst::FlowError::Error
        })?;
        Ok(())
    }

    /// Serialise the aux blob (full caps + serialisable metas), or empty if caps are unknown.
    /// `limit` bounds it for the fixed zero-copy reserve; `None` for the copy path. See [`crate::aux`].
    fn aux_blob(
        caps: Option<&gst::Caps>,
        buffer: &gst::BufferRef,
        limit: Option<usize>,
    ) -> Vec<u8> {
        match caps {
            Some(c) => crate::aux::build_aux(c, buffer, limit),
            None => Vec::new(),
        }
    }

    /// Build a `VideoFrameHeader` from the buffer's timing + the cached caps layout.
    fn build_header(buffer: &gst::Buffer, caps: &CapsInfo) -> VideoFrameHeader {
        let mut h = VideoFrameHeader {
            pts: buffer.pts().map(|t| t.nseconds()).unwrap_or(u64::MAX),
            dts: buffer.dts().map(|t| t.nseconds()).unwrap_or(u64::MAX),
            duration: buffer.duration().map(|t| t.nseconds()).unwrap_or(u64::MAX),
            offset: buffer.offset(),
            flags: buffer.flags().bits() as u64,
            width: caps.width,
            height: caps.height,
            n_planes: caps.n_planes,
            aux_size: 0,
            stride: caps.stride,
            plane_offsets: caps.plane_offsets,
            format: [0u8; gst_plugin_iceoryx2_video::FORMAT_LEN],
        };
        h.set_format(&caps.format);
        h
    }

    /// Map a core-SDK transport error (which already carries its own context) onto a GStreamer
    /// start-time error message.
    fn to_err_msg(e: gst_plugin_iceoryx2_video::Error) -> gst::ErrorMessage {
        gst::error_msg!(gst::ResourceError::Failed, ["{e}"])
    }
}

glib::wrapper! {
    pub struct Iceoryx2Sink(ObjectSubclass<imp::Iceoryx2Sink>)
        @extends gst_base::BaseSink, gst::Element, gst::Object;
}

/// Register the `iceoryx2sink` element factory with the plugin.
pub fn register(plugin: &gst::Plugin) -> Result<(), glib::BoolError> {
    gst::Element::register(
        Some(plugin),
        "iceoryx2sink",
        gst::Rank::NONE,
        Iceoryx2Sink::static_type(),
    )
}
