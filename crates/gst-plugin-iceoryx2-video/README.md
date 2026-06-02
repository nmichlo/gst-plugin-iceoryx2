# gst-plugin-iceoryx2-video

The **GStreamer-free** Rust core of the
[`gst-plugin-iceoryx2`](https://crates.io/crates/gst-plugin-iceoryx2) project: move raw video frames
between processes **zero-copy** over [iceoryx2](https://crates.io/crates/iceoryx2) shared memory, with
no GStreamer dependency.

This crate owns the wire contract and the transport, so there is exactly one implementation shared by
the GStreamer elements, this SDK, and the Python `gst_iceoryx2.video` mirror:

- `VideoFrameHeader` — the fixed `#[repr(C)]` per-sample user-header.
- `build_aux` / `parse_aux` — the variable-length aux-blob framing (caps string + serialised metas).
- `validate_geometry` — bounds-checks an untrusted header against its payload.
- `VideoFramePublisher` / `VideoFrameSubscriber` / `VideoFrame` — the publish/subscribe SDK.

It shares one neutral vocabulary with the Python `gst_iceoryx2.video` SDK, kept in lockstep by
[`PARITY.md`](https://github.com/nmichlo/gst-plugin-iceoryx2/blob/main/PARITY.md) + a drift-guard test.
Optional `(H, W, C)` reshape helpers (the numpy counterpart) live behind the `ndarray` feature.

```rust
use gst_plugin_iceoryx2_video::{VideoFramePublisher, VideoFrameSubscriber, FrameParams};

# fn main() -> Result<(), Box<dyn std::error::Error>> {
// Publish (no GStreamer):
let publisher = VideoFramePublisher::new("video/cam0/frame/v2", 640 * 480 * 3)?;
publisher.publish_frame(&vec![0u8; 640 * 480 * 3],
    &FrameParams { width: 640, height: 480, ..Default::default() })?;

// Subscribe from another process:
let sub = VideoFrameSubscriber::new("video/cam0/frame/v2")?;
if let Some(frame) = sub.receive_blocking(Some(1000))? {
    println!("{}x{}", frame.header().width, frame.header().height);
}
# Ok(()) }
```

The wire format is documented in [`SPEC.md`](https://github.com/nmichlo/gst-plugin-iceoryx2/blob/main/SPEC.md).
For the GStreamer `iceoryx2sink`/`iceoryx2src` elements built on this crate, see
[`gst-plugin-iceoryx2`](https://crates.io/crates/gst-plugin-iceoryx2).

Licensed under MIT.
