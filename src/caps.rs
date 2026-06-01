//! Shared `video/x-raw` caps for both the [`sink`](crate::sink) and [`source`](crate::source)
//! elements, so the two ends advertise an identical format set.
//!
//! The [`VideoFrameHeader`](crate::format::VideoFrameHeader) can describe any GStreamer raw format
//! (it carries `n_planes` + per-plane `stride`/`offset`), so the supported set is governed solely by
//! what the pad templates advertise. We list the packed formats the current pipeline produces
//! (`BGR`/`RGB`) plus the two common planar formats (`I420`/`NV12`) to prove the multi-plane path.

use gst_video::VideoFormat;

/// The raw video formats both elements accept/produce, in negotiation-preference order.
pub const SUPPORTED_FORMATS: [VideoFormat; 4] = [
    VideoFormat::Bgr,
    VideoFormat::Rgb,
    VideoFormat::I420,
    VideoFormat::Nv12,
];

/// `video/x-raw` caps with [`SUPPORTED_FORMATS`]; `width`/`height`/`framerate` left open
/// (caps-parameterised — the actual geometry is fixed by the negotiated stream / received header).
pub fn supported_video_caps() -> gst::Caps {
    gst_video::VideoCapsBuilder::new()
        .format_list(SUPPORTED_FORMATS)
        .build()
}
