"""
api_utils.py
=============
YouTube Data API 共通のリトライ・レート制御ユーティリティ。

【提供機能】
  - execute_with_retry(request, ...): googleapiclient リクエストを
    指数バックオフ＋ジッター付きで実行する
  - 429 (rateLimitExceeded) は Retry-After ヘッダがあれば最優先で従う
  - 500/502/503/504 系の一時的サーバーエラーも自動リトライ
  - 403 quotaExceeded は即失敗（リトライ無意味）
  - YouTubeKeyRotator: 複数APIキーを保持し、quotaExceeded（1日の総クォータ
    使い切り）を検知したら次のキーへ自動的に切り替えるラッパー。
    rateLimitExceeded/userRateLimitExceeded のような一時的なレート制限は
    キー切替の対象ではなく、execute_with_retry 側の指数バックオフで吸収する
    （役割分担: 一時的制限=バックオフ、1日の上限到達=キー切替）。

【使い方（単一キー、従来通り）】
  from api_utils import execute_with_retry

  request = youtube.search().list(...)
  response = execute_with_retry(request, label='search MX')

【使い方（複数キーローテーション）】
  from api_utils import YouTubeKeyRotator

  rotator = YouTubeKeyRotator(['KEY1の値', 'KEY2の値', 'KEY3の値'])
  response = rotator.execute(
      lambda youtube: youtube.search().list(...),
      label='search MX',
  )
  # quotaExceededになったら内部で次のキーのyoutubeクライアントへ切り替えて
  # 同じリクエストを作り直して再試行する。全キーを使い切ったら例外を再raiseする。
"""

import os
import time
import random
import json
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


# リトライ対象の HTTPステータスコード
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

# デフォルトのリトライ設定
DEFAULT_MAX_ATTEMPTS = 4        # 初回 + 3回リトライ
DEFAULT_BASE_DELAY = 2.0        # 初回バックオフ秒
DEFAULT_MAX_DELAY = 60.0        # 上限
DEFAULT_JITTER_RATIO = 0.3      # ±30% のジッター


def _extract_error_reason(http_error):
    """
    HttpError から reason 文字列を抽出する。取れなければ None。

    Google APIのエラーレスポンスには新旧2つのフォーマットがある:
      旧: {"error": {"errors": [{"reason": "quotaExceeded", ...}], ...}}
      新: {"error": {"status": "RESOURCE_EXHAUSTED", "details": [...], ...}}
    さらに厄介なことに、実際の1日クォータ枯渇でも errors[].reason に
    "rateLimitExceeded"（本来は秒間/分間の一時的制限を指す文字列）が
    入って返ってくるケースが実際に確認されている
    （例: "Quota exceeded for ... 'Search Queries per day' ..." という
    message と共に reason="rateLimitExceeded" が返る）。
    reason の文字列だけで一時的制限か恒久的な枯渇かを判定するのは
    信頼できないため、status: "RESOURCE_EXHAUSTED" を最優先の判定材料
    として扱い、これが立っていれば reason の中身に関わらず
    quotaExceeded として正規化する（status の方が権威ある情報源）。
    """
    try:
        content = json.loads(http_error.content.decode('utf-8'))
        error_obj = content.get('error', {})
        status = error_obj.get('status', '')

        # status: RESOURCE_EXHAUSTED は最優先。reasonの文字列(rateLimitExceeded
        # 等)に惑わされず、1日クォータ枯渇として扱う。
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
    """HttpError から status/reason/生コンテンツ先頭を1行にまとめてログ用に返す。"""
    status = http_error.resp.status if http_error.resp is not None else None
    reason = _extract_error_reason(http_error)
    raw = ''
    try:
        raw = http_error.content.decode('utf-8')[:300]
    except Exception:
        raw = '(content decode failed)'
    return f'http_status={status} reason={reason} raw={raw}'


def _extract_retry_after(http_error):
    """Retry-After ヘッダ(秒)を返す。無ければ None。"""
    try:
        resp = http_error.resp
        if resp is None:
            return None
        retry_after = resp.get('retry-after') or resp.get('Retry-After')
        if retry_after is None:
            return None
        # 数値秒のフォーマットを想定 (HTTP-date形式は省略)
        return float(retry_after)
    except (TypeError, ValueError, AttributeError):
        return None


def _calc_backoff(attempt, base_delay, max_delay, jitter_ratio):
    """指数バックオフ + ジッター を計算する"""
    delay = base_delay * (2 ** (attempt - 1))
    delay = min(delay, max_delay)
    # ±jitter_ratio のランダム揺らぎ
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
    googleapiclient リクエストを実行する。429/5xxは自動リトライ。

    戻り値: response (success) または HttpError を再 raise
    """
    last_exception = None

    for attempt in range(1, max_attempts + 1):
        try:
            return request.execute()

        except HttpError as e:
            last_exception = e
            status = e.resp.status if e.resp is not None else None
            reason = _extract_error_reason(e)

            # reasonが取れない/想定外の場合は生コンテンツを出す
            # （新旧フォーマットの取りこぼし・想定外エラーを見逃さないため）
            if verbose and (reason is None or reason not in (
                'quotaExceeded', 'rateLimitExceeded', 'userRateLimitExceeded',
            )):
                print(f'    [{label}] DEBUG: {_extract_error_debug_info(e)}')

            # quotaExceeded は永続エラー、リトライ無意味
            if reason == 'quotaExceeded':
                if verbose:
                    print(f'    [{label}] quotaExceeded - リトライしない')
                raise

            # リトライ対象外
            if status not in RETRYABLE_STATUS_CODES:
                if verbose:
                    print(f'    [{label}] HTTP {status} reason={reason} '
                          f'- リトライ対象外')
                raise

            # 最終試行で失敗
            if attempt >= max_attempts:
                if verbose:
                    print(f'    [{label}] HTTP {status} reason={reason} '
                          f'- 最大試行回数 {max_attempts} 到達、諦め')
                raise

            # Retry-After ヘッダ優先、なければ指数バックオフ
            retry_after = _extract_retry_after(e)
            if retry_after is not None:
                delay = retry_after
                why = f'Retry-After header ({retry_after}s)'
            else:
                delay = _calc_backoff(attempt, base_delay, max_delay, jitter_ratio)
                why = f'exp_backoff attempt={attempt}'

            if verbose:
                print(f'    [{label}] HTTP {status} reason={reason} '
                      f'- {delay:.1f}秒待機 ({why})')
            time.sleep(delay)

    # ここには到達しない想定だが念のため
    if last_exception:
        raise last_exception
    raise RuntimeError(f'execute_with_retry [{label}] unexpected exit')


class YouTubeKeyRotator:
    """
    複数のYouTube Data APIキーを保持し、quotaExceeded（1日の総クォータ使い切り）
    を検知したら次のキーへ自動的に切り替えて続行するラッパー。

    一時的なレート制限(rateLimitExceeded等)は execute_with_retry の指数バックオフ
    で吸収する。quotaExceeded は execute_with_retry が即raiseする設計なので、
    ここでそれを捕まえて「次のキーでyoutubeクライアントを作り直し、リクエストも
    作り直して再試行」する。

    使い方:
        rotator = YouTubeKeyRotator(['KEY1', 'KEY2', 'KEY3'])
        response = rotator.execute(
            lambda youtube: youtube.search().list(q='...', part='id'),
            label='search MX',
        )

    request_builder は youtube クライアントを受け取り、googleapiclient の
    リクエストオブジェクト（まだ.execute()していないもの）を返す関数。
    一度実行したリクエストオブジェクトは再利用できないため、キー切替時は
    必ず request_builder を呼び直してリクエストごと作り直す。
    """

    def __init__(self, api_keys, service_name='youtube', service_version='v3'):
        keys = [k for k in api_keys if k and k != 'your_api_key_here']
        if not keys:
            raise ValueError('YouTubeKeyRotator: 有効なAPIキーが1つもありません。')
        self._keys = keys
        self._service_name = service_name
        self._service_version = service_version
        self._current_index = 0
        self._client = None  # 遅延生成（最初のexecute呼び出し時に作る）
        self._request_count = 0  # デバッグ用: 累計リクエスト数

    @property
    def current_key_index(self):
        return self._current_index

    @property
    def total_keys(self):
        return len(self._keys)

    def _key_tag(self, index=None):
        """ログ表示用にキーをマスクした識別子を返す（例: key2/4:...ab12）。"""
        if index is None:
            index = self._current_index
        key = self._keys[index]
        tail = key[-4:] if len(key) >= 4 else key
        return f'key{index + 1}/{len(self._keys)}:...{tail}'

    def _build_client(self):
        key = self._keys[self._current_index]
        return build(self._service_name, self._service_version, developerKey=key)

    def client(self):
        """現在アクティブなyoutubeクライアントを返す（必要なら生成）。"""
        if self._client is None:
            self._client = self._build_client()
            print(f'    [YouTubeKeyRotator] クライアント初期生成: {self._key_tag()}')
        return self._client

    def _advance_key(self, label):
        """次のキーに切り替える。切替できればTrue、もう無ければFalse。"""
        if self._current_index >= len(self._keys) - 1:
            print(f'    [{label}] quotaExceeded だが切替先キーが無い '
                  f'(現在 {self._key_tag()} が最後のキー)')
            return False
        old_tag = self._key_tag()
        self._current_index += 1
        new_tag = self._key_tag()
        print(f'    [{label}] quotaExceeded - キー切替: {old_tag} -> {new_tag}')
        self._client = self._build_client()
        return True

    def execute(self, request_builder, label='request', **retry_kwargs):
        """
        request_builder(youtube) -> request を呼んでリクエストを組み立て、
        execute_with_retry で実行する。quotaExceededなら次のキーに切り替えて
        request_builder からやり直す。全キーを使い切ったら例外を再raiseする。
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
                print(f'    [{label}] 失敗確定 {key_tag} reason={reason} '
                      f'(累計リクエスト数={self._request_count})')
                if reason == 'quotaExceeded' and self._advance_key(label):
                    continue
                raise


def load_youtube_api_keys(env_var_names=None):
    """
    指定した環境変数名のリストからAPIキーを集めてリストで返す。
    未設定/プレースホルダ値は除外する。env_var_names省略時は
    ['YOUTUBE_API_GLC_KEY', 'COPYRIGHT_CHECK_KEY1', 'YOUTUBE_API_KEY_TEST',
     'YOUTUBE_API_ZIGYOU_KEY'] の4つを見る（現行運用キー＋追加3キー）。
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
