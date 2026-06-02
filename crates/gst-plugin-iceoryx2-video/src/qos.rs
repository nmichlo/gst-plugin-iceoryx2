//! Service naming + ring-buffer QoS. These values **must match across every participant** (the Rust
//! sink/source elements, this SDK, and the Python `gst_iceoryx2.video` SDK) for iceoryx2's
//! `open_or_create` to attach to the same service rather than failing on a QoS mismatch.

/// Default service name (the `/v2` slice + user-header video format). Bakes in no naming policy — an
/// application supplies the name it chose.
pub const DEFAULT_SERVICE: &str = "video/default/frame/v2";

/// `subscriber-max-buffer-size` — ring depth.
pub const VIDEO_BUFFER_SIZE: u32 = 10;
/// `subscriber-max-borrowed-samples` — samples a subscriber may hold at once.
pub const VIDEO_BORROWED_MAX: u32 = 10;
/// Publisher `history-size`.
pub const VIDEO_HISTORY_SIZE: u32 = 0;
/// Ring `safe-overflow` (overwrite unread samples rather than back-pressure).
pub const VIDEO_SAFE_OVERFLOW: bool = true;

/// The publish/subscribe QoS shared by both ends. [`Default`] is the `VIDEO_*` constants above.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Qos {
    /// `subscriber-max-buffer-size`.
    pub buffer_size: u32,
    /// `subscriber-max-borrowed-samples`.
    pub borrowed_max: u32,
    /// publisher `history-size`.
    pub history_size: u32,
    /// ring `safe-overflow`.
    pub safe_overflow: bool,
}

impl Default for Qos {
    fn default() -> Self {
        Self {
            buffer_size: VIDEO_BUFFER_SIZE,
            borrowed_max: VIDEO_BORROWED_MAX,
            history_size: VIDEO_HISTORY_SIZE,
            safe_overflow: VIDEO_SAFE_OVERFLOW,
        }
    }
}
