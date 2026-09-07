FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
COPY hr_callback.py .
COPY hr_internal.py .
COPY hr_readonly.py .
ENV PORT=8080
CMD ["sh", "-c", "uvicorn app:api --host 0.0.0.0 --port ${PORT}"]
