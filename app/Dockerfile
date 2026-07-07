# SoccerScope Web — image for Cloud Run
# Because agent.py starts the official MongoDB MCP via `npx mongodb-mcp-server`,
# both Python and Node.js v22 are required (v18 does not support styleText and fails to start).

FROM python:3.11-slim

# --- Node.js v22 (NodeSource) ---
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# --- Pre-fetch the MCP server to avoid npx downloads during requests ---
# NPM_CONFIG_PREFER_OFFLINE also makes npx prefer the cache and avoids runtime registry lookups.
ENV NPM_CONFIG_PREFER_OFFLINE=true
RUN npm install -g mongodb-mcp-server@latest \
    && npx -y mongodb-mcp-server --version || true

WORKDIR /app

# --- Python dependencies ---
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# --- App ---
COPY . .

# Cloud Run passes $PORT (default: 8080)
ENV PORT=8080
EXPOSE 8080

# Use the uvicorn startup in main.py (respects $PORT)
CMD ["python", "main.py"]
