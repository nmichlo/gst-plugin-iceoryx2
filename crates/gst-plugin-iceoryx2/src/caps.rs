//! Shared `video/x-raw` caps for both the [`sink`](crate::sink) and [`source`](crate::source)
//! elements, so the two ends advertise an identical format set.
//!
//! The supported set lives in the core crate as
//! [`SUPPORTED_FORMATS`](gst_plugin_iceoryx2_video::SUPPORTED_FORMATS) (the same list its
//! `validate_geometry` recognises); here we just map those names to `gst_video::VideoFormat` to build
//! the pad-template caps, so the plugin and the core cannot disagree on what is supported.

use gst_video::VideoFormat;

/// The core crate's canonical format names mapped to `gst_video::VideoFormat`, in the same order.
fn supported_formats() -> Vec<VideoFormat> {
    gst_plugin_iceoryx2_video::SUPPORTED_FORMATS
        .iter()
        .map(|name| VideoFormat::from_string(name))
        .collect()
}

/// `video/x-raw` caps with the supported formats; `width`/`height`/`framerate` left open
/// (caps-parameterised — the actual geometry is fixed by the negotiated stream / received header).
pub fn supported_video_caps() -> gst::Caps {
    gst_video::VideoCapsBuilder::new()
        .format_list(supported_formats())
        .build()
}
