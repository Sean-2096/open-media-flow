#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
project_dir="$(cd "${script_dir}/.." && pwd)"

model="${LLM_LOCAL_HF_MODEL:-Qwen/Qwen3-14B-GGUF:Q4_K_M}"
port="${LLM_LOCAL_PORT:-8081}"
context_size="${LLM_LOCAL_CTX_SIZE:-16384}"
export LLAMA_CACHE="${LLAMA_CACHE:-${project_dir}/data/models}"

if ! command -v llama-server >/dev/null 2>&1; then
  echo "llama-server is missing. Install it with: brew install llama.cpp" >&2
  exit 1
fi

exec llama-server \
  -hf "${model}" \
  --host 127.0.0.1 \
  --port "${port}" \
  --ctx-size "${context_size}" \
  --jinja
