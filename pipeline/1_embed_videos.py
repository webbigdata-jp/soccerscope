#!/usr/bin/env python3
"""
Stage 1: Embed video metadata and save it locally.

For every video in the phase7 file, this script builds embedding text,
creates 768-dimensional Gemini embeddings with RETRIEVAL_DOCUMENT, manually
normalizes them, and saves the original metadata plus embedding values to
videos_embedded_<timestamp>.json.

This is the first half of the two-stage pipeline. This is the only script that
calls the Gemini API. Re-running the MongoDB load step (stage 2,
load_to_mongo.py) will not consume additional free-tier quota unless this
script is run again.

Setup:
    pip install google-genai numpy
    export GEMINI_API_KEY='...'

Usage:
    python embed_videos.py [path_to_phase7_json]
    If omitted, the latest data/phase7_with_buzz_score_*.json file in the
    current working directory is selected automatically.
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
EMBED_DIM = 768            # Fixed at 768. Do not change after deployment.
DESC_MAX_CHARS = 500       # Use the first 500 chars to save input tokens.
CHUNK_SIZE = 20            # Conservative batch size against API limits.
SLEEP_BETWEEN_CHUNKS = 1.0 # Short delay between chunks, in seconds.

from pathlib import Path
SCRIPT_DIR = Path(__file__).parent
load_dotenv(SCRIPT_DIR.parent / 'app' / 'soccer_agent' / '.env')


def find_phase7_path() -> str:
    if len(sys.argv) > 1:
        return sys.argv[1]
    hits = sorted(glob.glob("data/phase7_with_buzz_score_*.json"))
    if not hits:
        print(
            "ERROR: phase7_with_buzz_score_*.json was not found. "
            "Pass the path as an argument.",
            file=sys.stderr,
        )
        sys.exit(1)
    return hits[-1]


def build_embed_text(v: dict) -> str:
    """Build text for embedding from title, description, and country names.

    Tags are not used because they are often empty. Since phase3 now stores
    countries as a many-to-many array with reach values, all available
    country_name_en values are listed in their existing order.
    """
    title = v.get("title", "") or ""
    desc = (v.get("description") or "")[:DESC_MAX_CHARS]
    countries = v.get("countries", []) or []
    country_names = [c.get("country_name_en", "") for c in countries if c.get("country_name_en")]
    country = ", ".join(country_names)
    return f"{title}\n{desc}\n{country}".strip()


def normalize(vec) -> list:
    """Apply manual L2 normalization because 768-d vectors are unnormalized."""
    arr = np.asarray(vec, dtype=float)
    norm = np.linalg.norm(arr)
    if norm == 0:
        raise ValueError("Zero vector. The embedding input text may be empty.")
    return (arr / norm).tolist()


def embed_chunk_with_retry(client: genai.Client, texts: list, max_retries: int = 4) -> list:
    """Embed one chunk. Retry with exponential backoff on rate-limit errors."""
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
                wait = 5 * (2 ** attempt)  # 5, 10, 20, 40 seconds
                print(f"  Possible rate limit. Waiting {wait}s before retry... ({attempt+1}/{max_retries})")
                time.sleep(wait)
            else:
                raise
    raise RuntimeError("Embedding retry limit reached.")


def main() -> int:
    if not os.environ.get("GEMINI_API_KEY"):
        print("ERROR: GEMINI_API_KEY is not set.", file=sys.stderr)
        return 1

    path = find_phase7_path()
    print(f"Input file: {path}")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    videos = data.get("videos", [])
    if not videos:
        print("ERROR: videos is empty.", file=sys.stderr)
        return 1
    print(f"Target videos: {len(videos)}")

    # Build embedding text for every video and warn on empty text.
    texts = []
    for v in videos:
        t = build_embed_text(v)
        if not t:
            print(f"  WARNING: Empty embedding text for video_id={v.get('video_id')}.", file=sys.stderr)
        texts.append(t)

    client = genai.Client()

    # Split the input into chunks and embed each chunk.
    all_vecs = []
    n_chunks = (len(texts) + CHUNK_SIZE - 1) // CHUNK_SIZE
    print(
        f"\nEmbedding {n_chunks} chunks of up to {CHUNK_SIZE} videos "
        f"with {EMBED_DIM}-d normalized RETRIEVAL_DOCUMENT embeddings."
    )
    for i in range(0, len(texts), CHUNK_SIZE):
        chunk = texts[i:i + CHUNK_SIZE]
        idx = i // CHUNK_SIZE + 1
        print(f"  Embedding chunk {idx}/{n_chunks} ({len(chunk)} items)...")
        vecs = embed_chunk_with_retry(client, chunk)
        if len(vecs) != len(chunk):
            print(
                f"ERROR: Returned vector count ({len(vecs)}) does not match input count ({len(chunk)}).",
                file=sys.stderr,
            )
            return 1
        all_vecs.extend(vecs)
        if idx < n_chunks:
            time.sleep(SLEEP_BETWEEN_CHUNKS)

    assert len(all_vecs) == len(videos), "Vector count does not match video count."
    print(f"\nEmbedding complete: {len(all_vecs)} items / {len(all_vecs[0])} dimensions each")

    # Merge the original metadata with embeddings. Stage 2 does not reread phase7.
    out_videos = []
    for v, vec, t in zip(videos, all_vecs, texts):
        rec = dict(v)               # Keep all original phase7 metadata.
        rec["embedding"] = vec
        rec["_embed_text"] = t      # Debug field; stage 2 may ignore it.
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
    print(f"\nSaved: {out_path}")
    print("Next, load this file into MongoDB with stage 2 (load_to_mongo.py).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
