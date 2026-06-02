//! Optional `ndarray` reshape helpers — the Rust counterpart to the Python SDK's
//! `header_pixels_to_numpy` / `VideoFrame.to_numpy`. Gated behind the `ndarray` cargo feature so the
//! default build keeps its dependency set to `iceoryx2` only (the layering rule: the core crate
//! stays free of heavy/optional deps unless a consumer opts in).

use ndarray::Array3;

use crate::header::VideoFrameHeader;
use crate::validate::format_channels;

/// Reshape a packed pixel buffer into a contiguous `(H, W, C)` `u8` array (a copy), honouring
/// `stride[0]` row padding: the buffer is `stride[0] * height` bytes and each row's leading
/// `width * channels` bytes are the pixels. Mirrors the Python `header_pixels_to_numpy`.
///
/// `Err` for a non-[packed](crate::PACKED_FORMATS) format, or when the payload is too small for the
/// declared `stride[0] * height` extent.
pub fn header_pixels_to_ndarray(
    header: &VideoFrameHeader,
    pixels: &[u8],
) -> core::result::Result<Array3<u8>, String> {
    let format = header.format_name();
    let channels = format_channels(format)
        .ok_or_else(|| format!("non-packed/unsupported format {format:?}"))?;
    let width = header.width as usize;
    let height = header.height as usize;
    let row_bytes = width * channels;
    let stride0 = if header.stride[0] != 0 {
        header.stride[0] as usize
    } else {
        row_bytes
    };
    if stride0 < row_bytes {
        return Err(format!("stride[0] {stride0} < row bytes {row_bytes}"));
    }
    let needed = stride0
        .checked_mul(height)
        .ok_or_else(|| "extent overflow".to_string())?;
    if needed > pixels.len() {
        return Err(format!(
            "pixels {} too small for {width}x{height} stride {stride0}",
            pixels.len()
        ));
    }
    // Drop any per-row stride padding into a tight (row_bytes * height) buffer, then shape it.
    let mut buf = Vec::with_capacity(row_bytes * height);
    for y in 0..height {
        let start = y * stride0;
        buf.extend_from_slice(&pixels[start..start + row_bytes]);
    }
    Array3::from_shape_vec((height, width, channels), buf).map_err(|e| e.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::header::FORMAT_LEN;

    fn header(format: &str, width: u32, height: u32, stride0: u32) -> VideoFrameHeader {
        let mut h = VideoFrameHeader {
            width,
            height,
            n_planes: 1,
            stride: [stride0, 0, 0, 0],
            format: [0; FORMAT_LEN],
            ..Default::default()
        };
        h.set_format(format);
        h
    }

    #[test]
    fn packed_no_padding() {
        let h = header("BGR", 4, 2, 12);
        let pixels: Vec<u8> = (0..24u8).collect();
        let arr = header_pixels_to_ndarray(&h, &pixels).unwrap();
        assert_eq!(arr.shape(), &[2, 4, 3]);
        assert_eq!(arr.as_slice().unwrap(), pixels.as_slice());
    }

    #[test]
    fn honours_row_stride() {
        let h = header("BGR", 2, 2, 8); // 2*3 = 6 pixel bytes + 2 padding per row
        let pixels = vec![1, 2, 3, 4, 5, 6, 0, 0, 7, 8, 9, 10, 11, 12, 0, 0];
        let arr = header_pixels_to_ndarray(&h, &pixels).unwrap();
        assert_eq!(arr.shape(), &[2, 2, 3]);
        assert_eq!(arr.as_slice().unwrap(), &[1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]);
    }

    #[test]
    fn stride0_zero_derives_packed() {
        let h = header("RGB", 2, 1, 0);
        let pixels = vec![10, 20, 30, 40, 50, 60];
        let arr = header_pixels_to_ndarray(&h, &pixels).unwrap();
        assert_eq!(arr.shape(), &[1, 2, 3]);
        assert_eq!(arr.as_slice().unwrap(), pixels.as_slice());
    }

    #[test]
    fn rejects_non_packed_and_short_payload() {
        assert!(header_pixels_to_ndarray(&header("I420", 4, 2, 4), &[0; 64]).is_err());
        assert!(header_pixels_to_ndarray(&header("BGR", 4, 2, 12), &[0; 23]).is_err());
    }
}
