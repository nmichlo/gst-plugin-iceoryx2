//! Zero-copy buffer pool — a non-reusing [`gst::BufferPool`] whose buffers are backed directly by
//! loaned iceoryx2 slice samples.
//!
//! Why a *non-reusing* pool? A standard `GstBufferPool` recycles buffers, but an iceoryx2 loan is
//! one-shot: once a sample is `send()`-published it belongs to the subscriber, so the same shared
//! memory must never be handed back to upstream to overwrite. We therefore override
//! `acquire_buffer` to always allocate a fresh loan and `release_buffer` to drop it.
//!
//! Flow: upstream (`videoconvert`) acquires a buffer → it is a fresh iceoryx2 sample's payload →
//! upstream writes pixels straight into shared memory → the sink's `render` recognises the buffer
//! (its payload pointer is in the [`LoanRegistry`]), takes the loan, fills the header, and sends it
//! with **no copy**. If a buffer did not come from this pool (pointer not registered), the sink
//! falls back to copying. The `LoanHandle` wrapping each sample un-registers it on drop, so a
//! buffer that is dropped before reaching the sink simply returns its loan.

use gst::glib;
use gst::subclass::prelude::*;
use std::collections::HashMap;
use std::mem::MaybeUninit;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use iceoryx2::port::publisher::Publisher;
use iceoryx2::sample_mut_uninit::SampleMutUninit;

use crate::format::VideoFrameHeader;
use crate::IpcService;

/// A loaned, not-yet-sent iceoryx2 slice sample.
type UninitSlice = SampleMutUninit<IpcService, [MaybeUninit<u8>], VideoFrameHeader>;
/// Shared publisher (also used by the sink's copy-fallback path).
pub type SharedPublisher = Arc<Publisher<IpcService, [u8], VideoFrameHeader>>;

/// Wrapper asserting the loaned sample is `Send`.
///
/// # Safety
/// The sample's internal raw pointer addresses process-global iceoryx2 shared memory that is valid
/// from any thread; all access is serialised behind the [`LoanRegistry`] mutex and, in practice,
/// the single GStreamer streaming thread.
pub struct SendSample(pub UninitSlice);
unsafe impl Send for SendSample {}

/// Maps a loaned sample's payload base pointer → the sample awaiting `send()`.
///
/// Why a side registry rather than stashing the `SampleMut` *inside* the `GstMemory` (the more
/// idiomatic design)? `gst::Memory::from_mut_slice` wraps the loan in gstreamer-rs's private
/// `WrappedMemory<T>`, and the crate exposes **no way to recover `&T`** from a `MemoryRef` — only
/// hand-rolled FFI memory types are `downcast`-able. Owning the sample in the memory would therefore
/// mean ~200 lines of `unsafe` FFI (a `#[repr(C)]` memory struct + registered allocator + map/
/// unmap/share/copy/free) plus interior mutability to move the sample out in `render`, for no
/// measurable gain (the per-frame cost here is one uncontended mutex + one hashmap op, far below the
/// memcpy/`videoconvert` bound). The registry is the pragmatic Rust choice; revisit only if profiling
/// ever shows it mattering.
pub type LoanRegistry = Arc<Mutex<HashMap<usize, SendSample>>>;

/// The byte container handed to `gst::Memory::from_mut_slice`: it owns nothing but the address +
/// length of a loaned payload, plus a registry handle so a dropped (un-sent) buffer returns its
/// loan.
struct LoanHandle {
    ptr: usize,
    len: usize,
    registry: LoanRegistry,
}

impl AsMut<[u8]> for LoanHandle {
    fn as_mut(&mut self) -> &mut [u8] {
        // SAFETY: ptr/len describe a live loaned payload in shared memory; it stays valid until the
        // sample is taken (sent) by the sink or this handle is dropped (which un-registers it).
        unsafe { std::slice::from_raw_parts_mut(self.ptr as *mut u8, self.len) }
    }
}

impl Drop for LoanHandle {
    fn drop(&mut self) {
        // No-op if the sink already took (sent) the sample; otherwise the loan is returned here.
        let _ = self.registry.lock().unwrap().remove(&self.ptr);
    }
}

mod imp {
    use super::*;

    pub struct Inner {
        pub publisher: SharedPublisher,
        pub registry: LoanRegistry,
        pub size: usize,
        /// Extra bytes loaned *after* the pixel region to hold the aux blob (caps + metas). Upstream
        /// only ever sees the pixel region (`size`); the sink fills this tail in `render`.
        pub aux_reserve: usize,
        /// Lossless mode: block-retry a failed loan (ring full) instead of erroring.
        pub lossless: bool,
        /// Shared with the sink so `unlock` can break the lossless wait on flush/shutdown.
        pub unlocked: Arc<AtomicBool>,
    }

    /// Poll interval while a lossless `acquire_buffer` waits for the consumer to free a slot.
    const POOL_WAIT: Duration = Duration::from_millis(20);

    #[derive(Default)]
    pub struct Iceoryx2BufferPool {
        pub inner: Mutex<Option<Inner>>,
    }

    #[glib::object_subclass]
    impl ObjectSubclass for Iceoryx2BufferPool {
        const NAME: &'static str = "GstIceoryx2BufferPool";
        type Type = super::Iceoryx2BufferPool;
        type ParentType = gst::BufferPool;
    }

    impl ObjectImpl for Iceoryx2BufferPool {}
    impl GstObjectImpl for Iceoryx2BufferPool {}

    impl BufferPoolImpl for Iceoryx2BufferPool {
        // Never recycle: every acquisition is a fresh loan.
        fn acquire_buffer(
            &self,
            params: Option<&gst::BufferPoolAcquireParams>,
        ) -> Result<gst::Buffer, gst::FlowError> {
            self.alloc_buffer(params)
        }

        fn alloc_buffer(
            &self,
            _params: Option<&gst::BufferPoolAcquireParams>,
        ) -> Result<gst::Buffer, gst::FlowError> {
            // Snapshot what we need and drop the lock, so a lossless wait does not stall other
            // pool operations (e.g. release_buffer on another thread).
            let (publisher, registry, size, aux_reserve, lossless, unlocked) = {
                let guard = self.inner.lock().unwrap();
                let inner = guard.as_ref().ok_or(gst::FlowError::Error)?;
                (
                    inner.publisher.clone(),
                    inner.registry.clone(),
                    inner.size,
                    inner.aux_reserve,
                    inner.lossless,
                    inner.unlocked.clone(),
                )
            };

            // Loan pixels + the aux-blob tail; upstream only ever writes the pixel region.
            let mut sample = loop {
                match publisher.loan_slice_uninit(size + aux_reserve) {
                    Ok(s) => break s,
                    // Lossless: ring full (consumer behind) → wait for a slot rather than error.
                    Err(_) if lossless && !unlocked.load(Ordering::Acquire) => {
                        std::thread::sleep(POOL_WAIT)
                    }
                    Err(_) => return Err(gst::FlowError::Error),
                }
            };
            let ptr = sample.payload_mut().as_mut_ptr() as usize;
            registry.lock().unwrap().insert(ptr, SendSample(sample));

            // Expose only the pixel region to upstream; the sink writes the reserved tail itself.
            let handle = LoanHandle {
                ptr,
                len: size,
                registry: registry.clone(),
            };
            let mem = gst::Memory::from_mut_slice(handle);
            let mut buffer = gst::Buffer::new();
            buffer.get_mut().unwrap().append_memory(mem);
            Ok(buffer)
        }

        // Never recycle: drop the buffer (its LoanHandle un-registers the loan).
        fn release_buffer(&self, buffer: gst::Buffer) {
            self.free_buffer(buffer);
        }
    }
}

glib::wrapper! {
    pub struct Iceoryx2BufferPool(ObjectSubclass<imp::Iceoryx2BufferPool>)
        @extends gst::BufferPool, gst::Object;
}

impl Iceoryx2BufferPool {
    /// Build a pool that loans `size`-byte pixel buffers from `publisher` (plus a hidden
    /// `aux_reserve`-byte tail for the sink's aux blob), registering each in `registry`. `lossless`
    /// + `unlocked` control back-pressure when the ring is full (see [`imp::Inner`]).
    pub fn new(
        publisher: SharedPublisher,
        size: usize,
        aux_reserve: usize,
        registry: LoanRegistry,
        lossless: bool,
        unlocked: Arc<AtomicBool>,
    ) -> Self {
        let obj: Self = glib::Object::new();
        *obj.imp().inner.lock().unwrap() = Some(imp::Inner {
            publisher,
            registry,
            size,
            aux_reserve,
            lossless,
            unlocked,
        });
        obj
    }
}
