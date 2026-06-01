---
name: Bug report
about: Something is broken or behaves unexpectedly
title: ''
labels: bug
assignees: ''
---

## What happened

A clear description of the bug, and what you expected instead.

## Reproduce

Minimal steps — ideally a short pipeline or SDK snippet:

```python
# e.g. the iceoryx2sink/iceoryx2src pipeline or the gst_iceoryx2.video calls that trigger it
```

## Environment

- `gst-plugin-iceoryx2` version (`python -c "import gst_iceoryx2._gst_iceoryx2 as m; print(m.__version__)"`):
- Installed from: wheel / sdist / `make develop`
- OS + arch:
- Python version:
- GStreamer version (`gst-launch-1.0 --version`), if using the elements:
- `iceoryx2` version:

## Logs

Relevant output. For the elements, `GST_DEBUG=iceoryx2sink:5,iceoryx2src:5` (and `iceoryx2aux:5`)
is helpful. Note whether it reproduces with the SDK alone (no GStreamer) or only via a pipeline.
