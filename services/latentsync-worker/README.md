# LatentSync quality worker

This directory is the OpenMediaFlow adapter for the official
`ByteDance/LatentSync` 1.6 runtime. It is part of this project, but it must run
on a CUDA machine with at least 18 GB VRAM. The current Apple Silicon host keeps
using MuseTalk for the fast path.

The worker exposes the same contract as MuseTalk:

- `GET /health`
- `POST /lipsync` with `video_b64`, `audio_b64`, and optional `avatar_key`

On the CUDA host, install the official repository and its 1.6 checkpoints,
install this directory's requirements into the same environment, then run:

```bash
LATENTSYNC_REPO=/opt/LatentSync \
LATENTSYNC_CHECKPOINT=/opt/LatentSync/checkpoints/latentsync_unet.pt \
uvicorn server:app --app-dir /path/to/open-media-flow/services/latentsync-worker \
  --host 0.0.0.0 --port 8092
```

Set `OMF_LATENTSYNC_BASE_URL=http://<cuda-host>:8092` in the main project's
`.env`. No model credential or worker URL is stored in a content task.
