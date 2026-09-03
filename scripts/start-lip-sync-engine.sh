#!/bin/sh
set -eu

project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
runtime_dir="${OMF_MUSETALK_RUNTIME:-$project_dir/data/lip-sync/musetalk-mac}"
python_bin="$runtime_dir/.venv/bin/python"
model="$runtime_dir/upstream/models/musetalkV15/unet.pth"

if [ ! -x "$python_bin" ] || [ ! -f "$model" ]; then
  echo "MuseTalk runtime is incomplete. Run: ./scripts/install-lip-sync-runtime.sh" >&2
  exit 1
fi

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
export PYTORCH_ENABLE_MPS_FALLBACK=1
export GLOG_minloglevel=2
export MUSETALK_FP16_VAE="${MUSETALK_FP16_VAE:-1}"
export MUSETALK_FP16_UNET="${MUSETALK_FP16_UNET:-0}"
export MUSETALK_BATCH_SIZE="${MUSETALK_BATCH_SIZE:-4}"
export MUSETALK_EXTRA_MARGIN="${MUSETALK_EXTRA_MARGIN:-6}"
export MUSETALK_PARSING_MODE="${MUSETALK_PARSING_MODE:-jaw}"
export MUSETALK_LEFT_CHEEK_WIDTH="${MUSETALK_LEFT_CHEEK_WIDTH:-60}"
export MUSETALK_RIGHT_CHEEK_WIDTH="${MUSETALK_RIGHT_CHEEK_WIDTH:-60}"
export MUSETALK_UPPER_BOUNDARY_RATIO="${MUSETALK_UPPER_BOUNDARY_RATIO:-0.55}"

cd "$runtime_dir"
exec "$python_bin" -m uvicorn server:app \
  --host 127.0.0.1 \
  --port "${OMF_MUSETALK_PORT:-8091}"
