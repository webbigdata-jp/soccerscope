"""
phase2_collect_video_ids.py
============================
【目的】
  countries.json の48カ国に対して、各国 regionCode で search.list を1回ずつ実行し、
  サッカー（W杯）関連動画のID候補プールを作成する。

【処理フロー】
  1. countries.json を読み込み
  2. 各国に対して、主要言語のサッカー語 + 共通の "World Cup" 系を OR で繋いだクエリを生成
  3. search.list を regionCode 指定で実行（最大50件 = 1ページ）
  4. 重要国 (is_priority=true) は max 20件、それ以外は max 5件にトリミング
  5. data/phase2_video_ids_YYYYMMDD.json に保存

【レート制御】
  - リクエスト間に SLEEP_BETWEEN_REQUESTS 秒のスリープ
  - 429 (rateLimitExceeded) は指数バックオフで最大3回リトライ
  - Retry-After ヘッダがあれば最優先

【--retry-from オプション】
  既存のphase2出力JSONを渡すと、エラーだった国 (error が非None または video_count=0)
  だけを再取得して結果をマージする。フルクォータ消費を避けたい場合に使う。

【APIクォータ】
  search.list: 100 units × 48カ国 = 4,800 units (フル実行時)
  --retry-from 使用時は失敗国数 × 100 units
"""

import os
import json
import time
import argparse
from datetime import datetime, timezone, timedelta
from pathlib import Path
from dotenv import load_dotenv
from googleapiclient.errors import HttpError

from api_utils import YouTubeKeyRotator, load_youtube_api_keys

# ==========================================
# 設定
# ==========================================

SCRIPT_DIR = Path(__file__).parent
DATA_DIR = SCRIPT_DIR / "data"
COUNTRIES_FILE = SCRIPT_DIR / "countries.json"

# search.list は maxResults を増やしてもクォータは1回100unitsで不変
# （コストはページ単位。1ページ最大50件）。順位ベースの国割り当て(phase3)を
# 公平にするため、全国一律で1ページ満タンの50件を取得する。
# is_priority は他用途のタグとして残すが、収集本数の出し分けには使わない。
MAX_RESULTS_PER_COUNTRY = 50
PAGE_MAX_RESULTS = 50

# 後方互換のため名前は残す（いずれも同値）
PRIORITY_MAX_RESULTS = MAX_RESULTS_PER_COUNTRY
NORMAL_MAX_RESULTS = MAX_RESULTS_PER_COUNTRY

# リクエスト間スリープ（秒）。429防止の基本対策。
# 連続アクセスによる一時的なレート制限(rateLimitExceeded等)を予防するため2秒に設定。
# なお1日の総クォータ超過(quotaExceeded)はスリープでは解決しないため、
# YouTubeKeyRotator による複数キー切替で対応する（役割分担は api_utils.py 参照）。
SLEEP_BETWEEN_REQUESTS = 2.0

# 共通検索語
COMMON_KEYWORDS = ['"World Cup 2026"', '"FIFA World Cup"']

# 除外語: "World Cup"系のフレーズはサッカー以外の競技（クリケット、バスケ、
# バレーボール等にも"World Cup"を冠した大会が存在する）にもヒットしてしまう
# ため、search.list の q パラメータの NOT(-) 演算子で明示的に除外する。
# 例: クリケットの "ICC Women's T20 World Cup" がサッカー検索に混入していた
# 実例があったため追加。除外語は他競技を狙い撃ちした最小限の単語に留め、
# サッカー動画自体の取りこぼしを増やさないようにする。
EXCLUDE_KEYWORDS = [
    'cricket', 'IPL', 'basketball', 'NBA', 'volleyball',
]

# ==========================================
# 関数
# ==========================================

def load_countries(path):
    with open(path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    return data['countries'], data['soccer_words']


def build_query(country, soccer_words):
    lang = country['primary_lang']
    soccer_word = soccer_words.get(lang, 'football')
    parts = [soccer_word] + COMMON_KEYWORDS
    query = ' | '.join(parts)
    if EXCLUDE_KEYWORDS:
        # NOT(-)演算子は ' -単語' の形式で末尾に追加する（公式ドキュメント準拠）。
        # 例: 'futbol | "World Cup 2026" | "FIFA World Cup" -cricket -IPL -basketball -NBA -volleyball'
        exclude_str = ' '.join(f'-{w}' for w in EXCLUDE_KEYWORDS)
        query = f'{query} {exclude_str}'
    return query


def search_videos_for_country(rotator, country, soccer_words, published_after, max_results, label,
                               published_before=None):
    """1カ国分の動画IDを取得する（リトライ付き・キーローテーション付き）。

    published_before を指定すると publishedAfter との範囲指定になり、
    特定の過去日に投稿された動画に絞り込める（--target-date 用）。
    ただし view_count/再生数順位は常にAPI呼び出し時点(=今日)の値である点に
    注意（過去のその時点のバズ状態そのものは復元できない。引き継ぎメモ参照）。
    """
    query = build_query(country, soccer_words)
    region_code = country['code']
    if region_code == 'SC':
        region_code = 'GB'

    base_params = {
        'q': query,
        'part': 'id',
        'type': 'video',
        'maxResults': min(max_results, PAGE_MAX_RESULTS),
        'order': 'viewCount',
        'publishedAfter': published_after,
        'relevanceLanguage': country['primary_lang'],
    }
    if published_before:
        base_params['publishedBefore'] = published_before

    # 1段目: regionCode 付き
    try:
        response = rotator.execute(
            lambda yt: yt.search().list(regionCode=region_code, **base_params),
            label=f'{label} (region={region_code})',
        )
        items = response.get('items', [])
        video_ids = [item['id']['videoId'] for item in items if 'videoId' in item.get('id', {})]
        return {
            'query': query,
            'video_ids': video_ids[:max_results],
            'video_count': len(video_ids[:max_results]),
            'error': None,
        }
    except HttpError as e:
        error_reason = str(e)[:200]
        print(f"    1段目失敗 ({country['code']}): {error_reason[:120]}")

        # 2段目: regionCode 外して言語フォールバック
        try:
            response = rotator.execute(
                lambda yt: yt.search().list(**base_params),
                label=f'{label} (lang-fallback)',
            )
            items = response.get('items', [])
            video_ids = [item['id']['videoId'] for item in items if 'videoId' in item.get('id', {})]
            print(f'    -> 言語フォールバック成功: {len(video_ids)}件')
            return {
                'query': query,
                'video_ids': video_ids[:max_results],
                'video_count': len(video_ids[:max_results]),
                'error': f'regionCode_unsupported_fallback_to_language: {error_reason}',
            }
        except HttpError as e2:
            return {
                'query': query,
                'video_ids': [],
                'video_count': 0,
                'error': f'both_failed: {str(e2)[:200]}',
            }


def is_failed_entry(entry):
    """既存JSONエントリが「失敗」か判定する。再取得対象を選ぶ用。"""
    if entry is None:
        return True
    if entry.get('error'):
        err = entry.get('error', '')
        # regionCode重複スキップは再取得不要
        if err.startswith('skipped_duplicate_regionCode_'):
            return False
        # 言語フォールバックが成功して件数も取れている場合は再取得不要
        if err.startswith('regionCode_unsupported_fallback_to_language') and entry.get('video_count', 0) > 0:
            return False
        return True
    if entry.get('video_count', 0) == 0:
        return True
    return False


def main():
    parser = argparse.ArgumentParser(description='Phase 2: 48カ国の動画IDを収集')
    parser.add_argument('--days', type=int, default=2,
                        help='publishedAfter の日数(過去N日)。デフォルト2日。--target-date指定時も「何日分」として使う')
    parser.add_argument('--target-date', type=str, default=None,
                        help='YYYY-MM-DD形式。指定すると、その日のUTC0時を基準に '
                             '[target-date - days日, target-date] の範囲で publishedAfter/'
                             'publishedBefore を設定する（過去日の取り直し用）。'
                             '省略時は従来通り「今からdays日前」のpublishedAfterのみ。'
                             '注意: view_count等の統計値・順位は常に実行時点(=今日)の値になる。')
    parser.add_argument('--dry-run', action='store_true',
                        help='APIを叩かずクエリだけ表示')
    parser.add_argument('--retry-from', type=str, default=None,
                        help='既存phase2出力JSONを指定して失敗国のみ再取得する')
    parser.add_argument('--sleep', type=float, default=SLEEP_BETWEEN_REQUESTS,
                        help=f'リクエスト間スリープ秒 (デフォルト {SLEEP_BETWEEN_REQUESTS})')
    args = parser.parse_args()

    load_dotenv(SCRIPT_DIR / '../../../.env')
    api_keys = load_youtube_api_keys()
    if not api_keys:
        print('エラー: .env にYOUTUBE_API_KEY_TEST を設定してください。')
        return
    print(f'利用可能なAPIキー数: {len(api_keys)}（quotaExceeded時に自動で次のキーへ切替）')

    countries, soccer_words = load_countries(COUNTRIES_FILE)
    print(f'対象国数: {len(countries)} カ国')
    priority_count = sum(1 for c in countries if c.get('is_priority'))
    print(f'  - 収集本数: 全国一律 max {MAX_RESULTS_PER_COUNTRY} 本/国（is_priorityは本数に影響しない）')
    print(f'  - うち is_priority: {priority_count} カ国（タグのみ）')
    print(f'  - リクエスト間スリープ: {args.sleep} 秒')

    if args.target_date:
        try:
            target_dt = datetime.strptime(args.target_date, '%Y-%m-%d').replace(tzinfo=timezone.utc)
        except ValueError:
            print(f'エラー: --target-date は YYYY-MM-DD 形式で指定してください: {args.target_date}')
            return
        published_before_dt = target_dt
        published_after_dt = target_dt - timedelta(days=args.days)
        published_before = published_before_dt.strftime('%Y-%m-%dT%H:%M:%SZ')
        published_after = published_after_dt.strftime('%Y-%m-%dT%H:%M:%SZ')
        print(f'検索期間: {published_after} 〜 {published_before} '
              f'(--target-date={args.target_date} 基準, UTC0時)')
        print('  注意: view_count等の統計値・再生数順位は実行時点(今日)の値です。'
              '過去その時点のバズ状態そのものは復元できません。')
    else:
        published_before = None
        published_after_dt = datetime.now(timezone.utc) - timedelta(days=args.days)
        published_after = published_after_dt.strftime('%Y-%m-%dT%H:%M:%SZ')
        print(f'検索期間: {published_after} 以降')

    # --retry-from 処理: 既存JSON読込
    existing_results = {}
    if args.retry_from:
        retry_path = Path(args.retry_from)
        if not retry_path.exists():
            print(f'エラー: --retry-from で指定したファイルが見つかりません: {retry_path}')
            return
        with open(retry_path, 'r', encoding='utf-8') as f:
            existing = json.load(f)
        existing_results = existing.get('by_country', {})
        print(f'既存ファイル: {retry_path.name} (国数 {len(existing_results)})')
        # 検索期間も既存ファイルと揃える（時系列ずれを防ぐ）
        existing_published_after = existing.get('published_after')
        if existing_published_after:
            published_after = existing_published_after
            print(f'  検索期間(after)は既存ファイルに揃える: {published_after}')
        existing_published_before = existing.get('published_before')
        if existing_published_before:
            published_before = existing_published_before
            print(f'  検索期間(before)は既存ファイルに揃える: {published_before}')

        failed_codes = [c['code'] for c in countries
                        if is_failed_entry(existing_results.get(c['code']))]
        print(f'  再取得対象: {len(failed_codes)} カ国 ({", ".join(failed_codes)})')
        target_countries = [c for c in countries if c['code'] in failed_codes]
    else:
        target_countries = countries
    print()

    if args.dry_run:
        print('=== DRY RUN: クエリ一覧 ===')
        for c in target_countries:
            q = build_query(c, soccer_words)
            max_r = MAX_RESULTS_PER_COUNTRY
            print(f"  [{c['code']}] {c['name_ja']:20s} "
                  f"lang={c['primary_lang']:3s} max={max_r:3d} q={q}")
        return

    if not target_countries:
        print('再取得対象が0カ国です。終了します。')
        return

    rotator = YouTubeKeyRotator(api_keys)

    # 既存結果を初期値として持つ
    results = dict(existing_results)
    processed_region_codes = set()

    # --retry-from で既に成功していた国の regionCode は処理済みに登録
    if args.retry_from:
        for code, entry in existing_results.items():
            if not is_failed_entry(entry):
                eff = 'GB' if code == 'SC' else code
                processed_region_codes.add(eff)

    for idx, country in enumerate(target_countries, 1):
        code = country['code']
        is_priority = country.get('is_priority', False)
        max_results = MAX_RESULTS_PER_COUNTRY  # 全国一律
        effective_region = 'GB' if code == 'SC' else code

        if effective_region in processed_region_codes:
            print(f"[{idx:2d}/{len(target_countries)}] {code} ({country['name_ja']}) ... "
                  f"regionCode={effective_region} 既処理スキップ")
            results[code] = {
                'country_name_ja': country['name_ja'],
                'country_name_en': country['name_en'],
                'primary_lang': country['primary_lang'],
                'is_priority': is_priority,
                'query': build_query(country, soccer_words),
                'video_ids': [],
                'video_count': 0,
                'error': f'skipped_duplicate_regionCode_{effective_region}',
            }
            continue

        priority_tag = '★' if is_priority else ' '
        print(f"[{idx:2d}/{len(target_countries)}] {priority_tag} {code} ({country['name_ja']}) "
              f"lang={country['primary_lang']} max={max_results} ...")

        result = search_videos_for_country(
            rotator, country, soccer_words, published_after, max_results,
            label=f'search {code}', published_before=published_before,
        )
        results[code] = {
            'country_name_ja': country['name_ja'],
            'country_name_en': country['name_en'],
            'primary_lang': country['primary_lang'],
            'is_priority': is_priority,
            **result,
        }
        processed_region_codes.add(effective_region)
        print(f"    -> {result['video_count']} 件取得")

        # 各リクエスト後にレート緩和スリープ
        if idx < len(target_countries):
            time.sleep(args.sleep)

    # 全国の累計
    total_videos = sum(v.get('video_count', 0) for v in results.values())

    # 保存
    DATA_DIR.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    if args.target_date:
        output_path = DATA_DIR / f'phase2_video_ids_{args.target_date.replace("-", "")}_{timestamp}.json'
    else:
        output_path = DATA_DIR / f'phase2_video_ids_{timestamp}.json'

    output = {
        'collected_at': datetime.now(timezone.utc).isoformat(),
        'published_after': published_after,
        'published_before': published_before,
        'target_date': args.target_date,
        'total_videos': total_videos,
        'max_per_country': MAX_RESULTS_PER_COUNTRY,
        'priority_max_per_country': MAX_RESULTS_PER_COUNTRY,
        'normal_max_per_country': MAX_RESULTS_PER_COUNTRY,
        'retry_from': args.retry_from,
        'by_country': results,
    }

    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f'\n=== 完了 ===')
    print(f'  合計動画ID数: {total_videos}')
    print(f'  保存先: {output_path}')

    error_countries = [c for c, v in results.items() if is_failed_entry(v)]
    if error_countries:
        print(f'  失敗した国: {len(error_countries)} カ国 ({", ".join(error_countries)})')
        print(f'  -> 失敗国だけ再取得するには:')
        print(f'     python {Path(__file__).name} --retry-from {output_path}')
    else:
        print(f'  全国成功 ✓')


if __name__ == '__main__':
    main()
