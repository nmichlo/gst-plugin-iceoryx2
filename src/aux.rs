//! The **aux blob** — the variable-length tail appended after the pixels in a sample payload, used
//! to carry the fidelity the fixed [`VideoFrameHeader`](crate::format::VideoFrameHeader) cannot:
//! the full serialised `GstCaps` string (colorimetry, framerate, pixel-aspect-ratio, interlace)
//! and any serialisable `GstMeta`s. This closes the last two parity gaps with `unixfdsink`, which
//! puts the same caps + metas on its control socket.
//!
//! ## Wire layout (little-endian, self-describing)
//! ```text
//!   u32 caps_len            // bytes of the caps string (0 = no caps)
//!   u8[caps_len] caps_str   // gst_caps_to_string output, UTF-8, no NUL
//!   u32 n_metas             // number of serialised metas that follow
//!   repeated n_metas times:
//!       u32 meta_len        // bytes of this meta's gst_meta_serialize output
//!       u8[meta_len] meta   // the serialised meta
//!   // any trailing bytes (zero padding up to the reserved tail) are ignored
//! ```
//! `n_metas` is explicit so the parser stops exactly after the last meta and never reads the
//! zero-padding a fixed-size zero-copy reserve leaves behind it.
//!
//! ## Why a count-prefixed format rather than a terminator
//! The zero-copy sink reserves a *fixed* tail (`aux-bytes`) at pool-allocation time, before it knows
//! how large a given frame's caps + metas will be; it writes the blob into the head of that tail and
//! leaves the rest untouched. A length/count-prefixed format lets the receiver read precisely the
//! real content and ignore the slack, so no terminator sentinel (which the padding could collide
//! with) is needed.

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
/// The full caps string is always written first (it is small and the high-value parity item). Then
/// every *serialisable* `GstMeta` on the buffer is appended, **except** `GstVideoMeta` — its
/// information already travels in the fixed header and the receiver rebuilds it from there, so
/// serialising it too would duplicate it on the far side.
///
/// `limit` (the reserved tail size, for the zero-copy path) bounds the total: caps is written even
/// if it alone exceeds `limit` (the caller then falls back to a copy), but metas are only appended
/// while they still fit, so a frame with oversized metadata silently keeps as many as the reserve
/// allows rather than failing. `None` means unbounded (the copy path, which loans to fit).
pub fn build_aux(caps: &gst::Caps, buffer: &gst::BufferRef, limit: Option<usize>) -> Vec<u8> {
    let mut out = Vec::new();

    let caps_str = caps.to_string();
    let caps_bytes = caps_str.as_bytes();
    out.extend_from_slice(&(caps_bytes.len() as u32).to_le_bytes());
    out.extend_from_slice(caps_bytes);

    // Reserve the n_metas slot; patch it once we know how many we actually wrote.
    let n_metas_pos = out.len();
    out.extend_from_slice(&0u32.to_le_bytes());

    let video_meta_api = gst_video::VideoMeta::meta_api();
    let mut n_metas: u32 = 0;
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
        let entry = 4 + len; // u32 length prefix + payload
        if let Some(limit) = limit {
            if out.len() + entry > limit {
                gst::trace!(CAT, "aux blob hit {limit}B reserve; dropping further metas");
                break;
            }
        }
        out.extend_from_slice(&(len as u32).to_le_bytes());
        out.extend_from_slice(&scratch[..len]);
        n_metas += 1;
    }
    out[n_metas_pos..n_metas_pos + 4].copy_from_slice(&n_metas.to_le_bytes());
    out
}

/// The parsed contents of an aux blob: the full caps (if any) and the raw serialised meta blobs.
#[derive(Debug, Default)]
pub struct ParsedAux {
    /// The publisher's full `GstCaps`, parsed from its string form (`None` if absent/unparsable).
    pub caps: Option<gst::Caps>,
    /// Each serialised meta, ready to hand to `gst::Meta::deserialize`.
    pub metas: Vec<Vec<u8>>,
}

/// Parse an aux blob produced by [`build_aux`]. Malformed or truncated input yields whatever was
/// recovered up to the fault (best-effort), never a panic — a corrupt tail must not kill the stream.
pub fn parse_aux(aux: &[u8]) -> ParsedAux {
    let mut out = ParsedAux::default();
    let mut pos = 0usize;

    let caps_len = match read_u32(aux, &mut pos) {
        Some(n) => n as usize,
        None => return out,
    };
    if pos + caps_len > aux.len() {
        return out;
    }
    if caps_len > 0 {
        if let Ok(s) = std::str::from_utf8(&aux[pos..pos + caps_len]) {
            match s.parse::<gst::Caps>() {
                Ok(c) => out.caps = Some(c),
                Err(e) => gst::warning!(CAT, "aux caps unparsable ({e}): {s}"),
            }
        }
        pos += caps_len;
    }

    let n_metas = match read_u32(aux, &mut pos) {
        Some(n) => n,
        None => return out,
    };
    for _ in 0..n_metas {
        let len = match read_u32(aux, &mut pos) {
            Some(n) => n as usize,
            None => break,
        };
        if pos + len > aux.len() {
            break;
        }
        out.metas.push(aux[pos..pos + len].to_vec());
        pos += len;
    }
    out
}

/// Read a little-endian `u32` at `*pos`, advancing it; `None` if fewer than 4 bytes remain.
fn read_u32(buf: &[u8], pos: &mut usize) -> Option<u32> {
    let end = pos.checked_add(4)?;
    if end > buf.len() {
        return None;
    }
    let v = u32::from_le_bytes([buf[*pos], buf[*pos + 1], buf[*pos + 2], buf[*pos + 3]]);
    *pos = end;
    Some(v)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn init() {
        gst::init().unwrap();
    }

    #[test]
    fn roundtrip_caps_no_metas() {
        init();
        let caps = gst::Caps::builder("video/x-raw")
            .field("format", "BGR")
            .field("width", 640i32)
            .field("height", 480i32)
            .field("framerate", gst::Fraction::new(30, 1))
            .build();
        let buffer = gst::Buffer::new();
        let blob = build_aux(&caps, &buffer, None);
        let parsed = parse_aux(&blob);
        assert_eq!(parsed.caps.as_ref(), Some(&caps));
        assert!(parsed.metas.is_empty());
    }

    #[test]
    fn ignores_trailing_padding() {
        init();
        let caps = gst::Caps::builder("video/x-raw")
            .field("format", "RGB")
            .build();
        let buffer = gst::Buffer::new();
        let mut blob = build_aux(&caps, &buffer, None);
        blob.extend_from_slice(&[0u8; 64]); // simulate the zero-copy reserve's slack
        let parsed = parse_aux(&blob);
        assert_eq!(parsed.caps.as_ref(), Some(&caps));
        assert!(parsed.metas.is_empty());
    }

    #[test]
    fn truncated_input_is_safe() {
        init();
        let parsed = parse_aux(&[5, 0, 0, 0, b'a', b'b']); // claims 5-byte caps, only 2 present
        assert!(parsed.caps.is_none());
        assert!(parsed.metas.is_empty());
    }

    #[test]
    fn empty_input_is_safe() {
        let parsed = parse_aux(&[]);
        assert!(parsed.caps.is_none());
        assert!(parsed.metas.is_empty());
    }
}
