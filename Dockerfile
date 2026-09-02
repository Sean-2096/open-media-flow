FROM python:3.11-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src ./src
COPY config ./config

RUN --mount=type=cache,target=/root/.cache/pip pip install .

EXPOSE 8000

CMD ["uvicorn", "open_media_flow.api:app", "--host", "0.0.0.0", "--port", "8000"]
