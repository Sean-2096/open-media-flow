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
exec .venv/bin/uvicorn open_media_flow.local_media_runtime:app \
  --host 0.0.0.0 \
  --port "${LOCAL_MEDIA_RUNTIME_PORT:-8090}"
