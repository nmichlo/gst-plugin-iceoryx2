# gst-plugin-iceoryx2 — developer tasks
#
# Cargo workspace: the GStreamer-free `gst-plugin-iceoryx2-video` core SDK + the `gst-plugin-iceoryx2`
# plugin built on it. Build is maturin (its cffi binding ships the pure GStreamer plugin cdylib in the
# wheel — no pyo3). On macOS the GStreamer libraries live under the brew prefix; the GSTREAMER_ENV
# prefix points gi/Gst at them.

# Detect brew prefix for GStreamer libraries (macOS only).
BREW_PREFIX := $(shell brew --prefix 2>/dev/null)

ifdef BREW_PREFIX
GSTREAMER_ENV := DYLD_LIBRARY_PATH=$(BREW_PREFIX)/lib:$(DYLD_LIBRARY_PATH) GI_TYPELIB_PATH=$(BREW_PREFIX)/lib/girepository-1.0:$(GI_TYPELIB_PATH)
PKG_CONFIG_ENV := PKG_CONFIG_PATH=$(BREW_PREFIX)/lib/pkgconfig:$(PKG_CONFIG_PATH)
endif

.PHONY: build develop test test-integration cargo-test lint fmt clean benchmark version version-stamp

# Print the git-derived version (clean X.Y.Z on a tag, X.Y.Z.devN+g<sha> between tags). Read-only.
version:
	uv run python scripts/version.py

# Stamp the git-derived version into Cargo.toml (used by the release workflow; dirties the working
# tree locally, so prefer plain `version` for a preview).
version-stamp:
	uv run python scripts/version.py --write

# Build + install the extension into the active uv environment (editable Python wrapper).
develop:
	$(PKG_CONFIG_ENV) uv run maturin develop --uv

# Build a release wheel into target/wheels/.
build:
	$(PKG_CONFIG_ENV) uv run maturin build --release

# Rust unit tests (header layout, format round-trip).
cargo-test:
	$(PKG_CONFIG_ENV) cargo test

# Python unit tests (no IPC / no pipeline). Requires `make develop` first.
# `--group gst` pulls in PyGObject (kept out of the default `dev` group); the `setup_gstreamer`
# unit test needs `gi`.
test:
	$(GSTREAMER_ENV) uv run --group gst pytest -m "not integration"

# Integration tests (real GStreamer pipeline + iceoryx2 shared memory). Also runs the Rust SDK
# round-trip, which is #[ignore] because it maps shared memory (run explicitly with --ignored).
test-integration:
	$(PKG_CONFIG_ENV) cargo test -p gst-plugin-iceoryx2-video -- --ignored
	$(GSTREAMER_ENV) uv run --group gst pytest -m integration

# Transport comparison: iox2 zero-copy (plugin) vs iox2 one-copy (Python) vs Redis.
# Needs `make develop` first, plus the benchmark deps (redis, psutil) and redis-server on PATH.
benchmark:
	$(GSTREAMER_ENV) uv run --group benchmark python benchmarks/run.py $(ARGS)

lint:
	$(PKG_CONFIG_ENV) cargo clippy --all-targets -- -D warnings
	uv run ruff check python benchmarks scripts

fmt:
	cargo fmt
	uv run ruff format python scripts

clean:
	cargo clean
	rm -rf target/wheels
