# SoccerScope Web — Cloud Run 用イメージ
# agent.py が公式MongoDB MCP を `npx mongodb-mcp-server` で起動するため、
# Python と Node.js v22 の両方が要る（v18 は styleText 未対応で起動失敗）。

FROM python:3.11-slim

# --- Node.js v22（NodeSource）---
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# --- MCP サーバを事前取得（リクエスト時の npx ダウンロードを無くす）---
# NPM_CONFIG_PREFER_OFFLINE で npx もキャッシュ優先になり、実行時のレジストリ参照を避ける。
ENV NPM_CONFIG_PREFER_OFFLINE=true
RUN npm install -g mongodb-mcp-server@latest \
    && npx -y mongodb-mcp-server --version || true

WORKDIR /app

# --- Python 依存 ---
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# --- アプリ ---
COPY . .

# Cloud Run は $PORT を渡してくる（既定 8080）
ENV PORT=8080
EXPOSE 8080

# main.py 内の uvicorn 起動を使う（$PORT を尊重）
CMD ["python", "main.py"]
