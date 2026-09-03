#!/bin/sh
set -eu

project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$project_dir"

if [ -f .env ]; then
  set -a
  . ./.env
  set +a
fi

export OMF_NATIVE_AUDIO_DIR="$project_dir/data/inbox/generated/audio"
export OMF_NATIVE_MEDIA_ROOT="$project_dir/data/inbox"
export OMF_NATIVE_INTERPOLATION_DIR="$project_dir/data/inbox/generated/interpolated"
export OMF_NATIVE_LIP_SYNC_DIR="$project_dir/data/inbox/generated/lip-sync"
export OMF_MUSETALK_RUNTIME="${OMF_MUSETALK_RUNTIME:-$project_dir/data/lip-sync/musetalk-mac}"
export OMF_MUSETALK_BASE_URL="${OMF_MUSETALK_BASE_URL:-http://127.0.0.1:8091}"
export OMF_TTS_MODEL_PATH="${OMF_TTS_MODEL_PATH:-$project_dir/data/models/qwen3-tts-1.7b-customvoice-8bit}"
export OMF_RIFE_WEIGHTS_DIR="${OMF_RIFE_WEIGHTS_DIR:-$project_dir/data/models/rife-4.25}"
export PYTHONPATH="$project_dir/src${PYTHONPATH:+:$PYTHONPATH}"

runtime_python="$project_dir/.venv-media/bin/python"
if [ ! -x "$runtime_python" ]; then
  echo "Missing .venv-media. Run: ./scripts/install-media-runtime.sh" >&2
  exit 1
fi

exec "$runtime_python" -m uvicorn open_media_flow.local_media_runtime:app \
  --host 0.0.0.0 \
  --port "${LOCAL_MEDIA_RUNTIME_PORT:-8090}"
