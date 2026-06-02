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

/// A GStreamer-free property value, as produced by [`SinkConfig::gst_properties`]. Keeping the SDK
/// free of any `gst`/`glib` type, this enum is the boundary: the `gst-plugin-iceoryx2` plugin crate
/// maps each variant onto the matching gobject `Value` when configuring an `iceoryx2sink` element.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum PropValue {
    /// A string property (e.g. `service`).
    Str(String),
    /// An unsigned-int property (`ParamSpecUInt`; e.g. `max-bytes`, `buffer-size`).
    Uint(u32),
    /// A boolean property (e.g. `safe-overflow`).
    Bool(bool),
}

/// Connection + QoS parameters for the `iceoryx2sink` element — the Rust mirror of the Python
/// `SinkConfig`. A plain value object that bakes in **no** naming policy: the caller supplies the
/// `service` name (and optionally `max_bytes` + QoS). [`gst_properties`](Self::gst_properties)
/// renders them as the element's hyphenated GObject property names.
///
/// `max_bytes == 0` lets the element derive the slice length from the negotiated caps.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SinkConfig {
    /// iceoryx2 service name (publish/subscribe + event).
    pub service: String,
    /// Max slice length; `0` derives it from the negotiated caps.
    pub max_bytes: u32,
    /// `subscriber-max-buffer-size` QoS.
    pub buffer_size: u32,
    /// `subscriber-max-borrowed-samples` QoS.
    pub borrowed_max: u32,
    /// publisher `history-size` QoS.
    pub history_size: u32,
    /// ring `safe-overflow` QoS.
    pub safe_overflow: bool,
}

impl SinkConfig {
    /// A config for `service` with `max_bytes = 0` (caps-derived) and the default [`Qos`].
    pub fn new(service: impl Into<String>) -> Self {
        let qos = Qos::default();
        Self {
            service: service.into(),
            max_bytes: 0,
            buffer_size: qos.buffer_size,
            borrowed_max: qos.borrowed_max,
            history_size: qos.history_size,
            safe_overflow: qos.safe_overflow,
        }
    }

    /// The `iceoryx2sink` property names → values (hyphenated GObject names), in a stable order.
    /// Mirrors Python `SinkConfig.gst_properties()`.
    pub fn gst_properties(&self) -> Vec<(&'static str, PropValue)> {
        vec![
            ("service", PropValue::Str(self.service.clone())),
            ("max-bytes", PropValue::Uint(self.max_bytes)),
            ("buffer-size", PropValue::Uint(self.buffer_size)),
            ("borrowed-max", PropValue::Uint(self.borrowed_max)),
            ("history-size", PropValue::Uint(self.history_size)),
            ("safe-overflow", PropValue::Bool(self.safe_overflow)),
        ]
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sink_config_defaults_and_properties() {
        let cfg = SinkConfig::new("video/cam0/frame/v2");
        assert_eq!(cfg.max_bytes, 0, "0 derives the slice length from caps");
        let props = cfg.gst_properties();
        assert_eq!(
            props[0],
            ("service", PropValue::Str("video/cam0/frame/v2".into()))
        );
        assert_eq!(props[1], ("max-bytes", PropValue::Uint(0)));
        assert_eq!(
            props[2],
            ("buffer-size", PropValue::Uint(VIDEO_BUFFER_SIZE))
        );
        assert_eq!(
            props[5],
            ("safe-overflow", PropValue::Bool(VIDEO_SAFE_OVERFLOW))
        );
    }
}
