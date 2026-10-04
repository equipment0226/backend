FROM python:3.11-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends fonts-nanum libgl1 libglib2.0-0 && rm -rf /var/lib/apt/lists/*
COPY requirements.txt requirements-ocr.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY scripts/setup_ocr.py scripts/setup_ocr.py
ARG ENABLE_KOREAN_OCR=0
RUN if [ "$ENABLE_KOREAN_OCR" = "1" ]; then pip install --no-cache-dir -r requirements-ocr.txt && python scripts/setup_ocr.py --skip-install; fi
COPY apps/api ./apps/api
COPY data ./data
COPY examples ./examples
ENV DEBTOFF_DEMO_MODE=0 DEBTOFF_START_PROFILE=fresh DEBTOFF_DATA_DIR=/data DEBTOFF_CORPUS_DIR=/data/corpus PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["sh", "-c", "uvicorn apps.api.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]
