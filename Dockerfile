FROM python:3.11-slim

# Install Node.js 20
RUN apt-get update && apt-get install -y curl gnupg && \
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y nodejs && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python deps (own layer for cache)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Dashboard deps (own layer for cache)
COPY dashboard/package.json dashboard/package-lock.json ./dashboard/
RUN cd dashboard && npm ci --prefer-offline

# Copy source
COPY . .

# Build dashboard — VITE_SHOPIFY_API_KEY is the public Shopify app client_id,
# not a secret (it's embedded in HTML and visible to anyone). Lint warning is
# a false positive; the value is safe to pass as a build arg.
ARG VITE_SHOPIFY_API_KEY
ENV VITE_SHOPIFY_API_KEY=$VITE_SHOPIFY_API_KEY
RUN cd dashboard && npm run build

EXPOSE 8000

COPY start.sh .
RUN chmod +x start.sh

CMD ["./start.sh"]
