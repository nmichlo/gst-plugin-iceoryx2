//! The **aux blob** — the variable-length tail appended after the pixels in a sample payload, used
//! to carry the fidelity the fixed [`VideoFrameHeader`](crate::VideoFrameHeader) cannot: the full
//! serialised `GstCaps` *string* (colorimetry, framerate, pixel-aspect-ratio, interlace) and any
//! serialisable `GstMeta`s.
//!
//! This module owns only the **byte framing** — it is GStreamer-free, taking the caps as a `&str`
//! and the metas as already-serialised `Vec<u8>` blobs. The `gst-plugin-iceoryx2` plugin crate owns
//! the `GstCaps`/`GstMeta` ↔ bytes conversion and calls these to (de)serialise the wire layout, so a
//! pure-Rust SDK consumer parses the exact same blobs the plugin produces.
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

/// Build the aux blob from a caps string + already-serialised meta blobs.
///
/// The caps string is always written first (it is small and the high-value parity item). Then each
/// meta blob is appended in order, **while it still fits** under `limit` (the reserved tail size for
/// the zero-copy path); the remainder are dropped. `None` means unbounded (the copy path, which
/// loans to fit). A frame with oversized metadata silently keeps as many metas as the reserve allows
/// rather than failing.
pub fn build_aux(caps: &str, metas: &[Vec<u8>], limit: Option<usize>) -> Vec<u8> {
    let mut out = Vec::new();

    let caps_bytes = caps.as_bytes();
    out.extend_from_slice(&(caps_bytes.len() as u32).to_le_bytes());
    out.extend_from_slice(caps_bytes);

    // Reserve the n_metas slot; patch it once we know how many we actually wrote.
    let n_metas_pos = out.len();
    out.extend_from_slice(&0u32.to_le_bytes());

    let mut n_metas: u32 = 0;
    for meta in metas {
        let entry = 4 + meta.len(); // u32 length prefix + payload
        if let Some(limit) = limit
            && out.len() + entry > limit
        {
            break; // hit the reserve; drop this and any further metas
        }
        out.extend_from_slice(&(meta.len() as u32).to_le_bytes());
        out.extend_from_slice(meta);
        n_metas += 1;
    }
    out[n_metas_pos..n_metas_pos + 4].copy_from_slice(&n_metas.to_le_bytes());
    out
}

/// The parsed contents of an aux blob: the caps string (if any) and the raw serialised meta blobs.
#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct ParsedAux {
    /// The publisher's full `GstCaps` in string form (`None` if absent or the length was truncated).
    /// The plugin parses this back into a `gst::Caps`; an SDK consumer can use it as-is.
    pub caps: Option<String>,
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
        if let Ok(s) = core::str::from_utf8(&aux[pos..pos + caps_len]) {
            out.caps = Some(s.to_string());
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

    #[test]
    fn roundtrip_caps_no_metas() {
        let caps = "video/x-raw, format=(string)BGR, width=(int)640, height=(int)480";
        let blob = build_aux(caps, &[], None);
        let parsed = parse_aux(&blob);
        assert_eq!(parsed.caps.as_deref(), Some(caps));
        assert!(parsed.metas.is_empty());
    }

    #[test]
    fn roundtrip_caps_with_metas() {
        let metas = vec![vec![1u8, 2, 3], vec![9u8; 40]];
        let blob = build_aux("video/x-raw", &metas, None);
        let parsed = parse_aux(&blob);
        assert_eq!(parsed.caps.as_deref(), Some("video/x-raw"));
        assert_eq!(parsed.metas, metas);
    }

    #[test]
    fn limit_drops_overflowing_metas() {
        // caps(4+11) + n_metas(4) = 19; one 4+10=14-byte meta fits under 34, the second does not.
        let metas = vec![vec![7u8; 10], vec![8u8; 10]];
        let blob = build_aux("video/x-raw", &metas, Some(34));
        let parsed = parse_aux(&blob);
        assert_eq!(
            parsed.metas.len(),
            1,
            "only the first meta fits the reserve"
        );
        assert_eq!(parsed.metas[0], vec![7u8; 10]);
    }

    #[test]
    fn ignores_trailing_padding() {
        let mut blob = build_aux("video/x-raw, format=(string)RGB", &[], None);
        blob.extend_from_slice(&[0u8; 64]); // simulate the zero-copy reserve's slack
        let parsed = parse_aux(&blob);
        assert_eq!(
            parsed.caps.as_deref(),
            Some("video/x-raw, format=(string)RGB")
        );
        assert!(parsed.metas.is_empty());
    }

    #[test]
    fn truncated_input_is_safe() {
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
