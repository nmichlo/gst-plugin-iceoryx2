# iceoryx2sink transport benchmark

- **platform**: macOS-26.4.1-arm64-arm-64bit-Mach-O
- **machine**: arm64
- **cpu_count**: 10
- **python**: 3.13.7
- **pixel_format**: BGR
- **throughput_pass**: 600 frames, unthrottled (free-run source, sink sync=false)
- **latency_pass**: 150 frames @ 30 fps (live source paced, sink sync=false so send==publish instant)

## 640x640

| transport | producer FPS | delivered FPS | drop % | MB/s | CPU ms/frame (total) | CPU ms/frame (marginal) | copies/frame | latency p50 / p95 / p99 (µs) |
|---|---|---|---|---|---|---|---|---|
| fakesink (baseline) | 3487 | — | — | 4285 | 0.292 | 0.000 (ref) | 0 | — (no consumer) |
| iox2 zero-copy (plugin) | 3224 | 3224 | 0.0 | 3961 | 0.314 | 0.022 | 0 (shm loan, no memcpy) | 42 / 59 / 70 |
| iox2 one-copy (Python) | 2887 | 2887 | 0.0 | 3548 | 0.348 | 0.056 | 2 (buffer→bytes + bytes→shm) | 101 / 128 / 167 |
| redis pub/sub | 1354 | 1354 | 0.0 | 1664 | 0.480 | 0.188 | 2 (buffer→bytes + framing (+ socket/server)) | 5150 / 5904 / 15678 |

## 1280x720

| transport | producer FPS | delivered FPS | drop % | MB/s | CPU ms/frame (total) | CPU ms/frame (marginal) | copies/frame | latency p50 / p95 / p99 (µs) |
|---|---|---|---|---|---|---|---|---|
| fakesink (baseline) | 1574 | — | — | 4352 | 0.637 | 0.000 (ref) | 0 | — (no consumer) |
| iox2 zero-copy (plugin) | 1529 | 1529 | 0.0 | 4226 | 0.658 | 0.020 | 0 (shm loan, no memcpy) | 42 / 65 / 87 |
| iox2 one-copy (Python) | 1267 | 1267 | 0.0 | 3503 | 0.789 | 0.152 | 2 (buffer→bytes + bytes→shm) | 200 / 274 / 441 |
| redis pub/sub | 577 | 577 | 0.0 | 1595 | 1.323 | 0.686 | 2 (buffer→bytes + framing (+ socket/server)) | 7628 / 8169 / 8452 |

## 1920x1080

| transport | producer FPS | delivered FPS | drop % | MB/s | CPU ms/frame (total) | CPU ms/frame (marginal) | copies/frame | latency p50 / p95 / p99 (µs) |
|---|---|---|---|---|---|---|---|---|
| fakesink (baseline) | 654 | — | — | 4071 | 1.531 | 0.000 (ref) | 0 | — (no consumer) |
| iox2 zero-copy (plugin) | 601 | 601 | 0.0 | 3738 | 1.614 | 0.083 | 0 (shm loan, no memcpy) | 47 / 76 / 85 |
| iox2 one-copy (Python) | 536 | 536 | 0.0 | 3335 | 1.869 | 0.338 | 2 (buffer→bytes + bytes→shm) | 367 / 422 / 942 |
| redis pub/sub | 241 | 241 | 0.0 | 1497 | 2.905 | 1.374 | 2 (buffer→bytes + framing (+ socket/server)) | 17139 / 17849 / 18075 |

## 3840x2160

| transport | producer FPS | delivered FPS | drop % | MB/s | CPU ms/frame (total) | CPU ms/frame (marginal) | copies/frame | latency p50 / p95 / p99 (µs) |
|---|---|---|---|---|---|---|---|---|
| fakesink (baseline) | 167 | — | — | 4163 | 5.987 | 0.000 (ref) | 0 | — (no consumer) |
| iox2 zero-copy (plugin) | 166 | 166 | 0.0 | 4123 | 6.046 | 0.059 | 0 (shm loan, no memcpy) | 41 / 62 / 80 |
| iox2 one-copy (Python) | 135 | 135 | 0.0 | 3368 | 7.396 | 1.409 | 2 (buffer→bytes + bytes→shm) | 1255 / 1411 / 2237 |
