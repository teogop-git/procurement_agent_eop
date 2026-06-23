FROM python:3.11-slim

WORKDIR /app

# Системни зависимости за Python пакети + Playwright Chromium
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libxml2-dev libxslt-dev \
    antiword \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libxkbcommon0 libxcomposite1 \
    libxdamage1 libxfixes3 libxrandr2 libgbm1 \
    libasound2 libpango-1.0-0 libcairo2 libxshmfence1 \
    libx11-6 libxcb1 libxext6 fonts-liberation \
    wget ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Инсталирай само Chromium (без --with-deps, системните са вече горе)
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN playwright install chromium

COPY main.py .
COPY modules/ ./modules/

RUN mkdir -p /app/logs /app/output/downloads /app/output/reports

ENV PYTHONUNBUFFERED=1

CMD ["python", "main.py"]
