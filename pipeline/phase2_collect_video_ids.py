"""
phase2_collect_video_ids.py
============================
Purpose
  For the 48 countries in countries.json, run search.list once per country with each
  country's regionCode, and create a candidate pool of soccer/World Cup-related
  video IDs.

Processing flow
  1. Load countries.json
  2. For each country, build a query by joining the main-language soccer term and
     common "World Cup" terms with OR
  3. Run search.list with regionCode specified (up to 50 results = 1 page)
  4. Trim important countries (is_priority=true) to max 20 results, and all others
     to max 5 results
  5. Save to data/phase2_video_ids_YYYYMMDD.json

Rate control
  - Sleep for SLEEP_BETWEEN_REQUESTS seconds between requests
  - Retry 429 (rateLimitExceeded) up to 3 times with exponential backoff
  - Prioritize the Retry-After header if present

--retry-from option
  If an existing Phase 2 output JSON is passed, only countries that failed
  (error is non-None or video_count=0) are fetched again, and the results are
  merged. Use this to avoid consuming the full quota.

API quota
  search.list: 100 units x 48 countries = 4,800 units (for a full run)
  With --retry-from: number of failed countries x 100 units
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
# Settings
# ==========================================

SCRIPT_DIR = Path(__file__).parent
DATA_DIR = SCRIPT_DIR / "data"
COUNTRIES_FILE = SCRIPT_DIR / "countries.json"

# search.list costs 100 quota units per request even if maxResults is increased.
# Cost is per page, and one page can contain up to 50 results. To make the
# rank-based country allocation in Phase 3 fair, fetch a full 50-result page for
# every country uniformly.
# Keep is_priority as a tag for other purposes, but do not use it to vary the
# number of collected videos.
MAX_RESULTS_PER_COUNTRY = 50
PAGE_MAX_RESULTS = 50

# Keep these names for backward compatibility. Both values are the same.
PRIORITY_MAX_RESULTS = MAX_RESULTS_PER_COUNTRY
NORMAL_MAX_RESULTS = MAX_RESULTS_PER_COUNTRY

# Sleep between requests in seconds. This is the basic safeguard against 429.
# Set to 2 seconds to prevent temporary rate limits caused by rapid consecutive
# access, such as rateLimitExceeded.
# Daily quota exhaustion (quotaExceeded) cannot be solved by sleeping, so it is
# handled by switching among multiple keys via YouTubeKeyRotator. See api_utils.py
# for the division of responsibilities.
SLEEP_BETWEEN_REQUESTS = 2.0

# Common search terms
COMMON_KEYWORDS = ['"World Cup 2026"', '"FIFA World Cup"']

# Exclusion terms: "World Cup" phrases can also match non-soccer sports such as
# cricket, basketball, and volleyball, because those sports also have tournaments
# named "World Cup". Explicitly exclude them with the NOT (-) operator in the
# search.list q parameter.
# Example: this was added after actual contamination by cricket videos such as
# "ICC Women's T20 World Cup". Keep exclusion terms to the minimum set targeting
# other sports so soccer videos are not unnecessarily missed.
EXCLUDE_KEYWORDS = [
    'cricket', 'IPL', 'basketball', 'NBA', 'volleyball',
]

# ==========================================
# Functions
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
        # Add the NOT (-) operator at the end in the form ' -word', following the
        # official documentation.
        # Example: 'futbol | "World Cup 2026" | "FIFA World Cup" -cricket -IPL -basketball -NBA -volleyball'
        exclude_str = ' '.join(f'-{w}' for w in EXCLUDE_KEYWORDS)
        query = f'{query} {exclude_str}'
    return query


def search_videos_for_country(rotator, country, soccer_words, published_after, max_results, label,
                               published_before=None):
    """Fetch video IDs for one country, with retries and key rotation.

    If published_before is specified, it becomes a range together with
    publishedAfter, allowing videos posted on a specific past date to be narrowed
    down for --target-date.
    Note, however, that view_count and view-count ranking are always values from
    the time of the API call (= today). The buzz state at that exact point in the
    past cannot be reconstructed. See the handoff notes.
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

    # First step: with regionCode
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
        print(f"    First step failed ({country['code']}): {error_reason[:120]}")

        # Second step: remove regionCode and fall back to language
        try:
            response = rotator.execute(
                lambda yt: yt.search().list(**base_params),
                label=f'{label} (lang-fallback)',
            )
            items = response.get('items', [])
            video_ids = [item['id']['videoId'] for item in items if 'videoId' in item.get('id', {})]
            print(f'    -> Language fallback succeeded: {len(video_ids)} items')
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
    """Determine whether an existing JSON entry is a failure for retry selection."""
    if entry is None:
        return True
    if entry.get('error'):
        err = entry.get('error', '')
        # A skipped duplicate regionCode does not need to be fetched again.
        if err.startswith('skipped_duplicate_regionCode_'):
            return False
        # If language fallback succeeded and returned results, it does not need to
        # be fetched again.
        if err.startswith('regionCode_unsupported_fallback_to_language') and entry.get('video_count', 0) > 0:
            return False
        return True
    if entry.get('video_count', 0) == 0:
        return True
    return False


def main():
    parser = argparse.ArgumentParser(description='Phase 2: Collect video IDs from 48 countries')
    parser.add_argument('--days', type=int, default=2,
                        help='Number of days for publishedAfter (past N days). Default: 2 days. Also used as the number of days when --target-date is specified')
    parser.add_argument('--target-date', type=str, default=None,
                        help='YYYY-MM-DD format. If specified, use UTC midnight on that day as the reference and set '
                             'publishedAfter/publishedBefore for the range [target-date - days, target-date] '
                             '(for re-fetching past dates). '
                             'If omitted, only publishedAfter is set to "days ago from now" as before. '
                             'Note: statistics such as view_count and ranking are always values at execution time (= today).')
    parser.add_argument('--dry-run', action='store_true',
                        help='Show queries only without calling the API')
    parser.add_argument('--retry-from', type=str, default=None,
                        help='Specify an existing Phase 2 output JSON and re-fetch only failed countries')
    parser.add_argument('--sleep', type=float, default=SLEEP_BETWEEN_REQUESTS,
                        help=f'Sleep seconds between requests (default {SLEEP_BETWEEN_REQUESTS})')
    args = parser.parse_args()

    load_dotenv(SCRIPT_DIR / '../../../.env')
    api_keys = load_youtube_api_keys()
    if not api_keys:
        print('Error: Set YOUTUBE_API_KEY_TEST in .env.')
        return
    print(f'Available API keys: {len(api_keys)} (automatically switches to the next key on quotaExceeded)')

    countries, soccer_words = load_countries(COUNTRIES_FILE)
    print(f'Target countries: {len(countries)}')
    priority_count = sum(1 for c in countries if c.get('is_priority'))
    print(f'  - Collection size: uniform max {MAX_RESULTS_PER_COUNTRY} videos/country (is_priority does not affect the count)')
    print(f'  - is_priority countries: {priority_count} (tag only)')
    print(f'  - Sleep between requests: {args.sleep} seconds')

    if args.target_date:
        try:
            target_dt = datetime.strptime(args.target_date, '%Y-%m-%d').replace(tzinfo=timezone.utc)
        except ValueError:
            print(f'Error: --target-date must be specified in YYYY-MM-DD format: {args.target_date}')
            return
        published_before_dt = target_dt
        published_after_dt = target_dt - timedelta(days=args.days)
        published_before = published_before_dt.strftime('%Y-%m-%dT%H:%M:%SZ')
        published_after = published_after_dt.strftime('%Y-%m-%dT%H:%M:%SZ')
        print(f'Search period: {published_after} to {published_before} '
              f'(based on --target-date={args.target_date}, UTC midnight)')
        print('  Note: statistics such as view_count and view-count ranking are values at execution time (today). '
              'The buzz state at that exact point in the past cannot be reconstructed.')
    else:
        published_before = None
        published_after_dt = datetime.now(timezone.utc) - timedelta(days=args.days)
        published_after = published_after_dt.strftime('%Y-%m-%dT%H:%M:%SZ')
        print(f'Search period: after {published_after}')

    # --retry-from processing: load existing JSON
    existing_results = {}
    if args.retry_from:
        retry_path = Path(args.retry_from)
        if not retry_path.exists():
            print(f'Error: The file specified by --retry-from was not found: {retry_path}')
            return
        with open(retry_path, 'r', encoding='utf-8') as f:
            existing = json.load(f)
        existing_results = existing.get('by_country', {})
        print(f'Existing file: {retry_path.name} ({len(existing_results)} countries)')
        # Align the search period with the existing file to avoid time-series drift.
        existing_published_after = existing.get('published_after')
        if existing_published_after:
            published_after = existing_published_after
            print(f'  Aligning search period (after) with existing file: {published_after}')
        existing_published_before = existing.get('published_before')
        if existing_published_before:
            published_before = existing_published_before
            print(f'  Aligning search period (before) with existing file: {published_before}')

        failed_codes = [c['code'] for c in countries
                        if is_failed_entry(existing_results.get(c['code']))]
        print(f'  Re-fetch targets: {len(failed_codes)} countries ({", ".join(failed_codes)})')
        target_countries = [c for c in countries if c['code'] in failed_codes]
    else:
        target_countries = countries
    print()

    if args.dry_run:
        print('=== DRY RUN: Query list ===')
        for c in target_countries:
            q = build_query(c, soccer_words)
            max_r = MAX_RESULTS_PER_COUNTRY
            print(f"  [{c['code']}] {c['name_ja']:20s} "
                  f"lang={c['primary_lang']:3s} max={max_r:3d} q={q}")
        return

    if not target_countries:
        print('There are 0 countries to re-fetch. Exiting.')
        return

    rotator = YouTubeKeyRotator(api_keys)

    # Use existing results as the initial value.
    results = dict(existing_results)
    processed_region_codes = set()

    # For --retry-from, register the regionCodes of countries that already
    # succeeded as processed.
    if args.retry_from:
        for code, entry in existing_results.items():
            if not is_failed_entry(entry):
                eff = 'GB' if code == 'SC' else code
                processed_region_codes.add(eff)

    for idx, country in enumerate(target_countries, 1):
        code = country['code']
        is_priority = country.get('is_priority', False)
        max_results = MAX_RESULTS_PER_COUNTRY  # Uniform across all countries
        effective_region = 'GB' if code == 'SC' else code

        if effective_region in processed_region_codes:
            print(f"[{idx:2d}/{len(target_countries)}] {code} ({country['name_ja']}) ... "
                  f"regionCode={effective_region} already processed; skipping")
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
        print(f"    -> Retrieved {result['video_count']} items")

        # Sleep after each request to reduce rate pressure.
        if idx < len(target_countries):
            time.sleep(args.sleep)

    # Nationwide cumulative total
    total_videos = sum(v.get('video_count', 0) for v in results.values())

    # Save
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

    print(f'\n=== Completed ===')
    print(f'  Total video IDs: {total_videos}')
    print(f'  Saved to: {output_path}')

    error_countries = [c for c, v in results.items() if is_failed_entry(v)]
    if error_countries:
        print(f'  Failed countries: {len(error_countries)} ({", ".join(error_countries)})')
        print(f'  -> To re-fetch only failed countries:')
        print(f'     python {Path(__file__).name} --retry-from {output_path}')
    else:
        print(f'  All countries succeeded ✓')


if __name__ == '__main__':
    main()
