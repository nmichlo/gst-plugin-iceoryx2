# Contributing to `gst-plugin-iceoryx2`

Thanks for your interest. This is a Rust GStreamer plugin (`iceoryx2sink`/`iceoryx2src`) shipped as a
maturin wheel, with a pure-Python `gst_iceoryx2.video` SDK alongside it. `SPEC.md` is the canonical
contract; `README.md` is the tour; `TODO.md` tracks phase status.

## Development setup

Prerequisites: a Rust toolchain (stable), [`uv`](https://docs.astral.sh/uv/), and a GStreamer
**1.24+** install with development headers.

- **Linux** — `apt install libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev gstreamer1.0-plugins-base gstreamer1.0-plugins-good libclang-dev`
- **macOS** — `brew install gstreamer`

```bash
uv sync --dev          # create the dev environment
make develop           # build the cdylib + install it into the uv env (maturin develop)
make test              # Python unit tests (header equivalence + setup; no IPC)
make cargo-test        # Rust layout/format/validation tests
make test-integration  # real pipeline + iceoryx2 shared memory round-trips
make lint              # cargo clippy + ruff (python, benchmarks, scripts)
make fmt               # cargo fmt + ruff format
```

## Ground rules

- **`SPEC.md` is the source of truth.** Any change to the `VideoFrameHeader` layout, the iceoryx2
  service/QoS, the aux-blob codec, or the elements' properties/lifecycle must update `SPEC.md` **in
  the same commit**. The header layout is asserted from both Rust (`cargo test`) and Python
  (`test_header_equivalence`) — never let the two drift.
- **Keep the layering rule.** The `gst_iceoryx2.video` SDK must stay pure-Python (ctypes + iceoryx2
  + lazy numpy) and must **never** import the compiled `_gst_iceoryx2` — a consumer subscribes with
  no GStreamer installed. Importing the package (or `.video`) must have no side effects;
  `setup_gstreamer()` is the only thing that touches GStreamer.
- **Style.** British English, declarative, no fluff; backticks for identifiers, em dashes for inline
  asides. Match the surrounding code's conventions.
- **Always `uv run python`** — never bare `python`/`python3`. Prefer `rg`/`fd` over `grep`/`find`.

## Tests

Unmarked tests are fast unit tests (no IPC, no pipeline). Tests marked `@pytest.mark.integration`
need a real GStreamer pipeline + iceoryx2 shared memory (`make test-integration`). Add tests for any
behaviour change; the cross-language header contract and the wire format are both covered from both
sides and should stay that way.

## Versioning & releases

The version is **derived from git tags** by `scripts/version.py` (using `dunamai`) and stamped into
`Cargo.toml` at build time — there is no maturin plugin for this, so it is a build-time stamp.
`make version` previews the version your tree would produce. A clean tag `vX.Y.Z` yields `X.Y.Z`;
between tags you get `X.Y.(Z+1).devN+g<sha>` (a local version that cannot be published to PyPI). A
release is cut by pushing a tag (`git tag v0.1.0 && git push --tags`), which runs the release
workflow (wheels + sdist → GitHub Release + PyPI). Update `CHANGELOG.md` in the release commit.

## Pull requests

- Branch from `main`; keep PRs focused.
- Ensure `make lint`, `make cargo-test`, and `make test` pass; run `make test-integration` if you can.
- Describe the change and link any issue. CI (`pytest`, `pre-commit`) runs on every PR.
