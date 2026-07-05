#!/usr/bin/env python3
"""
Phase5 ステージ1: コメント感情分析 + サッカー関連性判定 → ローカル保存

phase4_comments_*.json の各動画について、タイトル・説明文・コメント群を
Gemini 3.1 Flash-Lite で分析し、サッカー関連性(is_soccer_related)・
感情比率・ポジ/ネガのテーマ(ja/en)・引用候補(ja/en訳付き) を生成して
comment_analysis_<timestamp>.json に保存する。

is_soccer_related は、search.list の "World Cup" 系クエリがクリケット・
バスケットボール等の他競技の大会名にもヒットしてしまう問題に対応するための
事後フィルタ。4_load_comment_analysis.py が投入時にこのフラグを見て、
false の動画をMongoDBから削除する。

【2段構成の前半】Gemini APIを叩くのはこのスクリプトだけ。
MongoDB投入(ステージ2)をやり直してもAPIを再消費しない。

設計:
  - 1動画 = 1リクエスト（構造化出力 response_schema で確実にJSON化）
  - コメントはいいね数の多い順に最大100件を投入（バズの声と引用候補は上位に集まる）
  - is_soccer_related の判定はタイトル・説明文を主な根拠にするため、
    コメント無効動画(error有り)・コメント0件の動画もスキップせず分析対象にする
    （タイトルが完全に空などで判定材料が無い場合のみスキップする）
  - レート制限(429)は指数バックオフでリトライ

事前準備:
    pip install google-genai pydantic
    export GEMINI_API_KEY='...'

実行:
    python analyze_comments.py [phase4_comments_*.jsonのパス]
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

MODEL = "gemini-3.1-flash-lite"      # 2026-05 GA。コメント分析・翻訳に最適
MAX_COMMENTS = 100                    # 1動画あたりGeminiに渡す上限（いいね順）
MAX_QUOTES = 3                        # 引用候補の最大数
MAX_TEAMS = 8                         # 言及チームの最大数
SLEEP_BETWEEN = 4.0                   # リクエスト間スリープ(秒)。無料枠RPMに応じ調整可
TEMPERATURE = 0.3                     # 分析タスクなので低めで安定寄り
MAX_OUTPUT_TOKENS = 4096              # 出力全体のハードリミット（暴走防止）

from dotenv import load_dotenv
from pathlib import Path
SCRIPT_DIR = Path(__file__).parent
load_dotenv(SCRIPT_DIR.parent / 'app' / 'soccer_agent' / '.env')

# ---- 構造化出力スキーマ（引き継ぎ書の comment_analysis に準拠）----
class Sentiment(BaseModel):
    positive: float   # 比率(%)。3つの合計が約100になる想定
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
    team: str          # 集計のため英語の代表チーム名で正規化（例: "Argentina", "Japan"）
    sentiment: str     # そのチームへの全体論調: "positive" / "neutral" / "negative"
    mention_count: int  # 言及したと思われるコメント数の概算


class CommentAnalysis(BaseModel):
    is_soccer_related: bool   # サッカー(FIFAワールドカップ含む)に関連する動画か
    relevance_reason: str     # 関連性判定の簡潔な理由（日本語、1文程度）
    sentiment: Sentiment
    positive_themes: list[Theme]
    negative_themes: list[Theme]
    quotable_comments: list[QuotableComment]
    mentioned_teams: list[TeamMention]


SYSTEM_INSTRUCTION = (
    "あなたはサッカー(FIFAワールドカップ)関連のYouTube動画コメントを分析する専門家です。"
    "与えられた多言語のコメント群を分析し、視聴者の感情・話題・引用に値する声を構造化して返します。"
)


def find_phase4_path() -> str:
    if len(sys.argv) > 1:
        return sys.argv[1]
    hits = sorted(glob.glob("data/phase4_comments_*.json"))
    if not hits:
        print("ERROR: phase4_comments_*.json が見つかりません。引数でパスを渡してください。",
              file=sys.stderr)
        sys.exit(1)
    return hits[-1]


def build_prompt(meta: dict, comments: list) -> str:
    """動画メタ + コメント一覧から分析プロンプトを組む。

    meta['countries'] は (b) 多対多方式で、動画が出現した全国のリスト。
    1動画が複数国の検索結果に出現し得るため、国名・言語は複数列挙する。

    is_soccer_related の判定はタイトル・説明文を主な根拠とする（コメントが
    0件/取得失敗の動画でも判定できるようにするため）。コメントがあれば
    判定の補助材料として使ってよい。
    """
    lines = []
    for c in comments:
        author = c.get("author_display_name", "")
        likes = c.get("like_count", 0)
        text = (c.get("text_original") or "").replace("\n", " ").strip()
        lines.append(f"[likes={likes}] {author}: {text}")
    comments_block = "\n".join(lines) if lines else "(コメントなし、または取得不可)"

    countries = meta.get("countries", []) or []
    country_names_ja = [c.get("country_name_ja", "") for c in countries if c.get("country_name_ja")]
    primary_langs = sorted({c.get("primary_lang", "") for c in countries if c.get("primary_lang")})
    countries_str = "、".join(country_names_ja) if country_names_ja else "不明"
    langs_str = "、".join(primary_langs) if primary_langs else "不明"

    description = (meta.get("description") or "").strip()
    description_block = description[:500] if description else "(説明文なし)"

    return (
        f"# 動画情報\n"
        f"出現国(この動画が話題になっている国、複数の場合あり): {countries_str}\n"
        f"主要言語(出現国の言語、複数の場合あり): {langs_str}\n"
        f"タイトル: {meta.get('title')}\n"
        f"説明文(先頭500字): {description_block}\n\n"
        f"# コメント({len(comments)}件、いいね数の多い順)\n"
        f"{comments_block}\n\n"
        f"# 指示\n"
        f"上記の動画情報・コメントを分析し、以下を生成してください:\n"
        f"0. is_soccer_related: この動画がサッカー（FIFAワールドカップ含む）に"
        f"関連する内容かどうかを true/false で判定してください。タイトル・説明文を"
        f"主な根拠にし、コメントがあれば補助的に使ってください。"
        f"検索キーワードに「World Cup」が含まれていても、クリケット・バスケットボール・"
        f"バレーボール等の他競技の「World Cup」を冠した大会である場合は false としてください"
        f"（例: ICC Women's T20 World Cup はクリケットなので false）。"
        f"判断に迷う場合や情報が不十分な場合は true（除外しすぎない方を優先）としてください。"
        f"relevance_reason にはその判定理由を日本語1文程度で書いてください。\n"
        f"以降の1〜4は is_soccer_related が false の場合でも形式上埋めてください"
        f"（コメントが無ければ sentiment は positive=0,negative=0,neutral=100、"
        f"各リストは空配列で構いません）。\n"
        f"1. sentiment: ポジティブ/ネガティブ/中立の比率(%)。合計が約100になるように。\n"
        f"2. positive_themes / negative_themes: 主要な話題を、theme_ja(日本語)・"
        f"theme_en(英語)・mention_count(言及したと思われるコメント数の概算)で。各最大5件。\n"
        f"3. quotable_comments: 記事に引用して映える、いいね数が多く印象的なコメントを最大{MAX_QUOTES}件。"
        f"original(原文そのまま)・translated_ja(日本語訳)・translated_en(英語訳)・"
        f"author(投稿者名)・likes(入力のlikes値)・original_language(原文の言語コード)。\n"
        f"4. mentioned_teams: コメントで言及されている『代表チーム』を最大{MAX_TEAMS}件。"
        f"team は後段で言語をまたいで集計するため、必ず英語の代表チーム名/国名で正規化する"
        f"(例: 'Argentina','Brazil','Japan','Morocco'。'日本'や'🇲🇽'やクラブ名ではなく代表名に寄せる)。"
        f"sentiment はそのチームに対する全体の論調を 'positive'/'neutral'/'negative' のいずれかで。"
        f"mention_count はそのチームに言及したと思われるコメント数の概算。"
        f"代表チームへの言及が無ければ空配列で良い。\n"
        f"コメントが少ない/分析困難な場合は、可能な範囲で返してください。"
    )


def analyze_with_retry(client: genai.Client, prompt: str, max_retries: int = 4):
    """1動画を分析。429時は指数バックオフ。CommentAnalysis or None を返す。"""
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
                    # 思考トークンの暴走によるコスト高騰を防ぐ。Gemini 3系は
                    # 数値の thinking_budget ではなく thinking_level を使う(混在は400)。
                    # minimal = 分類/翻訳/分析のbulk処理向け、Flash-Liteの既定でもある。
                    thinking_config=types.ThinkingConfig(thinking_level="minimal"),
                    # 出力全体のハードリミット（JSON生成が延々続く事故への保険）。
                    max_output_tokens=MAX_OUTPUT_TOKENS,
                ),
            )
            parsed = resp.parsed
            if parsed is None:
                print(f"  WARNING: パース失敗。生応答先頭: {(resp.text or '')[:120]}", file=sys.stderr)
                return None
            return parsed
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            if ("429" in msg or "RESOURCE_EXHAUSTED" in msg) and attempt < max_retries - 1:
                wait = 5 * (2 ** attempt)
                print(f"  レート制限の可能性。{wait}秒待機してリトライ... ({attempt+1}/{max_retries})")
                time.sleep(wait)
            else:
                print(f"  ERROR: 分析失敗: {msg[:160]}", file=sys.stderr)
                return None
    return None


def main() -> int:
    if not os.environ.get("GEMINI_API_KEY"):
        print("ERROR: GEMINI_API_KEY が未設定です。", file=sys.stderr)
        return 1

    path = find_phase4_path()
    print(f"入力ファイル: {path}")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    by_video = data.get("comments_by_video", {})
    if not by_video:
        print("ERROR: comments_by_video が空です。", file=sys.stderr)
        return 1
    print(f"対象動画: {len(by_video)}件")

    client = genai.Client()

    analyses = {}
    skipped = []
    items = list(by_video.items())
    for i, (vid, meta) in enumerate(items, 1):
        # タイトルが無い(判定材料が一切無い)場合のみスキップする。
        # コメント無効/取得失敗(error有り)・コメント0件でも、タイトル・説明文が
        # あれば is_soccer_related の判定とformat上の分析は実施する。
        if not (meta.get("title") or "").strip():
            skipped.append({"video_id": vid, "reason": "no_title_no_judgeable_info"})
            print(f"[{i}/{len(items)}] {vid} スキップ (タイトルも無く判定材料が無い)")
            continue

        comments = meta.get("comments", []) or []
        # いいね順に上位MAX_COMMENTS件（0件ならそのまま空リスト）
        top = sorted(comments, key=lambda c: c.get("like_count", 0), reverse=True)[:MAX_COMMENTS]
        prompt = build_prompt(meta, top)

        note = f"コメント{len(top)}件" if top else "コメント無し(タイトル/説明文のみで判定)"
        if meta.get("error"):
            note += f" / 元エラー={meta.get('error')}"
        print(f"[{i}/{len(items)}] {vid} 分析中 ({note})...")
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

    print(f"\n分析完了: 成功{len(analyses)}件 / スキップ{len(skipped)}件")
    print(f"保存しました: {out_path}")
    print("次はステージ2でこれを videos.comment_analysis に投入します。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
