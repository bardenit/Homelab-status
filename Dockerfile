FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY *.py ./
COPY templates ./templates
# admin UI persists config (hosts, tokens, admin password) here; mount a volume
RUN mkdir -p /data
EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
