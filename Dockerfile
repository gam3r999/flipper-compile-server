FROM python:3.11-slim

WORKDIR /app

# Install build tools needed by ufbt
RUN apt-get update && apt-get install -y \
    git curl wget unzip tar \
    gcc g++ make \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

EXPOSE 8080

CMD ["python3", "server.py"]
