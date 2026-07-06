#!/usr/bin/env python3
"""
Phase 5, stage 1: Analyze comment sentiment and soccer relevance, then save locally.

For each video in phase4_comments_*.json, this script analyzes the title,
description, and comments with Gemini 3.1 Flash-Lite. It generates soccer
relevance (is_soccer_related), sentiment ratios, positive and negative themes
in Japanese and English, quotable comments with Japanese and English
translations, and team mentions. The result is saved to
comment_analysis_<timestamp>.json.

is_soccer_related is a post-filter for cases where YouTube search.list queries
such as "World Cup" also match other sports events, such as cricket or
basketball World Cups. 4_load_comment_analysis.py uses this flag at load time
and removes false videos from MongoDB.

This is the first half of the two-stage pipeline. This is the only script that
calls the Gemini API. Re-running the MongoDB load step (stage 2) will not
consume additional API quota.

Design:
  - One video equals one request. Structured output with response_schema keeps
    the response reliably JSON-compatible.
  - Up to 100 comments are sent, sorted by like count descending. Strong buzz
    signals and quotable comments usually appear near the top.
  - is_soccer_related is mainly judged from the title and description, so
    videos with comment errors or zero comments are still analyzed. Only videos
    with no title, and therefore no reliable judgment material, are skipped.
  - Rate-limit errors (429) are retried with exponential backoff.

Setup:
    pip install google-genai pydantic
    export GEMINI_API_KEY='...'

Usage:
    python analyze_comments.py [path_to_phase4_comments_json]
"""

import os
import sys
import glob
import json
import time
from datetime import datetime, timezone

from pydantic import BaseModel
from google import genai
from google.genai import types

MODEL = "gemini-3.1-flash-lite"      # 2026-05 GA; suitable for comment analysis and translation.
MAX_COMMENTS = 100                    # Maximum comments sent to Gemini per video, sorted by likes.
MAX_QUOTES = 3                        # Maximum number of quotable comments.
MAX_TEAMS = 8                         # Maximum number of mentioned national teams.
SLEEP_BETWEEN = 4.0                   # Delay between requests, in seconds. Adjust based on free-tier RPM.
TEMPERATURE = 0.3                     # Low value for stable analysis output.
MAX_OUTPUT_TOKENS = 4096              # Hard output limit to avoid runaway JSON generation.

from dotenv import load_dotenv
from pathlib import Path
SCRIPT_DIR = Path(__file__).parent
load_dotenv(SCRIPT_DIR.parent / 'app' / 'soccer_agent' / '.env')

# ---- Structured output schema, compatible with the handoff comment_analysis format. ----
class Sentiment(BaseModel):
    positive: float   # Percentage. The three values are expected to total about 100.
    negative: float
    neutral: float


class Theme(BaseModel):
    theme_ja: str
    theme_en: str
    mention_count: int


class QuotableComment(BaseModel):
    original: str
    translated_ja: str
    translated_en: str
    author: str
    likes: int
    original_language: str


class TeamMention(BaseModel):
    team: str          # Normalized English national team name, such as "Argentina" or "Japan".
    sentiment: str     # Overall sentiment toward this team: "positive", "neutral", or "negative".
    mention_count: int  # Approximate number of comments that mention this team.


class CommentAnalysis(BaseModel):
    is_soccer_related: bool   # Whether the video is related to soccer, including the FIFA World Cup.
    relevance_reason: str     # Brief reason for the relevance decision, in Japanese.
    sentiment: Sentiment
    positive_themes: list[Theme]
    negative_themes: list[Theme]
    quotable_comments: list[QuotableComment]
    mentioned_teams: list[TeamMention]


SYSTEM_INSTRUCTION = (
    "You are an expert analyst of YouTube comments about soccer, including the FIFA World Cup. "
    "Analyze multilingual comments and return structured insights about audience sentiment, "
    "discussion topics, and quotable audience reactions."
)


def find_phase4_path() -> str:
    if len(sys.argv) > 1:
        return sys.argv[1]
    hits = sorted(glob.glob("data/phase4_comments_*.json"))
    if not hits:
        print(
            "ERROR: phase4_comments_*.json was not found. Pass the path as an argument.",
            file=sys.stderr,
        )
        sys.exit(1)
    return hits[-1]


def build_prompt(meta: dict, comments: list) -> str:
    """Build the analysis prompt from video metadata and comment lines.

    meta['countries'] uses the phase3 many-to-many format, listing every
    country where the same video appeared. Because one video can appear in
    multiple countries' search results, the prompt lists multiple countries and
    languages when available.

    The is_soccer_related decision is mainly based on the title and description
    so that videos with zero comments or comment-fetch errors can still be
    judged. Comments may be used as supporting evidence when available.
    """
    lines = []
    for c in comments:
        author = c.get("author_display_name", "")
        likes = c.get("like_count", 0)
        text = (c.get("text_original") or "").replace("\n", " ").strip()
        lines.append(f"[likes={likes}] {author}: {text}")
    comments_block = "\n".join(lines) if lines else "(No comments, or comments could not be fetched.)"

    countries = meta.get("countries", []) or []
    country_names_en = [c.get("country_name_en", "") for c in countries if c.get("country_name_en")]
    primary_langs = sorted({c.get("primary_lang", "") for c in countries if c.get("primary_lang")})
    countries_str = ", ".join(country_names_en) if country_names_en else "Unknown"
    langs_str = ", ".join(primary_langs) if primary_langs else "Unknown"

    description = (meta.get("description") or "").strip()
    description_block = description[:500] if description else "(No description.)"

    return (
        f"# Video information\n"
        f"Countries where this video appears to be popular, possibly multiple: {countries_str}\n"
        f"Primary languages of those countries, possibly multiple: {langs_str}\n"
        f"Title: {meta.get('title')}\n"
        f"Description, first 500 characters: {description_block}\n\n"
        f"# Comments ({len(comments)} comments, sorted by like count descending)\n"
        f"{comments_block}\n\n"
        f"# Instructions\n"
        f"Analyze the video information and comments above, then generate the following fields:\n"
        f"0. is_soccer_related: Decide true or false for whether this video is related to soccer, "
        f"including the FIFA World Cup. Use the title and description as the main evidence, and use "
        f"comments as supporting evidence when available. Even if the search keyword contains 'World Cup', "
        f"set this to false when the video is about another sport's World Cup, such as cricket, basketball, "
        f"or volleyball. Example: ICC Women's T20 World Cup is cricket, so it is false. If evidence is "
        f"ambiguous or insufficient, choose true to avoid over-filtering. Write relevance_reason as one "
        f"brief Japanese sentence explaining the decision.\n"
        f"Fill fields 1 through 4 even when is_soccer_related is false. If there are no comments, set "
        f"sentiment to positive=0, negative=0, neutral=100, and use empty arrays for the list fields.\n"
        f"1. sentiment: Percentage ratios for positive, negative, and neutral comments. The total should "
        f"be about 100.\n"
        f"2. positive_themes / negative_themes: Main topics. For each item, provide theme_ja in Japanese, "
        f"theme_en in English, and mention_count as an approximate number of comments that mention it. "
        f"Return up to 5 items for each list.\n"
        f"3. quotable_comments: Up to {MAX_QUOTES} memorable, high-like comments worth quoting in an article. "
        f"For each item, provide original exactly as written, translated_ja in Japanese, translated_en in "
        f"English, author, likes from the input, and original_language as a language code.\n"
        f"4. mentioned_teams: Up to {MAX_TEAMS} national teams mentioned in the comments. Normalize team to "
        f"the English national team or country name for cross-language aggregation, such as 'Argentina', "
        f"'Brazil', 'Japan', or 'Morocco'. Do not use local-language names, flag emojis, or club names. "
        f"Set sentiment to one of 'positive', 'neutral', or 'negative' for the overall tone toward that "
        f"team. Set mention_count to the approximate number of comments mentioning that team. Return an "
        f"empty array when no national teams are mentioned.\n"
        f"If comments are sparse or difficult to analyze, return the best possible structured result."
    )


def analyze_with_retry(client: genai.Client, prompt: str, max_retries: int = 4):
    """Analyze one video. Retry 429 errors with exponential backoff."""
    for attempt in range(max_retries):
        try:
            resp = client.models.generate_content(
                model=MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_INSTRUCTION,
                    response_mime_type="application/json",
                    response_schema=CommentAnalysis,
                    temperature=TEMPERATURE,
                    # Prevent cost spikes from excessive thinking tokens. Gemini 3 models use
                    # thinking_level instead of numeric thinking_budget; mixing both causes 400.
                    # minimal is suitable for bulk classification, translation, and analysis.
                    thinking_config=types.ThinkingConfig(thinking_level="minimal"),
                    # Hard output limit as insurance against runaway JSON generation.
                    max_output_tokens=MAX_OUTPUT_TOKENS,
                ),
            )
            parsed = resp.parsed
            if parsed is None:
                print(f"  WARNING: Parse failed. Raw response prefix: {(resp.text or '')[:120]}", file=sys.stderr)
                return None
            return parsed
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            if ("429" in msg or "RESOURCE_EXHAUSTED" in msg) and attempt < max_retries - 1:
                wait = 5 * (2 ** attempt)
                print(f"  Possible rate limit. Waiting {wait}s before retry... ({attempt+1}/{max_retries})")
                time.sleep(wait)
            else:
                print(f"  ERROR: Analysis failed: {msg[:160]}", file=sys.stderr)
                return None
    return None


def main() -> int:
    if not os.environ.get("GEMINI_API_KEY"):
        print("ERROR: GEMINI_API_KEY is not set.", file=sys.stderr)
        return 1

    path = find_phase4_path()
    print(f"Input file: {path}")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    by_video = data.get("comments_by_video", {})
    if not by_video:
        print("ERROR: comments_by_video is empty.", file=sys.stderr)
        return 1
    print(f"Target videos: {len(by_video)}")

    client = genai.Client()

    analyses = {}
    skipped = []
    items = list(by_video.items())
    for i, (vid, meta) in enumerate(items, 1):
        # Skip only when there is no title and therefore no judgment material.
        # Even videos with comment errors or zero comments can still be judged
        # from the title and description, then filled with the required format.
        if not (meta.get("title") or "").strip():
            skipped.append({"video_id": vid, "reason": "no_title_no_judgeable_info"})
            print(f"[{i}/{len(items)}] {vid} skipped; no title and no judgeable information")
            continue

        comments = meta.get("comments", []) or []
        # Keep the top MAX_COMMENTS comments by like count. An empty list is allowed.
        top = sorted(comments, key=lambda c: c.get("like_count", 0), reverse=True)[:MAX_COMMENTS]
        prompt = build_prompt(meta, top)

        note = f"{len(top)} comments" if top else "no comments; judging from title and description only"
        if meta.get("error"):
            note += f" / source error={meta.get('error')}"
        print(f"[{i}/{len(items)}] Analyzing {vid} ({note})...")
        result = analyze_with_retry(client, prompt)
        if result is None:
            skipped.append({"video_id": vid, "reason": "analysis_failed"})
        else:
            rec = result.model_dump()
            rec["analyzed_at"] = datetime.now(timezone.utc).isoformat()
            rec["total_analyzed"] = len(top)
            analyses[vid] = rec

        if i < len(items):
            time.sleep(SLEEP_BETWEEN)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = f"comment_analysis_{ts}.json"
    payload = {
        "generated_at": datetime.now().astimezone().isoformat(),
        "source_file": os.path.basename(path),
        "model": MODEL,
        "total_videos_analyzed": len(analyses),
        "total_skipped": len(skipped),
        "skipped": skipped,
        "analyses": analyses,
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)

    print(f"\nAnalysis complete: {len(analyses)} succeeded / {len(skipped)} skipped")
    print(f"Saved: {out_path}")
    print("Next, load this into videos.comment_analysis with stage 2.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
