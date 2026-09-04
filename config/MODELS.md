# Local model manifest

OpenMediaFlow keeps model weights outside Git under `data/`. Verify large downloads before
enabling their production path.

## AI comic keyframes

| Purpose | Model | Local filename | Size | SHA-256 | License |
| --- | --- | --- | ---: | --- | --- |
| Anime character sheets and keyframes | [Animagine XL 4.0 Opt](https://huggingface.co/cagliostrolab/animagine-xl-4.0) | `animagine-xl-4.0-opt.safetensors` | 6,938,350,040 bytes | `6327eca98bfb6538dd7a4edce22484a1bbc57a8cff6b11d075d40da1afb847ac` | CreativeML Open RAIL++-M |

The model is stored at:

```text
data/comfyui/OpenMediaFlow ComfyUI/ComfyUI/models/checkpoints/
```

The project uses `config/workflows/comic_image.json` with CFG 5 and Euler Ancestral. The
local M1 profile uses 20 steps at 576x1024 (exact 9:16), then scales during motion rendering;
the upstream 28-step 768x1344 profile exceeded the practical unified-memory latency budget.

Before enabling AI comic generation:

```bash
shasum -a 256 "data/comfyui/OpenMediaFlow ComfyUI/ComfyUI/models/checkpoints/animagine-xl-4.0-opt.safetensors"
```

The result must exactly match the manifest above. Preserve the upstream model license and
notices when redistributing a bundle that contains the weight file.
