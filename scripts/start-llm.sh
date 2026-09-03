#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
project_dir="$(cd "${script_dir}/.." && pwd)"

model="${LLM_LOCAL_HF_MODEL:-Qwen/Qwen3-14B-GGUF:Q4_K_M}"
port="${LLM_LOCAL_PORT:-8081}"
context_size="${LLM_LOCAL_CTX_SIZE:-16384}"
export LLAMA_CACHE="${LLAMA_CACHE:-${project_dir}/data/models}"

llama_server="$(command -v llama-server 2>/dev/null || true)"
if [[ -z "${llama_server}" ]]; then
  for candidate in /opt/homebrew/bin/llama-server /usr/local/bin/llama-server; do
    if [[ -x "${candidate}" ]]; then
      llama_server="${candidate}"
      break
    fi
  done
fi

if [[ -z "${llama_server}" ]]; then
  echo "llama-server is missing. Install it with: brew install llama.cpp" >&2
  exit 1
fi

exec "${llama_server}" \
  -hf "${model}" \
  --host 127.0.0.1 \
  --port "${port}" \
  --ctx-size "${context_size}" \
  --jinja
