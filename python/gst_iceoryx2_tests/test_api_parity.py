"""The cross-language **API surface** contract: the ``gst_iceoryx2.video`` SDK must expose every
shared symbol named in the committed ``api_manifest.json`` golden, under the same neutral name.

This is the Python half of the drift guard. The Rust core emits the canonical manifest
(``crates/gst-plugin-iceoryx2-video/tests/api_manifest_golden.rs``, regenerated with
``UPDATE_GOLDEN=1``); this test reads that file, so it needs **no compiled module, no GStreamer, and
no Rust toolchain** — it runs in the fast unit suite. A rename on either side fails a test: the Rust
emitter fails to compile, this asserts the Python ``__all__`` + class members still match.

The manifest pins the *shared* surface (same name on both); per-language extras (the numpy/ndarray
reshape pair, Rust plumbing) and the documented memory-model/teardown carve-outs live in ``PARITY.md``.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import gst_iceoryx2.video as video

MANIFEST = json.loads((Path(__file__).parent / "api_manifest.json").read_text())

_EXPORTS = set(video.__all__)


def _has_member(cls: type, member: str) -> bool:
    """Whether ``member`` is a public member of ``cls`` — method, property, dataclass field,
    ctypes ``_fields_`` entry, or NamedTuple field."""
    if hasattr(cls, member):
        return True
    if member in getattr(cls, "__dataclass_fields__", {}):
        return True
    if member in {name for name, *_ in getattr(cls, "_fields_", [])}:
        return True
    return False


def test_exports_match_all():
    """``__all__`` is the real export set (every listed name resolves on the package)."""
    for name in video.__all__:
        assert hasattr(video, name), f"{name} in __all__ but not importable from gst_iceoryx2.video"


def test_shared_constants_present():
    for name in MANIFEST["constants"]:
        assert name in _EXPORTS, f"shared constant {name!r} missing from gst_iceoryx2.video.__all__"


def test_shared_functions_present():
    for name in MANIFEST["functions"]:
        assert name in _EXPORTS, f"shared function {name!r} missing from gst_iceoryx2.video.__all__"
        assert callable(getattr(video, name)), f"{name!r} is not callable"


def test_shared_types_and_members_present():
    for type_name, members in MANIFEST["types"].items():
        assert type_name in _EXPORTS, f"shared type {type_name!r} missing from __all__"
        cls = getattr(video, type_name)
        for member in members:
            assert _has_member(cls, member), f"{type_name}.{member} missing on the Python SDK"


def test_language_specific_python_extras_present():
    """The Python-only module-level extras the manifest records must actually exist."""
    for name in MANIFEST["language_specific"]["python"]:
        assert name in _EXPORTS, f"python-specific {name!r} missing from __all__"


def test_no_undocumented_dataclass_fields():
    """Every real dataclass field must be named in the manifest. Combined with
    ``test_shared_types_and_members_present`` (every manifest member must exist), this pins the
    dataclass field sets exactly — an added field that the Rust mirror lacks is caught here."""
    for type_name in ("FrameParams", "Qos", "SinkConfig"):
        cls = getattr(video, type_name)
        assert dataclasses.is_dataclass(cls), f"{type_name} should be a dataclass"
        documented = set(MANIFEST["types"][type_name])
        undocumented = {f.name for f in dataclasses.fields(cls)} - documented
        assert not undocumented, (
            f"{type_name} has undocumented field(s) {sorted(undocumented)}; "
            "add them to the Rust manifest (api_manifest_golden.rs) + PARITY.md"
        )
