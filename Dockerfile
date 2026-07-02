FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# --chmod: some source files are 600 locally; the app runs as uid 1000 and
# must be able to read them
COPY --chmod=644 *.py ./
COPY --chmod=755 templates ./templates
# run unprivileged; /data (mounted volume) holds config + secrets and is the
# only writable path the app needs
RUN useradd -r -u 1000 -m app && mkdir -p /data && chown app /data
USER app
EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--no-server-header"]
