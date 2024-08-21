FROM python:3.8-slim

WORKDIR /app

COPY requirements.txt /app/requirements.txt
COPY config.yaml /app/config.yaml
COPY rules /app/rules
COPY elastalert /app/elastalert

RUN apt-get update && apt-get install -y gcc build-essential

RUN pip install --no-cache-dir -r requirements.txt


CMD ["python3.8", "-m", "elastalert.elastalert", "--config", "/app/config.yaml","--verbose"]
