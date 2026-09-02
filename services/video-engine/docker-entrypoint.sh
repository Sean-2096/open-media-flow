#!/bin/sh
set -eu

mkdir -p /runtime /MoneyPrinterTurbo/storage/local_videos /MoneyPrinterTurbo/storage/tasks

if [ ! -f /runtime/config.toml ]; then
  cp /MoneyPrinterTurbo/config.example.toml /runtime/config.toml
fi

exec "$@"
