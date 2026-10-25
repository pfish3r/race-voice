FROM python:3.12-slim
WORKDIR /app
COPY server/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY relay.py admin.py metrics.py ./
COPY dashboard ./dashboard
USER 65534:65534
CMD ["python", "relay.py"]
