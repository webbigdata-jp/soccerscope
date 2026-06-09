# SoccerScope — 独自Web UI（ライブ・エージェント）

各国で“今バズっている”サッカー動画を意味検索で横断的に集め、**レポート / SNS投稿 / Webページ**
として書き出す ADK エージェントに、独自フロント（英日切替・自由入力フォーム・出力形式選択）を
被せたもの。`adk web`（ローカル開発用）ではなく、**自前の FastAPI バックエンド**でエージェントを
配信し、提出物URLとして Cloud Run に出す構成。

```
[ブラウザ / static/index.html]                         [FastAPI: main.py]
  ・右上 EN/JA トグル                       POST /api/generate
  ・自由入力フォーム            ───────────▶  └ ADK Runner で root_agent を実行
  ・出力形式 Report/SNS/Web                       └ search_videos → 公式MongoDB MCP($vectorSearch)
  ・結果を即レンダリング        ◀───────────       └ find/count（詳細・件数）
                                                   ▼
                                       MongoDB Atlas  soccertube.videos (768次元)
```

- 出力形式と言語は、エージェント本体（`soccer_agent/agent.py`）を**無改変**のまま、
  サーバ側（`main.py`）で**プロンプトに指示を注入**して切り替える。
- 読み出しはすべて公式MongoDB MCP経由（MCP統合要件を維持）。書き込み（日次更新バッチ）は別系統。

## 構成ファイル

```
soccerscope-app/
├── main.py                  FastAPI: /api/generate ＋ 静的配信
├── soccer_agent/
│   ├── __init__.py          root_agent を export
│   ├── agent.py             既存 v1（無改変）
│   └── .env.example         GOOGLE_API_KEY / MONGODB_URI
├── static/
│   └── index.html           独自フロント（英日切替・フォーム・出力形式）
├── requirements.txt
├── Dockerfile               Python + Node 22（npx で MCP 起動するため）
└── README.md
```

## ローカル実行

前提: Python 3.10+ / Node.js v20.19+ または v22+（`node --version`）。

```bash
cd soccerscope-app
python3 -m venv venv && . venv/bin/activate
pip install -r requirements.txt

cp soccer_agent/.env.example soccer_agent/.env
# soccer_agent/.env を編集: GOOGLE_API_KEY と MONGODB_URI を記入

python main.py
# → http://localhost:8080 を開く
```

初回の生成は `npx` が `mongodb-mcp-server` を取得・起動するため数十秒かかることがある
（`agent.py` 側で timeout=120 を設定済み）。

## Cloud Run デプロイ

`.env` は使わず、環境変数はデプロイ時に渡す。

```bash
gcloud run deploy soccerscope \
  --source . \
  --region asia-northeast1 \
  --allow-unauthenticated \
  --memory 1Gi \
  --timeout 300 \
  --set-env-vars "GOOGLE_API_KEY=xxxx,GOOGLE_GENAI_USE_VERTEXAI=FALSE,MONGODB_URI=mongodb+srv://...."
```

メモ:
- `--timeout 300`：初回 MCP コールドスタート＋生成に余裕を持たせる。
- `--memory 1Gi`：Node プロセス（MCP）＋ Python の同居分。足りなければ増やす。
- 機微情報（APIキー / 接続文字列）は Secret Manager 経由が望ましい
  （`--set-secrets` を使用）。
- Atlas 側のネットワークアクセス（IP許可リスト）に Cloud Run からの egress を許可しておく
  （簡易には 0.0.0.0/0、本番は VPC コネクタ＋固定IP）。

## API

```
POST /api/generate
  { "query": "...", "format": "report|sns|webpage", "lang": "ja|en" }
→ { "format": "...", "lang": "...", "content": "<markdown or posts>" }

GET /healthz → {"status":"ok","agent":"soccer_agent"}
```

## TODO（後日）

- W杯期間中の**日次データ更新**：Cloud Scheduler → Cloud Run Job（既存の取り込み＋
  `gemini-embedding-001`＋pymongo upsert パイプライン）を cron 実行。本リポジトリに `batch/` として追加予定。
