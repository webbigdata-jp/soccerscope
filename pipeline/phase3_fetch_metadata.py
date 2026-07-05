"""
phase3_fetch_metadata.py
=========================
【目的】
  Phase 2 で収集した動画IDに対して videos.list でメタデータを一括取得する。
  description / 統計情報 / duration / チャンネル情報を取得し、
  後段の Gemini 分類 (Phase 6) と バズスコア計算 (Phase 7) で使えるよう保存。

【国の帰属方針 (b) 多対多】
  1動画が複数国の検索結果に出現した場合、単一国へ割り当てず、出現した国すべてを
  countries 配列として保持する。同一言語圏でのバイラルを特定の1国へ恣意的に
  割り当てないための方針（詳細は引き継ぎメモ参照）。reach はここで一度だけ確定し、
  以降のフェーズ(phase7/Mongo/build_dataset)では再計算せず素通しする。

  country_codes は countries と同じ国コードを単純な文字列配列として複製したもの。
  MongoDB Atlas Vector Search の filter type インデックスは「オブジェクトの配列」
  内のフィールドを直接インデックスできない制約があるため、フィルタ専用に
  文字列配列(country_codes)を別途持たせている（countries は詳細情報の保持用、
  country_codes はAtlas索引のfilter対象という役割分担）。

【処理フロー】
  1. data/ から最新の phase2_video_ids_*.json を読み込む（--input で指定も可）
  2. 全国分の video_id を平坦化（動画ごとに出現した全国を countries 配列として集約）
  3. videos.list を50件バッチで呼び、part="snippet,statistics,contentDetails" を取得
  4. 取得失敗した動画はスキップしログ出力
  5. data/phase3_metadata_YYYYMMDD_HHMMSS.json に保存

【出力JSONフォーマット】
  {
    "collected_at": "...",
    "source_file": "phase2_video_ids_xxx.json",
    "total_videos": 400,
    "missing_video_ids": [...],
    "videos": [
      {
        "video_id": "abc123",
        "countries": [
          {
            "country": "MX",
            "country_name_ja": "メキシコ",
            "country_name_en": "Mexico",
            "primary_lang": "es",
            "is_priority": true,
            "rank": 3
          },
          {
            "country": "AR",
            "country_name_ja": "アルゼンチン",
            "country_name_en": "Argentina",
            "primary_lang": "es",
            "is_priority": false,
            "rank": 7
          }
        ],
        "reach": 2,
        "country_codes": ["MX", "AR"],
        "title": "...",
        "description": "...",
        "published_at": "...",
        "duration_iso": "PT5M30S",
        "duration_seconds": 330,
        "thumbnail_url": "...",
        "channel_id": "...",
        "channel_title": "...",
        "tags": [...],
        "category_id": "17",
        "default_language": "es",
        "default_audio_language": "es",
        "stats": {
          "view_count": 12345,
          "like_count": 678,
          "comment_count": 90
        },
        "url": "https://www.youtube.com/watch?v=abc123",
        "embed_html": "<iframe ...></iframe>"
      },
      ...
    ]
  }

【APIクォータ】
  videos.list: 1 unit per call × (動画数 / 50) ≈ 9 units (405動画の場合)
"""

import os
import json
import time
import argparse
import re
import glob
from datetime import datetime, timezone
from pathlib import Path
from dotenv import load_dotenv
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from api_utils import execute_with_retry

# ==========================================
# 設定
# ==========================================

SCRIPT_DIR = Path(__file__).parent
DATA_DIR = SCRIPT_DIR / "data"

BATCH_SIZE = 50  # videos.list の最大ID数
SLEEP_BETWEEN_BATCHES = 0.5  # バッチ間スリープ

# ==========================================
# 関数
# ==========================================

def find_latest_phase2_file():
    """data/ から最新の phase2_video_ids_*.json を探す"""
    pattern = str(DATA_DIR / 'phase2_video_ids_*.json')
    files = sorted(glob.glob(pattern))
    if not files:
        return None
    return files[-1]


def parse_iso_duration_to_seconds(duration):
    """
    ISO 8601 duration (例: PT1H2M3S) を秒数に変換する。
    """
    if not duration:
        return 0
    match = re.match(
        r'^PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$',
        duration
    )
    if not match:
        return 0
    h, m, s = match.groups()
    return int(h or 0) * 3600 + int(m or 0) * 60 + int(s or 0)


def flatten_video_ids(phase2_data):
    """
    by_country 構造を平坦化して、video_id => 出現国メタデータ のマップを作る。

    割り当て方針 (b) 多対多: 同じ動画が複数国の検索結果に出た場合、
      出現した国すべてを保持する（単一国への割り当ては行わない）。
    各国エントリには、その国の viewCount順リストでの順位(rank, 0始まり)を持たせる。
    phase2 は order='viewCount' で取得しているので、video_ids 内の位置がそのまま順位。
    国の並び順は rank 昇順（同順位は priority国優先 → by_country の出現順）。
    """
    by_country = phase2_data['by_country']
    # by_country の出現順をタイブレーク用の序列に使う（phase2はcountries.json順で書き出す）
    order_index = {code: i for i, code in enumerate(by_country.keys())}

    # vid -> [country_entry, ...]（出現した全国を保持）
    video_countries = {}
    for country_code, country_data in by_country.items():
        is_pri = country_data.get('is_priority', False)
        for rank, vid in enumerate(country_data.get('video_ids', [])):
            entry = {
                'country': country_code,
                'country_name_ja': country_data.get('country_name_ja', ''),
                'country_name_en': country_data.get('country_name_en', ''),
                'primary_lang': country_data.get('primary_lang', ''),
                'is_priority': is_pri,
                'rank': rank,
            }
            video_countries.setdefault(vid, []).append(entry)

    # 各動画内で国を rank 昇順（同rankはpriority優先→出現順）に整列
    video_country_map = {}
    for vid, entries in video_countries.items():
        entries.sort(key=lambda e: (
            e['rank'],
            0 if e['is_priority'] else 1,
            order_index[e['country']],
        ))
        video_country_map[vid] = entries
    return video_country_map


def fetch_metadata_batch(youtube, video_ids, batch_label='batch'):
    """50件以下の動画IDをvideos.listで一括取得する（リトライ付き）"""
    if not video_ids:
        return []
    try:
        request = youtube.videos().list(
            part='snippet,statistics,contentDetails',
            id=','.join(video_ids),
            maxResults=BATCH_SIZE,
        )
        response = execute_with_retry(request, label=batch_label)
        return response.get('items', [])
    except HttpError as e:
        print(f"    APIエラー ({batch_label}): {e}")
        return []


def normalize_video_item(item, countries):
    """videos.listのレスポンス1件を保存用辞書に変換する。

    countries: flatten_video_ids が作る国エントリのリスト（出現順にソート済み）。
    reach はその場で len(countries) として確定し、以降のフェーズでは再計算しない。
    """
    snippet = item.get('snippet', {})
    stats = item.get('statistics', {})
    content = item.get('contentDetails', {})
    vid = item['id']

    duration_iso = content.get('duration', '')
    thumbnails = snippet.get('thumbnails', {})
    # 高解像度サムネを優先、無ければmedium、default
    thumb_url = (
        thumbnails.get('high', {}).get('url')
        or thumbnails.get('medium', {}).get('url')
        or thumbnails.get('default', {}).get('url', '')
    )

    return {
        'video_id': vid,
        'countries': countries,
        'country_codes': [c['country'] for c in countries],
        'reach': len(countries),
        'title': snippet.get('title', ''),
        'description': snippet.get('description', ''),
        'published_at': snippet.get('publishedAt', ''),
        'duration_iso': duration_iso,
        'duration_seconds': parse_iso_duration_to_seconds(duration_iso),
        'thumbnail_url': thumb_url,
        'channel_id': snippet.get('channelId', ''),
        'channel_title': snippet.get('channelTitle', ''),
        'tags': snippet.get('tags', []),
        'category_id': snippet.get('categoryId', ''),
        'default_language': snippet.get('defaultLanguage', ''),
        'default_audio_language': snippet.get('defaultAudioLanguage', ''),
        'stats': {
            'view_count': int(stats.get('viewCount', 0)),
            'like_count': int(stats.get('likeCount', 0)),
            'comment_count': int(stats.get('commentCount', 0)),
        },
        'url': f'https://www.youtube.com/watch?v={vid}',
        'embed_html': f'<iframe width="560" height="315" src="https://www.youtube.com/embed/{vid}" frameborder="0" allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture" allowfullscreen></iframe>',
    }


def main():
    parser = argparse.ArgumentParser(description='Phase 3: 動画メタデータ取得')
    parser.add_argument(
        '--input',
        type=str,
        default=None,
        help='Phase 2 のJSONファイルパス（省略時は最新を自動選択）'
    )
    args = parser.parse_args()

    # .env 読み込み
    load_dotenv(SCRIPT_DIR / '../../../.env')
    api_key = os.environ.get('YOUTUBE_API_GLC_KEY')
    if not api_key or api_key == 'your_api_key_here':
        print('エラー: .env に YOUTUBE_API_KEY を設定してください。')
        return

    # Phase 2 ファイル取得
    input_file = args.input or find_latest_phase2_file()
    if not input_file or not Path(input_file).exists():
        print(f'エラー: Phase 2 のJSONファイルが見つかりません: {input_file}')
        print('まず phase2_collect_video_ids.py を実行してください。')
        return

    print(f'入力ファイル: {input_file}')
    with open(input_file, 'r', encoding='utf-8') as f:
        phase2_data = json.load(f)

    # video_id を平坦化
    video_country_map = flatten_video_ids(phase2_data)
    all_video_ids = list(video_country_map.keys())
    print(f'ユニーク動画ID数: {len(all_video_ids)}')

    if not all_video_ids:
        print('動画IDが0件です。Phase 2の結果を確認してください。')
        return

    # YouTube API クライアント
    youtube = build('youtube', 'v3', developerKey=api_key)

    # バッチ処理
    all_videos = []
    fetched_ids = set()
    total_batches = (len(all_video_ids) + BATCH_SIZE - 1) // BATCH_SIZE

    for i in range(0, len(all_video_ids), BATCH_SIZE):
        batch_ids = all_video_ids[i:i + BATCH_SIZE]
        batch_num = i // BATCH_SIZE + 1
        print(f'バッチ {batch_num}/{total_batches}: {len(batch_ids)} 件取得中...')

        items = fetch_metadata_batch(youtube, batch_ids, batch_label=f'videos.list batch {batch_num}')
        for item in items:
            vid = item['id']
            countries = video_country_map.get(vid, [])
            all_videos.append(normalize_video_item(item, countries))
            fetched_ids.add(vid)

        print(f'  -> {len(items)} 件取得 (累計 {len(all_videos)})')

        # バッチ間のレート緩和
        if batch_num < total_batches:
            time.sleep(SLEEP_BETWEEN_BATCHES)

    # 取得できなかった動画ID (削除済み/非公開など)
    missing_ids = [vid for vid in all_video_ids if vid not in fetched_ids]
    if missing_ids:
        print(f'\n取得失敗: {len(missing_ids)} 件 (削除/非公開の可能性)')

    # 保存
    DATA_DIR.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    output_path = DATA_DIR / f'phase3_metadata_{timestamp}.json'

    output = {
        'collected_at': datetime.now(timezone.utc).isoformat(),
        'source_file': Path(input_file).name,
        'total_videos': len(all_videos),
        'missing_video_ids': missing_ids,
        'videos': all_videos,
    }

    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f'\n=== 完了 ===')
    print(f'  取得成功: {len(all_videos)} 件')
    print(f'  取得失敗: {len(missing_ids)} 件')
    print(f'  保存先: {output_path}')

    # 国別分布（延べ出現数: 1動画が複数国に出ればその数だけ加算）
    from collections import Counter
    country_dist = Counter()
    reach_dist = Counter()
    for v in all_videos:
        reach_dist[v.get('reach', 0)] += 1
        for c in v.get('countries', []):
            country_dist[c.get('country', '??')] += 1

    print(f'\n=== 国別 延べ出現本数（上位15）===')
    for code, n in country_dist.most_common(15):
        print(f'  {code:3s} {n:3d}')
    print(f'  出現のあった国数: {len(country_dist)}')

    print(f'\n=== reach分布（1動画が何カ国の検索結果に出たか）===')
    for reach, n in sorted(reach_dist.items()):
        pct = 100 * n / len(all_videos) if all_videos else 0
        print(f'  reach={reach:2d}  {n:3d}本  ({pct:4.1f}%)')


if __name__ == '__main__':
    main()
