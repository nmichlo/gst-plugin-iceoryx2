//! GStreamer adapter for the **aux blob**: converts `gst::Caps` ↔ its string form and `GstMeta` ↔
//! serialised bytes, then defers the wire framing to the GStreamer-free
//! [`gst_plugin_iceoryx2_video::aux`] core. This is the only place that knows about GStreamer types;
//! the byte layout (and its parsing) is owned by the core crate, so the Rust SDK and Python parse the
//! identical blob this produces. See [`gst_plugin_iceoryx2_video::aux`] for the layout.

use gst::prelude::*;
use std::sync::LazyLock;

static CAT: LazyLock<gst::DebugCategory> = LazyLock::new(|| {
    gst::DebugCategory::new(
        "iceoryx2aux",
        gst::DebugColorFlags::empty(),
        Some("iceoryx2 aux blob"),
    )
});

/// Build the aux blob for `buffer` under the negotiated `caps`.
///
/// The full caps string is always written first (small, high-value parity item). Then every
/// *serialisable* `GstMeta` on the buffer is appended, **except** `GstVideoMeta` — its information
/// already travels in the fixed header and the receiver rebuilds it from there. `limit` (the reserved
/// tail size, for the zero-copy path) bounds the total; metas that do not fit are dropped (the core
/// framing keeps as many as fit, in order). `None` means unbounded (the copy path).
pub fn build_aux(caps: &gst::Caps, buffer: &gst::BufferRef, limit: Option<usize>) -> Vec<u8> {
    let video_meta_api = gst_video::VideoMeta::meta_api();
    let mut metas: Vec<Vec<u8>> = Vec::new();
    let mut scratch = Vec::new();
    for meta in buffer.iter_meta::<gst::Meta>() {
        if meta.api() == video_meta_api {
            continue; // carried in the header, rebuilt on the far side
        }
        scratch.clear();
        // Not all metas implement serialize (no serialize_func) — skip those silently.
        let len = match meta.serialize(&mut scratch) {
            Ok(len) => len,
            Err(_) => continue,
        };
        metas.push(scratch[..len].to_vec());
    }
    gst_plugin_iceoryx2_video::build_aux(&caps.to_string(), &metas, limit)
}

/// The parsed contents of an aux blob: the full caps (if any) and the raw serialised meta blobs.
#[derive(Debug, Default)]
pub struct ParsedAux {
    /// The publisher's full `GstCaps`, parsed from its string form (`None` if absent/unparsable).
    pub caps: Option<gst::Caps>,
    /// Each serialised meta, ready to hand to `gst::Meta::deserialize`.
    pub metas: Vec<Vec<u8>>,
}

/// Parse an aux blob produced by [`build_aux`] (delegates framing to the core, then parses the caps
/// string back into a `gst::Caps`). Best-effort: a corrupt tail yields whatever was recovered.
pub fn parse_aux(aux: &[u8]) -> ParsedAux {
    let parsed = gst_plugin_iceoryx2_video::parse_aux(aux);
    let caps = parsed.caps.and_then(|s| match s.parse::<gst::Caps>() {
        Ok(c) => Some(c),
        Err(e) => {
            gst::warning!(CAT, "aux caps unparsable ({e}): {s}");
            None
        }
    });
    ParsedAux {
        caps,
        metas: parsed.metas,
    }
}
