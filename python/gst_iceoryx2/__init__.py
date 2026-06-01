"""gst-plugin-iceoryx2 — GStreamer ``iceoryx2sink``/``iceoryx2src`` elements shipped as a maturin wheel.

The compiled extension (:mod:`gst_iceoryx2._gst_iceoryx2`) is a single cdylib that is *both* a pyo3
module and a GStreamer plugin. :func:`setup_gstreamer` registers the plugin with the host GStreamer
at runtime (no import side effects: nothing happens until it is called).

**Importing this package does not load the compiled ``.so``.** The ``.so`` links libgstreamer, so
loading it requires a GStreamer runtime. The compiled module is imported lazily — only inside
:func:`setup_gstreamer` (and on explicit ``gst_iceoryx2._gst_iceoryx2`` access) — so the pure-Python
:mod:`gst_iceoryx2.video` SDK (ctypes + iceoryx2 + numpy) can be imported and used by a subscriber
with **no GStreamer installed**.
"""

from __future__ import annotations

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


def __getattr__(name: str):
    """Lazily expose the compiled module as ``gst_iceoryx2._gst_iceoryx2``.

    Deferred so that importing this package (or ``gst_iceoryx2.video``) never loads the
    GStreamer-linked cdylib — only an explicit attribute access (or ``setup_gstreamer``) does.

    Uses :func:`importlib.import_module` rather than ``from gst_iceoryx2 import _gst_iceoryx2``:
    the latter form makes ``_handle_fromlist`` call ``hasattr(pkg, "_gst_iceoryx2")``, which re-enters
    this very ``__getattr__`` before the submodule is imported — an infinite recursion. Importing the
    submodule by its full dotted name sidesteps the attribute lookup entirely.
    """
    if name == "_gst_iceoryx2":
        import importlib

        return importlib.import_module("gst_iceoryx2._gst_iceoryx2")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _plugin_link_path() -> str:
    """Path to a ``libgst<PLUGIN_NAME>.so`` symlink pointing at the compiled cdylib.

    GStreamer derives the plugin descriptor symbol from the *filename*, so the maturin-named
    ``_gst_iceoryx2.<abi>.so`` must be exposed under the GStreamer-derivable name. The link lives
    in a per-user temp dir and is (re)created idempotently.

    The ``.so`` suffix is intentional on Linux *and* macOS: CPython extension modules are ``.so`` on
    both (not ``.dylib`` on macOS), and GStreamer loads the plugin by the path we hand it via
    ``g_module``, so the suffix only has to match the actual file. Windows (``.pyd`` / a different
    plugin-loading story) is not currently supported — the build/dev tooling targets Linux + macOS.
    """
    from gst_iceoryx2 import _gst_iceoryx2

    so_path = _gst_iceoryx2.__file__
    if so_path is None:  # pragma: no cover - defensive
        raise RuntimeError("gst_iceoryx2._gst_iceoryx2 has no __file__; cannot locate plugin")
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

    Idempotent. Sets ``GST_REGISTRY_FORK=no`` (the dual-purpose cdylib references Python symbols,
    so the default out-of-process ``gst-plugin-scanner`` — which has no libpython — cannot load it;
    in-process scanning resolves the symbols from the running interpreter).

    Requires the ``gst`` extra (``pip install gst-plugin-iceoryx2[gst]``) for the GStreamer Python
    bindings, and a GStreamer 1.24+ runtime.

    Args:
        verify: if ``True`` (default), raise ``RuntimeError`` when any element fails to register
            (e.g. an ABI/version mismatch).
    """
    # Must be set before Gst scans plugins.
    os.environ.setdefault("GST_REGISTRY_FORK", "no")

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
