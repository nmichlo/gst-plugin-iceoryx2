//! The `VideoFrameHeader` wire struct — the iceoryx2 per-sample user-header.
//!
//! Modelled on GStreamer's own `GstVideoMeta` + `GstBuffer` timing (the same fields `unixfd`
//! serialises), expressed as a fixed `#[repr(C)]` POD so iceoryx2 can match it cross-language and
//! carry it zero-copy. `SPEC.md` section 3 is canonical; this module + the Python
//! `gst_iceoryx2.video` SDK mirror (`VideoFrameHeader`) must both agree with it. The
//! `gst-plugin-iceoryx2` plugin crate re-uses this struct verbatim — there is one Rust definition.
//!
//! Layout (size 104, align 8):
//! ```text
//!  0  pts:u64   8  dts:u64  16  duration:u64  24  offset:u64  32  flags:u64
//! 40  width:u32 44 height:u32 48 n_planes:u32 52 aux_size:u32
//! 56  stride:[u32;4]  72  plane_offsets:[u32;4]  88  format:[u8;16]
//! ```

use iceoryx2::prelude::*;

/// `GST_VIDEO_MAX_PLANES`.
pub const MAX_PLANES: usize = 4;

/// Private sentinel bit set in [`VideoFrameHeader::flags`] to mark an **end-of-stream** sample
/// (carries no pixels). `GstBufferFlags` occupy only the low 32 bits, so bit 32 is free for our use
/// and never collides with a real buffer flag. The source returns `FlowError::Eos` on seeing it.
pub const HEADER_FLAG_EOS: u64 = 1 << 32;
/// Default size of the **aux blob** tail reserved after the pixels in a zero-copy sample (bytes).
/// Holds the serialised full `GstCaps` string (colorimetry/framerate/PAR/interlace) plus any
/// serialisable `GstMeta`s — the fidelity the fixed header cannot carry. See [`crate::aux`].
pub const DEFAULT_AUX_BYTES: u32 = 4096;
/// Length of the null-padded GStreamer format string field.
pub const FORMAT_LEN: usize = 16;
/// The iceoryx2 user-header type name. Python derives the same name from its ctypes class.
pub const HEADER_TYPE_NAME: &str = "VideoFrameHeader";

/// Per-sample metadata published alongside the raw pixel slice. See module docs / `SPEC.md`.
#[derive(Clone, Copy, Debug, Default, ZeroCopySend)]
#[type_name("VideoFrameHeader")]
#[repr(C)]
pub struct VideoFrameHeader {
    /// `GstBuffer.pts` — presentation timestamp (ns); `u64::MAX` if none.
    pub pts: u64,
    /// `GstBuffer.dts` — decode timestamp (ns); `u64::MAX` if none.
    pub dts: u64,
    /// `GstBuffer.duration` — frame duration (ns); `u64::MAX` if none.
    pub duration: u64,
    /// `GstBuffer.offset` — frame counter / media offset; `u64::MAX` if none.
    pub offset: u64,
    /// `GstBufferFlags` (GAP, CORRUPTED, DELTA_UNIT, …).
    pub flags: u64,
    /// Pixel width.
    pub width: u32,
    /// Pixel height.
    pub height: u32,
    /// Number of planes (1 for packed BGR/RGB; >1 for I420/NV12/…).
    pub n_planes: u32,
    /// Length in bytes of the **aux blob** appended after the pixels in the same payload slice
    /// (`0` if none). The receiver recovers the pixel region as `payload[..payload.len() -
    /// aux_size]` and the aux blob as `payload[payload.len() - aux_size..]`. Occupies the 4 bytes
    /// that previously padded the arrays to 8-byte alignment, so the wire layout is unchanged.
    pub aux_size: u32,
    /// `GstVideoInfo.stride` — per-plane bytes per row (planes `>= n_planes` are zero).
    pub stride: [u32; MAX_PLANES],
    /// `GstVideoInfo.offset` — per-plane byte offset within the payload.
    pub plane_offsets: [u32; MAX_PLANES],
    /// Null-padded GStreamer format string, e.g. `b"BGR\0…"` (`gst_video_format_to_string`).
    pub format: [u8; FORMAT_LEN],
}

impl VideoFrameHeader {
    /// Write a GStreamer format name (e.g. `"BGR"`) into the fixed `format` field, null-padded
    /// and always null-terminated (truncated to `FORMAT_LEN - 1` bytes if longer).
    pub fn set_format(&mut self, name: &str) {
        self.format = [0u8; FORMAT_LEN];
        let bytes = name.as_bytes();
        let n = bytes.len().min(FORMAT_LEN - 1);
        self.format[..n].copy_from_slice(&bytes[..n]);
    }

    /// Read the format name back as a `&str` (up to the first NUL).
    pub fn format_name(&self) -> &str {
        let end = self
            .format
            .iter()
            .position(|&b| b == 0)
            .unwrap_or(FORMAT_LEN);
        core::str::from_utf8(&self.format[..end]).unwrap_or("")
    }
}

/// `size_of::<VideoFrameHeader>()` — pinned at 104.
pub const HEADER_SIZE: usize = core::mem::size_of::<VideoFrameHeader>();
/// `align_of::<VideoFrameHeader>()` — pinned at 8.
pub const HEADER_ALIGN: usize = core::mem::align_of::<VideoFrameHeader>();

/// `(field_name, byte_offset)` for every field — the canonical layout contract. The plugin's
/// build emits this (plus [`HEADER_SIZE`]/[`HEADER_ALIGN`]/[`HEADER_TYPE_NAME`]) into a committed
/// golden file that both a Rust test and the Python `test_header_equivalence` assert against, so the
/// `gst_iceoryx2.video` ctypes mirror can never silently drift from this struct.
pub fn field_offsets() -> [(&'static str, usize); 12] {
    use core::mem::offset_of;
    [
        ("pts", offset_of!(VideoFrameHeader, pts)),
        ("dts", offset_of!(VideoFrameHeader, dts)),
        ("duration", offset_of!(VideoFrameHeader, duration)),
        ("offset", offset_of!(VideoFrameHeader, offset)),
        ("flags", offset_of!(VideoFrameHeader, flags)),
        ("width", offset_of!(VideoFrameHeader, width)),
        ("height", offset_of!(VideoFrameHeader, height)),
        ("n_planes", offset_of!(VideoFrameHeader, n_planes)),
        ("aux_size", offset_of!(VideoFrameHeader, aux_size)),
        ("stride", offset_of!(VideoFrameHeader, stride)),
        ("plane_offsets", offset_of!(VideoFrameHeader, plane_offsets)),
        ("format", offset_of!(VideoFrameHeader, format)),
    ]
}

#[cfg(test)]
#[allow(clippy::field_reassign_with_default)] // default + set a couple of fields reads clearer here
mod tests {
    use super::*;

    #[test]
    fn header_size_and_align() {
        assert_eq!(HEADER_SIZE, 104, "wire size is pinned by SPEC.md");
        assert_eq!(HEADER_ALIGN, 8);
    }

    #[test]
    fn header_field_offsets() {
        let expected = [
            ("pts", 0usize),
            ("dts", 8),
            ("duration", 16),
            ("offset", 24),
            ("flags", 32),
            ("width", 40),
            ("height", 44),
            ("n_planes", 48),
            ("aux_size", 52),
            ("stride", 56),
            ("plane_offsets", 72),
            ("format", 88),
        ];
        assert_eq!(field_offsets(), expected);
    }

    #[test]
    fn header_byte_layout() {
        let mut h = VideoFrameHeader::default();
        h.pts = 0x0102_0304_0506_0708;
        h.width = 0x1122_3344;
        h.set_format("BGR");
        let bytes: &[u8] = unsafe {
            core::slice::from_raw_parts((&h as *const VideoFrameHeader).cast::<u8>(), HEADER_SIZE)
        };
        // little-endian u64 at offset 0
        assert_eq!(
            &bytes[0..8],
            &[0x08, 0x07, 0x06, 0x05, 0x04, 0x03, 0x02, 0x01]
        );
        // little-endian u32 width at offset 40
        assert_eq!(&bytes[40..44], &[0x44, 0x33, 0x22, 0x11]);
        // format string at offset 88, null-padded
        assert_eq!(&bytes[88..92], b"BGR\0");
    }

    #[test]
    fn format_string_roundtrip() {
        for s in ["BGR", "RGB", "I420", "NV12", "RGBA"] {
            let mut h = VideoFrameHeader::default();
            h.set_format(s);
            assert_eq!(h.format_name(), s);
        }
        // overly long names are truncated but stay null-terminated
        let mut h = VideoFrameHeader::default();
        h.set_format("AAAAAAAAAAAAAAAAAAAA");
        assert_eq!(h.format_name().len(), FORMAT_LEN - 1);
        assert_eq!(h.format[FORMAT_LEN - 1], 0);
    }

    #[test]
    fn iceoryx2_type_name_matches() {
        assert_eq!(HEADER_TYPE_NAME, "VideoFrameHeader");
    }
}
