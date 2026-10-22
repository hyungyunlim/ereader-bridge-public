FROM python:3.12-slim
RUN pip install --no-cache-dir aiosmtpd==1.4.6
WORKDIR /app
COPY bridge.py textsync.py ./
COPY koreader ./koreader
COPY web ./web
ENV PYTHONUNBUFFERED=1
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request,os;urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"HTTP_PORT\",\"8086\")}/health',timeout=3)"
CMD ["python", "bridge.py"]
