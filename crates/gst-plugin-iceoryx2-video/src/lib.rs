//! `gst-plugin-iceoryx2-video` — the GStreamer-free Rust core of the
//! [`gst-plugin-iceoryx2`](https://crates.io/crates/gst-plugin-iceoryx2) project: move raw video
//! frames between processes **zero-copy** over [iceoryx2](https://crates.io/crates/iceoryx2) shared
//! memory, with no GStreamer dependency.
//!
//! This crate owns the wire contract and the transport, so there is exactly one implementation:
//!
//! * [`VideoFrameHeader`] — the fixed `#[repr(C)]` per-sample user-header (`SPEC.md` §3).
//! * [`build_aux`] / [`parse_aux`] — the variable-length aux-blob framing (caps string + metas).
//! * [`validate_geometry`] — bounds-checks an untrusted header against its payload.
//! * [`VideoFramePublisher`] / [`VideoFrameSubscriber`] — the publish/subscribe SDK, mirroring the
//!   Python `gst_iceoryx2.video` classes and interoperating with the GStreamer `iceoryx2sink` /
//!   `iceoryx2src` elements (which are built on these same primitives).
//!
//! ```no_run
//! use gst_plugin_iceoryx2_video::{VideoFrameSubscriber, FrameParams, VideoFramePublisher};
//!
//! # fn main() -> Result<(), Box<dyn std::error::Error>> {
//! // Publish a frame (no GStreamer in sight):
//! let pub_ = VideoFramePublisher::new("video/cam0/frame/v2", 640 * 480 * 3)?;
//! let pixels = vec![0u8; 640 * 480 * 3];
//! pub_.publish_frame(&pixels, &FrameParams { width: 640, height: 480, ..Default::default() })?;
//!
//! // Subscribe from another process:
//! let sub = VideoFrameSubscriber::new("video/cam0/frame/v2")?;
//! if let Some(frame) = sub.receive_blocking(Some(1000))? {
//!     println!("{}x{} pts {}", frame.header().width, frame.header().height, frame.header().pts);
//! }
//! # Ok(()) }
//! ```

pub mod aux;
pub mod error;
pub mod header;
pub mod qos;
pub mod transport;
pub mod validate;

pub use aux::{build_aux, parse_aux, ParsedAux};
pub use error::{Error, Result};
pub use header::{
    field_offsets, VideoFrameHeader, DEFAULT_AUX_BYTES, FORMAT_LEN, HEADER_ALIGN, HEADER_FLAG_EOS,
    HEADER_SIZE, HEADER_TYPE_NAME, MAX_PLANES,
};
pub use qos::{
    Qos, DEFAULT_SERVICE, VIDEO_BORROWED_MAX, VIDEO_BUFFER_SIZE, VIDEO_HISTORY_SIZE,
    VIDEO_SAFE_OVERFLOW,
};
pub use transport::{
    create_listener, create_node, create_notifier, open_video_service, FrameParams, IpcService,
    ReceivedFrame, VideoFramePublisher, VideoFrameSubscriber, VideoPubSub,
};
pub use validate::{plane_heights, validate_geometry, SUPPORTED_FORMATS};
