#!/usr/bin/env python3
"""
ステージ1: 動画メタデータをembedしてローカル保存

phase7のvideos全件について embedテキストを作り、Gemini で768次元embed
（RETRIEVAL_DOCUMENT・正規化）を生成し、元メタデータ + embedding を
videos_embedded_<timestamp>.json に保存する。

【2段構成の前半】Gemini APIを叩くのはこのスクリプトだけ。
MongoDB投入(ステージ2 load_to_mongo.py)をやり直しても、ここを再実行しない限り
無料枠を消費しない。

事前準備:
    pip install google-genai numpy
    export GEMINI_API_KEY='...'

実行:
    python embed_videos.py [phase7のJSONパス]
    （省略時はカレントの phase7_with_buzz_score_*.json を自動で探す）
"""

import os
import sys
import glob
import json
import time
from datetime import datetime

import numpy as np
from google import genai
from google.genai import types
from dotenv import load_dotenv

EMBED_MODEL = "gemini-embedding-001"
EMBED_DIM = 768            # 後から変更不可。768で確定
DESC_MAX_CHARS = 500       # descriptionは先頭500字（入力トークン節約・主題は冒頭に出る）
CHUNK_SIZE = 20            # 1リクエストあたりの件数（上限250/20kトークンに対し安全側）
SLEEP_BETWEEN_CHUNKS = 1.0 # チャンク間の軽い待機（秒）

from pathlib import Path
SCRIPT_DIR = Path(__file__).parent
load_dotenv(SCRIPT_DIR.parent / 'app' / 'soccer_agent' / '.env')

def find_phase7_path() -> str:
    if len(sys.argv) > 1:
        return sys.argv[1]
    hits = sorted(glob.glob("data/phase7_with_buzz_score_*.json"))
    if not hits:
        print("ERROR: phase7_with_buzz_score_*.json が見つかりません。引数でパスを渡してください。",
              file=sys.stderr)
        sys.exit(1)
    return hits[-1]


def build_embed_text(v: dict) -> str:
    """embed対象: title + description(先頭500字) + 国名(en, 複数列挙)。tagsは空が多いので不使用。

    phase3が (b) 多対多方式で countries 配列(+reach) を持つようになったため、
    出現した全国の country_name_en をカンマ区切りで列挙する（rank昇順=出現順のまま）。
    """
    title = v.get("title", "") or ""
    desc = (v.get("description") or "")[:DESC_MAX_CHARS]
    countries = v.get("countries", []) or []
    country_names = [c.get("country_name_en", "") for c in countries if c.get("country_name_en")]
    country = ", ".join(country_names)
    return f"{title}\n{desc}\n{country}".strip()


def normalize(vec) -> list:
    """768次元は非正規化で返るため手動L2正規化（必須）。"""
    arr = np.asarray(vec, dtype=float)
    norm = np.linalg.norm(arr)
    if norm == 0:
        raise ValueError("ゼロベクトル（embed対象テキストが空の可能性）")
    return (arr / norm).tolist()


def embed_chunk_with_retry(client: genai.Client, texts: list, max_retries: int = 4) -> list:
    """1チャンクをembed。レート制限(429)時は指数バックオフでリトライ。"""
    for attempt in range(max_retries):
        try:
            result = client.models.embed_content(
                model=EMBED_MODEL,
                contents=texts,
                config=types.EmbedContentConfig(
                    task_type="RETRIEVAL_DOCUMENT",
                    output_dimensionality=EMBED_DIM,
                ),
            )
            return [normalize(e.values) for e in result.embeddings]
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            if ("429" in msg or "RESOURCE_EXHAUSTED" in msg) and attempt < max_retries - 1:
                wait = 5 * (2 ** attempt)  # 5,10,20,40秒
                print(f"  レート制限の可能性。{wait}秒待機してリトライします... ({attempt+1}/{max_retries})")
                time.sleep(wait)
            else:
                raise
    raise RuntimeError("embedリトライ上限に達しました。")


def main() -> int:
    if not os.environ.get("GEMINI_API_KEY"):
        print("ERROR: GEMINI_API_KEY が未設定です。", file=sys.stderr)
        return 1

    path = find_phase7_path()
    print(f"入力ファイル: {path}")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    videos = data.get("videos", [])
    if not videos:
        print("ERROR: videos が空です。", file=sys.stderr)
        return 1
    print(f"対象動画: {len(videos)}件")

    # embedテキストを全件分作る（空テキストは検出して警告）
    texts = []
    for v in videos:
        t = build_embed_text(v)
        if not t:
            print(f"  WARNING: video_id={v.get('video_id')} のembedテキストが空です。", file=sys.stderr)
        texts.append(t)

    client = genai.Client()

    # チャンク分割してembed
    all_vecs = []
    n_chunks = (len(texts) + CHUNK_SIZE - 1) // CHUNK_SIZE
    print(f"\n{CHUNK_SIZE}件ずつ {n_chunks}チャンクでembedします（768次元・正規化・RETRIEVAL_DOCUMENT）。")
    for i in range(0, len(texts), CHUNK_SIZE):
        chunk = texts[i:i + CHUNK_SIZE]
        idx = i // CHUNK_SIZE + 1
        print(f"  チャンク {idx}/{n_chunks}（{len(chunk)}件）をembed中...")
        vecs = embed_chunk_with_retry(client, chunk)
        if len(vecs) != len(chunk):
            print(f"ERROR: 返却ベクトル数({len(vecs)})が入力数({len(chunk)})と不一致。", file=sys.stderr)
            return 1
        all_vecs.extend(vecs)
        if idx < n_chunks:
            time.sleep(SLEEP_BETWEEN_CHUNKS)

    assert len(all_vecs) == len(videos), "ベクトル総数と動画数が不一致"
    print(f"\nembed完了: {len(all_vecs)}件 / 各{len(all_vecs[0])}次元")

    # 元メタデータ + embedding をマージして保存（ステージ2はphase7を読み直さない）
    out_videos = []
    for v, vec, t in zip(videos, all_vecs, texts):
        rec = dict(v)               # phase7の元データを丸ごと保持
        rec["embedding"] = vec
        rec["_embed_text"] = t      # デバッグ用（何をembedしたか）。ステージ2で無視してよい
        out_videos.append(rec)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = f"videos_embedded_{ts}.json"
    payload = {
        "generated_at": datetime.now().astimezone().isoformat(),
        "source_file": os.path.basename(path),
        "embed_model": EMBED_MODEL,
        "embed_dim": EMBED_DIM,
        "task_type": "RETRIEVAL_DOCUMENT",
        "normalized": True,
        "total": len(out_videos),
        "videos": out_videos,
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    print(f"\n保存しました: {out_path}")
    print("次はステージ2 (load_to_mongo.py) でこのファイルをMongoDBに投入します。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

