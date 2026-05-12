FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY mono_sync ./mono_sync

ENV PYTHONUNBUFFERED=1
VOLUME ["/data"]

ENTRYPOINT ["python", "-m", "mono_sync"]
