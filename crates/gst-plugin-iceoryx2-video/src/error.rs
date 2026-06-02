//! A small error type so the SDK does not leak iceoryx2's many error enums into its public API. Each
//! error carries the operation that failed plus the underlying message; the plugin maps it onto a
//! GStreamer `ErrorMessage` by `Display`.

use std::fmt;

/// An SDK transport error: the failed operation (`context`) plus the underlying cause (`message`).
#[derive(Debug, Clone)]
pub struct Error {
    context: &'static str,
    message: String,
}

impl Error {
    /// Wrap any displayable cause with the operation that produced it.
    pub fn new(context: &'static str, cause: impl fmt::Display) -> Self {
        Self {
            context,
            message: cause.to_string(),
        }
    }
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}: {}", self.context, self.message)
    }
}

impl std::error::Error for Error {}

/// Result alias for the SDK.
pub type Result<T> = std::result::Result<T, Error>;
