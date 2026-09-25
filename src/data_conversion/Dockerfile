FROM python:3.13-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        libreoffice \
        git \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

# Check if libreoffice installed
RUN which soffice || which libreoffice

WORKDIR /app

CMD ["/bin/bash"]