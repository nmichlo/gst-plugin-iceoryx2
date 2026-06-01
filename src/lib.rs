//! `gst-plugin-iceoryx2` — a single cdylib with two entry points:
//!
//! * **GStreamer plugin** — `gst::plugin_define!` exports `gst_plugin_iceoryx2_get_desc`,
//!   registering the [`iceoryx2sink`](sink) and [`iceoryx2src`](source) elements.
//! * **pyo3 module** — `_gst_iceoryx2` exposes the wire-format constants so the Python side can
//!   assert layout equivalence and locate the plugin file.
//!
//! See `SPEC.md` for the wire format and `README.md` for the overview.

use gst::glib;
use pyo3::prelude::*;

mod aux;
mod caps;
mod format;
mod pool;
mod sink;
mod source;

/// iceoryx2 service flavour used throughout: thread-safe handles (`Send + Sync`), wire-identical to
/// `ipc::Service` (the two differ only in their local `ArcThreadSafetyPolicy`), so it interoperates
/// with a Python subscriber opened on `ServiceType.Ipc`.
pub(crate) type IpcService = iceoryx2::service::ipc_threadsafe::Service;

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

// ---- pyo3 module entry point ----------------------------------------------

/// The pyo3 module. Exposes the `VideoFrameHeader` layout contract (see [`format`]) so the Python
/// `test_header_equivalence` test can pin the struct from both sides.
#[pymodule]
fn _gst_iceoryx2(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    m.add("ELEMENT_NAME", "iceoryx2sink")?;
    m.add("SINK_ELEMENT_NAME", "iceoryx2sink")?;
    m.add("SRC_ELEMENT_NAME", "iceoryx2src")?;
    m.add("PLUGIN_NAME", "iceoryx2")?;
    format::register_pymodule(m)?;
    Ok(())
}
