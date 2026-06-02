//! The cross-language layout contract, emitted as a committed golden file.
//!
//! This is the single pin that stops the Rust `#[repr(C)]` `VideoFrameHeader` and the hand-written
//! Python `gst_iceoryx2.video` ctypes mirror from drifting — replacing the old runtime pyo3 export
//! (the plugin no longer is a Python module). This test serialises the live layout
//! ([`field_offsets`](gst_plugin_iceoryx2_video::field_offsets) + size/align/constants) and asserts
//! it equals `python/gst_iceoryx2_tests/header_layout.json`; the Python `test_header_equivalence`
//! reads the same file and asserts its ctypes class matches. Regenerate after an intentional layout
//! change with `UPDATE_GOLDEN=1 cargo test -p gst-plugin-iceoryx2-video`.

use gst_plugin_iceoryx2_video as v;
use std::path::PathBuf;

/// The committed golden, relative to this crate's manifest dir.
fn golden_path() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../python/gst_iceoryx2_tests/header_layout.json")
}

/// Deterministic, hand-rolled JSON (no serde dep) in struct-field order so the committed file is
/// stable. Integers only — no escaping needed beyond the fixed string keys.
fn golden_json() -> String {
    let mut s = String::new();
    s.push_str("{\n");
    s.push_str(&format!("  \"size\": {},\n", v::HEADER_SIZE));
    s.push_str(&format!("  \"align\": {},\n", v::HEADER_ALIGN));
    s.push_str(&format!("  \"type_name\": \"{}\",\n", v::HEADER_TYPE_NAME));
    s.push_str("  \"constants\": {\n");
    s.push_str(&format!("    \"MAX_PLANES\": {},\n", v::MAX_PLANES));
    s.push_str(&format!("    \"FORMAT_LEN\": {},\n", v::FORMAT_LEN));
    s.push_str(&format!("    \"HEADER_FLAG_EOS\": {},\n", v::HEADER_FLAG_EOS));
    s.push_str(&format!("    \"DEFAULT_AUX_BYTES\": {}\n", v::DEFAULT_AUX_BYTES));
    s.push_str("  },\n");
    s.push_str("  \"field_offsets\": {\n");
    let fields = v::field_offsets();
    for (i, (name, off)) in fields.iter().enumerate() {
        let comma = if i + 1 < fields.len() { "," } else { "" };
        s.push_str(&format!("    \"{name}\": {off}{comma}\n"));
    }
    s.push_str("  }\n");
    s.push_str("}\n");
    s
}

#[test]
fn header_layout_matches_golden() {
    let expected = golden_json();
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
        actual, expected,
        "VideoFrameHeader layout drifted from the committed golden ({}); if intentional, \
         regenerate with `UPDATE_GOLDEN=1 cargo test -p gst-plugin-iceoryx2-video` and update the \
         Python ctypes mirror to match",
        path.display(),
    );
}
