//! The cross-language **API surface** contract, emitted as a committed golden file.
//!
//! Companion to `header_layout_golden.rs` (which pins the *wire layout*): this pins the *public API
//! surface* shared by the Rust core crate and the Python `gst_iceoryx2.video` SDK, so the two cannot
//! drift in name. It does two things:
//!
//! 1. **Compile-time existence** ([`surface_symbols_exist`]) — references every shared symbol (and
//!    the documented Rust-only extras). Renaming/removing one on the Rust side fails to compile here.
//! 2. **Golden manifest** ([`api_manifest_matches_golden`]) — serialises the canonical neutral
//!    surface to `python/gst_iceoryx2_tests/api_manifest.json`; the Python `test_api_parity` reads
//!    the same file and asserts its `__all__` + class members match. Renaming on the Python side
//!    fails *there*.
//!
//! Editing [`manifest_json`] is the deliberate act of changing the cross-language contract: regenerate
//! with `UPDATE_GOLDEN=1 cargo test -p gst-plugin-iceoryx2-video` and update the Python mirror to match.

use gst_plugin_iceoryx2_video as v;
use std::path::PathBuf;

/// The committed golden, relative to this crate's manifest dir.
fn golden_path() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../python/gst_iceoryx2_tests/api_manifest.json")
}

/// An inline JSON array of quoted strings: `["a", "b"]`.
fn arr(items: &[&str]) -> String {
    let inner = items
        .iter()
        .map(|s| format!("\"{s}\""))
        .collect::<Vec<_>>()
        .join(", ");
    format!("[{inner}]")
}

/// The canonical neutral API surface, as deterministic hand-rolled JSON (no serde, struct-stable
/// order). `constants`/`functions`/`types` are the surface present on **both** SDKs under the same
/// name; `language_specific` records the module-level items intentionally unique to one side (the
/// numpy vs ndarray reshape pair, Rust plumbing). Per-type member lists hold the shared, externally
/// checkable members (methods/properties/fields) — instance-only attrs and one-sided extras live in
/// `PARITY.md`, not here.
fn manifest_json() -> String {
    let constants = [
        "DEFAULT_AUX_BYTES",
        "DEFAULT_SERVICE",
        "FORMAT_LEN",
        "HEADER_ALIGN",
        "HEADER_FLAG_EOS",
        "HEADER_SIZE",
        "HEADER_TYPE_NAME",
        "MAX_PLANES",
        "PACKED_FORMATS",
        "SUPPORTED_FORMATS",
        "VIDEO_BORROWED_MAX",
        "VIDEO_BUFFER_SIZE",
        "VIDEO_HISTORY_SIZE",
        "VIDEO_SAFE_OVERFLOW",
    ];
    let functions = [
        "build_aux",
        "create_listener",
        "create_node",
        "create_notifier",
        "format_channels",
        "open_video_service",
        "parse_aux",
        "plane_heights",
        "validate_geometry",
    ];
    let types: [(&str, &[&str]); 8] = [
        (
            "FrameParams",
            &["format", "height", "n_planes", "offset", "pts", "stride0", "width"],
        ),
        ("ParsedAux", &["caps", "metas"]),
        (
            "Qos",
            &["borrowed_max", "buffer_size", "history_size", "safe_overflow"],
        ),
        (
            "SinkConfig",
            &[
                "borrowed_max",
                "buffer_size",
                "gst_properties",
                "history_size",
                "max_bytes",
                "safe_overflow",
                "service",
            ],
        ),
        (
            "VideoFrame",
            &["aux", "is_eos", "parse_aux", "pixel_size", "pixels"],
        ),
        ("VideoFrameHeader", &["format_name"]),
        ("VideoFramePublisher", &["publish_frame"]),
        ("VideoFrameSubscriber", &["receive", "receive_blocking"]),
    ];
    let lang_python = ["header_pixels_to_numpy"];
    let lang_rust = [
        "Error",
        "IpcService",
        "PropValue",
        "Result",
        "VideoPubSub",
        "field_offsets",
        "header_pixels_to_ndarray",
    ];

    let mut s = String::new();
    s.push_str("{\n");
    s.push_str(&format!("  \"constants\": {},\n", arr(&constants)));
    s.push_str(&format!("  \"functions\": {},\n", arr(&functions)));
    s.push_str("  \"types\": {\n");
    for (i, (name, members)) in types.iter().enumerate() {
        let comma = if i + 1 < types.len() { "," } else { "" };
        s.push_str(&format!("    \"{name}\": {}{comma}\n", arr(members)));
    }
    s.push_str("  },\n");
    s.push_str("  \"language_specific\": {\n");
    s.push_str(&format!("    \"python\": {},\n", arr(&lang_python)));
    s.push_str(&format!("    \"rust\": {}\n", arr(&lang_rust)));
    s.push_str("  }\n");
    s.push_str("}\n");
    s
}

#[test]
fn api_manifest_matches_golden() {
    let expected = manifest_json();
    let path = golden_path();

    if std::env::var_os("UPDATE_GOLDEN").is_some() {
        std::fs::write(&path, &expected).expect("write golden file");
        return;
    }

    let actual = std::fs::read_to_string(&path).unwrap_or_else(|e| {
        panic!(
            "cannot read golden {}: {e}\nrun `UPDATE_GOLDEN=1 cargo test -p gst-plugin-iceoryx2-video` to create it",
            path.display()
        )
    });
    assert_eq!(
        actual,
        expected,
        "the API surface drifted from the committed golden ({}); if intentional, regenerate with \
         `UPDATE_GOLDEN=1 cargo test -p gst-plugin-iceoryx2-video` and update the Python \
         gst_iceoryx2.video mirror (test_api_parity) + PARITY.md to match",
        path.display(),
    );
}

/// Reference every symbol the manifest names, so a Rust-side rename/removal breaks compilation here
/// (the Python-side counterpart is `test_api_parity`). Kept in step with [`manifest_json`].
#[test]
#[allow(clippy::no_effect, path_statements)]
fn surface_symbols_exist() {
    // ---- shared constants ----
    let _ = v::DEFAULT_AUX_BYTES;
    let _ = v::DEFAULT_SERVICE;
    let _ = v::FORMAT_LEN;
    let _ = v::HEADER_ALIGN;
    let _ = v::HEADER_FLAG_EOS;
    let _ = v::HEADER_SIZE;
    let _ = v::HEADER_TYPE_NAME;
    let _ = v::MAX_PLANES;
    let _ = &v::PACKED_FORMATS;
    let _ = &v::SUPPORTED_FORMATS;
    let _ = v::VIDEO_BORROWED_MAX;
    let _ = v::VIDEO_BUFFER_SIZE;
    let _ = v::VIDEO_HISTORY_SIZE;
    let _ = v::VIDEO_SAFE_OVERFLOW;

    // ---- shared free functions ----
    let _ = v::build_aux;
    let _ = v::create_listener;
    let _ = v::create_node;
    let _ = v::create_notifier;
    let _ = v::format_channels;
    let _ = v::open_video_service;
    let _ = v::parse_aux;
    let _ = v::plane_heights;
    let _ = v::validate_geometry;

    // ---- shared type members (methods/properties) ----
    let _ = v::VideoFrameHeader::format_name;
    let _ = v::VideoFramePublisher::publish_frame;
    let _ = v::VideoFrameSubscriber::receive;
    let _ = v::VideoFrameSubscriber::receive_blocking;
    let _ = v::VideoFrame::aux;
    let _ = v::VideoFrame::is_eos;
    let _ = v::VideoFrame::parse_aux;
    let _ = v::VideoFrame::pixel_size;
    let _ = v::VideoFrame::pixels;
    let _ = v::SinkConfig::gst_properties;

    // ---- shared type fields (access proves they exist; block bodies so no borrow escapes) ----
    let _ = |p: v::FrameParams| {
        let _ = (p.format, p.height, p.n_planes, p.offset, p.pts, p.stride0, p.width);
    };
    let _ = |a: v::ParsedAux| {
        let _ = (a.caps, a.metas);
    };
    let _ = |q: v::Qos| {
        let _ = (q.buffer_size, q.borrowed_max, q.history_size, q.safe_overflow);
    };
    let _ = |c: v::SinkConfig| {
        let _ = (
            c.service,
            c.max_bytes,
            c.buffer_size,
            c.borrowed_max,
            c.history_size,
            c.safe_overflow,
        );
    };

    // ---- documented Rust-only extras (language_specific.rust + a few method-level) ----
    let _ = v::Error::new("context", "cause"); // impl-Trait arg: reference by calling, not turbofish
    let _: Option<v::IpcService> = None;
    let _: Option<v::VideoPubSub> = None;
    let _: Option<v::Result<()>> = None;
    let _ = v::PropValue::Bool(true);
    let _ = v::field_offsets;
    let _ = v::VideoFrameHeader::set_format;
    let _ = v::VideoFrame::header;
    let _ = v::VideoFrame::payload;
    let _ = v::VideoFramePublisher::new;
    let _ = v::VideoFramePublisher::with_node;
    let _ = v::VideoFrameSubscriber::new;
    let _ = v::VideoFrameSubscriber::with_node;
    #[cfg(feature = "ndarray")]
    {
        let _ = v::header_pixels_to_ndarray;
        let _ = v::VideoFrame::to_ndarray;
    }
}
