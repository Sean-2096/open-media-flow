FROM python:3.11-slim

WORKDIR /MoneyPrinterTurbo

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN --mount=type=cache,target=/root/.cache/pip pip install -r requirements.txt

COPY app ./app
COPY resource ./resource
COPY webui ./webui
COPY main.py cli.py config.example.toml ./
COPY docker-entrypoint.sh /usr/local/bin/video-engine-entrypoint

RUN chmod +x /usr/local/bin/video-engine-entrypoint \
    && ln -s /runtime/config.toml /MoneyPrinterTurbo/config.toml

EXPOSE 8080 8501

ENTRYPOINT ["video-engine-entrypoint"]
CMD ["python", "main.py"]
