FROM python:3.11-slim

ARG WHISPER_CUDA=0
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HF_HOME=/data/huggingface \
    WHISPER_TOKENS_FILE=/data/tokens.json

WORKDIR /app
COPY pyproject.toml README.md ./
COPY transcriber ./transcriber
COPY whisper_service ./whisper_service

RUN python -m pip install --no-cache-dir '.[server]' \
    && if [ "$WHISPER_CUDA" = "1" ]; then \
         python -m pip install --no-cache-dir 'nvidia-cublas-cu12' 'nvidia-cudnn-cu12==9.*'; \
       fi \
    && mkdir -p /data \
    && chown -R 10001:10001 /data

ENV LD_LIBRARY_PATH=/usr/local/lib/python3.11/site-packages/nvidia/cublas/lib:/usr/local/lib/python3.11/site-packages/nvidia/cudnn/lib
COPY docker/entrypoint.sh /usr/local/bin/whisper-entrypoint
RUN chmod 0555 /usr/local/bin/whisper-entrypoint

USER 10001:10001
VOLUME ["/data"]
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=3s --start-period=15m --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/healthz', timeout=2)" || exit 1

ENTRYPOINT ["sh", "/usr/local/bin/whisper-entrypoint"]
CMD ["whisper-server", "serve", "--host", "0.0.0.0", "--port", "8765", "--device", "auto", "--model", "base", "--token-file", "/data/tokens.json", "--max-session-seconds", "300", "--max-concurrent", "1", "--max-starts-per-window", "10", "--rate-window-seconds", "3600"]
