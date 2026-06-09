"""
SoccerScope 検索診断 v2 — LLM/uvicorn を介さず、ツール内部の真因を出す。

v1 からの変更:
  MCP stdio の「閉じる瞬間に出る BrokenResourceError（後始末ノイズ）」で診断が
  途中終了していたため、結果取得後の終了時例外は握りつぶすヘルパー mcp_call に統一。
  さらに「検索インデックス一覧」を直接出す段を追加し、video_semantic_index の
  存在を白黒つける。

使い方:
    python diag.py                      # 既定クエリ
    python diag.py "ブラジル 久保建英"   # 任意クエリ
"""

import asyncio
import math
import os
import sys

try:
    from dotenv import load_dotenv
    _here = os.path.dirname(os.path.abspath(__file__))
    load_dotenv(os.path.join(_here, ".env"))
    load_dotenv(os.path.join(_here, "soccer_agent", ".env"))
except Exception:
    pass

from soccer_agent.agent import (  # noqa: E402
    _embed_query_sync,
    _mcp_server_params,
    search_videos,
    DB_NAME,
    COLLECTION,
    VECTOR_INDEX,
)
from mcp.client.stdio import stdio_client  # noqa: E402
from mcp import ClientSession  # noqa: E402

QUERY = sys.argv[1] if len(sys.argv) > 1 else "日本代表が世界で話題になっているバズ動画"


def _texts(result):
    out = []
    for b in (getattr(result, "content", None) or []):
        t = getattr(b, "text", None)
        if t:
            out.append(t)
    return "\n".join(out)


async def mcp_call(tool: str, args: dict):
    """MCPツールを1回呼ぶ。結果取得後の終了時 BrokenResourceError は握りつぶす。"""
    result = None
    try:
        async with stdio_client(_mcp_server_params()) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                result = await s.call_tool(tool, args)
    except Exception as e:  # noqa: BLE001
        if result is None:
            raise
        # 結果は取れている。閉じる瞬間の後始末ノイズなので無視。
    return result


async def main():
    print("=" * 60)
    print("QUERY:", QUERY)
    print("DB/COLLECTION/INDEX:", DB_NAME, "/", COLLECTION, "/", VECTOR_INDEX)
    print("ENV  MONGODB_URI set :", bool(os.environ.get("MONGODB_URI")))
    print("ENV  GOOGLE_API_KEY  :", bool(os.environ.get("GOOGLE_API_KEY")))
    print("=" * 60)

    # [1] 埋め込み
    print("\n[1] embedding ...")
    try:
        vec = await asyncio.to_thread(_embed_query_sync, QUERY)
        print(f"    OK  dim={len(vec)}  norm={math.sqrt(sum(x*x for x in vec)):.4f}")
    except Exception as e:  # noqa: BLE001
        print("    >>> EMBEDDING FAILED:", repr(e)); return

    # [2] count（疎通）
    print("\n[2] MCP count ...")
    try:
        res = await mcp_call("count", {"database": DB_NAME, "collection": COLLECTION, "query": {}})
        print("    OK ", _texts(res)[:200])
    except Exception as e:  # noqa: BLE001
        print("    >>> COUNT FAILED:", repr(e)); return

    # [3] 検索インデックス一覧（video_semantic_index が在るか）
    print("\n[3] list search indexes ($listSearchIndexes) ...")
    try:
        res = await mcp_call("aggregate", {
            "database": DB_NAME, "collection": COLLECTION,
            "pipeline": [{"$listSearchIndexes": {}}],
        })
        raw = _texts(res)
        print("    raw:", raw[:600] if raw else "(empty)")
        if VECTOR_INDEX in raw:
            print(f"    => '{VECTOR_INDEX}' は存在します。")
        else:
            print(f"    => ★ '{VECTOR_INDEX}' が見つかりません（削除された疑い濃厚）。")
    except Exception as e:  # noqa: BLE001
        print("    >>> LIST INDEX FAILED:", repr(e))

    # [4] search_videos 本体
    print("\n[4] search_videos ...")
    try:
        out = await search_videos(QUERY, limit=5)
        print("    keys :", list(out.keys()))
        if out.get("error"):
            print("    >>> TOOL RETURNED ERROR:", out["error"])
        print("    count:", out.get("count"))
        for v in (out.get("videos") or [])[:5]:
            print(f"      - [{v.get('country')}] {str(v.get('title'))[:46]}  score={v.get('score')}")
        if "raw" in out:
            print("    raw(先頭):", str(out["raw"])[:300])
    except Exception as e:  # noqa: BLE001
        print("    >>> search_videos RAISED:", repr(e))

    print("\nDONE.")


if __name__ == "__main__":
    asyncio.run(main())
