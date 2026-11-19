FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DB_PATH=/app/data/exam.db
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY static ./static

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/api/health')"
# 只開 1 個 worker：資料庫放在 Docker Desktop（macOS）掛載的資料夾時，多個行程共用 SQLite WAL
# 的 -shm 記憶體對映會不同步，高並發下資料庫會損毀。單一行程實測 100 人同時作答交卷沒問題。
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
