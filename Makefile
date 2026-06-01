# gst-plugin-iceoryx2 — developer tasks
#
# Build is maturin (one cdylib = GStreamer plugin + pyo3 module). On macOS the GStreamer
# libraries live under the brew prefix; the GSTREAMER_ENV prefix points gi/Gst at them.

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

# Python integration tests (real GStreamer pipeline + iceoryx2 shared memory).
test-integration:
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
