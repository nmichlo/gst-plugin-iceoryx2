"""gst-plugin-iceoryx2 — GStreamer ``iceoryx2sink``/``iceoryx2src`` elements shipped as a maturin wheel.

The wheel ships the compiled GStreamer plugin under :mod:`gst_iceoryx2._native` (built by maturin from
the ``gst-plugin-iceoryx2`` Rust crate). It is a **pure GStreamer plugin** — no pyo3, no Python
symbols — so the standard out-of-process ``gst-plugin-scanner`` can load it. :func:`setup_gstreamer`
registers it with the host GStreamer at runtime (no import side effects: nothing happens until it is
called).

**Importing this package does not load the compiled plugin.** The plugin links libgstreamer, so
loading it needs a GStreamer runtime. We never *import* the ``_native`` module — :func:`setup_gstreamer`
only locates its file *path* and hands that to GStreamer — so the pure-Python :mod:`gst_iceoryx2.video`
SDK (ctypes + iceoryx2 + numpy) can be imported and used by a subscriber with **no GStreamer installed**.
"""

from __future__ import annotations

import glob
import importlib.util
import os
import tempfile

__all__ = [
    "setup_gstreamer",
    "ELEMENT_NAME",
    "SINK_ELEMENT_NAME",
    "SRC_ELEMENT_NAME",
    "ELEMENT_NAMES",
    "PLUGIN_NAME",
]

#: The registered GStreamer sink element factory name.
SINK_ELEMENT_NAME = "iceoryx2sink"
#: The registered GStreamer source element factory name.
SRC_ELEMENT_NAME = "iceoryx2src"
#: All element factory names this plugin registers.
ELEMENT_NAMES = (SINK_ELEMENT_NAME, SRC_ELEMENT_NAME)
#: Backwards-compatible alias for the sink element name.
ELEMENT_NAME = SINK_ELEMENT_NAME
#: The GStreamer plugin name. The on-disk plugin file must be ``libgst<PLUGIN_NAME>.so`` so
#: GStreamer can derive the ``gst_plugin_<PLUGIN_NAME>_get_desc`` descriptor symbol from it.
PLUGIN_NAME = "iceoryx2"


def _native_lib_path() -> str:
    """Filesystem path of the compiled plugin under :mod:`gst_iceoryx2._native`.

    Resolved via :func:`importlib.util.find_spec` so we get the package *directory* **without
    importing** the module — importing it would dlopen the libgstreamer-linked plugin, defeating the
    "use the SDK with no GStreamer" guarantee. The maturin ``cffi`` build ships exactly one compiled
    library (``lib_native.so`` / ``.dylib``) in that directory.
    """
    spec = importlib.util.find_spec("gst_iceoryx2._native")
    if spec is None or not spec.submodule_search_locations:
        raise RuntimeError(
            "gst_iceoryx2._native not found; the compiled plugin is missing from the wheel "
            "(was it built with `maturin develop`/`maturin build`?)"
        )
    native_dir = spec.submodule_search_locations[0]
    libs = sorted(glob.glob(os.path.join(native_dir, "*.so")) + glob.glob(os.path.join(native_dir, "*.dylib")))
    if not libs:
        raise RuntimeError(f"no compiled plugin (*.so/*.dylib) found in {native_dir!r}")
    return libs[0]


def _plugin_link_path() -> str:
    """Path to a ``libgst<PLUGIN_NAME>.so`` symlink pointing at the compiled plugin.

    GStreamer derives the plugin descriptor symbol from the *filename*, so the maturin-named
    ``lib_native.<abi>.so`` must be exposed under the GStreamer-derivable name. The link lives in a
    per-user temp dir and is (re)created idempotently.

    The ``.so`` suffix is intentional on Linux *and* macOS: GStreamer loads the plugin by the path we
    hand it via ``g_module``, so the suffix only has to match the actual file. Windows is not
    currently supported — the build/dev tooling targets Linux + macOS.
    """
    so_path = _native_lib_path()
    plugin_dir = os.path.join(tempfile.gettempdir(), "gst_iceoryx2_plugin")
    os.makedirs(plugin_dir, exist_ok=True)
    link = os.path.join(plugin_dir, f"libgst{PLUGIN_NAME}.so")
    # Recreate if missing or pointing at a stale build.
    if os.path.lexists(link) and os.path.realpath(link) != os.path.realpath(so_path):
        os.unlink(link)
    if not os.path.lexists(link):
        os.symlink(so_path, link)
    return link


def setup_gstreamer(*, verify: bool = True) -> None:
    """Register the ``iceoryx2sink``/``iceoryx2src`` elements with the host GStreamer.

    Idempotent. The plugin is a pure GStreamer plugin (no Python symbols), so the standard
    out-of-process ``gst-plugin-scanner`` loads it — no ``GST_REGISTRY_FORK`` workaround is needed.

    Requires the ``gst`` extra (``pip install gst-plugin-iceoryx2[gst]``) for the GStreamer Python
    bindings, and a GStreamer 1.24+ runtime.

    Args:
        verify: if ``True`` (default), raise ``RuntimeError`` when any element fails to register
            (e.g. an ABI/version mismatch).
    """
    import gi

    gi.require_version("Gst", "1.0")
    from gi.repository import Gst

    link = _plugin_link_path()
    plugin_dir = os.path.dirname(link)

    Gst.init(None)
    Gst.Registry.get().scan_path(plugin_dir)

    if verify:
        missing = [name for name in ELEMENT_NAMES if Gst.ElementFactory.find(name) is None]
        if missing:
            raise RuntimeError(
                f"GStreamer element(s) {missing} failed to register from {link!r} "
                "(ABI/version mismatch, or GStreamer could not load the plugin)"
            )
