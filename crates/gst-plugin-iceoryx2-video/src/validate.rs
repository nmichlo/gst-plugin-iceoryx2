//! Format metadata + untrusted-geometry validation, expressed over the format **name** so it stays
//! GStreamer-free. Both the `iceoryx2src` element and an SDK [`VideoFrameSubscriber`] use these, so a
//! received header is bounds-checked identically whether it is consumed through a pipeline or the SDK.

use crate::header::{VideoFrameHeader, MAX_PLANES};

/// The raw video formats both ends accept/produce, in negotiation-preference order. The plugin maps
/// each name to a `gst_video::VideoFormat` when building caps; this is the single source of truth for
/// the supported set so the two cannot diverge.
pub const SUPPORTED_FORMATS: [&str; 4] = ["BGR", "RGB", "I420", "NV12"];

/// The **packed** formats that reshape to a contiguous `(H, W, C)` array, as `(name, channels)`
/// pairs. This is the *reshape* set used by [`format_channels`] and the optional `ndarray`/numpy
/// helpers — deliberately distinct from [`SUPPORTED_FORMATS`] (the *negotiation/validation* set):
/// it includes 4-channel `BGRA`/`RGBA` (trivially reshapeable) but not the planar `I420`/`NV12`
/// (which [`validate_geometry`] handles but a single `(H, W, C)` array cannot represent). The Python
/// `gst_iceoryx2.video.PACKED_FORMATS` mirror must hold the same set.
pub const PACKED_FORMATS: [(&str, usize); 4] =
    [("BGR", 3), ("RGB", 3), ("BGRA", 4), ("RGBA", 4)];

/// Channels per pixel for a [packed format](PACKED_FORMATS) (pixels are always `u8`), or `None` for a
/// non-packed/unrecognised format. Mirrors the Python `format_channels`.
pub fn format_channels(format: &str) -> Option<usize> {
    PACKED_FORMATS
        .iter()
        .find(|(name, _)| *name == format)
        .map(|(_, channels)| *channels)
}

/// Pixel rows per plane for a supported format at `height` — mirrors the subsampling of the formats
/// [`SUPPORTED_FORMATS`] advertises. `None` for a format we don't recognise (so [`validate_geometry`]
/// rejects it rather than guessing an extent).
pub fn plane_heights(format: &str, height: u32) -> Option<Vec<u32>> {
    let chroma = height.div_ceil(2); // 4:2:0 chroma height
    Some(match format {
        "BGR" | "RGB" => vec![height],
        "I420" => vec![height, chroma, chroma],
        "NV12" => vec![height, chroma],
        _ => return None,
    })
}

/// Reject a header whose declared per-plane layout would read past the `pixel_size`-byte payload (or
/// whose plane count is impossible). The header is untrusted wire data — any process on the same
/// service can publish it — so this is the bound that keeps a malformed/hostile publisher from making
/// the receiver read out of the buffer. Returns `Ok(())` for a sound frame, `Err(reason)` to drop it.
pub fn validate_geometry(header: &VideoFrameHeader, pixel_size: usize) -> Result<(), String> {
    let n = header.n_planes as usize;
    if n == 0 || n > MAX_PLANES {
        return Err(format!("n_planes {n} out of range 1..={MAX_PLANES}"));
    }
    let format = header.format_name();
    let heights = plane_heights(format, header.height)
        .ok_or_else(|| format!("unsupported format {format:?}"))?;
    if heights.len() != n {
        return Err(format!(
            "n_planes {n} != {} expected for {format:?}",
            heights.len()
        ));
    }
    for (i, &rows) in heights.iter().enumerate() {
        let off = header.plane_offsets[i] as usize;
        let stride = header.stride[i] as usize;
        let extent = stride
            .checked_mul(rows as usize)
            .and_then(|span| off.checked_add(span))
            .ok_or_else(|| format!("plane {i} extent overflow"))?;
        if extent > pixel_size {
            return Err(format!(
                "plane {i} extent {extent} exceeds payload {pixel_size}"
            ));
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn header(
        format: &str,
        height: u32,
        n_planes: u32,
        stride: [u32; MAX_PLANES],
        offsets: [u32; MAX_PLANES],
    ) -> VideoFrameHeader {
        let mut h = VideoFrameHeader {
            height,
            n_planes,
            stride,
            plane_offsets: offsets,
            ..Default::default()
        };
        h.set_format(format);
        h
    }

    #[test]
    fn packed_bgr_within_payload_ok() {
        // 4x2 BGR: stride 12, 2 rows → 24 bytes.
        let h = header("BGR", 2, 1, [12, 0, 0, 0], [0, 0, 0, 0]);
        assert!(validate_geometry(&h, 24).is_ok());
        assert!(
            validate_geometry(&h, 23).is_err(),
            "one byte short must fail"
        );
    }

    #[test]
    fn i420_three_planes_ok() {
        // 4x2 I420: Y 4x2=8 @0, U 2x1=2 @8, V 2x1=2 @10 → 12 bytes.
        let h = header("I420", 2, 3, [4, 2, 2, 0], [0, 8, 10, 0]);
        assert!(validate_geometry(&h, 12).is_ok());
        assert!(validate_geometry(&h, 11).is_err());
    }

    #[test]
    fn rejects_wrong_plane_count_for_format() {
        let h = header("BGR", 2, 3, [12, 12, 12, 0], [0, 0, 0, 0]);
        assert!(validate_geometry(&h, 100_000).is_err());
    }

    #[test]
    fn rejects_oversized_stride() {
        let h = header("BGR", 2, 1, [u32::MAX, 0, 0, 0], [0, 0, 0, 0]);
        assert!(validate_geometry(&h, 24).is_err());
    }

    #[test]
    fn rejects_unadvertised_format() {
        // RGBA is not in SUPPORTED_FORMATS, so geometry can't be validated → reject.
        let h = header("RGBA", 2, 1, [16, 0, 0, 0], [0, 0, 0, 0]);
        assert!(validate_geometry(&h, 100_000).is_err());
    }

    #[test]
    fn rejects_zero_planes() {
        let h = header("BGR", 2, 0, [0, 0, 0, 0], [0, 0, 0, 0]);
        assert!(validate_geometry(&h, 100_000).is_err());
    }

    #[test]
    fn format_channels_packed_only() {
        assert_eq!(format_channels("BGR"), Some(3));
        assert_eq!(format_channels("RGBA"), Some(4));
        // planar formats are validated but not single-array reshapeable, so not "packed"
        assert_eq!(format_channels("I420"), None);
        assert_eq!(format_channels("nonsense"), None);
    }
}
