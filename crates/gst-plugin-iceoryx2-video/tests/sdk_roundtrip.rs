//! End-to-end round-trip of the SDK transport: a [`VideoFramePublisher`] frame is received intact by
//! a [`VideoFrameSubscriber`] over a real iceoryx2 service.
//!
//! `#[ignore]` by default: it maps POSIX shared memory, so it needs an iceoryx2 runtime (and, on
//! Linux, a raised memlock limit). The CI integration job runs it with `cargo test -- --ignored`; run
//! it locally the same way.

use gst_plugin_iceoryx2_video::{FrameParams, VideoFramePublisher, VideoFrameSubscriber};

#[test]
#[ignore = "maps shared memory; run with --ignored in the integration environment"]
fn publisher_to_subscriber_roundtrip() {
    // Unique per process so concurrent test runs do not share a service.
    let service = format!("test/sdk/roundtrip/{}/v2", std::process::id());

    // Subscriber first, so it is connected when the publisher sends.
    let sub = VideoFrameSubscriber::new(&service).expect("create subscriber");
    let publisher = VideoFramePublisher::new(&service, 4 * 2 * 3).expect("create publisher");

    // A 4x2 BGR frame (stride 12, 2 rows = 24 bytes) with a distinctive pixel ramp.
    let pixels: Vec<u8> = (0..24u8).collect();
    publisher
        .publish_frame(
            &pixels,
            &FrameParams {
                width: 4,
                height: 2,
                format: "BGR",
                pts: 12_345,
                offset: 7,
                ..Default::default()
            },
        )
        .expect("publish frame");

    let frame = sub
        .receive_blocking(Some(1000))
        .expect("receive ok")
        .expect("a frame within 1s");

    let header = frame.header();
    assert_eq!(header.width, 4);
    assert_eq!(header.height, 2);
    assert_eq!(header.pts, 12_345);
    assert_eq!(header.offset, 7);
    assert_eq!(header.format_name(), "BGR");
    assert_eq!(header.stride[0], 12, "derived packed stride = len/height");
    assert_eq!(header.aux_size, 0);
    assert_eq!(frame.pixels(), pixels.as_slice());
    assert!(frame.aux().is_empty());
    assert!(!frame.is_eos());
}
