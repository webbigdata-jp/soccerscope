"""
phase7_calc_buzz_score.py
==========================
【目的】
  Phase 3 で取得したメタデータに対してバズスコアを計算し、
  buzz_score / is_buzz フィールドを付与して保存する。

【処理フロー】
  1. data/ から最新の phase3_metadata_*.json を読み込む（--input で指定も可）
  2. 各動画にバズスコアを計算
  3. 上位N件を is_buzz=true としてフラグ付け（デフォルト500件、Phase 4で使用）
  4. data/phase7_with_buzz_score_YYYYMMDD_HHMMSS.json に保存

【バズスコアの定義】
  バズスコア = log10(view_count + 1) * 1.0
             + log10(like_count + 1) * 1.5
             + log10(comment_count + 1) * 2.0
             + 経過時間ペナルティ (新しいほど高得点)

  - log を使うのは桁外れの再生数動画に引きずられないため
  - コメント数を最重視（コメント分析が次工程なので、コメント多い動画ほど価値が高い）
  - 経過時間ペナルティ: 直近24時間以内なら +2.0、7日以内+1.0、それ以上0

【出力JSON】
  Phase 3と同形式 + 各videoに buzz_score, is_buzz を追加。
  さらに top_500_video_ids リストをトップレベルに追加（Phase 4で参照）。
"""

import os
import json
import math
import argparse
import glob
from datetime import datetime, timezone
from pathlib import Path

# ==========================================
# 設定
# ==========================================

SCRIPT_DIR = Path(__file__).parent
DATA_DIR = SCRIPT_DIR / "data"

# バズフラグを立てる上位件数
DEFAULT_TOP_N = 500

# スコア重み
WEIGHT_VIEWS = 1.0
WEIGHT_LIKES = 1.5
WEIGHT_COMMENTS = 2.0

# 経過時間ボーナス
BONUS_RECENT_24H = 2.0
BONUS_RECENT_7D = 1.0

# ==========================================
# 関数
# ==========================================

def find_latest_phase3_file():
    pattern = str(DATA_DIR / 'phase3_metadata_*.json')
    files = sorted(glob.glob(pattern))
    return files[-1] if files else None


def calc_recency_bonus(published_at_str, reference_dt):
    """published_at(ISO8601) と reference_dt を比較し、経過時間ボーナスを返す"""
    if not published_at_str:
        return 0.0
    try:
        # 'Z' を '+00:00' に置換してパース
        pub_dt = datetime.fromisoformat(published_at_str.replace('Z', '+00:00'))
        if pub_dt.tzinfo is None:
            pub_dt = pub_dt.replace(tzinfo=timezone.utc)
        delta = reference_dt - pub_dt
        hours = delta.total_seconds() / 3600.0
        if hours < 24:
            return BONUS_RECENT_24H
        elif hours < 24 * 7:
            return BONUS_RECENT_7D
        else:
            return 0.0
    except (ValueError, TypeError):
        return 0.0


def calc_buzz_score(video, reference_dt):
    """1動画のバズスコアを計算する"""
    stats = video.get('stats', {})
    views = stats.get('view_count', 0)
    likes = stats.get('like_count', 0)
    comments = stats.get('comment_count', 0)

    score = (
        math.log10(views + 1) * WEIGHT_VIEWS
        + math.log10(likes + 1) * WEIGHT_LIKES
        + math.log10(comments + 1) * WEIGHT_COMMENTS
        + calc_recency_bonus(video.get('published_at', ''), reference_dt)
    )
    return round(score, 4)


def main():
    parser = argparse.ArgumentParser(description='Phase 7: バズスコア計算')
    parser.add_argument(
        '--input',
        type=str,
        default=None,
        help='Phase 3 のJSONファイルパス（省略時は最新を自動選択）'
    )
    parser.add_argument(
        '--top-n',
        type=int,
        default=DEFAULT_TOP_N,
        help=f'is_buzz フラグを立てる上位件数 (デフォルト {DEFAULT_TOP_N})'
    )
    args = parser.parse_args()

    # 入力
    input_file = args.input or find_latest_phase3_file()
    if not input_file or not Path(input_file).exists():
        print(f'エラー: Phase 3 のJSONファイルが見つかりません: {input_file}')
        return

    print(f'入力ファイル: {input_file}')
    with open(input_file, 'r', encoding='utf-8') as f:
        phase3_data = json.load(f)

    videos = phase3_data.get('videos', [])
    if not videos:
        print('動画データが0件です。')
        return

    print(f'対象動画数: {len(videos)}')

    # スコア計算の基準時刻 (現在UTC)
    reference_dt = datetime.now(timezone.utc)

    # 各動画にスコア付与
    for v in videos:
        v['buzz_score'] = calc_buzz_score(v, reference_dt)
        v['is_buzz'] = False  # 初期値、後で上位N件にtrueをセット

    # スコア降順ソート
    videos.sort(key=lambda x: x['buzz_score'], reverse=True)

    # 上位N件に is_buzz = True
    top_n = min(args.top_n, len(videos))
    for v in videos[:top_n]:
        v['is_buzz'] = True

    top_video_ids = [v['video_id'] for v in videos[:top_n]]

    # 保存
    DATA_DIR.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    output_path = DATA_DIR / f'phase7_with_buzz_score_{timestamp}.json'

    output = {
        'collected_at': datetime.now(timezone.utc).isoformat(),
        'source_file': Path(input_file).name,
        'total_videos': len(videos),
        'top_n': top_n,
        'top_video_ids': top_video_ids,
        'score_formula': {
            'view_weight': WEIGHT_VIEWS,
            'like_weight': WEIGHT_LIKES,
            'comment_weight': WEIGHT_COMMENTS,
            'recency_bonus_24h': BONUS_RECENT_24H,
            'recency_bonus_7d': BONUS_RECENT_7D,
            'description': 'log10(count+1)*weight の総和 + 経過時間ボーナス',
        },
        'videos': videos,
    }

    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    # サマリ
    print(f'\n=== 完了 ===')
    print(f'  バズフラグ付与: {top_n} 件 (上位 {top_n}/{len(videos)})')
    print(f'  保存先: {output_path}')
    print(f'\n=== バズスコア Top 10 ===')
    for i, v in enumerate(videos[:10], 1):
        codes_str = ','.join(v.get('country_codes', [])) or '??'
        print(f"  {i:2d}. [{codes_str:8s}] score={v['buzz_score']:6.2f} "
              f"views={v['stats']['view_count']:>10,d} "
              f"comments={v['stats']['comment_count']:>6,d} "
              f"| {v['title'][:60]}")


if __name__ == '__main__':
    main()
