# gst-plugin-iceoryx2

GStreamer `iceoryx2sink` / `iceoryx2src` elements that move raw video frames between processes
**zero-copy** through [iceoryx2](https://crates.io/crates/iceoryx2) shared memory.

- **`iceoryx2sink`** — publishes a pipeline's raw video frames into an iceoryx2 service (zero-copy via
  a non-reusing buffer pool, with a copy fallback), plus an event notification.
- **`iceoryx2src`** — subscribes to that service and pushes the received frames downstream zero-copy.

The wire contract + transport live in the GStreamer-free
[`gst-plugin-iceoryx2-video`](https://crates.io/crates/gst-plugin-iceoryx2-video) crate (one
implementation, also a standalone SDK); this crate is the GStreamer adapter around it. It is a *pure*
GStreamer plugin — no Python linkage — so the standard `gst-plugin-scanner` loads it.

```sh
# Register the elements in your own Rust GStreamer application:
cargo add gst-plugin-iceoryx2
```

```rust
gst::init()?;
gst_iceoryx2::plugin_register_static()?; // register iceoryx2sink/iceoryx2src in-process
let pipeline = gst::parse::launch(
    "videotestsrc ! videoconvert ! video/x-raw,format=BGR,width=640,height=480 \
     ! iceoryx2sink service=video/cam0/frame/v2",
)?;
```

It also ships as a Python wheel (`pip install gst-plugin-iceoryx2[gst]`) that registers the elements
with the host GStreamer and bundles the pure-Python `gst_iceoryx2.video` SDK. See the
[project README](https://github.com/nmichlo/gst-plugin-iceoryx2) and
[`SPEC.md`](https://github.com/nmichlo/gst-plugin-iceoryx2/blob/main/SPEC.md).

Licensed under MIT.
