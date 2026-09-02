# Vendored video engine

This directory contains a vendored snapshot of MoneyPrinterTurbo.

- Upstream: https://github.com/harry0703/MoneyPrinterTurbo
- Commit: `0df0ef4ac2d725c79fa53bfe072063ee9cdfba80`
- License: MIT; see `LICENSE`
- Local change preserved: 60 FPS rendering in `app/services/video.py`

The source is vendored so `open-media-flow` is a self-contained repository and does not
depend on a sibling checkout at runtime. Runtime configuration and generated files live under
the repository-level `data/` directory and are not committed.
