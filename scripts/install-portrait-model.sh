#!/bin/sh
set -eu

project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
checkpoint_dir="$project_dir/data/comfyui/OpenMediaFlow ComfyUI/ComfyUI/models/checkpoints"
filename="RealVisXL_V5.0_Lightning_fp16.safetensors"
destination="$checkpoint_dir/$filename"
partial="$destination.part"
url="https://huggingface.co/SG161222/RealVisXL_V5.0_Lightning/resolve/main/$filename"
expected_sha256="fabcadd9330dcc4f9702063428d40b9d4d07168d8acefc819b8d1d9db466b3ec"

mkdir -p "$checkpoint_dir"

if [ -f "$destination" ]; then
  actual=$(shasum -a 256 "$destination" | awk '{print $1}')
  if [ "$actual" = "$expected_sha256" ]; then
    echo "RealVisXL portrait model is already installed and verified."
    exit 0
  fi
  echo "Existing RealVisXL file failed SHA256 verification." >&2
  exit 1
fi

echo "Downloading RealVisXL V5.0 Lightning FP16 (6.94 GB)..."
curl --fail --location --retry 5 --retry-delay 3 --continue-at - \
  --progress-bar --output "$partial" "$url"

actual=$(shasum -a 256 "$partial" | awk '{print $1}')
if [ "$actual" != "$expected_sha256" ]; then
  echo "SHA256 mismatch: expected $expected_sha256, got $actual" >&2
  exit 1
fi

mv "$partial" "$destination"
echo "Installed and verified: $filename"
