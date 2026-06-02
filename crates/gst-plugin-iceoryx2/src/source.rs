//! The `iceoryx2src` element — a [`gst_base::PushSrc`] that subscribes to an iceoryx2 service and
//! pushes the received frames downstream **zero-copy**: the loaned shared-memory payload is wrapped
//! directly in a `GstBuffer` (no `memcpy`), and its [`VideoFrameHeader`] is mapped back onto the
//! buffer's timing + a `GstVideoMeta`.
//!
//! It is the exact inverse of [`iceoryx2sink`](crate::sink): the sink publishes the GstBuffer's
//! pixels + header into shared memory; this source recovers them. The two communicate over the same
//! `publish_subscribe::<[u8]>().user_header::<VideoFrameHeader>()` service plus the paired event,
//! so the source parks on the event `Listener` and wakes the instant a frame is published.
//!
//! **Caps are data-driven.** The format/geometry are unknown until the first sample arrives, so
//! [`negotiate`](imp::Iceoryx2Src) is a no-op and `create` calls `set_caps` from the received
//! header before pushing the first (and any format-changed) buffer.

use gst::glib;
use gst::prelude::*;
use gst::subclass::prelude::*;
use std::sync::{LazyLock, Mutex};

use gst_plugin_iceoryx2_video::{DEFAULT_SERVICE, IpcService, MAX_PLANES, VideoFrameHeader};

static CAT: LazyLock<gst::DebugCategory> = LazyLock::new(|| {
    gst::DebugCategory::new(
        "iceoryx2src",
        gst::DebugColorFlags::empty(),
        Some("iceoryx2 video source"),
    )
});

const DEFAULT_BUFFER_SIZE: u32 = 10;
const DEFAULT_BORROWED_MAX: u32 = 10;
const DEFAULT_HISTORY_SIZE: u32 = 0;
const DEFAULT_SAFE_OVERFLOW: bool = true;

mod imp {
    use super::*;
    use gst_base::prelude::*;
    use gst_base::subclass::base_src::CreateSuccess;
    use gst_base::subclass::prelude::*;
    use std::sync::OnceLock;
    use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
    use std::time::Duration;

    use iceoryx2::port::listener::Listener;
    use iceoryx2::port::subscriber::Subscriber;
    use iceoryx2::prelude::*;
    use iceoryx2::sample::Sample;

    /// Re-check the unlock flag this often while parked on the listener, so flush/shutdown is
    /// honoured promptly even though no notification arrives.
    const WAIT_TIMEOUT: Duration = Duration::from_millis(100);

    /// A received slice sample, kept alive for as long as the `GstBuffer` wrapping its payload
    /// lives. `Sample` is `Send + 'static` for the thread-safe service, so the buffer can be freed
    /// on any thread with no `unsafe`.
    ///
    /// The payload is `pixels[..pixel_size]` followed by the aux blob; `as_ref` exposes only the
    /// pixel region so the wrapping `GstBuffer` never sees the trailing caps/meta bytes.
    struct RecvSample {
        sample: Sample<IpcService, [u8], VideoFrameHeader>,
        pixel_size: usize,
    }

    impl AsRef<[u8]> for RecvSample {
        fn as_ref(&self) -> &[u8] {
            &self.sample.payload()[..self.pixel_size]
        }
    }

    /// User-configurable element properties — must match the publisher's service + QoS so
    /// `open_or_create` is compatible.
    #[derive(Debug, Clone)]
    pub struct Settings {
        pub service: String,
        pub buffer_size: u32,
        pub borrowed_max: u32,
        pub history_size: u32,
        pub safe_overflow: bool,
    }

    impl Default for Settings {
        fn default() -> Self {
            Self {
                service: DEFAULT_SERVICE.to_string(),
                buffer_size: DEFAULT_BUFFER_SIZE,
                borrowed_max: DEFAULT_BORROWED_MAX,
                history_size: DEFAULT_HISTORY_SIZE,
                safe_overflow: DEFAULT_SAFE_OVERFLOW,
            }
        }
    }

    /// Video layout recovered from a header, used to (re)negotiate caps and stamp the `VideoMeta`.
    /// `PartialEq` drives the "caps changed?" check between frames.
    #[derive(Debug, Clone, PartialEq, Eq)]
    pub struct CapsInfo {
        pub format: String,
        pub width: u32,
        pub height: u32,
        pub n_planes: u32,
        pub stride: [u32; MAX_PLANES],
        pub plane_offsets: [u32; MAX_PLANES],
    }

    impl CapsInfo {
        fn from_header(h: &VideoFrameHeader) -> Self {
            Self {
                format: h.format_name().to_string(),
                width: h.width,
                height: h.height,
                n_planes: h.n_planes,
                stride: h.stride,
                plane_offsets: h.plane_offsets,
            }
        }

        fn video_format(&self) -> gst_video::VideoFormat {
            gst_video::VideoFormat::from_string(&self.format)
        }

        fn to_caps(&self) -> Result<gst::Caps, glib::BoolError> {
            let format = self.video_format();
            if matches!(
                format,
                gst_video::VideoFormat::Unknown | gst_video::VideoFormat::Encoded
            ) {
                return Err(glib::bool_error!("unknown video format {:?}", self.format));
            }
            gst_video::VideoInfo::builder(format, self.width, self.height)
                .build()?
                .to_caps()
        }
    }

    /// Live iceoryx2 ports, created in `start`.
    pub struct State {
        pub _node: Node<IpcService>,
        pub subscriber: Subscriber<IpcService, [u8], VideoFrameHeader>,
        pub listener: Listener<IpcService>,
        /// Last caps we pushed, for change detection. The full `GstCaps` (not just geometry) so a
        /// colorimetry-only change recovered from the aux blob still triggers a renegotiation.
        pub last_caps: Option<gst::Caps>,
    }

    #[derive(Default)]
    pub struct Iceoryx2Src {
        pub settings: Mutex<Settings>,
        pub state: Mutex<Option<State>>,
        /// Set by `unlock` to break a blocked `create` out of its receive loop (flush / shutdown).
        pub unlocked: AtomicBool,
        /// Atomic so the `frames-received` property never contends on `state` — which `create`
        /// holds while parked on the listener (up to one `WAIT_TIMEOUT`).
        pub frames_received: AtomicU64,
    }

    #[glib::object_subclass]
    impl ObjectSubclass for Iceoryx2Src {
        const NAME: &'static str = "GstIceoryx2Src";
        type Type = super::Iceoryx2Src;
        type ParentType = gst_base::PushSrc;
    }

    impl ObjectImpl for Iceoryx2Src {
        fn constructed(&self) {
            self.parent_constructed();
            let obj = self.obj();
            // A live source whose buffers carry their own (publisher-stamped) timestamps.
            obj.set_live(true);
            obj.set_format(gst::Format::Time);
            obj.set_do_timestamp(false);
        }

        fn properties() -> &'static [glib::ParamSpec] {
            static PROPERTIES: OnceLock<Vec<glib::ParamSpec>> = OnceLock::new();
            PROPERTIES.get_or_init(|| {
                vec![
                    glib::ParamSpecString::builder("service")
                        .nick("Service")
                        .blurb("iceoryx2 service name (publish/subscribe + event)")
                        .default_value(Some(DEFAULT_SERVICE))
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
                        .blurb("publisher history-size QoS (must match the publisher)")
                        .default_value(DEFAULT_HISTORY_SIZE)
                        .build(),
                    glib::ParamSpecBoolean::builder("safe-overflow")
                        .nick("Safe overflow")
                        .blurb("ring-buffer safe-overflow QoS (must match the publisher)")
                        .default_value(DEFAULT_SAFE_OVERFLOW)
                        .build(),
                    glib::ParamSpecUInt64::builder("frames-received")
                        .nick("Frames received")
                        .blurb("total frames received from shared memory")
                        .read_only()
                        .build(),
                ]
            })
        }

        fn set_property(&self, _id: usize, value: &glib::Value, pspec: &glib::ParamSpec) {
            let mut s = self.settings.lock().unwrap();
            match pspec.name() {
                "service" => s.service = value.get::<String>().expect("string"),
                "buffer-size" => s.buffer_size = value.get().expect("uint"),
                "borrowed-max" => s.borrowed_max = value.get().expect("uint"),
                "history-size" => s.history_size = value.get().expect("uint"),
                "safe-overflow" => s.safe_overflow = value.get().expect("bool"),
                other => unimplemented!("unknown property {other}"),
            }
        }

        fn property(&self, _id: usize, pspec: &glib::ParamSpec) -> glib::Value {
            match pspec.name() {
                "service" => self.settings.lock().unwrap().service.to_value(),
                "buffer-size" => self.settings.lock().unwrap().buffer_size.to_value(),
                "borrowed-max" => self.settings.lock().unwrap().borrowed_max.to_value(),
                "history-size" => self.settings.lock().unwrap().history_size.to_value(),
                "safe-overflow" => self.settings.lock().unwrap().safe_overflow.to_value(),
                "frames-received" => self.frames_received.load(Ordering::Relaxed).to_value(),
                other => unimplemented!("unknown property {other}"),
            }
        }
    }

    impl GstObjectImpl for Iceoryx2Src {}

    impl ElementImpl for Iceoryx2Src {
        fn metadata() -> Option<&'static gst::subclass::ElementMetadata> {
            static METADATA: OnceLock<gst::subclass::ElementMetadata> = OnceLock::new();
            Some(METADATA.get_or_init(|| {
                gst::subclass::ElementMetadata::new(
                    "iceoryx2 source",
                    "Source/Video",
                    "Receives raw video frames from iceoryx2 shared memory (zero-copy)",
                    "Nathan Michlo <nathanjmichlo@gmail.com>",
                )
            }))
        }

        fn pad_templates() -> &'static [gst::PadTemplate] {
            static PAD_TEMPLATES: OnceLock<Vec<gst::PadTemplate>> = OnceLock::new();
            PAD_TEMPLATES.get_or_init(|| {
                let caps = crate::caps::supported_video_caps();
                let src_template = gst::PadTemplate::new(
                    "src",
                    gst::PadDirection::Src,
                    gst::PadPresence::Always,
                    &caps,
                )
                .expect("failed to build src pad template");
                vec![src_template]
            })
        }
    }

    impl BaseSrcImpl for Iceoryx2Src {
        fn start(&self) -> Result<(), gst::ErrorMessage> {
            use gst_plugin_iceoryx2_video as iox2v;
            let settings = self.settings.lock().unwrap().clone();

            let node = iox2v::create_node().map_err(to_err_msg)?;
            let qos = iox2v::Qos {
                buffer_size: settings.buffer_size,
                borrowed_max: settings.borrowed_max,
                history_size: settings.history_size,
                safe_overflow: settings.safe_overflow,
            };
            let pubsub =
                iox2v::open_video_service(&node, &settings.service, &qos).map_err(to_err_msg)?;
            let subscriber = pubsub
                .subscriber_builder()
                .create()
                .map_err(|e| err_msg("create subscriber", e))?;
            let listener = iox2v::create_listener(&node, &settings.service).map_err(to_err_msg)?;

            self.unlocked.store(false, Ordering::Release);
            self.frames_received.store(0, Ordering::Relaxed);
            gst::info!(CAT, imp = self, "started on service {:?}", settings.service);
            *self.state.lock().unwrap() = Some(State {
                _node: node,
                subscriber,
                listener,
                last_caps: None,
            });
            Ok(())
        }

        fn stop(&self) -> Result<(), gst::ErrorMessage> {
            self.state.lock().unwrap().take();
            gst::info!(
                CAT,
                imp = self,
                "stopped: {} frames received",
                self.frames_received.load(Ordering::Relaxed)
            );
            Ok(())
        }

        // A pure receiver: not seekable, and caps are data-driven so there is nothing to negotiate
        // up front. Returning Ok stops BaseSrc from fixating the open template caps to a default;
        // `create` sets the real caps from the first sample's header.
        fn is_seekable(&self) -> bool {
            false
        }

        fn negotiate(&self) -> Result<(), gst::LoggableError> {
            Ok(())
        }

        // Break a blocked `create` out of its receive loop on flush / state change.
        fn unlock(&self) -> Result<(), gst::ErrorMessage> {
            self.unlocked.store(true, Ordering::Release);
            Ok(())
        }

        fn unlock_stop(&self) -> Result<(), gst::ErrorMessage> {
            self.unlocked.store(false, Ordering::Release);
            Ok(())
        }
    }

    impl PushSrcImpl for Iceoryx2Src {
        fn create(
            &self,
            _buffer: Option<&mut gst::BufferRef>,
        ) -> Result<CreateSuccess, gst::FlowError> {
            loop {
                if self.unlocked.load(Ordering::Acquire) {
                    return Err(gst::FlowError::Flushing);
                }

                let mut guard = self.state.lock().unwrap();
                let state = guard.as_mut().ok_or(gst::FlowError::Error)?;
                match state.subscriber.receive() {
                    Ok(Some(sample)) => {
                        let header = *sample.user_header();
                        if header.flags & gst_plugin_iceoryx2_video::HEADER_FLAG_EOS != 0 {
                            drop(guard);
                            gst::debug!(CAT, imp = self, "received EOS sentinel");
                            return Err(gst::FlowError::Eos);
                        }

                        // Split the payload into pixels + aux blob (caps + serialised metas).
                        let payload_len = sample.payload().len();
                        let aux_size = (header.aux_size as usize).min(payload_len);
                        let pixel_size = payload_len - aux_size;

                        // The header's geometry is untrusted wire data (any process on the same
                        // service can publish it). Reject a frame whose declared planes would read
                        // past the pixel region before it reaches downstream — drop it, never panic.
                        if let Err(why) =
                            gst_plugin_iceoryx2_video::validate_geometry(&header, pixel_size)
                        {
                            drop(guard);
                            gst::warning!(
                                CAT,
                                imp = self,
                                "dropping frame with invalid geometry: {why}"
                            );
                            continue;
                        }
                        self.frames_received.fetch_add(1, Ordering::Relaxed);

                        let parsed = if aux_size > 0 {
                            crate::aux::parse_aux(&sample.payload()[pixel_size..])
                        } else {
                            crate::aux::ParsedAux::default()
                        };

                        let info = CapsInfo::from_header(&header);
                        // Prefer the publisher's full caps (colorimetry/framerate/PAR); fall back to
                        // rebuilding from the header geometry when no aux caps travelled.
                        let caps = match parsed.caps.clone() {
                            Some(c) => c,
                            None => info.to_caps().map_err(|e| {
                                gst::element_imp_error!(
                                    self,
                                    gst::CoreError::Negotiation,
                                    ["build caps: {e}"]
                                );
                                gst::FlowError::NotNegotiated
                            })?,
                        };
                        let caps_changed = state.last_caps.as_ref() != Some(&caps);
                        if caps_changed {
                            state.last_caps = Some(caps.clone());
                        }
                        drop(guard);
                        let recv = RecvSample { sample, pixel_size };
                        return self.deliver(recv, header, info, caps, caps_changed, parsed.metas);
                    }
                    Ok(None) => {
                        // Park until the sink's notifier wakes us (or the timeout elapses so we
                        // re-check the unlock flag). Holding the lock here only briefly stalls a
                        // `frames-received` read; `unlock` is lock-free so flush stays responsive.
                        let _ = state.listener.timed_wait_one(WAIT_TIMEOUT);
                        drop(guard);
                    }
                    Err(e) => {
                        drop(guard);
                        gst::element_imp_error!(self, gst::ResourceError::Read, ["receive: {e}"]);
                        return Err(gst::FlowError::Error);
                    }
                }
            }
        }
    }

    impl Iceoryx2Src {
        /// Negotiate caps (if they changed) and wrap the loaned payload into a zero-copy buffer
        /// carrying the header's timing, a stride-exact `VideoMeta`, and any metas from the aux blob.
        fn deliver(
            &self,
            sample: RecvSample,
            header: VideoFrameHeader,
            info: CapsInfo,
            caps: gst::Caps,
            caps_changed: bool,
            metas: Vec<Vec<u8>>,
        ) -> Result<CreateSuccess, gst::FlowError> {
            if caps_changed {
                self.obj().set_caps(&caps).map_err(|e| {
                    gst::element_imp_error!(
                        self,
                        gst::CoreError::Negotiation,
                        ["set caps {caps}: {e}"]
                    );
                    gst::FlowError::NotNegotiated
                })?;
                gst::info!(CAT, imp = self, "negotiated caps {}", caps);
            }

            let buffer = self.build_buffer(sample, &header, &info, &metas);
            Ok(CreateSuccess::NewBuffer(buffer))
        }

        /// Wrap the loaned shared-memory slice in a `GstBuffer` with no copy, then map the header's
        /// timing onto it and attach a `VideoMeta` carrying the publisher's exact per-plane
        /// `stride`/`offset` (so downstream reads the planes correctly regardless of the caps'
        /// default stride).
        fn build_buffer(
            &self,
            sample: RecvSample,
            header: &VideoFrameHeader,
            info: &CapsInfo,
            metas: &[Vec<u8>],
        ) -> gst::Buffer {
            let mem = gst::Memory::from_slice(sample);
            let mut buffer = gst::Buffer::new();
            {
                let b = buffer.get_mut().unwrap();
                b.append_memory(mem);
                b.set_pts(opt_clock(header.pts));
                b.set_dts(opt_clock(header.dts));
                b.set_duration(opt_clock(header.duration));
                b.set_offset(header.offset);
                b.set_flags(gst::BufferFlags::from_bits_truncate(header.flags as u32));

                let format = info.video_format();
                if !matches!(
                    format,
                    gst_video::VideoFormat::Unknown | gst_video::VideoFormat::Encoded
                ) {
                    let n = (info.n_planes as usize).clamp(1, MAX_PLANES);
                    let offsets: Vec<usize> = info.plane_offsets[..n]
                        .iter()
                        .map(|&x| x as usize)
                        .collect();
                    let strides: Vec<i32> = info.stride[..n].iter().map(|&x| x as i32).collect();
                    if let Err(e) = gst_video::VideoMeta::add_full(
                        b,
                        gst_video::VideoFrameFlags::empty(),
                        format,
                        info.width,
                        info.height,
                        &offsets,
                        &strides,
                    ) {
                        gst::warning!(CAT, imp = self, "could not attach VideoMeta: {e}");
                    }
                }

                // Re-attach any metas the publisher serialised into the aux blob. Best-effort: a
                // meta whose API is not registered in this process simply fails to deserialise.
                for blob in metas {
                    let mut consumed = 0;
                    if let Err(e) = gst::Meta::deserialize(b, blob, &mut consumed) {
                        gst::warning!(CAT, imp = self, "could not deserialise meta: {e}");
                    }
                }
            }
            buffer
        }
    }

    /// Map a header timestamp (`u64::MAX` == none) to an optional `ClockTime`.
    fn opt_clock(ns: u64) -> Option<gst::ClockTime> {
        (ns != u64::MAX).then(|| gst::ClockTime::from_nseconds(ns))
    }

    /// Convert an iceoryx2 builder error into a GStreamer start-time error message.
    fn err_msg(context: &str, e: impl std::fmt::Display) -> gst::ErrorMessage {
        gst::error_msg!(gst::ResourceError::Failed, ["{context}: {e}"])
    }

    /// Map a core-SDK transport error (which already carries its own context) onto a GStreamer
    /// start-time error message.
    fn to_err_msg(e: gst_plugin_iceoryx2_video::Error) -> gst::ErrorMessage {
        gst::error_msg!(gst::ResourceError::Failed, ["{e}"])
    }
}

glib::wrapper! {
    pub struct Iceoryx2Src(ObjectSubclass<imp::Iceoryx2Src>)
        @extends gst_base::PushSrc, gst_base::BaseSrc, gst::Element, gst::Object;
}

/// Register the `iceoryx2src` element factory with the plugin.
pub fn register(plugin: &gst::Plugin) -> Result<(), glib::BoolError> {
    gst::Element::register(
        Some(plugin),
        "iceoryx2src",
        gst::Rank::NONE,
        Iceoryx2Src::static_type(),
    )
}
