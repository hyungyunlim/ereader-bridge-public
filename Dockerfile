FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*  # audiobook clips
RUN pip install --no-cache-dir aiosmtpd==1.4.6
WORKDIR /app
COPY bridge.py textsync.py audiosync.py ./
COPY koreader ./koreader
COPY web ./web
ENV PYTHONUNBUFFERED=1
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request,os;urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"HTTP_PORT\",\"8086\")}/health',timeout=3)"
CMD ["python", "bridge.py"]
