"""
api_utils.py
=============
Common retry and rate-control utilities for the YouTube Data API.

Features:
  - execute_with_retry(request, ...): Executes googleapiclient requests with
    exponential backoff and jitter.
  - For 429 (rateLimitExceeded), the Retry-After header is honored first when present.
  - Temporary 500/502/503/504 server errors are retried automatically.
  - 403 quotaExceeded fails immediately because retrying is not useful.
  - YouTubeKeyRotator: A wrapper that keeps multiple API keys and automatically
    switches to the next key when quotaExceeded (the daily quota is exhausted)
    is detected. Temporary rate limits such as rateLimitExceeded/userRateLimitExceeded
    are not key-rotation targets; execute_with_retry absorbs them through
    exponential backoff. Division of responsibilities: temporary limits use
    backoff; daily quota exhaustion uses key rotation.

Usage with a single key, as before:
  from api_utils import execute_with_retry

  request = youtube.search().list(...)
  response = execute_with_retry(request, label='search MX')

Usage with multiple-key rotation:
  from api_utils import YouTubeKeyRotator

  rotator = YouTubeKeyRotator(['KEY1_VALUE', 'KEY2_VALUE', 'KEY3_VALUE'])
  response = rotator.execute(
      lambda youtube: youtube.search().list(...),
      label='search MX',
  )
  # When quotaExceeded occurs, the rotator switches internally to a YouTube client
  # built with the next key, rebuilds the same request, and retries it. If all keys
  # are exhausted, it re-raises the exception.
"""

import os
import time
import random
import json
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


# Retryable HTTP status codes
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

# Default retry settings
DEFAULT_MAX_ATTEMPTS = 4        # First attempt + 3 retries
DEFAULT_BASE_DELAY = 2.0        # Initial backoff seconds
DEFAULT_MAX_DELAY = 60.0        # Upper limit
DEFAULT_JITTER_RATIO = 0.3      # +/-30% jitter


def _extract_error_reason(http_error):
    """
    Extract the reason string from an HttpError. Return None if it cannot be read.

    Google API error responses have two old and new formats:
      Old: {"error": {"errors": [{"reason": "quotaExceeded", ...}], ...}}
      New: {"error": {"status": "RESOURCE_EXHAUSTED", "details": [...], ...}}
    To make matters trickier, even actual daily quota exhaustion has been observed
    returning "rateLimitExceeded" in errors[].reason, which normally means a
    temporary per-second or per-minute limit. For example, the response can contain
    a message like "Quota exceeded for ... 'Search Queries per day' ..." together
    with reason="rateLimitExceeded". Because the reason string alone is not a
    reliable way to distinguish temporary limits from permanent exhaustion, treat
    status: "RESOURCE_EXHAUSTED" as the highest-priority signal. If it is present,
    normalize the error to quotaExceeded regardless of the reason contents because
    status is the more authoritative source.
    """
    try:
        content = json.loads(http_error.content.decode('utf-8'))
        error_obj = content.get('error', {})
        status = error_obj.get('status', '')

        # status: RESOURCE_EXHAUSTED takes precedence. Do not be misled by the
        # reason string, such as rateLimitExceeded; treat it as daily quota exhaustion.
        if status == 'RESOURCE_EXHAUSTED':
            return 'quotaExceeded'

        errors = error_obj.get('errors', [])
        if errors:
            reason = errors[0].get('reason', '')
            if reason:
                return reason

        if status:
            return status
    except Exception:
        pass
    return None


def _extract_error_debug_info(http_error):
    """Return status, reason, and the first part of the raw content in one log line."""
    status = http_error.resp.status if http_error.resp is not None else None
    reason = _extract_error_reason(http_error)
    raw = ''
    try:
        raw = http_error.content.decode('utf-8')[:300]
    except Exception:
        raw = '(content decode failed)'
    return f'http_status={status} reason={reason} raw={raw}'


def _extract_retry_after(http_error):
    """Return the Retry-After header value in seconds, or None if absent."""
    try:
        resp = http_error.resp
        if resp is None:
            return None
        retry_after = resp.get('retry-after') or resp.get('Retry-After')
        if retry_after is None:
            return None
        # Assume the numeric-seconds format; HTTP-date format is omitted.
        return float(retry_after)
    except (TypeError, ValueError, AttributeError):
        return None


def _calc_backoff(attempt, base_delay, max_delay, jitter_ratio):
    """Calculate exponential backoff plus jitter."""
    delay = base_delay * (2 ** (attempt - 1))
    delay = min(delay, max_delay)
    # Random fluctuation of +/- jitter_ratio.
    jitter = delay * jitter_ratio * (2 * random.random() - 1)
    return max(0.1, delay + jitter)


def execute_with_retry(
    request,
    label='request',
    max_attempts=DEFAULT_MAX_ATTEMPTS,
    base_delay=DEFAULT_BASE_DELAY,
    max_delay=DEFAULT_MAX_DELAY,
    jitter_ratio=DEFAULT_JITTER_RATIO,
    verbose=True,
):
    """
    Execute a googleapiclient request. Automatically retry 429/5xx errors.

    Returns the response on success, or re-raises HttpError.
    """
    last_exception = None

    for attempt in range(1, max_attempts + 1):
        try:
            return request.execute()

        except HttpError as e:
            last_exception = e
            status = e.resp.status if e.resp is not None else None
            reason = _extract_error_reason(e)

            # Print raw content when the reason cannot be extracted or is unexpected,
            # so old/new format misses and unexpected errors are not overlooked.
            if verbose and (reason is None or reason not in (
                'quotaExceeded', 'rateLimitExceeded', 'userRateLimitExceeded',
            )):
                print(f'    [{label}] DEBUG: {_extract_error_debug_info(e)}')

            # quotaExceeded is a persistent error; retrying is not useful.
            if reason == 'quotaExceeded':
                if verbose:
                    print(f'    [{label}] quotaExceeded - not retrying')
                raise

            # Not retryable
            if status not in RETRYABLE_STATUS_CODES:
                if verbose:
                    print(f'    [{label}] HTTP {status} reason={reason} '
                          f'- not retryable')
                raise

            # Failed on the final attempt
            if attempt >= max_attempts:
                if verbose:
                    print(f'    [{label}] HTTP {status} reason={reason} '
                          f'- max attempts {max_attempts} reached; giving up')
                raise

            # Prefer the Retry-After header; otherwise use exponential backoff.
            retry_after = _extract_retry_after(e)
            if retry_after is not None:
                delay = retry_after
                why = f'Retry-After header ({retry_after}s)'
            else:
                delay = _calc_backoff(attempt, base_delay, max_delay, jitter_ratio)
                why = f'exp_backoff attempt={attempt}'

            if verbose:
                print(f'    [{label}] HTTP {status} reason={reason} '
                      f'- {delay:.1f}s wait ({why})')
            time.sleep(delay)

    # This should not be reached, but keep this as a safeguard.
    if last_exception:
        raise last_exception
    raise RuntimeError(f'execute_with_retry [{label}] unexpected exit')


class YouTubeKeyRotator:
    """
    A wrapper that keeps multiple YouTube Data API keys and automatically switches
    to the next key when quotaExceeded (daily quota exhaustion) is detected.

    Temporary rate limits such as rateLimitExceeded are absorbed by exponential
    backoff in execute_with_retry. Because execute_with_retry immediately raises
    quotaExceeded by design, this class catches it, rebuilds the YouTube client
    with the next key, rebuilds the request, and retries it.

    Usage:
        rotator = YouTubeKeyRotator(['KEY1', 'KEY2', 'KEY3'])
        response = rotator.execute(
            lambda youtube: youtube.search().list(q='...', part='id'),
            label='search MX',
        )

    request_builder is a function that receives a YouTube client and returns a
    googleapiclient request object that has not yet been executed. Because a
    request object that has already been executed cannot be reused, key switches
    must call request_builder again to rebuild the whole request.
    """

    def __init__(self, api_keys, service_name='youtube', service_version='v3'):
        keys = [k for k in api_keys if k and k != 'your_api_key_here']
        if not keys:
            raise ValueError('YouTubeKeyRotator: no valid API keys were provided.')
        self._keys = keys
        self._service_name = service_name
        self._service_version = service_version
        self._current_index = 0
        self._client = None  # Lazy initialization; created on the first execute call.
        self._request_count = 0  # For debugging: cumulative request count.

    @property
    def current_key_index(self):
        return self._current_index

    @property
    def total_keys(self):
        return len(self._keys)

    def _key_tag(self, index=None):
        """Return a masked key identifier for logs, e.g. key2/4:...ab12."""
        if index is None:
            index = self._current_index
        key = self._keys[index]
        tail = key[-4:] if len(key) >= 4 else key
        return f'key{index + 1}/{len(self._keys)}:...{tail}'

    def _build_client(self):
        key = self._keys[self._current_index]
        return build(self._service_name, self._service_version, developerKey=key)

    def client(self):
        """Return the currently active YouTube client, creating it if necessary."""
        if self._client is None:
            self._client = self._build_client()
            print(f'    [YouTubeKeyRotator] initial client creation: {self._key_tag()}')
        return self._client

    def _advance_key(self, label):
        """Switch to the next key. Return True if switched, or False if none remain."""
        if self._current_index >= len(self._keys) - 1:
            print(f'    [{label}] quotaExceeded, but there is no next key '
                  f'({self._key_tag()} is the last key)')
            return False
        old_tag = self._key_tag()
        self._current_index += 1
        new_tag = self._key_tag()
        print(f'    [{label}] quotaExceeded - switching key: {old_tag} -> {new_tag}')
        self._client = self._build_client()
        return True

    def execute(self, request_builder, label='request', **retry_kwargs):
        """
        Call request_builder(youtube) -> request to build the request, then execute it
        with execute_with_retry. On quotaExceeded, switch to the next key and
        restart from request_builder. If all keys are exhausted, re-raise the exception.
        """
        while True:
            youtube = self.client()
            self._request_count += 1
            key_tag = self._key_tag()
            request = request_builder(youtube)
            try:
                return execute_with_retry(request, label=f'{label} [{key_tag}]', **retry_kwargs)
            except HttpError as e:
                reason = _extract_error_reason(e)
                print(f'    [{label}] confirmed failure {key_tag} reason={reason} '
                      f'(cumulative_requests={self._request_count})')
                if reason == 'quotaExceeded' and self._advance_key(label):
                    continue
                raise


def load_youtube_api_keys(env_var_names=None):
    """
    Collect API keys from the specified environment variable names and return them
    as a list. Unset values and placeholder values are excluded. If env_var_names
    is omitted, the default list checks four variables:
    ['YOUTUBE_API_GLC_KEY', 'COPYRIGHT_CHECK_KEY1', 'YOUTUBE_API_KEY_TEST',
     'YOUTUBE_API_ZIGYOU_KEY'] (the current operational key plus three additional keys).
    """
    if env_var_names is None:
        env_var_names = [
            #'YOUTUBE_API_GLC_KEY',
            'COPYRIGHT_CHECK_KEY1',
            #'YOUTUBE_API_KEY_TEST',
            #'YOUTUBE_API_ZIGYOU_KEY',
            #'YOUTUBE_REPORT_CHECK1'
        ]
    keys = []
    for name in env_var_names:
        val = os.environ.get(name)
        if val and val != 'your_api_key_here':
            keys.append(val)
    return keys
