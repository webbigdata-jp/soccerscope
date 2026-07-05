#!/usr/bin/env python3
"""
Phase5 ステージ2: コメント分析結果を videos.comment_analysis に投入

ステージ1が出力した comment_analysis_<timestamp>.json を読み、既存の videos
コレクションの各ドキュメントに comment_analysis フィールドを $set で追加する。
embedding・メタデータなど既存フィールドは一切触らない。

is_soccer_related が false と判定された動画は、サッカーと無関係な動画
（例: search.list の "World Cup" 系クエリに誤ってヒットしたクリケット/
バスケットボール/バレーボール等の動画）とみなし、$setではなく
videosコレクションから完全に削除する。is_soccer_related フィールドが
存在しない（3_analyze_comments.py の旧バージョンで生成された）分析結果は
従来通り扱い、削除しない（後方互換）。

【2段構成の後半】Gemini APIは叩かない。video_idでマッチするので冪等。

事前準備:
    pip install "pymongo[srv]"
    export MONGODB_URI='mongodb+srv://<user>:<password>@xxxx.mongodb.net/'

実行:
    python load_comment_analysis.py [comment_analysis_*.jsonのパス]
"""

import os
import sys
import glob
import json
import shutil
from datetime import datetime, timezone

from pymongo import MongoClient, UpdateOne, DeleteOne
from pymongo.server_api import ServerApi
from pymongo.errors import ConnectionFailure, OperationFailure, ConfigurationError, BulkWriteError

DB_NAME = "soccertube"
COLL_NAME = "videos"

from dotenv import load_dotenv
from pathlib import Path
SCRIPT_DIR = Path(__file__).parent
load_dotenv(SCRIPT_DIR.parent / 'app' / 'soccer_agent' / '.env')

DB_NAME = os.environ.get("SOCCER_DB_NAME", "soccertube")
COLL_NAME = os.environ.get("SOCCER_COLL_NAME", "videos")


def find_input_path() -> str:
    if len(sys.argv) > 1:
        return sys.argv[1]
    hits = sorted(glob.glob("comment_analysis_*.json"))
    if not hits:
        print("ERROR: comment_analysis_*.json が見つかりません。引数でパスを渡してください。",
              file=sys.stderr)
        sys.exit(1)
    return hits[-1]


def archive_run_files():
    """この実行で使った中間JSONを data/<YYYYMMDD>/ に退避する。
    各パターンの最新ファイル（＝今回の実行分）を移動する。
    build_stats_page.py は data/<日付>/ も探索するので、退避後でも統計生成は可能。"""
    from pathlib import Path
    date_dir = Path("data") / datetime.now().strftime("%Y%m%d")
    date_dir.mkdir(parents=True, exist_ok=True)
    patterns = [
        "data/phase2_video_ids_*.json",
        "data/phase3_metadata_*.json",
        "data/phase7_with_buzz_score_*.json",
        "data/phase4_comments_*.json",
        "videos_embedded_*.json",        # 1_embed の出力（カレント）
        "comment_analysis_*.json",       # 3_analyze の出力（カレント）
    ]
    moved = []
    for pat in patterns:
        hits = sorted(glob.glob(pat))   # data/<日付>/ は非再帰globなので対象外
        if not hits:
            continue
        src = hits[-1]                  # 最新＝今回の実行分
        dest = date_dir / Path(src).name
        try:
            shutil.move(src, dest)
            moved.append(dest.name)
        except OSError as e:
            print(f"  WARNING: 退避失敗 {src}: {e}", file=sys.stderr)
    print(f"\n退避: 中間JSON {len(moved)}件を {date_dir}/ に移動しました。")
    for name in moved:
        print(f"    - {name}")


def main() -> int:
    uri = os.environ.get("MONGODB_URI")
    if not uri:
        print("ERROR: MONGODB_URI が未設定です。", file=sys.stderr)
        return 1

    path = find_input_path()
    print(f"入力ファイル: {path}")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    analyses = data.get("analyses", {})
    if not analyses:
        print("ERROR: analyses が空です。", file=sys.stderr)
        return 1
    print(f"投入対象: {len(analyses)}件の分析結果")

    try:
        mclient = MongoClient(uri, server_api=ServerApi("1"), serverSelectionTimeoutMS=5000)
        mclient.admin.command("ping")
    except (ConnectionFailure, OperationFailure, ConfigurationError) as e:
        print(f"ERROR: MongoDB接続失敗: {e}", file=sys.stderr)
        return 1
    coll = mclient[DB_NAME][COLL_NAME]

    # video_id で該当ドキュメントに comment_analysis を $set。
    # is_soccer_related == False の動画はvideosコレクションから削除する
    # （フィールドが存在しない旧フォーマットの分析結果は従来通りupdate扱い、
    #  後方互換のため削除しない）。
    to_update = {}
    to_delete_ids = []
    for vid, analysis in analyses.items():
        is_soccer_related = analysis.get("is_soccer_related")
        if is_soccer_related is False:
            to_delete_ids.append(vid)
        else:
            to_update[vid] = analysis

    print(f"\n内訳: 更新対象={len(to_update)}件 / "
          f"サッカー非関連のため削除対象={len(to_delete_ids)}件")
    if to_delete_ids:
        print("  削除対象 video_id: " + ", ".join(to_delete_ids))

    now = datetime.now(timezone.utc)

    if to_update:
        print("\nvideos.comment_analysis を更新中（$set, video_idマッチ）...")
        ops = [
            UpdateOne(
                {"video_id": vid},
                {"$set": {"comment_analysis": analysis, "last_analyzed": now}},
            )
            for vid, analysis in to_update.items()
        ]
        try:
            res = coll.bulk_write(ops, ordered=False)
        except BulkWriteError as e:
            print(f"ERROR: 一括更新で問題: {e.details}", file=sys.stderr)
            return 1

        matched = res.matched_count
        modified = res.modified_count
        print(f"  matched={matched}, modified={modified}")

        # video_idが videos に存在せずマッチしなかったものを洗い出す（通常は0のはず）
        if matched < len(to_update):
            existing_ids = set(coll.distinct("video_id"))
            unmatched = [vid for vid in to_update if vid not in existing_ids]
            print(f"  WARNING: videosに存在せずマッチしなかったvideo_id {len(unmatched)}件: {unmatched}",
                  file=sys.stderr)
    else:
        print("\n更新対象が0件のため $set はスキップします。")

    if to_delete_ids:
        print("\nサッカー非関連動画をvideosコレクションから削除中...")
        delete_ops = [DeleteOne({"video_id": vid}) for vid in to_delete_ids]
        try:
            del_res = coll.bulk_write(delete_ops, ordered=False)
        except BulkWriteError as e:
            print(f"ERROR: 一括削除で問題: {e.details}", file=sys.stderr)
            return 1
        print(f"  deleted_count={del_res.deleted_count}")
        if del_res.deleted_count < len(to_delete_ids):
            print(f"  WARNING: 削除対象{len(to_delete_ids)}件に対し実削除"
                  f"{del_res.deleted_count}件（既に存在しなかった可能性）", file=sys.stderr)

    # 検証: comment_analysis を持つドキュメント数
    with_analysis = coll.count_documents({"comment_analysis": {"$exists": True}})
    total = coll.count_documents({})
    print(f"\n検証: comment_analysis 保有 {with_analysis} / 全 {total} 件")

    mclient.close()
    print(f"\nステージ2完了。videosの{len(to_update)}件に感情分析結果を追加、"
          f"{len(to_delete_ids)}件をサッカー非関連として削除しました。")

    # この実行で使った中間JSONを data/<日付>/ へ退避（成功時のみ）
    archive_run_files()
    return 0


if __name__ == "__main__":
    sys.exit(main())
