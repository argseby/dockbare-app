FROM python:3.11-slim

RUN apt-get update && \
    apt-get install -y gcc libffi-dev libssl-dev curl docker.io && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . /app

EXPOSE 5000

CMD ["python", "app.py"]
