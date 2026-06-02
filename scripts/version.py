#!/usr/bin/env python3
"""Derive the package version from git (via ``dunamai``) and stamp it into the workspace ``Cargo.toml``.

This is a Cargo **workspace**: both crates inherit ``[workspace.package] version`` (the plugin crate's
version, which maturin reads for the wheel, and the core crate's, published to crates.io). The plugin
also depends on the core crate via ``[workspace.dependencies]`` with an exact ``=X.Y.Z`` pin, which is
stamped in lockstep so the two always match at ``cargo publish`` time. There is **no** maturin
build-backend plugin that derives the version from git tags — it is architectural (maturin does not
use setuptools/PEP 517 metadata hooks; see PyO3/maturin discussions #1267, #1772, #2127). So this
script is the setuptools-scm-equivalent: a build-time stamp, driven by the existing ``dunamai`` tool
(the engine behind poetry-/uv-dynamic-versioning).

The version written to ``Cargo.toml`` is **semver** (Cargo rejects PEP 440 forms like ``0.1.0.dev4``);
maturin converts it to the PEP 440 wheel version (``0.1.0-dev.4+g<sha>`` → ``0.1.0.dev4+g<sha>``).

Resulting versions (shown as semver → wheel):

- **On an exact tag** ``vX.Y.Z`` (or ``X.Y.Z``)  →  ``X.Y.Z``                  (no local segment → PyPI-publishable).
- **Between tags**                               →  ``X.Y.(Z+1)-dev.N+g<sha>`` → ``X.Y.(Z+1).devN+g<sha>``  (*not* publishable).
- **Before the first tag**                       →  ``<base>-dev.N+g<sha>``    (anchored on the committed
  Cargo.toml version so pre-release dev builds sit on the intended release line).

Usage::

    python scripts/version.py                 # print the computed version
    python scripts/version.py --write         # also stamp it into Cargo.toml
    python scripts/version.py --check-public  # exit 1 if the version carries a local (+...) segment
    python scripts/version.py --check-base    # exit 1 if the Cargo.toml floor is behind the latest tag

The release workflow runs ``--write`` on a clean tag (so the wheel/sdist carry ``X.Y.Z``) and the
publish job runs ``--check-public`` to refuse to upload a local-segment (dev/dirty) version to PyPI.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tomllib
from pathlib import Path

CARGO_TOML = Path(__file__).resolve().parent.parent / "Cargo.toml"
# Only the `[workspace.package]` version sits at column 0; dependency `version = "…"` keys are mid-line
# inside inline tables (`gst = { …, version = "0.23", … }`), so a line-anchored match is unambiguous.
_VERSION_LINE = re.compile(r'^version = ".*"$', re.MULTILINE)
# The internal plugin→core dependency pin, kept in lockstep with the workspace version so the two
# crates publish together: `gst-plugin-iceoryx2-video = { version = "=X.Y.Z", path = "…" }`.
_INTERNAL_DEP = re.compile(r'(gst-plugin-iceoryx2-video = \{ version = ")[^"]*(")')


def _git(*args: str) -> str | None:
    try:
        out = subprocess.run(["git", *args], capture_output=True, text=True, check=True)
        return out.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def _cargo_base_version() -> str:
    with CARGO_TOML.open("rb") as fh:
        return tomllib.load(fh)["workspace"]["package"]["version"]


def _latest_tag_version() -> str | None:
    """The base (``X.Y.Z``) of the most recent reachable tag, or ``None`` if there are no tags."""
    tag = _git("describe", "--tags", "--abbrev=0")
    return tag.lstrip("v") if tag else None


def _release_tuple(version: str) -> tuple[int, ...]:
    """Comparable release tuple from a version's leading ``X.Y.Z`` (suffixes like ``-dev``/``+sha``
    and non-numeric parts are ignored)."""
    core = re.split(r"[-+]", version, maxsplit=1)[0]
    parts: list[int] = []
    for component in core.split("."):
        try:
            parts.append(int(component))
        except ValueError:
            break
    return tuple(parts)


def _bump_patch(base: str) -> str:
    """``0.1.0`` → ``0.1.1`` (a dev version targets the *next* patch); base returned as-is if its
    last component isn't numeric."""
    parts = base.split(".")
    try:
        parts[-1] = str(int(parts[-1]) + 1)
    except ValueError:
        return base
    return ".".join(parts)


def compute_version() -> str:
    """The git-derived **semver** version (see module docstring for the cases + the wheel mapping)."""
    from dunamai import Version

    has_tag = _git("describe", "--tags", "--abbrev=0") is not None
    version = Version.from_git()

    if has_tag:
        if version.distance == 0 and not version.dirty:
            return version.base  # exact, clean release tag → X.Y.Z
        base = _bump_patch(version.base)  # between tags: target the next patch
    else:
        # No tags yet: anchor on the committed base so dev builds sit on the intended release line
        # (dunamai would otherwise report a `0.0.0`-based version, ignoring Cargo.toml).
        base = _cargo_base_version()

    commit = version.commit or _git("rev-parse", "--short=7", "HEAD") or "0000000"
    dirty = ".dirty" if version.dirty else ""
    # `-dev.N` is valid semver and maturin maps it to PEP 440 `.devN` for the wheel.
    return f"{base}-dev.{version.distance}+g{commit}{dirty}"


def stamp(version: str) -> None:
    text = CARGO_TOML.read_text()
    new, n = _VERSION_LINE.subn(f'version = "{version}"', text, count=1)
    if n != 1:
        raise SystemExit(f"could not find a [workspace.package] version line in {CARGO_TOML}")
    # Keep the internal plugin→core dependency pin exact (`=X.Y.Z`) and in lockstep.
    new, n2 = _INTERNAL_DEP.subn(rf"\g<1>={version}\g<2>", new, count=1)
    if n2 != 1:
        raise SystemExit(
            f"could not find the internal gst-plugin-iceoryx2-video dependency line in {CARGO_TOML}"
        )
    CARGO_TOML.write_text(new)


def main() -> None:
    parser = argparse.ArgumentParser(description="Derive + stamp the version from git.")
    parser.add_argument("--write", action="store_true", help="stamp the version into Cargo.toml")
    parser.add_argument(
        "--check-public",
        action="store_true",
        help="exit non-zero if the version has a local (+...) segment (not PyPI-publishable)",
    )
    parser.add_argument(
        "--check-base",
        action="store_true",
        help="exit non-zero if the Cargo.toml [workspace.package] version is behind the latest git "
        "tag (a forgotten post-release floor bump / out-of-sync in-tree version)",
    )
    args = parser.parse_args()

    if args.check_base:
        base = _cargo_base_version()
        tag = _latest_tag_version()
        if tag is not None and _release_tuple(base) < _release_tuple(tag):
            print(
                f"error: Cargo.toml version {base!r} is behind the latest tag {tag!r}; bump the "
                "[workspace.package] version floor to at least the released version",
                file=sys.stderr,
            )
            raise SystemExit(1)

    version = compute_version()
    if args.check_public and "+" in version:
        print(
            f"error: version {version!r} has a local segment; tag a clean release "
            "(git tag vX.Y.Z) before publishing to PyPI",
            file=sys.stderr,
        )
        raise SystemExit(1)
    if args.write:
        stamp(version)
    print(version)


if __name__ == "__main__":
    main()
