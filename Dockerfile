FROM python:3.13-slim
WORKDIR /app
COPY requirements.lock.txt .
RUN pip install --no-cache-dir -r requirements.lock.txt
COPY app ./app
COPY skills ./skills
COPY scripts ./scripts
RUN mkdir -p /app/data && useradd --uid 10001 --create-home worker && chown -R worker:worker /app
USER worker
EXPOSE 8080
CMD ["python", "-m", "uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080"]
