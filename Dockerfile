FROM python:3.8-slim

WORKDIR /app

COPY requirements.txt /app/requirements.txt
COPY config.yaml /app/config.yaml
COPY rules /app/rules
COPY elastalert /app/elastalert

RUN pip install --no-cache-dir -r requirements.txt

CMD ["python", "-m", "elastalert.elastalert", "--config", "/app/config.yaml"]
