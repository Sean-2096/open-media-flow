#!/bin/sh
set -eu

project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$project_dir"

uv_bin="${UV_BIN:-/opt/homebrew/bin/uv}"
"$uv_bin" venv .venv-media --python 3.12
"$uv_bin" pip install --python .venv-media/bin/python \
  'mlx-audio[server]' \
  'git+https://github.com/xocialize/rife-mlx.git'

.venv-media/bin/python -c \
  "from huggingface_hub import snapshot_download; snapshot_download('mlx-community/Qwen3-TTS-12Hz-1.7B-CustomVoice-8bit', local_dir='data/models/qwen3-tts-1.7b-customvoice-8bit')"
.venv-media/bin/python -c \
  "from huggingface_hub import snapshot_download; snapshot_download('mlx-community/RIFE-4.25', local_dir='data/models/rife-4.25')"

echo "Native media runtime installed. Start it with ./scripts/start-media-runtime.sh"
