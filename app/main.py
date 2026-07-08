"""
SoccerScope — Web backend (FastAPI)

Backend for the custom Web UI.
  - Runs the existing ADK agent (soccer_agent.agent.root_agent) through the ADK Runner.
  - Reflects {query, format, lang} received from the frontend in the output format
    (report / sns / webpage) and output language (ja / en) by injecting instructions
    into the prompt without modifying the agent itself.
  - Also serves the static frontend (static/index.html) from the same origin.

The agent (agent.py) is left unchanged. It has no write operations, and reads are
performed by search_videos inside the agent through the official MongoDB MCP
(maintaining the MCP integration requirement).

Local run:
    uvicorn main:app --host 0.0.0.0 --port 8080
Cloud Run:
    Dockerfile included (Python + Node 22). See README.md.
"""

import os
import uuid

# --- Load .env. agent.py reads MONGODB_URI at import time, so this must happen
#     before importing the agent. It is harmless on Cloud Run even when .env is absent. ---
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

from soccer_agent.agent import root_agent  # noqa: E402  (import after loading .env)

APP_NAME = "soccerscope"

# ② Rate limit (per IP, in memory)
limiter = Limiter(key_func=get_remote_address)

# ① Query length limit (characters)
QUERY_MAX_LEN = 500

# Build Runner / Session only once at startup (reuse root_agent).
_session_service = InMemorySessionService()
_runner = Runner(agent=root_agent, app_name=APP_NAME, session_service=_session_service)


# --- Convert frontend options into instruction text for the agent. -------------
# Explicitly selects the article / SNS / HTML generation flows already present on
# the agent INSTRUCTION side from here. The agent itself is not modified.
FORMAT_DIRECTIVES = {
    # Report: Markdown article (country sections + overall synthesis)
    "report": (
        "OUTPUT FORMAT = REPORT. Produce a complete Markdown article exactly as "
        "described in your COMPOSING ARTICLES flow: a punchy title and short lead, "
        "one section per country (country name + flag, a 1-2 sentence buzz summary, "
        "the thumbnail as a Markdown image, and a [watch] link), sentiment where "
        "available, and a closing insightful synthesis (overall comment). "
        "Output Markdown only — do NOT wrap it in a code block, do NOT output raw HTML."
    ),
    # SNS: 2-3 X post drafts
    "sns": (
        "OUTPUT FORMAT = SNS POSTS. Output 2-3 short, ready-to-post social/X drafts "
        "based on the buzzing videos. Each draft: punchy, 1-2 relevant hashtags, and "
        "exactly one video link. Separate each draft with a blank line and prefix it "
        "with its number (1. / 2. / 3.). Do not add an article or extra commentary "
        "around the drafts — output the posts only."
    ),
    # Web page: return the same Markdown article as the report (the frontend styles it as a page).
    "webpage": (
        "OUTPUT FORMAT = WEB FEATURE PAGE. Produce a complete, shareable Markdown "
        "feature article as in your COMPOSING ARTICLES flow (title + lead, one section "
        "per country with flag + thumbnail image + watch link + sentiment, and a strong "
        "closing overall comment). Make it engaging and presentation-ready. "
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
    """Run the agent with one session per request and return the final response text."""
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
        # Aggregate only the final response text (ignore intermediate tool-call events).
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

# ② Connect slowapi to the app.
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],     # In production, this may be restricted to the submission URL origin.
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/healthz")
async def healthz():
    return {"status": "ok", "agent": root_agent.name}


@app.post("/api/generate")
@limiter.limit("3/minute")          # ② Up to 3 requests per minute per IP.
async def generate(req: GenerateRequest, request: Request):
    # ① Check query length.
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


# Static frontend (mount last: the API routes above take precedence).
app.mount("/", StaticFiles(directory="static", html=True), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
