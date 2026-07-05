"""
SoccerScope — Web backend (FastAPI)

独自Web UI のためのバックエンド。
  - 既存の ADK エージェント（soccer_agent.agent.root_agent）を ADK Runner で実行する。
  - フロントから受け取る {query, format, lang} を、エージェント本体を改変せずに
    「プロンプトへ指示を注入」する形で出力形式（report / sns / webpage）と
    出力言語（ja / en）に反映する。
  - 同一オリジンで静的フロント（static/index.html）も配信する。

エージェント(agent.py)は無改変。書き込み系は持たず、読み出しは agent 内の
search_videos → 公式MongoDB MCP 経由（MCP統合要件を維持）。

ローカル実行:
    uvicorn main:app --host 0.0.0.0 --port 8080
Cloud Run:
    Dockerfile 同梱（Python + Node 22）。README.md 参照。
"""

import os
import uuid

# --- .env を読み込む（agent.py はインポート時に MONGODB_URI を参照するので、
#     エージェント取り込みより前に読み込む。Cloud Run では .env が無くても無害）---
try:
    from dotenv import load_dotenv

    _here = os.path.dirname(os.path.abspath(__file__))
    load_dotenv(os.path.join(_here, ".env"))
    load_dotenv(os.path.join(_here, "soccer_agent", ".env"))
except Exception:  # noqa: BLE001
    pass

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from google.genai import types
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService

from soccer_agent.agent import root_agent  # noqa: E402  (.env 読み込み後にimport)

APP_NAME = "soccerscope"

# ② レートリミット（IP別、インメモリ）
limiter = Limiter(key_func=get_remote_address)

# ① クエリ長上限（文字数）
QUERY_MAX_LEN = 500

# Runner / Session は起動時に一度だけ構築（root_agent は使い回す）
_session_service = InMemorySessionService()
_runner = Runner(agent=root_agent, app_name=APP_NAME, session_service=_session_service)


# --- フロントから来る選択肢を、エージェントへの「指示文」に変換する ----------
# エージェントの INSTRUCTION 側に既にある記事/SNS/HTML生成フローを、ここから
# 明示的に呼び分ける。エージェント本体は触らない。
FORMAT_DIRECTIVES = {
    # レポート: マークダウン記事（国別セクション＋総合コメント）
    "report": (
        "OUTPUT FORMAT = REPORT. Produce a complete Markdown article exactly as "
        "described in your COMPOSING ARTICLES flow: a punchy title and short lead, "
        "one section per country (country name + flag, a 1-2 sentence buzz summary, "
        "the thumbnail as a Markdown image, and a [watch] link), sentiment where "
        "available, and a closing insightful synthesis (総合コメント). "
        "Output Markdown only — do NOT wrap it in a code block, do NOT output raw HTML."
    ),
    # SNS: X投稿ドラフト 2-3本
    "sns": (
        "OUTPUT FORMAT = SNS POSTS. Output 2-3 short, ready-to-post social/X drafts "
        "based on the buzzing videos. Each draft: punchy, 1-2 relevant hashtags, and "
        "exactly one video link. Separate each draft with a blank line and prefix it "
        "with its number (1. / 2. / 3.). Do not add an article or extra commentary "
        "around the drafts — output the posts only."
    ),
    # Webページ: レポートと同じマークダウン記事を返す（見た目はフロントで“ページ風”に整える）
    "webpage": (
        "OUTPUT FORMAT = WEB FEATURE PAGE. Produce a complete, shareable Markdown "
        "feature article as in your COMPOSING ARTICLES flow (title + lead, one section "
        "per country with flag + thumbnail image + watch link + sentiment, and a strong "
        "closing 総合コメント). Make it engaging and presentation-ready. "
        "Output Markdown only — do NOT output raw HTML or a code block."
    ),
}

LANG_DIRECTIVES = {
    "ja": "LANGUAGE = JAPANESE. Write the entire output in natural Japanese.",
    "en": (
        "LANGUAGE = ENGLISH. Write the entire output in natural English suitable for "
        "an international (US) audience. Use English country names. Translate any "
        "Japanese titles/quotes, but keep original video titles recognizable."
    ),
}


def _build_prompt(query: str, fmt: str, lang: str) -> str:
    fmt_d = FORMAT_DIRECTIVES.get(fmt, FORMAT_DIRECTIVES["report"])
    lang_d = LANG_DIRECTIVES.get(lang, LANG_DIRECTIVES["ja"])
    return (
        f"{query.strip()}\n\n"
        f"--- DELIVERY INSTRUCTIONS (follow strictly) ---\n"
        f"{fmt_d}\n{lang_d}\n"
        f"Use ONLY data returned by your tools (search_videos / find / count). "
        f"Never invent videos, stats, or quotes."
    )


async def _run_agent(prompt: str) -> str:
    """1リクエスト = 1セッションでエージェントを実行し、最終応答テキストを返す。"""
    user_id = "web"
    session_id = uuid.uuid4().hex
    await _session_service.create_session(
        app_name=APP_NAME, user_id=user_id, session_id=session_id
    )
    content = types.Content(role="user", parts=[types.Part(text=prompt)])

    chunks: list[str] = []
    async for event in _runner.run_async(
        user_id=user_id, session_id=session_id, new_message=content
    ):
        # 最終応答のテキストのみ集約（途中のツール呼び出しイベントは無視）
        if event.is_final_response() and getattr(event, "content", None):
            for part in (event.content.parts or []):
                if getattr(part, "text", None):
                    chunks.append(part.text)
    return "".join(chunks).strip()


# --- API ---------------------------------------------------------------------
class GenerateRequest(BaseModel):
    query: str
    format: str = "report"   # report | sns | webpage
    lang: str = "ja"         # ja | en


app = FastAPI(title="SoccerScope")

# ② slowapi をアプリに接続
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],     # 本番は提出URLのオリジンに絞ってよい
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/healthz")
async def healthz():
    return {"status": "ok", "agent": root_agent.name}


@app.post("/api/generate")
@limiter.limit("3/minute")          # ② IP別 1分間に3リクエストまで
async def generate(req: GenerateRequest, request: Request):
    # ① クエリ長チェック
    if len(req.query) > QUERY_MAX_LEN:
        raise HTTPException(
            status_code=400,
            detail=f"query too long (max {QUERY_MAX_LEN} chars, got {len(req.query)})",
        )
    if not req.query.strip():
        raise HTTPException(status_code=400, detail="query is empty")
    if req.format not in FORMAT_DIRECTIVES:
        raise HTTPException(status_code=400, detail=f"unknown format: {req.format}")
    if req.lang not in LANG_DIRECTIVES:
        raise HTTPException(status_code=400, detail=f"unknown lang: {req.lang}")

    prompt = _build_prompt(req.query, req.format, req.lang)
    try:
        content = await _run_agent(prompt)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"agent error: {e}")

    if not content:
        raise HTTPException(status_code=502, detail="agent returned empty output")

    return {"format": req.format, "lang": req.lang, "content": content}


# 静的フロント（最後にマウント：上の API ルートが優先される）
app.mount("/", StaticFiles(directory="static", html=True), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
