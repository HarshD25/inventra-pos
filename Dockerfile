FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN useradd -m app && mkdir -p /app/instance && chown -R app /app
USER app
ENV PORT=5000 DATABASE_PATH=/app/instance/inventra.db
EXPOSE 5000
HEALTHCHECK CMD python -c "import urllib.request;urllib.request.urlopen('http://localhost:5000/healthz')" || exit 1
CMD ["sh", "-c", "gunicorn app:app --workers 2 --bind 0.0.0.0:${PORT}"]
