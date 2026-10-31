FROM python:3.12-slim

RUN pip install --no-cache-dir requests base58 mnemonic solders==0.21.0

WORKDIR /app
COPY src/ /app/src/
COPY scripts/ /app/scripts/
RUN chmod +x /app/scripts/*.sh

ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["python", "/app/src/scheduler_probe.py"]
