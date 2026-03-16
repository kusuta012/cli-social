FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y \
    gcc \
    python3-dev \
    libssl-dev \
    && rm -rf /var/lib/apt/lists/*
COPY . .
RUN pip install --no-cache-dir .
ENTRYPOINT ["sxcl"]
CMD ["node"]