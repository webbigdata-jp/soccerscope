"""
phase7_calc_buzz_score.py
==========================
Purpose
  Calculate buzz scores for metadata obtained in Phase 3, add the buzz_score /
  is_buzz fields, and save the result.

Processing flow
  1. Load the latest phase3_metadata_*.json from data/ (or specify it with --input)
  2. Calculate the buzz score for each video
  3. Flag the top N videos as is_buzz=true (default: 500 videos, used in Phase 4)
  4. Save to data/phase7_with_buzz_score_YYYYMMDD_HHMMSS.json

Buzz score definition
  buzz_score = log10(view_count + 1) * 1.0
             + log10(like_count + 1) * 1.5
             + log10(comment_count + 1) * 2.0
             + elapsed-time bonus (newer videos receive higher scores)

  - log is used so the score is not dominated by videos with exceptionally high views
  - comment_count is weighted most heavily because comment analysis is the next step,
    so videos with more comments are more valuable
  - elapsed-time bonus: +2.0 within the last 24 hours, +1.0 within 7 days, otherwise 0

Output JSON
  Same format as Phase 3, with buzz_score and is_buzz added to each video.
  Also adds a top_500_video_ids list at the top level for Phase 4.
"""

import os
import json
import math
import argparse
import glob
from datetime import datetime, timezone
from pathlib import Path

# ==========================================
# Settings
# ==========================================

SCRIPT_DIR = Path(__file__).parent
DATA_DIR = SCRIPT_DIR / "data"

# Number of top videos to flag as buzz videos
DEFAULT_TOP_N = 500

# Score weights
WEIGHT_VIEWS = 1.0
WEIGHT_LIKES = 1.5
WEIGHT_COMMENTS = 2.0

# Elapsed-time bonus
BONUS_RECENT_24H = 2.0
BONUS_RECENT_7D = 1.0

# ==========================================
# Functions
# ==========================================


def find_latest_phase3_file():
    pattern = str(DATA_DIR / 'phase3_metadata_*.json')
    files = sorted(glob.glob(pattern))
    return files[-1] if files else None


def calc_recency_bonus(published_at_str, reference_dt):
    """Compare published_at (ISO8601) with reference_dt and return the recency bonus."""
    if not published_at_str:
        return 0.0
    try:
        # Replace 'Z' with '+00:00' before parsing.
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
    """Calculate the buzz score for one video."""
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
    parser = argparse.ArgumentParser(description='Phase 7: Calculate buzz scores')
    parser.add_argument(
        '--input',
        type=str,
        default=None,
        help='Path to the Phase 3 JSON file (automatically selects the latest file if omitted)'
    )
    parser.add_argument(
        '--top-n',
        type=int,
        default=DEFAULT_TOP_N,
        help=f'Number of top videos to set the is_buzz flag for (default {DEFAULT_TOP_N})'
    )
    args = parser.parse_args()

    # Input
    input_file = args.input or find_latest_phase3_file()
    if not input_file or not Path(input_file).exists():
        print(f'Error: Phase 3 JSON file was not found: {input_file}')
        return

    print(f'Input file: {input_file}')
    with open(input_file, 'r', encoding='utf-8') as f:
        phase3_data = json.load(f)

    videos = phase3_data.get('videos', [])
    if not videos:
        print('Video data is empty.')
        return

    print(f'Target videos: {len(videos)}')

    # Reference time for score calculation (current UTC)
    reference_dt = datetime.now(timezone.utc)

    # Add a score to each video.
    for v in videos:
        v['buzz_score'] = calc_buzz_score(v, reference_dt)
        v['is_buzz'] = False  # Initial value; set true for the top N videos later.

    # Sort by score descending.
    videos.sort(key=lambda x: x['buzz_score'], reverse=True)

    # Set is_buzz = True for the top N videos.
    top_n = min(args.top_n, len(videos))
    for v in videos[:top_n]:
        v['is_buzz'] = True

    top_video_ids = [v['video_id'] for v in videos[:top_n]]

    # Save
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
            'description': 'Sum of log10(count+1)*weight + elapsed-time bonus',
        },
        'videos': videos,
    }

    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    # Summary
    print(f'\n=== Completed ===')
    print(f'  Buzz flag assigned: {top_n} videos (top {top_n}/{len(videos)})')
    print(f'  Saved to: {output_path}')
    print(f'\n=== Buzz Score Top 10 ===')
    for i, v in enumerate(videos[:10], 1):
        codes_str = ','.join(v.get('country_codes', [])) or '??'
        print(f"  {i:2d}. [{codes_str:8s}] score={v['buzz_score']:6.2f} "
              f"views={v['stats']['view_count']:>10,d} "
              f"comments={v['stats']['comment_count']:>6,d} "
              f"| {v['title'][:60]}")


if __name__ == '__main__':
    main()
