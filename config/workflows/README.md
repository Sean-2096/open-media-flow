# ComfyUI API workflows

OpenMediaFlow reads ComfyUI API-format workflow JSON files from this directory:

- `image.json`: cover and still-image generation
- `comic_image.json`: AI comic character sheets and keyframes
- `video.json`: storyboard shot generation

The committed workflows target the project-owned local models:

- `image.json`: SDXL Turbo, one-step cover generation
- `comic_image.json`: Animagine XL 4.0 Opt, 20-step 9:16 anime keyframe generation
- `video.json`: LTX-Video 2B 0.9.8 distilled + FP8 T5, 24 FPS text-to-video fallback
- `video_i2v.json`: same local LTX model, 24 FPS image-to-video shots driven by the generated character reference

The provider defaults to 512x896 native generation (exact 9:16 and divisible by 32).
The compositor is responsible for producing the final platform-resolution video.

Custom workflows can be exported with ComfyUI's **Save (API Format)** and use these
placeholders where appropriate:

- `{{PROMPT}}`
- `{{NEGATIVE_PROMPT}}`
- `{{WIDTH}}`, `{{HEIGHT}}`
- `{{DURATION_SECONDS}}`, `{{FRAME_COUNT}}`
- `{{SEED}}`
- `{{FILENAME_PREFIX}}`

Generated files must land under the configured `COMFYUI_OUTPUT_DIR`, which is mounted
inside the same project `data/inbox` tree consumed by the compositor.
