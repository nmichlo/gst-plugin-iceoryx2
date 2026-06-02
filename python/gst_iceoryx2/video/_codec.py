"""
The **aux blob** codec — the variable-length tail appended after the pixels in a sample payload,
carrying the fidelity the fixed ``VideoFrameHeader`` cannot: the full serialised ``GstCaps`` *string*
(colorimetry, framerate, pixel-aspect-ratio, interlace) plus any serialisable ``GstMeta`` blobs.

This is the pure-Python mirror of the Rust ``aux`` module
(``crates/gst-plugin-iceoryx2-video/src/auxblob.rs``) — the **byte framing** only, taking the caps as
a ``str`` and the metas as already-serialised ``bytes``. Both ends produce/parse the identical wire
layout, so an SDK consumer reads exactly what the GStreamer ``iceoryx2sink`` writes.

Wire layout (little-endian, self-describing)::

    u32 caps_len            # bytes of the caps string (0 = no caps)
    u8[caps_len] caps_str   # gst_caps_to_string output, UTF-8, no NUL
    u32 n_metas             # number of serialised metas that follow
    repeated n_metas times:
        u32 meta_len        # bytes of this meta's gst_meta_serialize output
        u8[meta_len] meta   # the serialised meta
    # any trailing bytes (zero padding up to the reserved tail) are ignored

``n_metas`` is explicit so the parser stops exactly after the last meta and never reads the
zero-padding a fixed-size zero-copy reserve leaves behind it.
"""

from __future__ import annotations

__all__ = [
    "ParsedAux",
    "build_aux",
    "parse_aux",
]

import struct
from typing import NamedTuple


class ParsedAux(NamedTuple):
    """The parsed contents of an aux blob (mirrors the Rust ``ParsedAux``).

    A ``NamedTuple``, so it still unpacks and compares as the plain ``(caps, metas)`` tuple.
    """

    #: The publisher's full ``GstCaps`` in string form (``None`` if absent or truncated).
    caps: str | None
    #: Each serialised meta, ready to hand to ``gst::Meta::deserialize``.
    metas: list[bytes]


def build_aux(caps: str, metas: list[bytes], limit: int | None = None) -> bytes:
    """Build the aux blob from a caps string + already-serialised meta blobs.

    The caps string is always written first (small, high-value). Then each meta is appended **while
    it still fits** under ``limit`` (the reserved tail size for the zero-copy path); the remainder are
    dropped. ``None`` means unbounded (the copy path, which loans to fit). Mirrors the Rust
    ``build_aux``.
    """
    out = bytearray()
    caps_bytes = caps.encode()
    out += struct.pack("<I", len(caps_bytes))
    out += caps_bytes

    # Reserve the n_metas slot; patch it once we know how many we actually wrote.
    n_metas_pos = len(out)
    out += struct.pack("<I", 0)

    n_metas = 0
    for meta in metas:
        entry = 4 + len(meta)  # u32 length prefix + payload
        if limit is not None and len(out) + entry > limit:
            break  # hit the reserve; drop this and any further metas
        out += struct.pack("<I", len(meta))
        out += meta
        n_metas += 1
    out[n_metas_pos : n_metas_pos + 4] = struct.pack("<I", n_metas)
    return bytes(out)


def parse_aux(aux: bytes) -> ParsedAux:
    """Decode an aux blob produced by :func:`build_aux`, best-effort (never raises on truncation).

    Trailing zero-padding (a zero-copy producer reserves a fixed tail) is ignored because the meta
    count bounds the read. Mirrors the Rust ``parse_aux``.
    """
    if len(aux) < 4:
        return ParsedAux(None, [])
    pos = 0
    (caps_len,) = struct.unpack_from("<I", aux, pos)
    pos += 4
    if pos + caps_len > len(aux):
        return ParsedAux(None, [])
    caps = aux[pos : pos + caps_len].decode(errors="replace") or None
    pos += caps_len

    if pos + 4 > len(aux):
        return ParsedAux(caps, [])
    (n_metas,) = struct.unpack_from("<I", aux, pos)
    pos += 4

    metas: list[bytes] = []
    for _ in range(n_metas):
        if pos + 4 > len(aux):
            break
        (mlen,) = struct.unpack_from("<I", aux, pos)
        pos += 4
        if pos + mlen > len(aux):
            break
        metas.append(aux[pos : pos + mlen])
        pos += mlen
    return ParsedAux(caps, metas)
