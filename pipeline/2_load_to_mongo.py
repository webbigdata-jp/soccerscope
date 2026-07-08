#!/usr/bin/env python3
"""
Stage 2: Load embedded data into MongoDB and create a Vector Search index.

Reads videos_embedded_<timestamp>.json produced by Stage 1, then upserts it into
the production videos collection using video_id as the key. After loading the
records, creates the Vector Search index (video_semantic_index) and waits until
it becomes queryable.

[Second half of the two-stage pipeline] This script never calls the Gemini API.
Running it repeatedly does not consume the free quota. Data is upserted by
video_id, so the operation is idempotent and reruns do not create duplicates.

Prerequisites:
    pip install "pymongo[srv]"
    export MONGODB_URI='mongodb+srv://<user>:<password>@xxxx.mongodb.net/'

Usage:
    python load_to_mongo.py [path to videos_embedded_*.json]
    (If omitted, the script automatically looks for videos_embedded_*.json in the current directory.)
"""

import os
import sys
import glob
import json
import time
from datetime import datetime, timezone

from pymongo import MongoClient
from pymongo.server_api import ServerApi
from pymongo.operations import SearchIndexModel
from pymongo.errors import ConnectionFailure, OperationFailure, ConfigurationError, BulkWriteError
from pymongo import ReplaceOne
from dotenv import load_dotenv
from pathlib import Path

DB_NAME = "soccertube"
COLL_NAME = "videos"                  # Production collection
INDEX_NAME = "video_semantic_index"
EMBED_DIM = 768
DROP_FIELDS = ("_embed_text",)        # Debug fields that should not be inserted into production

SCRIPT_DIR = Path(__file__).parent
load_dotenv(SCRIPT_DIR.parent / 'app' / 'soccer_agent' / '.env')

DB_NAME = os.environ.get("SOCCER_DB_NAME", "soccertube")
COLL_NAME = os.environ.get("SOCCER_COLL_NAME", "videos")
INDEX_NAME = os.environ.get("SOCCER_INDEX_NAME", "video_semantic_index")


def find_input_path() -> str:
    if len(sys.argv) > 1:
        return sys.argv[1]
    hits = sorted(glob.glob("videos_embedded_*.json"))
    if not hits:
        print("ERROR: videos_embedded_*.json was not found. Please pass the path as an argument.",
              file=sys.stderr)
        sys.exit(1)
    return hits[-1]


def parse_dt(s):
    """Convert 'YYYY-MM-DDTHH:MM:SSZ' to a timezone-aware datetime so date-range filters work."""
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def to_doc(v: dict) -> dict:
    """Format a document for insertion: convert published_at to datetime and remove debug fields."""
    doc = {k: val for k, val in v.items() if k not in DROP_FIELDS}
    if "published_at" in doc:
        doc["published_at"] = parse_dt(doc.get("published_at"))
    doc["ingested_at"] = datetime.now(timezone.utc)
    return doc


def ensure_index(coll) -> bool:
    """Create the Vector Search index if it does not exist, then wait until it is queryable.
    If the index cannot be created from code, print JSON that can be pasted into the UI."""
    existing = list(coll.list_search_indexes(INDEX_NAME))
    if existing:
        print(f"  Index '{INDEX_NAME}' already exists.")
    else:
        definition = {
            "fields": [
                {"type": "vector", "path": "embedding",
                 "numDimensions": EMBED_DIM, "similarity": "cosine"},
                # Note: countries is an array of objects. Atlas Vector Search
                # filter-type indexes cannot directly index fields inside object
                # arrays due to vectorSearch index constraints. For filtering,
                # use country_codes, a simple string array generated as a copy in phase 3.
                {"type": "filter", "path": "country_codes"},
                {"type": "filter", "path": "published_at"},
                {"type": "filter", "path": "is_buzz"},
                {"type": "filter", "path": "category"},
            ]
        }
        model = SearchIndexModel(definition=definition, name=INDEX_NAME, type="vectorSearch")
        try:
            coll.create_search_indexes([model])
            print(f"  Requested creation of index '{INDEX_NAME}' asynchronously.")
        except OperationFailure as e:
            print(f"\nERROR: Failed to create the index from code: {e}", file=sys.stderr)
            print("→ Paste the following into Atlas UI > Atlas Search > Create Index > JSON Editor:",
                  file=sys.stderr)
            print(json.dumps({"name": INDEX_NAME, "type": "vectorSearch", "definition": definition},
                             ensure_ascii=False, indent=2), file=sys.stderr)
            return False

    print("  Waiting until the index becomes queryable. This may take a few minutes...")
    deadline = time.time() + 300
    while time.time() < deadline:
        info = list(coll.list_search_indexes(INDEX_NAME))
        if info and info[0].get("queryable"):
            print("  Index queryable=True.")
            return True
        time.sleep(5)
    print("ERROR: The index did not become queryable within the time limit.", file=sys.stderr)
    return False


def main() -> int:
    uri = os.environ.get("MONGODB_URI")
    if not uri:
        print("ERROR: MONGODB_URI is not set.", file=sys.stderr)
        return 1

    path = find_input_path()
    print(f"Input file: {path}")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    videos = data.get("videos", [])
    if not videos:
        print("ERROR: videos is empty.", file=sys.stderr)
        return 1

    # Validate the embedding dimension as a precaution in case it differs from Stage 1.
    dim = len(videos[0].get("embedding", []))
    if dim != EMBED_DIM:
        print(f"ERROR: embedding dimension is {dim}. Expected {EMBED_DIM}.", file=sys.stderr)
        return 1
    print(f"Load target: {len(videos)} records / {dim} dimensions")

    # Connect to MongoDB.
    try:
        mclient = MongoClient(uri, server_api=ServerApi("1"), serverSelectionTimeoutMS=5000)
        mclient.admin.command("ping")
    except (ConnectionFailure, OperationFailure, ConfigurationError) as e:
        print(f"ERROR: MongoDB connection failed: {e}", file=sys.stderr)
        return 1
    coll = mclient[DB_NAME][COLL_NAME]

    # Upsert by video_id for idempotency. Insert/update in batches with bulk_write.
    print("\n[1/2] Loading into the videos collection using video_id upsert...")
    ops = []
    for v in videos:
        if not v.get("video_id"):
            print("  WARNING: Skipping a record without video_id", file=sys.stderr)
            continue
        ops.append(ReplaceOne({"video_id": v["video_id"]}, to_doc(v), upsert=True))
    try:
        res = coll.bulk_write(ops, ordered=False)
    except BulkWriteError as e:
        print(f"ERROR: Bulk load failed: {e.details}", file=sys.stderr)
        return 1
    print(f"  OK — upserted={res.upserted_count}, modified={res.modified_count}, "
          f"collection total={coll.count_documents({})}")

    # Create the index.
    print("\n[2/2] Checking/creating the Vector Search index...")
    if not ensure_index(coll):
        return 1

    mclient.close()
    print("\nStage 2 complete. The embedded 111 records have been loaded into the videos collection and are now searchable.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
