//! Optional `ndarray` reshape helpers — the Rust counterpart to the Python SDK's
//! `header_pixels_to_numpy_view` / `VideoFrame.numpy_view`. Gated behind the `ndarray` cargo feature so
//! the default build keeps its dependency set to `iceoryx2` only (the layering rule: the core crate
//! stays free of heavy/optional deps unless a consumer opts in).
//!
//! These are **zero-copy**: the returned [`ArrayView3`] borrows the pixel slice (which itself borrows
//! the loaned iceoryx2 sample). Row-padding (`stride[0] > width * channels`) is expressed as a
//! non-contiguous stride, never a copy — call `.to_owned()` on the view for an owned, contiguous array.

use ndarray::{ArrayView3, ShapeBuilder};

use crate::header::VideoFrameHeader;
use crate::validate::format_channels;

/// Borrow a packed pixel buffer as a zero-copy `(H, W, C)` `u8` view, honouring `stride[0]` row
/// padding via a non-contiguous element stride (`(stride0, channels, 1)`). The view borrows `pixels`;
/// no allocation, no copy. Mirrors the Python `header_pixels_to_numpy_view`.
///
/// `Err` for a non-[packed](crate::PACKED_FORMATS) format, or when the payload is too small for the
/// declared `stride[0] * height` extent.
pub fn header_pixels_to_ndarray_view<'a>(
    header: &VideoFrameHeader,
    pixels: &'a [u8],
) -> core::result::Result<ArrayView3<'a, u8>, String> {
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
    // Element strides (u8 → 1 byte): rows step by stride0, columns by channels, channels by 1. A
    // strided, possibly non-contiguous *view* — never a copy.
    ArrayView3::from_shape(
        (height, width, channels).strides((stride0, channels, 1)),
        &pixels[..needed],
    )
    .map_err(|e| e.to_string())
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
    fn packed_no_padding_is_zero_copy() {
        let h = header("BGR", 4, 2, 12);
        let pixels: Vec<u8> = (0..24u8).collect();
        let arr = header_pixels_to_ndarray_view(&h, &pixels).unwrap();
        assert_eq!(arr.shape(), &[2, 4, 3]);
        // zero-copy: the view points straight at the input buffer, no allocation.
        assert_eq!(arr.as_ptr(), pixels.as_ptr());
        // no padding → standard C layout → contiguous slice equals the input.
        assert_eq!(arr.as_slice().unwrap(), pixels.as_slice());
    }

    #[test]
    fn honours_row_stride_zero_copy() {
        let h = header("BGR", 2, 2, 8); // 2*3 = 6 pixel bytes + 2 padding per row
        let pixels = vec![1, 2, 3, 4, 5, 6, 0, 0, 7, 8, 9, 10, 11, 12, 0, 0];
        let arr = header_pixels_to_ndarray_view(&h, &pixels).unwrap();
        assert_eq!(arr.shape(), &[2, 2, 3]);
        assert_eq!(arr.as_ptr(), pixels.as_ptr(), "still a view, not a copy");
        // padded → non-contiguous view; iteration trims the padding bytes.
        assert!(arr.as_slice().is_none(), "padded view is non-contiguous");
        assert_eq!(
            arr.iter().copied().collect::<Vec<u8>>(),
            vec![1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]
        );
    }

    #[test]
    fn stride0_zero_derives_packed() {
        let h = header("RGB", 2, 1, 0);
        let pixels = vec![10, 20, 30, 40, 50, 60];
        let arr = header_pixels_to_ndarray_view(&h, &pixels).unwrap();
        assert_eq!(arr.shape(), &[1, 2, 3]);
        assert_eq!(arr.as_slice().unwrap(), pixels.as_slice());
    }

    #[test]
    fn rejects_non_packed_and_short_payload() {
        assert!(header_pixels_to_ndarray_view(&header("I420", 4, 2, 4), &[0; 64]).is_err());
        assert!(header_pixels_to_ndarray_view(&header("BGR", 4, 2, 12), &[0; 23]).is_err());
    }
}
