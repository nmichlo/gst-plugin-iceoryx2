//! `gst-plugin-iceoryx2` — the GStreamer plugin registering the [`iceoryx2sink`](sink) and
//! [`iceoryx2src`](source) elements, which move raw video frames between processes **zero-copy** over
//! iceoryx2 shared memory.
//!
//! The wire contract + transport live in the GStreamer-free
//! [`gst_plugin_iceoryx2_video`](gst_plugin_iceoryx2_video) core crate (one implementation, also the
//! crates.io SDK); this crate is the GStreamer adapter around it. It is a *pure* GStreamer plugin —
//! no pyo3 — so the standard out-of-process `gst-plugin-scanner` can load it; the maturin wheel ships
//! the same cdylib and `setup_gstreamer()` symlinks it as `libgsticeoryx2.so`.
//!
//! See `SPEC.md` for the wire format and `README.md` for the overview.

use gst::glib;

mod aux;
mod caps;
mod pool;
mod sink;
mod source;

// ---- GStreamer plugin entry point -----------------------------------------

gst::plugin_define!(
    iceoryx2,
    env!("CARGO_PKG_DESCRIPTION"),
    plugin_init,
    env!("CARGO_PKG_VERSION"),
    "MIT",
    "gst-plugin-iceoryx2",
    "gst-plugin-iceoryx2",
    env!("CARGO_PKG_REPOSITORY")
);

fn plugin_init(plugin: &gst::Plugin) -> Result<(), glib::BoolError> {
    sink::register(plugin)?;
    source::register(plugin)
}
