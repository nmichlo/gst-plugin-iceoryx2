"""Shared pytest fixtures.

On macOS the GStreamer libraries + typelibs live under the brew prefix and must be visible to the
dynamic loader *before* the Python process starts — `make test` sets `DYLD_LIBRARY_PATH` /
`GI_TYPELIB_PATH` for that reason. Run the suite via `make test`, not bare `pytest`.
"""

from __future__ import annotations

import pytest


@pytest.fixture(scope="session")
def gst():
    """The initialised GStreamer module with `iceoryx2sink` registered."""
    from gst_iceoryx2 import setup_gstreamer

    setup_gstreamer()

    import gi

    gi.require_version("Gst", "1.0")
    from gi.repository import Gst

    return Gst
