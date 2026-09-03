# Local lip-sync runtime

MuseTalk runtime code, its virtual environment, model weights, and generated caches
are installed below this directory by `scripts/install-lip-sync-runtime.sh`.

Large runtime files are intentionally excluded from Git. OpenMediaFlow exposes the
runtime through its existing native media service rather than requiring a separate
operator-facing application.
