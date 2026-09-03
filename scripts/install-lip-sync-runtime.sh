#!/bin/sh
set -eu

project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
runtime_dir="$project_dir/data/lip-sync/musetalk-mac"
repo_url="https://github.com/barnent1/musetalk-mac.git"
official_dir="$project_dir/data/lip-sync/musetalk-official"
official_repo_url="https://github.com/TMElyralab/MuseTalk.git"
constraints="$project_dir/config/lip-sync-constraints-mac.txt"
runtime_patch="$project_dir/config/musetalk-mac-m1.patch"

if [ ! -d "$runtime_dir/.git" ]; then
  mkdir -p "$project_dir/data/lip-sync"
  git clone --depth 1 "$repo_url" "$runtime_dir"
fi

# The current Mac port omits the upstream `musetalk/models` package. Restore it
# from Tencent's official repository so a clean installation is reproducible.
if [ ! -f "$runtime_dir/upstream/musetalk/models/vae.py" ]; then
  if [ ! -d "$official_dir/.git" ]; then
    git clone --depth 1 "$official_repo_url" "$official_dir"
  fi
  cp -R "$official_dir/musetalk/models" "$runtime_dir/upstream/musetalk/models"
fi
mkdir -p "$runtime_dir/upstream/data/demo_five"

if grep -q "batch_size: int = 16" "$runtime_dir/server.py"; then
  (cd "$runtime_dir" && patch -p1 < "$runtime_patch")
fi

if [ ! -x "$runtime_dir/.venv/bin/python" ]; then
  /opt/homebrew/bin/python3.11 -m venv "$runtime_dir/.venv"
fi

"$runtime_dir/.venv/bin/python" -m pip install --upgrade pip
"$runtime_dir/.venv/bin/pip" install \
  --constraint "$constraints" \
  --requirement "$runtime_dir/requirements-mac.txt"

if [ ! -f "$runtime_dir/upstream/models/musetalkV15/unet.pth" ]; then
  PATH="$runtime_dir/.venv/bin:$PATH"
  export PATH
  "$runtime_dir/download_weights_mac.sh"
fi

echo "MuseTalk Apple Silicon runtime installed under data/lip-sync."
