FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY api.py ./
COPY workflow.py ./
COPY schemas.py ./
COPY exchange_tools.py ./
COPY refund_tools.py ./
COPY order_tools.py ./
COPY session_store.py ./
COPY interpreter.py ./
COPY sqlite_store.py ./

RUN mkdir -p /data

RUN useradd --create-home --uid 10001 --shell /bin/bash appuser
RUN chown -R 10001:10001 /data
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=5).read()"

CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
