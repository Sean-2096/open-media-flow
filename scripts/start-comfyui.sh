#!/bin/sh
set -eu

project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
runtime_dir="$project_dir/data/comfyui/OpenMediaFlow ComfyUI/ComfyUI"
python_bin="$runtime_dir/.venv/bin/python"
port="${COMFYUI_PORT:-8188}"

if curl -fsS "http://127.0.0.1:$port/system_stats" >/dev/null 2>&1; then
  echo "ComfyUI is already running at http://127.0.0.1:$port"
  exit 0
fi

if [ ! -x "$python_bin" ]; then
  echo "ComfyUI runtime is not installed under data/comfyui." >&2
  echo "Complete the Comfy Desktop local MPS setup first." >&2
  exit 1
fi

mkdir -p \
  "$project_dir/data/comfyui/input" \
  "$project_dir/data/comfyui/user" \
  "$project_dir/data/inbox/generated/comfyui"

cd "$runtime_dir"
exec "$python_bin" main.py \
  --listen 0.0.0.0 \
  --port "$port" \
  --output-directory "$project_dir/data/inbox/generated/comfyui" \
  --input-directory "$project_dir/data/comfyui/input" \
  --user-directory "$project_dir/data/comfyui/user" \
  --use-split-cross-attention \
  --disable-auto-launch
