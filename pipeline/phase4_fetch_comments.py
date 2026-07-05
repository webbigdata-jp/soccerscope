"""
phase4_fetch_comments.py
=========================
【目的】
  Phase 7 でバズスコア付与・上位500本をフラグ付けした動画群に対して、
  commentThreads.list でコメントを取得する。
  後段の Phase 5 (Gemini感情分析) のための入力データになる。

【処理フロー】
  1. data/ から最新の phase7_with_buzz_score_*.json を読み込む（--input で指定も可）
  2. is_buzz=true の動画のみ対象
  3. 各動画につき commentThreads.list で最大N件のトップレベルコメントを取得
     - 取得は order=relevance (関連度順、デフォルト)
  4. コメント無効/取得失敗の動画も comments_by_video に comments=[] の状態で
     含める（errorフィールドに理由を記録）。後段の3_analyze_comments.pyで
     コメントが無くてもタイトル・説明文だけでサッカー関連性チェックを行うため。
     errors辞書にも従来通り別途記録する。
  5. data/phase4_comments_YYYYMMDD_HHMMSS.json に保存

【出力JSONフォーマット】
  {
    "collected_at": "...",
    "source_file": "phase7_*.json",
    "max_comments_per_video": 100,
    "stats": {
      "total_target_videos": 500,
      "succeeded": 480,
      "comments_disabled": 15,
      "failed_other": 5,
      "total_comments": 47000
    },
    "comments_by_video": {
      "video_id_xxx": {
        "countries": [{"country": "MX", "country_name_ja": "...", ...}, ...],
        "country_codes": ["MX", "AR"],
        "reach": 2,
        "title": "...",
        "description": "...",
        "comment_count_fetched": 100,
        "error": null,
        "comments": [
          {
            "comment_id": "...",
            "text_original": "...",
            "author_display_name": "...",
            "like_count": 12,
            "published_at": "...",
            "updated_at": "...",
            "reply_count": 3
          },
          ...
        ]
      },
      ...
    },
    "errors": {
      "video_id_yyy": "commentsDisabled",
      ...
    }
  }

【APIクォータ】
  commentThreads.list: 1 unit × 動画数 ≈ 500 units (500動画の場合、各1ページ100件)
"""

import os
import json
import argparse
import glob
import time
from datetime import datetime, timezone
from pathlib import Path
from dotenv import load_dotenv
from googleapiclient.errors import HttpError

from api_utils import YouTubeKeyRotator, load_youtube_api_keys

# ==========================================
# 設定
# ==========================================

SCRIPT_DIR = Path(__file__).parent
DATA_DIR = SCRIPT_DIR / "data"

# 1動画あたり取得するコメント上限（commentThreads.listは最大100/ページ）
DEFAULT_MAX_COMMENTS_PER_VIDEO = 100
# APIリクエスト間のスリープ（429防止）。連続アクセスでの一時的なレート制限を
# 予防するため2秒に設定。1日の総クォータ超過(quotaExceeded)はスリープでは
# 解決しないため、YouTubeKeyRotator による複数キー切替で対応する。
SLEEP_BETWEEN_REQUESTS = 2.0

# ==========================================
# 関数
# ==========================================

def find_latest_phase7_file():
    pattern = str(DATA_DIR / 'phase7_with_buzz_score_*.json')
    files = sorted(glob.glob(pattern))
    return files[-1] if files else None


def fetch_comments_for_video(rotator, video_id, max_comments):
    """
    1動画のコメントを取得する（リトライ付き・キーローテーション付き）。

    戻り値: (comments_list, error_reason_or_None)
    """
    comments = []
    next_page_token = None
    pages_fetched = 0
    max_pages = (max_comments + 99) // 100  # 100/ページなので

    try:
        while len(comments) < max_comments and pages_fetched < max_pages:
            page_size = min(100, max_comments - len(comments))

            request_params = {
                'part': 'snippet',
                'videoId': video_id,
                'maxResults': page_size,
                'order': 'relevance',
                'textFormat': 'plainText',
            }
            if next_page_token:
                request_params['pageToken'] = next_page_token

            response = rotator.execute(
                lambda yt, params=dict(request_params): yt.commentThreads().list(**params),
                label=f'commentThreads {video_id} page{pages_fetched + 1}',
                verbose=False,  # 動画毎に大量出力されないよう抑制
            )

            for item in response.get('items', []):
                top = item.get('snippet', {}).get('topLevelComment', {}).get('snippet', {})
                comments.append({
                    'comment_id': item.get('id', ''),
                    'text_original': top.get('textDisplay', ''),
                    'author_display_name': top.get('authorDisplayName', ''),
                    'like_count': top.get('likeCount', 0),
                    'published_at': top.get('publishedAt', ''),
                    'updated_at': top.get('updatedAt', ''),
                    'reply_count': item.get('snippet', {}).get('totalReplyCount', 0),
                })

            pages_fetched += 1
            next_page_token = response.get('nextPageToken')
            if not next_page_token:
                break

        return comments[:max_comments], None

    except HttpError as e:
        # コメント無効を判定
        error_str = str(e)
        reason = None
        try:
            err_content = json.loads(e.content.decode('utf-8'))
            errors = err_content.get('error', {}).get('errors', [])
            if errors:
                reason = errors[0].get('reason', '')
        except Exception:
            pass

        if reason == 'commentsDisabled':
            return [], 'commentsDisabled'
        elif reason == 'videoNotFound':
            return [], 'videoNotFound'
        elif reason == 'forbidden':
            return [], f'forbidden:{error_str[:120]}'
        else:
            return [], f'httpError:{reason or e.resp.status}'


def main():
    parser = argparse.ArgumentParser(description='Phase 4: コメント取得')
    parser.add_argument(
        '--input',
        type=str,
        default=None,
        help='Phase 7 のJSONファイルパス（省略時は最新を自動選択）'
    )
    parser.add_argument(
        '--max-comments',
        type=int,
        default=DEFAULT_MAX_COMMENTS_PER_VIDEO,
        help=f'1動画あたりの最大コメント数 (デフォルト {DEFAULT_MAX_COMMENTS_PER_VIDEO})'
    )
    parser.add_argument(
        '--limit-videos',
        type=int,
        default=None,
        help='処理する動画数を制限（テスト用、未指定なら全is_buzz動画）'
    )
    args = parser.parse_args()

    # .env 読み込み
    load_dotenv(SCRIPT_DIR / '../../../.env')
    api_keys = load_youtube_api_keys()
    if not api_keys:
        print('エラー: .env にYOUTUBE_API_KEY_TEST を設定してください。')
        return
    print(f'利用可能なAPIキー数: {len(api_keys)}（quotaExceeded時に自動で次のキーへ切替）')

    # 入力
    input_file = args.input or find_latest_phase7_file()
    if not input_file or not Path(input_file).exists():
        print(f'エラー: Phase 7 のJSONファイルが見つかりません: {input_file}')
        print('先に phase7_calc_buzz_score.py を実行してください。')
        return

    print(f'入力ファイル: {input_file}')
    with open(input_file, 'r', encoding='utf-8') as f:
        phase7_data = json.load(f)

    videos = phase7_data.get('videos', [])
    target_videos = [v for v in videos if v.get('is_buzz')]
    if args.limit_videos:
        target_videos = target_videos[:args.limit_videos]

    print(f'コメント取得対象: {len(target_videos)} 動画 '
          f'(1動画あたり最大 {args.max_comments} コメント)')

    if not target_videos:
        print('is_buzz=true の動画がありません。Phase 7の結果を確認してください。')
        return

    # YouTube API クライアント（複数キーローテーション対応）
    rotator = YouTubeKeyRotator(api_keys)

    # 取得
    comments_by_video = {}
    errors = {}
    succeeded = 0
    comments_disabled = 0
    failed_other = 0
    total_comments = 0

    for idx, video in enumerate(target_videos, 1):
        vid = video['video_id']
        country_codes_str = ','.join(video.get('country_codes', [])) or '??'
        print(f"[{idx:4d}/{len(target_videos)}] {vid} "
              f"({country_codes_str}) "
              f"score={video.get('buzz_score',0):.2f} "
              f"comments={video.get('stats',{}).get('comment_count',0):,d} ... ",
              end='', flush=True)

        comments, error = fetch_comments_for_video(rotator, vid, args.max_comments)

        if error:
            errors[vid] = error
            if error == 'commentsDisabled':
                comments_disabled += 1
                print(f'[コメント無効]')
            else:
                failed_other += 1
                print(f'[エラー: {error[:60]}]')
            # コメントが0件/無効でも、3_analyze_comments.py 側でタイトル・説明文
            # だけを使ったサッカー関連性チェックは行いたいため、comments_by_video
            # にも(comments=[]の状態で)入れておく。errors辞書への記録は従来通り
            # 残す（どちらが欠けても困る用途があるため両方に書く）。
            comments_by_video[vid] = {
                'countries': video.get('countries', []),
                'country_codes': video.get('country_codes', []),
                'reach': video.get('reach', 0),
                'title': video.get('title', ''),
                'description': video.get('description', ''),
                'buzz_score': video.get('buzz_score', 0),
                'comment_count_fetched': 0,
                'error': error,
                'comments': [],
            }
        else:
            succeeded += 1
            total_comments += len(comments)
            comments_by_video[vid] = {
                'countries': video.get('countries', []),
                'country_codes': video.get('country_codes', []),
                'reach': video.get('reach', 0),
                'title': video.get('title', ''),
                'description': video.get('description', ''),
                'buzz_score': video.get('buzz_score', 0),
                'comment_count_fetched': len(comments),
                'error': None,
                'comments': comments,
            }
            print(f'OK ({len(comments)}件)')

        time.sleep(SLEEP_BETWEEN_REQUESTS)

    # 保存
    DATA_DIR.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    output_path = DATA_DIR / f'phase4_comments_{timestamp}.json'

    output = {
        'collected_at': datetime.now(timezone.utc).isoformat(),
        'source_file': Path(input_file).name,
        'max_comments_per_video': args.max_comments,
        'stats': {
            'total_target_videos': len(target_videos),
            'succeeded': succeeded,
            'comments_disabled': comments_disabled,
            'failed_other': failed_other,
            'total_comments': total_comments,
        },
        'comments_by_video': comments_by_video,
        'errors': errors,
    }

    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f'\n=== 完了 ===')
    print(f'  対象動画: {len(target_videos)}')
    print(f'  成功: {succeeded} / コメント無効: {comments_disabled} / 他エラー: {failed_other}')
    print(f'  累計コメント数: {total_comments:,}')
    print(f'  保存先: {output_path}')


if __name__ == '__main__':
    main()
