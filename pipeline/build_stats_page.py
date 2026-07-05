#!/usr/bin/env python3
"""
build_stats_page.py — データから統計ページ(docs/<日付>/index.html)を生成する

data/<YYYYMMDD>/ 配下の phase7_with_buzz_score_*.json（動画メタ＋stats＋buzz）と
comment_analysis_*.json（感情・テーマ）を読み、サッカー動画バズの統計を集計して
GitHub Pages 用の自己完結HTMLを書き出す。

仕様:
- 出力日付は data/ 配下の YYYYMMDD フォルダ名から判定する（実行日ではない）
- 引数なし : 最新の YYYYMMDD フォルダ1つを処理
- -all     : data/ 配下の全 YYYYMMDD フォルダを処理し、各 docs/<日付>/ を生成
- YYYYMMDD フォルダが1つも無い場合はエラー終了
- phase7 / comment_analysis は同じ日付フォルダ内のファイルから読む
- ページは英語デフォルト＋日本語トグル（再読み込みなし / localStorage 記憶）
- 引用・拡散対策: OGP / Twitter Card / canonical / JSON-LD / 引用文コピー /
  X・Facebook・LINE 共有 / Web Share API / リンクコピー、スマホ最適化
- 依存ライブラリなし（標準ライブラリのみ）

実行:
    python build_stats_page.py        # 最新の日付フォルダ
    python build_stats_page.py -all   # 全日付フォルダ
"""

import os
import re
import sys
import glob
import json
import html
from collections import Counter, defaultdict

# ==== 編集ポイント：自分のURLに合わせて ====
TUBESAKU_URL = "https://tubesaku.com"            # データ提供元
TUBESAKU_LABEL = "TubeSaku — YouTube creator and video trend analysis"
SEARCH_URL = "https://soccer.tubesaku.com"       # 検索ページ（旧 live agent の置き換え）
SEARCH_LABEL = "SoccerScope World Cup 2026 Search"
PAGE_URL = "https://webbigdata-jp.github.io/soccerscope/"  # 公開URL（OGP / canonical / JSON-LD 用）
OGP_IMAGE_URL = PAGE_URL.rstrip("/") + "/images/soccerscope-ogp.png"  # docs/images/ に配置
TOP_N = 10
WORLD_CUP_EN = "FIFA World Cup 2026"
WORLD_CUP_JA = "FIFAワールドカップ2026"
SEO_KEYWORDS = [
    "FIFA World Cup 2026", "World Cup 2026", "2026 FIFA World Cup",
    "football video trends", "soccer video trends", "YouTube football trends",
    "viral soccer videos", "fan reactions", "sentiment analysis", "TubeSaku",
    "ワールドカップ2026", "FIFAワールドカップ2026", "サッカー動画", "YouTubeトレンド",
    "海外サッカー動画", "ファンコメント分析", "サッカー動画分析",
]
# ==== Schema.org / ライセンス関連 ====
ORG_NAME = "TubeSaku"
# 検索ページ（ブランド名）。CTAボタンの行き先。
SEARCH_BRAND = "SOCCER·SCOPE"
# 組織ロゴ（できれば正方形 112×112 以上を docs/images/ に配置）。無ければOGP画像で代用。
LOGO_URL = PAGE_URL.rstrip("/") + "/images/soccerscope-logo.png"
# データセットのライセンス：集計・分析(統計値)に対して CC BY 4.0（出典 TubeSaku）。
DATA_LICENSE_URL = "https://creativecommons.org/licenses/by/4.0/"
# ライセンスのスコープ注記（人間可読）への内部アンカー。
DATA_USAGE_INFO = PAGE_URL.rstrip("/") + "/#data-license"

# ナレーション（Gemini）設定。APIキー(GEMINI_API_KEY/GOOGLE_API_KEY)が無ければ自動でスキップ。
GEMINI_MODEL = os.environ.get("SOCCER_NARRATE_MODEL", "gemini-3.1-flash-lite")
NARRATE_TOP_N = 10           # ナレーションを付ける動画数
NARRATE_COMMENTS_PER_VIDEO = 0  # 生コメントは既定で渡さない（既存の分析結果を使う）


GTM_HEAD = """
<!-- Google tag (gtag.js) -->
<script async src="https://www.googletagmanager.com/gtag/js?id=G-Q24SL134WG"></script>
<script>
  window.dataLayer = window.dataLayer || [];
  function gtag(){dataLayer.push(arguments);}
  gtag('js', new Date());

  gtag('config', 'G-Q24SL134WG');
</script>
"""

# =========================================

DATA_DIR = "data"
DOCS_DIR = "../docs"

# ナレーション(②)を使う場合の API キーを .env から拾えるようにする（任意・無くても可）。
# python-dotenv が無ければ何もしない（その場合はシェル環境変数を使う）。
try:  # noqa: SIM105
    from dotenv import load_dotenv as _load_dotenv
    for _p in (".env", os.path.join("git", "soccer_agent", ".env"), os.path.join("..", ".env")):
        if os.path.exists(_p):
            _load_dotenv(_p)
except Exception:  # noqa: BLE001
    pass


def esc(s):
    """属性値・テキスト共用のHTMLエスケープ。"""
    return html.escape("" if s is None else str(s), quote=True)


def tspan(en, ja, cls=None, tag="span"):
    """英語/日本語を data 属性に持ち、初期表示は英語のトグル対応要素を返す。"""
    c = f' class="{cls}"' if cls else ""
    return f'<{tag}{c} data-en="{esc(en)}" data-ja="{esc(ja)}">{esc(en)}</{tag}>'


def fmt(n):
    try:
        return f"{int(n):,}"
    except (TypeError, ValueError):
        return str(n)


def bar(pct, color="var(--pitch)"):
    pct = max(0.0, min(100.0, pct))
    return f'<span class="bar"><i style="width:{pct:.1f}%;background:{color}"></i></span>'


def readable(date_str):
    """YYYYMMDD -> YYYY-MM-DD"""
    return f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"


def list_date_dirs():
    """data/ 直下の YYYYMMDD（8桁数字）フォルダ名を昇順で返す。
    （後方互換のため残すが、process_date系のメインロジックでは
    resolve_target_date_files() を使う。フォルダ名は「実行日」であり
    「収集対象日(--target-date)」と一致しない場合があるため。）"""
    if not os.path.isdir(DATA_DIR):
        return []
    out = []
    for name in os.listdir(DATA_DIR):
        if len(name) == 8 and name.isdigit() and os.path.isdir(os.path.join(DATA_DIR, name)):
            out.append(name)
    return sorted(out)


def find_in_dir(dirpath, *patterns):
    """指定フォルダ内で、パターンに一致する最新ファイル（ファイル名順）を返す。"""
    hits = []
    for pat in patterns:
        hits += glob.glob(os.path.join(dirpath, pat))
    return max(hits, key=lambda p: os.path.basename(p)) if hits else None


# ---- target_date(収集対象日)ベースのファイル対応付け ----------------------
#
# data/<実行日>/ フォルダ名は「処理を実行した日」であり、run_backfill.sh等で
# 過去日(--target-date)をまとめて取り直すと、1つの実行日フォルダに複数の
# 収集対象日のファイルが混在する。さらに 3_analyze_comments.py の再実行
# （is_soccer_related判定の遡及適用等）で comment_analysis_*.json が
# 同じ収集セットに対して複数回・離れたタイミングで生成されることもある。
#
# ファイル名のタイムスタンプの近さで「同じセットだろう」と推測する方式は
# この運用では成立しない（再分析が何時間も後に走るため）。
# 各中間JSONが持つ source_file フィールド（生成元ファイル名）を
# comment_analysis -> phase4 -> phase7 -> phase3 -> phase2 と確実に
# 辿ることで対応付ける。曖昧な推測は一切行わない。

def _index_files_by_basename(*filename_globs):
    """data/ 配下（直下 + 1階層下のYYYYMMDDフォルダ）を横断探索し、
    ファイル名(basename) -> フルパス の辞書を作る。
    同名ファイルが複数箇所にある場合はmtimeが新しい方を使う
    （通常は起こらないが、安全側の挙動として）。"""
    index = {}
    search_dirs = [DATA_DIR]
    if os.path.isdir(DATA_DIR):
        for name in os.listdir(DATA_DIR):
            p = os.path.join(DATA_DIR, name)
            if os.path.isdir(p):
                search_dirs.append(p)
    for d in search_dirs:
        for pat in filename_globs:
            for fp in glob.glob(os.path.join(d, pat)):
                bn = os.path.basename(fp)
                if bn not in index or os.path.getmtime(fp) > os.path.getmtime(index[bn]):
                    index[bn] = fp
    return index


def _safe_load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:  # noqa: BLE001
        print(f"  WARN: 読み込み失敗 {path}: {e}", file=sys.stderr)
        return None


def resolve_target_date_files():
    """収集対象日(target_date) -> {"phase7": path, "comment_analysis": path|None,
    "phase2": path} の辞書を、target_date昇順で返す。

    対応付けは source_file チェーンを確実に辿って行う:
      comment_analysis.source_file -> phase4ファイル
      phase4.source_file           -> phase7ファイル
      phase7.source_file           -> phase3ファイル
      phase3.source_file           -> phase2ファイル
      phase2.target_date           -> 収集対象日
        （phase2に target_date フィールドが無い古い形式の場合は、
          phase2のファイル名 phase2_video_ids_{target_date}_{ts}.json から
          target_dateを取る。--target-date無し実行の場合はファイル名が
          phase2_video_ids_{ts}.json のみなので、tsの日付部分を使う。
          いずれも「ファイル名から読み取れる確定情報」であり、タイムスタンプの
          近さによる推測ではない点に注意）。

    1つのtarget_dateに対して comment_analysis が複数存在する場合（再分析の
    繰り返し）は、analysesの中身ではなく各comment_analysisファイル自身の
    generated_at が最も新しいものを採用する。
    """
    phase2_index = _index_files_by_basename("phase2_video_ids_*.json")
    phase3_index = _index_files_by_basename("phase3_metadata_*.json")
    phase7_index = _index_files_by_basename("phase7_with_buzz_score_*.json", "phase7_*.json")
    phase4_index = _index_files_by_basename("phase4_comments_*.json")
    ca_index = _index_files_by_basename("comment_analysis_*.json")

    def phase2_target_date(phase2_path, phase2_name):
        data = _safe_load_json(phase2_path)
        if data and data.get("target_date"):
            return data["target_date"].replace("-", "")
        # target_dateフィールドが無い場合はファイル名から読み取る
        m = re.match(r'phase2_video_ids_(\d{8})_\d{8}_\d{6}\.json$', phase2_name)
        if m:
            return m.group(1)
        m2 = re.match(r'phase2_video_ids_(\d{8})_\d{6}\.json$', phase2_name)
        if m2:
            return m2.group(1)
        return None

    # phase2ファイルごとの target_date をあらかじめ全部解決しておく
    phase2_target_date_by_name = {}
    for name, path in phase2_index.items():
        td = phase2_target_date(path, name)
        if td:
            phase2_target_date_by_name[name] = td
        else:
            print(f"  WARN: {name} から target_date を特定できません。", file=sys.stderr)

    # comment_analysis -> phase4 -> phase7 -> phase3 -> phase2 -> target_date
    # のチェーンを辿り、target_date -> [(generated_at, ca_path, phase7_path, phase2_path), ...]
    by_target_date = {}
    for ca_name, ca_path in ca_index.items():
        ca_data = _safe_load_json(ca_path)
        if not ca_data:
            continue
        phase4_name = ca_data.get("source_file")
        phase4_path = phase4_index.get(phase4_name) if phase4_name else None
        if not phase4_path:
            print(f"  WARN: {ca_name} の source_file='{phase4_name}' が見つかりません。スキップ。",
                  file=sys.stderr)
            continue

        phase4_data = _safe_load_json(phase4_path)
        if not phase4_data:
            continue
        phase7_name = phase4_data.get("source_file")
        phase7_path = phase7_index.get(phase7_name) if phase7_name else None
        if not phase7_path:
            print(f"  WARN: {phase4_name} の source_file='{phase7_name}' が見つかりません。スキップ。",
                  file=sys.stderr)
            continue

        phase7_data = _safe_load_json(phase7_path)
        if not phase7_data:
            continue
        phase3_name = phase7_data.get("source_file")
        phase3_path = phase3_index.get(phase3_name) if phase3_name else None
        if not phase3_path:
            print(f"  WARN: {phase7_name} の source_file='{phase3_name}' が見つかりません。スキップ。",
                  file=sys.stderr)
            continue

        phase3_data = _safe_load_json(phase3_path)
        if not phase3_data:
            continue
        phase2_name = phase3_data.get("source_file")
        if not phase2_name or phase2_name not in phase2_target_date_by_name:
            print(f"  WARN: {phase3_name} の source_file='{phase2_name}' "
                  f"に対応するtarget_dateが見つかりません。スキップ。", file=sys.stderr)
            continue
        target_date = phase2_target_date_by_name[phase2_name]
        phase2_path = phase2_index[phase2_name]

        generated_at = ca_data.get("generated_at", "")  # 文字列比較で新旧判定（ISO8601想定）
        by_target_date.setdefault(target_date, []).append(
            (generated_at, ca_path, phase7_path, phase2_path)
        )

    # phase2はあるがcomment_analysisまで辿り着けなかった（コメント分析未実施）日も、
    # phase2 -> phase3 -> phase7 のチェーンだけで拾えるなら拾う
    # （comment_analysisがNoneの状態でページ生成自体は可能なため）。
    phase3_by_phase2name = {}
    for p3_name, p3_path in phase3_index.items():
        p3_data = _safe_load_json(p3_path)
        if p3_data and p3_data.get("source_file"):
            phase3_by_phase2name.setdefault(p3_data["source_file"], []).append((p3_name, p3_path))

    result = {}
    for target_date, candidates in by_target_date.items():
        # generated_atが最も新しいもの（=最新の再分析結果）を採用
        candidates.sort(key=lambda c: c[0])
        _generated_at, ca_path, phase7_path, phase2_path = candidates[-1]
        if len(candidates) > 1:
            print(f"  INFO: target_date={target_date} の comment_analysis が"
                  f"{len(candidates)}件見つかりました。最新を採用: {os.path.basename(ca_path)}")
        result[target_date] = {"phase7": phase7_path, "comment_analysis": ca_path, "phase2": phase2_path}

    # comment_analysisチェーンで拾えなかったtarget_dateを、phase2->phase3->phase7だけで補完
    for phase2_name, target_date in phase2_target_date_by_name.items():
        if target_date in result:
            continue
        phase2_path = phase2_index[phase2_name]
        p3_candidates = phase3_by_phase2name.get(phase2_name, [])
        if not p3_candidates:
            continue
        # 複数あれば最初に見つかったものを使う（同名衝突は通常起きない想定）
        _p3_name, p3_path = p3_candidates[0]
        p3_data = _safe_load_json(p3_path)
        if not p3_data:
            continue
        # phase7はphase3のsource_fileを参照する側なので逆引きが必要。
        # phase7_indexの中からsource_file==p3_nameのものを探す。
        matched_phase7 = None
        for p7_name, p7_path in phase7_index.items():
            p7_data = _safe_load_json(p7_path)
            if p7_data and p7_data.get("source_file") == _p3_name:
                matched_phase7 = p7_path
                break
        if not matched_phase7:
            print(f"  WARN: target_date={target_date} はcomment_analysis未生成で、"
                  f"対応するphase7も見つからないためスキップします。", file=sys.stderr)
            continue
        print(f"  INFO: target_date={target_date} はcomment_analysis未生成。"
              f"動画データのみでページ生成します。")
        result[target_date] = {"phase7": matched_phase7, "comment_analysis": None, "phase2": phase2_path}

    return result


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def attach_countries_from_phase2(videos, phase2_path):
    """
    (b) 多対多: phase2 の検索結果から、各動画に「出現したすべての国」を付ける。
    同じ日付フォルダの phase2_video_ids_*.json だけを使い、API も Mongo も触らない。

    各 video に以下を追加する:
      v["countries"] : 出現国コードのリスト（その国の検索結果に出た順位昇順）
      v["reach"]     : 出現国数
    phase2 に無い動画は v["country"]（あれば）を単独要素にフォールバックする。

    戻り値: (name_en, name_ja, lang_by_code) … code -> 表示名 / 主要言語 の辞書
    """
    name_en, name_ja, lang_by_code = {}, {}, {}
    appear = {}  # vid -> [(rank, code)]
    if phase2_path:
        by_country = load_json(phase2_path).get("by_country", {})
        for code, cd in by_country.items():
            name_en[code] = cd.get("country_name_en") or cd.get("country_name_ja") or code
            name_ja[code] = cd.get("country_name_ja") or cd.get("country_name_en") or code
            lang_by_code[code] = cd.get("primary_lang", "")
            for rank, vid in enumerate(cd.get("video_ids", [])):
                appear.setdefault(vid, []).append((rank, code))

    for v in videos:
        vid = v.get("video_id")
        lst = appear.get(vid)
        if lst:
            countries = [code for _rank, code in sorted(lst)]  # 順位の良い国順
        else:
            c = v.get("country")
            countries = [c] if c else []
            if c:  # フォールバック動画の表示名も拾っておく
                name_en.setdefault(c, v.get("country_name_en") or v.get("country_name_ja") or c)
                name_ja.setdefault(c, v.get("country_name_ja") or v.get("country_name_en") or c)
                lang_by_code.setdefault(c, v.get("primary_lang", ""))
        v["countries"] = countries
        v["reach"] = len(countries)
    return name_en, name_ja, lang_by_code


def aggregate_team_mentions(analyses):
    """comment_analysis 各動画の mentioned_teams を集計し、言及数の多い順に返す。
    各 analysis は mentioned_teams:[{team, sentiment(positive/neutral/negative), mention_count}]
    を持つ想定。無ければ空リスト（=セクション非表示）。
    戻り値: [{team, mentions, positive, neutral, negative, lean}] を mentions 降順。
    """
    agg = {}
    for a in analyses.values():
        for t in (a.get("mentioned_teams") or []):
            name = (t.get("team") or "").strip()
            if not name:
                continue
            cnt = int(t.get("mention_count", 0) or 0)
            sent = (t.get("sentiment") or "neutral").lower()
            d = agg.setdefault(name, {"team": name, "mentions": 0,
                                      "positive": 0, "neutral": 0, "negative": 0})
            d["mentions"] += cnt
            d[sent if sent in ("positive", "neutral", "negative") else "neutral"] += cnt
    teams = sorted(agg.values(), key=lambda d: d["mentions"], reverse=True)
    for d in teams:
        d["lean"] = ("positive" if d["positive"] > d["negative"]
                     else "negative" if d["negative"] > d["positive"] else "neutral")
    return teams


def narrate_top_videos(top_videos, analyses, name_en, lang_by_code):
    """上位動画に日英の短いコメントを Gemini で付ける（②）。
    APIキー未設定 / SDK未導入 / 失敗時は {} を返してスキップ（ページ生成は継続）。
    嘘防止のため、コメントは全世界1プールである事を前提に「各国民の意見の創作」を禁じ、
    出現国・言語・全体感情・言及チームなどの事実のみを根拠にさせる。
    戻り値: {video_id: {"en": str, "ja": str}}
    """
    if not (os.environ.get("GEMINI_API_KEY")):
        print("  ナレーション: APIキー未設定のためスキップ", file=sys.stderr)
        return {}
    try:
        from google import genai
        from google.genai import types
    except Exception as e:  # noqa: BLE001
        print(f"  ナレーション: google-genai 未導入のためスキップ ({e})", file=sys.stderr)
        return {}

    items = []
    for v in top_videos[:NARRATE_TOP_N]:
        vid = v.get("video_id")
        a = (analyses or {}).get(vid, {})
        codes = v.get("countries", [])
        items.append({
            "video_id": vid,
            "title": (v.get("title") or "")[:200],
            "description": (v.get("description") or "")[:500],
            "view_count": int(v.get("stats", {}).get("view_count", 0) or 0),
            "trended_in_countries": [name_en.get(c, c) for c in codes][:20],
            "audience_languages": sorted({lang_by_code.get(c, "") for c in codes} - {""}),
            "sentiment": a.get("sentiment"),
            "themes": [t.get("theme_en") or t.get("theme_ja")
                       for t in (a.get("positive_themes") or []) + (a.get("negative_themes") or [])][:8],
            "mentioned_teams": [t.get("team") for t in (a.get("mentioned_teams") or [])][:10],
        })
    if not items:
        return {}

    system = (
        "You write very short, factual bilingual blurbs about trending football (soccer) "
        "YouTube videos for a data page. STRICT RULES: base every statement ONLY on the given "
        "fields. The comment data is a single GLOBAL pool — do NOT invent or attribute distinct "
        "per-nationality opinions (never 'Mexicans think X while Brazilians think Y'). You MAY "
        "state which countries it trended in, the audience languages, the overall sentiment, and "
        "which teams/topics the comments mention. Invent no stats, quotes, or facts. "
        "1-2 sentences each, lively but accurate."
    )
    prompt = (
        system + "\n\nReturn ONLY a JSON array; each element: "
        '{"video_id": str, "blurb_en": str, "blurb_ja": str}. '
        "blurb_en in English, blurb_ja in natural Japanese.\n\nVIDEOS:\n"
        + json.dumps(items, ensure_ascii=False)
    )
    try:
        client = genai.Client()
        resp = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json"),
        )
        data = json.loads(resp.text)
    except Exception as e:  # noqa: BLE001
        print(f"  ナレーション: 生成失敗のためスキップ ({e})", file=sys.stderr)
        return {}

    out = {}
    for d in (data if isinstance(data, list) else []):
        vid = d.get("video_id")
        if vid:
            out[vid] = {"en": (d.get("blurb_en") or "").strip(),
                        "ja": (d.get("blurb_ja") or "").strip()}
    print(f"  ナレーション: {len(out)}/{len(items)} 件生成")
    return out


# ---- 共通: クライアント側のトグル / 共有 JS（素の文字列。f-string にしない）----
PAGE_JS = """
<script>
(function(){
  var KEY='ss-lang';
  function getStored(){ try{return localStorage.getItem(KEY)}catch(e){return null} }
  function store(l){ try{localStorage.setItem(KEY,l)}catch(e){} }
  function lang(){ return document.documentElement.lang==='ja' ? 'ja' : 'en'; }
  function cfg(){ return window.__SS__ || {}; }
  function apply(l){
    document.documentElement.lang = l;
    var nodes = document.querySelectorAll('[data-en]');
    for(var i=0;i<nodes.length;i++){
      var v = nodes[i].getAttribute('data-'+l);
      if(v!==null) nodes[i].textContent = v;
    }
    var c = cfg();
    if(c.title && c.title[l]) document.title = c.title[l];
    var md = document.querySelector('meta[name="description"]');
    if(md && c.desc && c.desc[l]) md.setAttribute('content', c.desc[l]);
    var ct = document.getElementById('citeText');
    if(ct && c.cite && c.cite[l]) ct.textContent = c.cite[l];
    var btn = document.getElementById('langBtn');
    if(btn) btn.textContent = (l==='en') ? '日本語' : 'English';
    store(l);
  }
  function shareUrl(){ var c=cfg(); return c.url || location.href; }
  function shareText(){ var c=cfg(); return (c.share && c.share[lang()]) ? c.share[lang()] : document.title; }
  function toast(){ var t=document.getElementById('toast'); if(!t)return; t.classList.add('show'); setTimeout(function(){t.classList.remove('show');},1500); }
  function copyText(text){
    if(navigator.clipboard && navigator.clipboard.writeText){
      navigator.clipboard.writeText(text).then(toast, function(){fallbackCopy(text);});
    } else { fallbackCopy(text); }
  }
  function fallbackCopy(text){
    var ta=document.createElement('textarea'); ta.value=text;
    ta.style.position='fixed'; ta.style.top='-1000px';
    document.body.appendChild(ta); ta.focus(); ta.select();
    try{document.execCommand('copy');}catch(e){}
    document.body.removeChild(ta); toast();
  }
  document.addEventListener('DOMContentLoaded', function(){
    var stored = getStored();
    apply(stored==='ja' ? 'ja' : 'en');

    var btn = document.getElementById('langBtn');
    if(btn) btn.addEventListener('click', function(){ apply(lang()==='en'?'ja':'en'); });

    var sb = document.getElementById('shareBtn');
    if(sb) sb.addEventListener('click', function(){
      if(navigator.share){
        navigator.share({title:document.title, text:shareText(), url:shareUrl()}).catch(function(){});
      } else { copyText(shareUrl()); }
    });

    var nets = document.querySelectorAll('[data-share]');
    for(var i=0;i<nets.length;i++){
      (function(a){
        a.addEventListener('click', function(e){
          e.preventDefault();
          var net=a.getAttribute('data-share');
          var u=encodeURIComponent(shareUrl());
          var t=encodeURIComponent(shareText());
          var href='';
          if(net==='x') href='https://twitter.com/intent/tweet?text='+t+'&url='+u;
          else if(net==='facebook') href='https://www.facebook.com/sharer/sharer.php?u='+u;
          else if(net==='line') href='https://social-plugins.line.me/lineit/share?url='+u;
          if(href) window.open(href,'_blank','noopener');
        });
      })(nets[i]);
    }

    var cl=document.getElementById('copyLinkBtn');
    if(cl) cl.addEventListener('click', function(){ copyText(shareUrl()); });

    var cc=document.getElementById('copyCiteBtn');
    if(cc) cc.addEventListener('click', function(){
      var c=cfg(); copyText((c.cite && c.cite[lang()]) ? c.cite[lang()] : shareUrl());
    });
  });
})();
</script>
"""


def ss_config_script(url, title_en, title_ja, desc_en, desc_ja,
                     share_en, share_ja, cite_en, cite_ja):
    """言語別の title / desc / 共有文 / 引用文を JS に渡す（XSS安全に json.dumps）。"""
    cfg = {
        "url": url,
        "title": {"en": title_en, "ja": title_ja},
        "desc": {"en": desc_en, "ja": desc_ja},
        "share": {"en": share_en, "ja": share_ja},
        "cite": {"en": cite_en, "ja": cite_ja},
    }
    dumped = json.dumps(cfg, ensure_ascii=False).replace("<", "\\u003c")
    return "<script>window.__SS__=" + dumped + ";</script>"


def head_meta(title_en, desc_en, canonical_url):
    """OGP / Twitter Card / canonical（英語=デフォルトをクローラに渡す）。"""
    img_alt = "SoccerScope — FIFA World Cup 2026 YouTube football trends"
    return (
        '<meta name="robots" content="index, follow, max-image-preview:large, max-snippet:-1, max-video-preview:-1">'
        '<meta name="theme-color" content="#0a0f0d">'
        '<meta name="format-detection" content="telephone=no">'
        '<meta property="og:type" content="website">'
        '<meta property="og:site_name" content="SoccerScope">'
        f'<meta property="og:title" content="{esc(title_en)}">'
        f'<meta property="og:description" content="{esc(desc_en)}">'
        f'<meta property="og:url" content="{esc(canonical_url)}">'
        f'<meta property="og:image" content="{esc(OGP_IMAGE_URL)}">'
        '<meta property="og:image:width" content="1200">'
        '<meta property="og:image:height" content="630">'
        f'<meta property="og:image:alt" content="{esc(img_alt)}">'
        '<meta property="og:locale" content="en_US">'
        '<meta property="og:locale:alternate" content="ja_JP">'
        '<meta name="twitter:card" content="summary_large_image">'
        f'<meta name="twitter:title" content="{esc(title_en)}">'
        f'<meta name="twitter:description" content="{esc(desc_en)}">'
        f'<meta name="twitter:image" content="{esc(OGP_IMAGE_URL)}">'
        f'<meta name="twitter:image:alt" content="{esc(img_alt)}">'
        f'<link rel="canonical" href="{esc(canonical_url)}">'
    )


# --- Schema.org 共通エンティティ & UI部品 -------------------------------------

def world_cup_event():
    """ワールドカップ2026を表す完全な SportsEvent。
    Google Event の必須(name/startDate/location)＋推奨(endDate/eventStatus/
    eventAttendanceMode/organizer/description)を充足し、構造化データエラーを解消する。"""
    return {
        "@type": "SportsEvent",
        "name": WORLD_CUP_EN,
        "description": ("The 2026 FIFA World Cup, the 23rd edition of the men's football "
                        "world championship, co-hosted by the United States, Canada and Mexico."),
        "startDate": "2026-06-11",
        "endDate": "2026-07-19",
        "eventStatus": "https://schema.org/EventScheduled",
        "eventAttendanceMode": "https://schema.org/OfflineEventAttendanceMode",
        "location": [
            {"@type": "Place", "name": "United States",
             "address": {"@type": "PostalAddress", "addressCountry": "US"}},
            {"@type": "Place", "name": "Canada",
             "address": {"@type": "PostalAddress", "addressCountry": "CA"}},
            {"@type": "Place", "name": "Mexico",
             "address": {"@type": "PostalAddress", "addressCountry": "MX"}},
        ],
        "organizer": {"@type": "SportsOrganization", "name": "FIFA", "url": "https://www.fifa.com"},
        "sameAs": "https://en.wikipedia.org/wiki/2026_FIFA_World_Cup",
    }


def organization_jsonld():
    """発行組織(TubeSaku)。検索でのロゴ表示・ナレッジグラフ向け。"""
    return {
        "@context": "https://schema.org", "@type": "Organization",
        "name": ORG_NAME, "url": TUBESAKU_URL, "logo": LOGO_URL,
        "sameAs": [SEARCH_URL, TUBESAKU_URL],
    }


def website_jsonld():
    """サイト全体を表す WebSite エンティティ。"""
    return {
        "@context": "https://schema.org", "@type": "WebSite",
        "name": "SoccerScope",
        "alternateName": SEARCH_BRAND,
        "url": PAGE_URL.rstrip("/") + "/",
        "inLanguage": ["en", "ja"],
        "publisher": {"@type": "Organization", "name": ORG_NAME, "url": TUBESAKU_URL},
    }


def breadcrumb_jsonld(readable_date, dated_url):
    """日別ページ用パンくず（Home → 日付）。Googleのパンくずリッチリザルト対応。"""
    return {
        "@context": "https://schema.org", "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "SoccerScope",
             "item": PAGE_URL.rstrip("/") + "/"},
            {"@type": "ListItem", "position": 2, "name": readable_date, "item": dated_url},
        ],
    }


def jsonld_blocks(*objs):
    """複数のJSON-LDオブジェクトを <script> ブロック群にまとめる。"""
    return "".join('<script type="application/ld+json">'
                   + json.dumps(o, ensure_ascii=False) + "</script>" for o in objs)


def search_cta_html():
    """検索ページ(SOCCER·SCOPE / soccer.tubesaku.com)への目立つCTAボタン。"""
    return (
        '<div class="cta-wrap">'
        f'<a class="cta" href="{SEARCH_URL}" rel="noopener">'
        + tspan("⚽ Search World Cup 2026 videos & creators",
                "⚽ ワールドカップ2026の動画・クリエイターを検索")
        + '<span class="cta-arrow num">&rarr;</span></a>'
        + tspan(f"Open {SEARCH_BRAND} — search engine for World Cup 2026 football videos, creators & posts",
                f"{SEARCH_BRAND} を開く — ワールドカップ2026のサッカー動画・クリエイター・投稿を検索",
                cls="cta-sub")
        + '</div>'
    )


def license_note_html():
    """フッター用の人間可読なライセンス注記（CC BY 4.0＋YouTube素材は対象外）。"""
    return (
        '<p class="license" id="data-license">'
        + tspan("Statistics & analysis by TubeSaku are licensed under ",
                "TubeSaku による統計・分析は ")
        + f'<a href="{DATA_LICENSE_URL}" rel="noopener license">CC BY 4.0</a>'
        + tspan(". Underlying YouTube videos, titles, thumbnails and comments remain the property "
                "of YouTube and their respective creators, and are not covered by this license.",
                " で提供されます。元となる YouTube 動画・タイトル・サムネイル・コメント等の権利は "
                "YouTube および各制作者に帰属し、本ライセンスの対象には含まれません。")
        + '</p>'
    )


def share_cite_section(heading_en, heading_ja, cite_en):
    """共有ボタン＋引用ブロック。引用枠は英語を初期表示し、言語切替で __SS__ から差し替え。"""
    return (
        '<section class="card">'
        '<h2>' + tspan(heading_en, heading_ja) + '</h2>'
        '<div class="share">'
        + tspan("Share", "共有", cls="sbtn primary", tag="button").replace("<button", '<button id="shareBtn"')
        + '<a href="#" data-share="x" class="sbtn">X</a>'
        + '<a href="#" data-share="facebook" class="sbtn">Facebook</a>'
        + '<a href="#" data-share="line" class="sbtn">LINE</a>'
        + tspan("Copy link", "リンクをコピー", cls="sbtn", tag="button").replace("<button", '<button id="copyLinkBtn"')
        + '</div>'
        '<div class="cite">'
        + tspan("Cite this snapshot", "このデータを引用", cls="cite-h")
        + f'<code id="citeText">{esc(cite_en)}</code>'
        + tspan("Copy citation", "引用文をコピー", cls="sbtn", tag="button").replace("<button", '<button id="copyCiteBtn"')
        + '</div>'
        '</section>'
    )


# 共通スタイル（言語トグル・共有・引用・トースト）。素の文字列。
SHARED_UI_CSS = (
    ".topbar{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}"
    ".langtog{background:transparent;border:1px solid var(--line);color:var(--text);border-radius:999px;"
    "padding:8px 15px;font-size:13px;cursor:pointer;line-height:1;white-space:nowrap}"
    ".langtog:hover{border-color:var(--pitch);color:var(--pitch)}"
    ".share{display:flex;flex-wrap:wrap;gap:10px;margin:2px 0 4px}"
    ".sbtn{appearance:none;border:1px solid var(--line);background:var(--surface2,#16221d);color:var(--text);"
    "border-radius:999px;padding:11px 17px;font-size:13.5px;cursor:pointer;text-decoration:none;"
    "display:inline-flex;align-items:center;line-height:1}"
    ".sbtn:hover{border-color:var(--pitch);color:var(--pitch)}"
    ".sbtn.primary{background:var(--pitch);color:#06231a;border-color:var(--pitch);font-weight:700}"
    ".sbtn.primary:hover{color:#06231a;filter:brightness(1.05)}"
    ".cite{margin-top:18px}"
    ".cite-h{display:block;font-size:11.5px;letter-spacing:.1em;text-transform:uppercase;color:var(--muted2);margin-bottom:9px}"
    ".cite code{display:block;background:#0a0f0d;border:1px solid var(--line);border-radius:10px;padding:13px 15px;"
    "font-size:12.5px;color:var(--muted);word-break:break-word;line-height:1.5;"
    "font-family:ui-monospace,SFMono-Regular,Menlo,monospace}"
    ".cite .sbtn{margin-top:11px}"
    "#toast{position:fixed;left:50%;bottom:26px;transform:translateX(-50%) translateY(20px);background:var(--pitch);"
    "color:#06231a;font-weight:700;padding:11px 20px;border-radius:999px;opacity:0;pointer-events:none;"
    "transition:opacity .25s,transform .25s;font-size:13.5px;z-index:60}"
    "#toast.show{opacity:1;transform:translateX(-50%) translateY(0)}"
    # --- 検索ページへのCTA ---
    ".cta-wrap{margin:22px 0 6px;display:flex;flex-direction:column;gap:9px;align-items:flex-start}"
    ".cta{display:inline-flex;align-items:center;gap:12px;background:var(--pitch);color:#06231a;"
    "font-weight:800;font-size:16px;letter-spacing:.01em;padding:15px 26px;border-radius:999px;"
    "text-decoration:none;box-shadow:0 6px 22px rgba(22,224,138,.28);"
    "transition:transform .15s,box-shadow .2s,filter .2s}"
    ".cta:hover{transform:translateY(-2px);box-shadow:0 10px 30px rgba(22,224,138,.42);filter:brightness(1.05);color:#06231a}"
    ".cta .cta-arrow{font-size:18px}"
    ".cta-sub{font-size:12px;color:var(--muted2);letter-spacing:.02em}"
    # --- ライセンス注記 ---
    ".license{color:var(--muted2);font-size:11.5px;line-height:1.65;margin:20px 0 0;max-width:72ch}"
    ".license a{color:var(--muted)}"
    "@media(max-width:640px){.cta{width:100%;justify-content:center;text-align:center}.cta-wrap{align-items:stretch}}"
)


# ============================ ルート一覧ページ ============================

def build_root_index():
    """docs/ 配下の日付フォルダを走査し、各日付ページへのリンク一覧 docs/index.html を作る。"""
    if not os.path.isdir(DOCS_DIR):
        return
    days = []
    for name in os.listdir(DOCS_DIR):
        d = os.path.join(DOCS_DIR, name)
        if not (os.path.isdir(d) and len(name) == 8 and name.isdigit()):
            continue
        if not os.path.exists(os.path.join(d, "index.html")):
            continue
        summary = {}
        sp = os.path.join(d, "stats.json")
        if os.path.exists(sp):
            try:
                with open(sp, encoding="utf-8") as f:
                    summary = json.load(f)
            except Exception:  # noqa: BLE001
                summary = {}
        days.append((name, summary))
    days.sort(key=lambda x: x[0], reverse=True)  # 新しい順

    rows = []
    for name, s in days:
        bits_en, bits_ja = [], []
        if s.get("videos_analyzed") is not None:
            bits_en.append(f"{fmt(s['videos_analyzed'])} videos")
            bits_ja.append(f"{fmt(s['videos_analyzed'])}本")
        if s.get("countries") is not None:
            bits_en.append(f"{s['countries']} countries")
            bits_ja.append(f"{s['countries']}カ国")
        if (s.get("totals") or {}).get("views"):
            bits_en.append(f"{fmt(s['totals']['views'])} views")
            bits_ja.append(f"{fmt(s['totals']['views'])}回再生")
        meta_en = " · ".join(bits_en)
        meta_ja = " · ".join(bits_ja)
        rows.append(
            f'<a class="day" href="{name}/"><span class="d num">{readable(name)}</span>'
            f'<span class="m" data-en="{esc(meta_en)}" data-ja="{esc(meta_ja)}">{esc(meta_en)}</span>'
            f'<span class="go num">&rarr;</span></a>'
        )
    days_html = "\n".join(rows) if rows else (
        '<p class="lead">' + tspan("No snapshots yet.", "スナップショットはまだありません。") + "</p>"
    )

    page_url = PAGE_URL.rstrip("/") + "/"
    title_en = "SoccerScope — FIFA World Cup 2026 YouTube Trends & Fan Reactions"
    title_ja = "SoccerScope — FIFAワールドカップ2026のYouTubeトレンド分析"
    desc_en = ("Daily FIFA World Cup 2026 statistics on YouTube football videos trending worldwide — "
               "views, countries, fan sentiment, teams, and themes. Data & analysis by TubeSaku.")
    desc_ja = ("FIFAワールドカップ2026に関連して世界で話題のYouTubeサッカー動画の日次統計 — "
               "再生数・対象国・ファン感情・代表チーム・トレンド話題。データ・分析：TubeSaku。")
    share_en = ("⚽ Daily FIFA World Cup 2026 YouTube football trends — "
                "viral videos, fan sentiment & themes. SoccerScope by TubeSaku")
    share_ja = ("⚽ FIFAワールドカップ2026のYouTubeサッカー動画トレンドを毎日データ化 — "
                "再生数・感情・話題。SoccerScope（TubeSaku）")
    cite_en = f"SoccerScope by TubeSaku — daily FIFA World Cup 2026 YouTube football trend snapshots. {page_url}"
    cite_ja = f"SoccerScope（TubeSaku）— FIFAワールドカップ2026 YouTubeサッカー動画トレンドの日次スナップショット。{page_url}"

    json_ld = {
        "@context": "https://schema.org", "@type": "Dataset",
        "name": "SoccerScope — FIFA World Cup 2026 YouTube Football Trends (daily snapshots)",
        "alternateName": ["World Cup 2026 YouTube Trends", "ワールドカップ2026 サッカー動画トレンド"],
        "description": ("Daily statistics on trending FIFA World Cup 2026 football (soccer) videos across countries: "
                        "view counts, audience sentiment, mentioned teams and themes."),
        "url": page_url,
        "keywords": SEO_KEYWORDS,
        "inLanguage": ["en", "ja"],
        "isAccessibleForFree": True,
        "license": DATA_LICENSE_URL,
        "usageInfo": DATA_USAGE_INFO,
        "sameAs": [SEARCH_URL, TUBESAKU_URL],
        "creator": {"@type": "Organization", "name": "TubeSaku", "url": TUBESAKU_URL},
        "publisher": {"@type": "Organization", "name": "TubeSaku", "url": TUBESAKU_URL},
        "about": [
            world_cup_event(),
            {"@type": "Thing", "name": "YouTube football video trends"},
            {"@type": "Thing", "name": "fan comment sentiment analysis"},
        ],
    }
    if days:
        latest = days[0][0]
        json_ld["dateModified"] = readable(latest)
        json_ld["distribution"] = [{"@type": "DataDownload", "encodingFormat": "application/json",
                                    "contentUrl": page_url + latest + "/stats.json"}]
    json_ld_html = ('<script type="application/ld+json">'
                    + json.dumps(json_ld, ensure_ascii=False) + "</script>")

    css = (
        ":root{--ink:#0a0f0d;--surface:#111b17;--surface2:#16221d;--line:#23332c;--pitch:#16e08a;--gold:#ffd23f;"
        "--text:#f1f6f3;--muted:#8ba096;--muted2:#5f7268}"
        "*{box-sizing:border-box;margin:0;padding:0}"
        'body{background:var(--ink);color:var(--text);font-family:"Zen Kaku Gothic New",sans-serif;line-height:1.6}'
        '.num{font-family:"Anton",sans-serif;letter-spacing:.02em}'
        ".wrap{max-width:820px;margin:0 auto;padding:0 22px}"
        "header{padding:30px 0 18px}"
        '.brand{font-family:"Anton";font-size:24px;letter-spacing:.04em}.brand .dot{color:var(--pitch)}'
        "h1{font-weight:900;font-size:clamp(28px,5vw,46px);line-height:1.05;margin:16px 0 12px}h1 .gold{color:var(--gold)}"
        ".lead{color:var(--muted);max-width:60ch}.lead a{color:var(--pitch);font-weight:700}"
        "h2{font-weight:900;font-size:18px;margin:30px 0 14px}"
        ".day{display:flex;align-items:center;gap:14px;background:var(--surface);border:1px solid var(--line);"
        "border-radius:14px;padding:16px 20px;margin:10px 0;text-decoration:none;color:var(--text);"
        "transition:border-color .2s,transform .15s}"
        ".day:hover{border-color:var(--pitch);transform:translateY(-2px)}"
        ".day .d{font-size:18px;color:var(--pitch);flex:0 0 130px}"
        ".day .m{color:var(--muted);font-size:13.5px;flex:1}"
        ".day .go{color:var(--muted2)}"
        ".card{background:var(--surface);border:1px solid var(--line);border-radius:14px;padding:22px 24px;margin:18px 0}"
        ".card h2{margin:0 0 14px}"
        ".credit{background:linear-gradient(180deg,var(--surface),var(--ink));border:1px solid var(--line);"
        "border-radius:14px;padding:24px;margin:26px 0;text-align:center}.credit a{color:var(--pitch);font-weight:700}"
        "footer{border-top:1px solid var(--line);margin-top:36px;padding:24px 0 60px;color:var(--muted2);"
        "font-size:12.5px;display:flex;gap:14px;flex-wrap:wrap;justify-content:space-between}footer a{color:var(--muted)}"
        + SHARED_UI_CSS
        + "@media(max-width:640px){.day{flex-wrap:wrap}.day .d{flex-basis:auto}.day .m{flex-basis:100%}}"
    )

    h1_html = ('<h1>' + tspan("FIFA World Cup 2026 YouTube trends, ", "FIFAワールドカップ2026のYouTubeトレンドを、")
               + tspan("in data", "データで", cls="gold") + tspan(".", "。") + '</h1>')

    lead_html = (
        '<p class="lead">'
        + tspan("Daily snapshots of FIFA World Cup 2026 football videos trending on YouTube across countries — "
                "views, fan sentiment, mentioned teams and themes. Data & analysis by ",
                "FIFAワールドカップ2026に関連して国をまたいで話題になっているYouTubeサッカー動画の日次スナップショット — "
                "再生数・ファンの感情・代表チーム言及・トレンド話題。データ・分析：")
        + f'<a href="{TUBESAKU_URL}" rel="noopener"><strong>{esc(TUBESAKU_LABEL)}</strong></a>'
        + tspan(".", "。") + '</p>'
    )


    faq_html = (
        '<section class="card"><h2>'
        + tspan("What makes this World Cup data useful?", "このワールドカップデータで何がわかるか") + '</h2>'
        + '<p class="lead">'
        + tspan("The pages combine YouTube football video trend signals, country coverage, view counts, fan-comment sentiment and mentioned national teams. This makes SoccerScope useful for World Cup 2026 content planning, football creator discovery, media research and sponsor research.",
                "YouTubeサッカー動画のトレンド信号、対象国、再生数、ファンコメントの感情、言及された代表チームを組み合わせています。ワールドカップ2026のコンテンツ企画、サッカー系クリエイター発掘、メディア調査、スポンサー調査に使えます。")
        + '</p></section>'
    )

    credit_html = (
        '<section class="credit">'
        + tspan("Data & analysis powered by ", "データ・分析：") + " "
        + f'<a href="{TUBESAKU_URL}" rel="noopener">{esc(TUBESAKU_LABEL)}</a>'
        + tspan(".", "。") + '<br>'
        + tspan("Search World Cup 2026 football videos and creators: ", "ワールドカップ2026関連のサッカー動画・クリエイターを検索：") + " "
        + f'<a href="{SEARCH_URL}" rel="noopener">{esc(SEARCH_LABEL)}</a>'
        + '</section>'
    )

    footer_html = (
        '<footer>'
        + tspan("Updated daily during the FIFA World Cup 2026", "FIFAワールドカップ2026期間中は毎日更新")
        + f'<span><a href="{TUBESAKU_URL}" rel="noopener">{esc(TUBESAKU_URL)}</a></span>'
        + '</footer>'
    )

    index_html = (
        '<!DOCTYPE html><html lang="en"><head>'
        + GTM_HEAD +
        '<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">'
        f"<title>{esc(title_en)}</title>"
        f'<meta name="description" content="{esc(desc_en)}">'
        + head_meta(title_en, desc_en, page_url)
        + json_ld_html
        + jsonld_blocks(organization_jsonld(), website_jsonld())
        + ss_config_script(page_url, title_en, title_ja, desc_en, desc_ja,
                           share_en, share_ja, cite_en, cite_ja)
        + '<link rel="preconnect" href="https://fonts.googleapis.com">'
        '<link href="https://fonts.googleapis.com/css2?family=Anton&family=Zen+Kaku+Gothic+New:wght@400;500;700;900&display=swap" rel="stylesheet">'
        f"<style>{css}</style>"
        '<meta name="msvalidate.01" content="40662D5BA70BBEC0B1E069CC25FCEF09" />'
        "</head><body><div class=\"wrap\">"
        '<header><div class="topbar"><div class="brand">SOCCER<span class="dot">·</span>SCOPE</div>'
        '<button id="langBtn" class="langtog">日本語</button></div>'
        + h1_html + lead_html + search_cta_html() + '</header>'
        + '<h2>' + tspan("Daily snapshots", "日別スナップショット") + '</h2>'
        + days_html
        + share_cite_section("Share & cite", "シェア・引用", cite_en)
        + faq_html
        + credit_html
        + license_note_html()
        + footer_html
        + '</div>'
        + '<div id="toast" data-en="Copied!" data-ja="コピーしました">Copied!</div>'
        + PAGE_JS
        + "</body></html>"
    )
    with open(os.path.join(DOCS_DIR, "index.html"), "w", encoding="utf-8") as f:
        f.write(index_html)


    # Discovery files for crawlers, AI answer engines, and search consoles.
    urls = [(page_url, readable(days[0][0]) if days else None, "daily", "1.0")]
    for name, _summary in days:
        urls.append((page_url + name + "/", readable(name), "daily", "0.9"))
    sitemap = ['<?xml version="1.0" encoding="UTF-8"?>', '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for loc, lastmod, changefreq, priority in urls:
        sitemap.append("  <url>")
        sitemap.append(f"    <loc>{esc(loc)}</loc>")
        if lastmod:
            sitemap.append(f"    <lastmod>{esc(lastmod)}</lastmod>")
        sitemap.append(f"    <changefreq>{changefreq}</changefreq>")
        sitemap.append(f"    <priority>{priority}</priority>")
        sitemap.append("  </url>")
    sitemap.append('</urlset>')
    with open(os.path.join(DOCS_DIR, "sitemap.xml"), "w", encoding="utf-8") as f:
        f.write("\n".join(sitemap) + "\n")
    with open(os.path.join(DOCS_DIR, "robots.txt"), "w", encoding="utf-8") as f:
        f.write(
            "User-agent: *\n"
            "Allow: /\n\n"
            "# AI/user-request crawlers are allowed to read the public World Cup 2026 data pages.\n"
            "User-agent: OAI-SearchBot\nAllow: /\n"
            "User-agent: ChatGPT-User\nAllow: /\n"
            "User-agent: PerplexityBot\nAllow: /\n"
            "User-agent: Claude-SearchBot\nAllow: /\n"
            "User-agent: Claude-User\nAllow: /\n\n"
            f"Sitemap: {page_url}sitemap.xml\n"
        )
    with open(os.path.join(DOCS_DIR, "llms.txt"), "w", encoding="utf-8") as f:
        f.write(
            "# SoccerScope\n\n"
            "SoccerScope publishes daily FIFA World Cup 2026 YouTube football video trend snapshots.\n"
            "Use these pages to understand viral soccer videos, fan reactions, mentioned national teams, and cross-country trend signals.\n\n"
            f"- Home: {page_url}\n"
            f"- Sitemap: {page_url}sitemap.xml\n"
            f"- Search app: {SEARCH_URL}\n"
            f"- Data provider: {TUBESAKU_URL}\n"
        )
    print(f"  書き出し: {os.path.join(DOCS_DIR, 'sitemap.xml')}")
    print(f"  書き出し: {os.path.join(DOCS_DIR, 'robots.txt')}")
    print(f"  書き出し: {os.path.join(DOCS_DIR, 'llms.txt')}")


# ============================ 日次ページ ============================

def build_day_page(date_str, phase7_path, ca_path, phase2_path):
    """1日分のデータを集計し docs/<date_str>/{index.html, stats.json} を書き出す。"""
    videos = load_json(phase7_path).get("videos", [])
    if not videos:
        print(f"  WARN: {phase7_path} の videos が空。スキップ。", file=sys.stderr)
        return False

    # ---- comment_analysis を先読みし、is_soccer_related==False の動画を除外 ----
    # 4_load_comment_analysis.py は同じ判定でMongoDBから動画を削除しているが、
    # build_stats_page.py は data/ の中間JSON(phase7)を直接読むため、MongoDB側の
    # 削除とは独立に同じフィルタをここでも適用する必要がある（そうしないと、
    # 既にMongoDBから削除済みのサッカー非関連動画が統計ページにだけ残ってしまう）。
    # comment_analysisがまだ無い動画（分析未実施）は除外しない（false確定情報が
    # 無い限り、誤って除外しすぎないようにするため）。
    analyses = {}
    if ca_path:
        analyses = load_json(ca_path).get("analyses", {})

    excluded_count = 0
    filtered_videos = []
    for v in videos:
        a = analyses.get(v.get("video_id"))
        if a is not None and a.get("is_soccer_related") is False:
            excluded_count += 1
            continue
        filtered_videos.append(v)
    if excluded_count:
        print(f"  サッカー非関連のため除外: {excluded_count}件 "
              f"(is_soccer_related=false in {os.path.basename(ca_path)})")
    videos = filtered_videos
    if not videos:
        print(f"  WARN: {phase7_path} の動画が全てサッカー非関連として除外されました。スキップ。",
              file=sys.stderr)
        return False

    # team言及集計・ナレーション等で使うanalysesも、除外後のvideosに含まれる
    # video_idだけに絞る（除外したクリケット動画等のmentioned_teamsが
    # チームランキングに混入しないようにするため）。
    remaining_ids = {v.get("video_id") for v in videos}
    analyses = {vid: a for vid, a in analyses.items() if vid in remaining_ids}

    # (b) 多対多: phase2 から各動画に「出現したすべての国」を付ける。
    # API・Mongo は触らない。phase2 が無ければ phase7 の単一 country にフォールバック。
    # phase2_path は resolve_target_date_files() で同じ収集セットとして
    # 対応付けられたものを使う（同ディレクトリ内の「最新ファイル」探索はしない。
    # 1ディレクトリに複数日分が混在するケースがあるため）。
    country_en, country_ja, lang_by_code = attach_countries_from_phase2(videos, phase2_path)
    if phase2_path:
        print(f"  countries付与(b): {len(videos)}件 "
              f"(phase2={os.path.basename(phase2_path)})")
    else:
        print(f"  WARN: phase2 が無いため country(単一)のままフォールバック。", file=sys.stderr)

    # ---- 動画系の集計 ----
    n_videos = len(videos)

    # 国別の量的ランキングは廃止（同一言語の国が同じバイラルを共有し、視聴回数も
    # 全世界1値のため、何を指標にしても同値で並ぶ＝意味を成さない）。
    # 「どの国で話題か」は動画単位の事実として残す。
    countries_seen = set()
    total_views = total_likes = total_comments = 0
    for v in videos:
        countries_seen.update(v.get("countries", []))
        st = v.get("stats", {})
        total_views += int(st.get("view_count", 0) or 0)
        total_likes += int(st.get("like_count", 0) or 0)
        total_comments += int(st.get("comment_count", 0) or 0)
    n_countries = len(countries_seen)

    top_by_views = sorted(
        videos, key=lambda x: int(x.get("stats", {}).get("view_count", 0) or 0), reverse=True
    )[:TOP_N]

    # reach（出現国数）降順。attach_countries_from_phase2は既に呼ばれた後なので
    # 各動画にreachが付与済み。同率の場合は再生数で安定的にタイブレークする。
    top_by_reach = sorted(
        videos,
        key=lambda x: (int(x.get("reach", 0) or 0),
                       int(x.get("stats", {}).get("view_count", 0) or 0)),
        reverse=True,
    )[:TOP_N]

    # ---- 感情・テーマの集計 ----
    sentiment = None
    pos_themes = neg_themes = []
    n_analyzed = 0
    total_comments_analyzed = 0
    if ca_path:
        n_analyzed = len(analyses)
        s_acc = {"positive": 0.0, "negative": 0.0, "neutral": 0.0}
        pos_map, neg_map = {}, {}

        def add_theme(target, t):
            en = t.get("theme_en") or t.get("theme_ja") or "?"
            ja = t.get("theme_ja") or t.get("theme_en") or en
            d = target.setdefault(en, {"en": en, "ja": ja, "count": 0})
            d["count"] += int(t.get("mention_count", 0) or 0)

        for a in analyses.values():
            s = a.get("sentiment", {}) or {}
            for k in s_acc:
                s_acc[k] += float(s.get(k, 0) or 0)
            total_comments_analyzed += int(a.get("total_analyzed", 0) or 0)
            for t in (a.get("positive_themes") or []):
                add_theme(pos_map, t)
            for t in (a.get("negative_themes") or []):
                add_theme(neg_map, t)
        if n_analyzed:
            sentiment = {k: s_acc[k] / n_analyzed for k in s_acc}
        pos_themes = sorted(pos_map.values(), key=lambda d: d["count"], reverse=True)[:8]
        neg_themes = sorted(neg_map.values(), key=lambda d: d["count"], reverse=True)[:8]

    # ---- ① ヒーロー指標: 言及の多い代表チーム（コメント由来＝国の偏りと無関係に信頼できる）----
    top_teams = aggregate_team_mentions(analyses)
    max_team = max((t["mentions"] for t in top_teams), default=1) or 1

    # ---- ② 上位動画への日英ナレーション（出現国・言語・言及チームベース）----
    narration = narrate_top_videos(top_by_views, analyses, country_en, lang_by_code)

    # ---- URL / 日付 ----
    readable_date = readable(date_str)
    page_url = PAGE_URL.rstrip("/") + "/"
    dated_url = page_url + date_str + "/"

    # ---- 機械可読データ（stats.json）----
    stats_payload = {
        "generated": readable_date,
        "videos_analyzed": n_videos,
        "countries": n_countries,
        "totals": {"views": total_views, "likes": total_likes, "comments": total_comments},
        "country_attribution": "multi (b): a video is counted in every country whose search surfaced it; no per-country ranking (unreliable)",
        "top_teams": [
            {"team": t["team"], "mentions": t["mentions"], "lean": t["lean"],
             "positive": t["positive"], "neutral": t["neutral"], "negative": t["negative"]}
            for t in top_teams[:TOP_N]
        ],
        "top_videos_by_views": [
            {"title": v.get("title", ""),
             "countries": v.get("countries", []), "reach": v.get("reach", 0),
             "narration_en": narration.get(v.get("video_id"), {}).get("en", ""),
             "narration_ja": narration.get(v.get("video_id"), {}).get("ja", ""),
             "views": int(v.get("stats", {}).get("view_count", 0) or 0), "url": v.get("url", "")}
            for v in top_by_views
        ],
        "top_videos_by_reach": [
            {"title": v.get("title", ""),
             "countries": v.get("countries", []), "reach": v.get("reach", 0),
             "narration_en": narration.get(v.get("video_id"), {}).get("en", ""),
             "narration_ja": narration.get(v.get("video_id"), {}).get("ja", ""),
             "views": int(v.get("stats", {}).get("view_count", 0) or 0), "url": v.get("url", "")}
            for v in top_by_reach
        ],
        "sentiment_avg": sentiment,
        "comments_analyzed": total_comments_analyzed,
        "top_positive_themes": [{"theme_en": d["en"], "theme_ja": d["ja"], "mentions": d["count"]} for d in pos_themes],
        "top_negative_themes": [{"theme_en": d["en"], "theme_ja": d["ja"], "mentions": d["count"]} for d in neg_themes],
    }

    # ---- JSON-LD ----
    json_ld = {
        "@context": "https://schema.org", "@type": "Dataset",
        "name": f"SoccerScope — FIFA World Cup 2026 YouTube Football Trends Dataset ({readable_date})",
        "alternateName": [f"World Cup 2026 YouTube Trends {readable_date}", f"ワールドカップ2026 サッカー動画トレンド {readable_date}"],
        "description": (f"Open statistics on FIFA World Cup 2026 football (soccer) videos trending across {n_countries} countries: "
                        "view counts, audience sentiment, mentioned teams, and trending themes."),
        "url": dated_url,
        "keywords": SEO_KEYWORDS,
        "inLanguage": ["en", "ja"],
        "isAccessibleForFree": True,
        "dateModified": readable_date,
        "temporalCoverage": readable_date,
        "license": DATA_LICENSE_URL,
        "usageInfo": DATA_USAGE_INFO,
        "sameAs": [SEARCH_URL, TUBESAKU_URL],
        "creator": {"@type": "Organization", "name": "TubeSaku", "url": TUBESAKU_URL},
        "publisher": {"@type": "Organization", "name": "TubeSaku", "url": TUBESAKU_URL},
        "distribution": [{"@type": "DataDownload", "encodingFormat": "application/json",
                          "contentUrl": dated_url + "stats.json"}],
        "variableMeasured": ["view_count", "like_count", "comment_count", "sentiment", "mentioned_teams", "trending_themes"],
        "about": [
            world_cup_event(),
            {"@type": "Thing", "name": "YouTube football video trends"},
            {"@type": "Thing", "name": "fan comment sentiment analysis"},
        ],
        "measurementTechnique": "TubeSaku YouTube metadata aggregation and fan-comment sentiment/theme analysis",
        "mainEntityOfPage": dated_url,
    }
    json_ld_html = ('<script type="application/ld+json">'
                    + json.dumps(json_ld, ensure_ascii=False) + "</script>")

    # ---- 行 ----
    # ① 言及チーム ランキング（バー=言及数の比率、感情の傾きを色で）
    _lean_color = {"positive": "var(--pitch)", "negative": "#d64545", "neutral": "#9aa0a6"}
    _lean_ja = {"positive": "好意的", "negative": "否定的", "neutral": "中立"}
    team_rows = "\n".join(
        f'<div class="row"><span class="lbl">{esc(t["team"])} '
        f'<em data-en="{t["lean"]}" data-ja="{_lean_ja[t["lean"]]}">{t["lean"]}</em></span>'
        f'{bar(100*t["mentions"]/max_team, color=_lean_color.get(t["lean"], "var(--pitch)"))}'
        f'<span class="val num">{fmt(t["mentions"])}</span></div>'
        for t in top_teams[:TOP_N]
    ) or (
        '<p class="muted" data-en="No team-mention data for this day."'
        ' data-ja="この日付には言及チームのデータがありません。">No team-mention data for this day.</p>'
    )

    def _country_label(v):
        codes = v.get("countries", [])
        reach = v.get("reach", 0)
        head = ", ".join(country_en.get(c, c) for c in codes[:3])
        head_ja = "・".join(country_ja.get(c, c) for c in codes[:3])
        if reach > 3:
            return (f"{esc(head)} +{reach-3}", f"{esc(head_ja)} 他{reach-3}カ国")
        return (esc(head or "—"), esc(head_ja or "—"))

    def _video_li(v, highlight="views"):
        en, ja = _country_label(v)
        nb = narration.get(v.get("video_id"), {})
        blurb = ""
        if nb.get("en") or nb.get("ja"):
            blurb = (f'<p class="blurb" data-en="{esc(nb.get("en",""))}" '
                     f'data-ja="{esc(nb.get("ja",""))}">{esc(nb.get("en","") or nb.get("ja",""))}</p>')
        if highlight == "reach":
            reach = v.get("reach", 0)
            metric_html = (
                f'<b class="num">{fmt(reach)}</b> '
                f'<span data-en="countries" data-ja="カ国でランクイン">countries</span>'
                f' · <b class="num">{fmt(v.get("stats",{}).get("view_count",0))}</b> '
                f'<span data-en="views" data-ja="回再生">views</span>'
            )
        else:
            metric_html = (
                f'<b class="num">{fmt(v.get("stats",{}).get("view_count",0))}</b> '
                f'<span data-en="views" data-ja="回再生">views</span>'
            )
        return (
            f'<li><a href="{esc(v.get("url",""))}" target="_blank" rel="noopener">'
            f'{esc((v.get("title") or "")[:90])}</a>'
            f'<span class="meta"><em data-en="{en}" data-ja="{ja}">{en}</em>'
            f' · {metric_html}</span>{blurb}</li>'
        )

    video_rows = "\n".join(_video_li(v) for v in top_by_views)
    video_rows_by_reach = "\n".join(_video_li(v, highlight="reach") for v in top_by_reach)

    sentiment_html = ""
    if sentiment:
        sub = tspan(
            f"Average sentiment across {fmt(total_comments_analyzed)} analyzed comments on {n_analyzed} videos.",
            f"動画{n_analyzed}本・コメント{fmt(total_comments_analyzed)}件を分析した平均感情。",
            cls="sub")
        sentiment_html = (
            '<section class="card">'
            + '<h2>' + tspan("How fans feel", "ファンの反応") + '</h2>'
            + sub
            + f'<div class="row"><span class="lbl">{tspan("Positive","ポジティブ")}</span>'
              f'{bar(sentiment["positive"], "var(--pitch)")}<span class="val num">{sentiment["positive"]:.0f}%</span></div>'
            + f'<div class="row"><span class="lbl">{tspan("Neutral","中立")}</span>'
              f'{bar(sentiment["neutral"], "#6b7b73")}<span class="val num">{sentiment["neutral"]:.0f}%</span></div>'
            + f'<div class="row"><span class="lbl">{tspan("Negative","ネガティブ")}</span>'
              f'{bar(sentiment["negative"], "#ff6b5e")}<span class="val num">{sentiment["negative"]:.0f}%</span></div>'
            + '</section>'
        )

    themes_html = ""
    if pos_themes or neg_themes:
        pos_li = "".join(
            f'<li><span data-en="{esc(d["en"])}" data-ja="{esc(d["ja"])}">{esc(d["en"])}</span> '
            f"<b class='num'>{fmt(d['count'])}</b></li>" for d in pos_themes)
        neg_li = "".join(
            f'<li><span data-en="{esc(d["en"])}" data-ja="{esc(d["ja"])}">{esc(d["en"])}</span> '
            f"<b class='num'>{fmt(d['count'])}</b></li>" for d in neg_themes)
        themes_html = (
            '<section class="grid2">'
            + '<div class="card"><h2>' + tspan("Top positive themes", "ポジティブな話題トップ")
            + f'</h2><ul class="themes pos">{pos_li}</ul></div>'
            + '<div class="card"><h2>' + tspan("Top negative themes", "ネガティブな話題トップ")
            + f'</h2><ul class="themes neg">{neg_li}</ul></div>'
            + '</section>'
        )

    # ---- 共有・引用テキスト ----
    title_en = f"FIFA World Cup 2026 YouTube Trends — {readable_date} | SoccerScope"
    title_ja = f"FIFAワールドカップ2026 YouTube動画トレンド — {readable_date} | SoccerScope"
    desc_en = (f"{readable_date} snapshot of FIFA World Cup 2026 YouTube football videos going viral across {n_countries} countries — "
               "views, fan sentiment, mentioned teams and trending themes. Data & analysis by TubeSaku.")
    desc_ja = (f"{readable_date}時点のFIFAワールドカップ2026関連YouTubeサッカー動画トレンド。{n_countries}カ国のバズ動画、再生数、ファン感情、代表チーム言及、話題を分析。データ・分析：TubeSaku。")
    share_en = (f"⚽ FIFA World Cup 2026 YouTube trends: {fmt(n_videos)} football videos from {n_countries} countries · "
                f"{fmt(total_views)} views — see fan reactions. SoccerScope by TubeSaku")
    share_ja = (f"⚽ FIFAワールドカップ2026 YouTubeトレンド：{n_countries}カ国のサッカー動画{fmt(n_videos)}本・総再生{fmt(total_views)}回。"
                "ファンの反応をデータで。SoccerScope（TubeSaku）")
    cite_en = f"SoccerScope by TubeSaku — FIFA World Cup 2026 YouTube football trends snapshot {readable_date}. {dated_url}"
    cite_ja = f"SoccerScope（TubeSaku）— FIFAワールドカップ2026 YouTubeサッカー動画トレンド {readable_date} 時点。{dated_url}"

    # ---- CSS（既存デザイン踏襲 + 共通UI）。素の文字列で連結 ----
    css = (
        ":root{--ink:#0a0f0d;--surface:#111b17;--surface2:#16221d;--line:#23332c;--pitch:#16e08a;--gold:#ffd23f;"
        "--text:#f1f6f3;--muted:#8ba096;--muted2:#5f7268}"
        "*{box-sizing:border-box;margin:0;padding:0}"
        'body{background:var(--ink);color:var(--text);font-family:"Zen Kaku Gothic New",sans-serif;'
        "line-height:1.6;-webkit-font-smoothing:antialiased}"
        '.num{font-family:"Anton",sans-serif;letter-spacing:.02em}'
        ".wrap{max-width:920px;margin:0 auto;padding:0 22px}"
        "header{padding:30px 0 26px}"
        '.brand{font-family:"Anton";font-size:24px;letter-spacing:.04em}.brand .dot{color:var(--pitch)}'
        ".crumb{font-size:12.5px;color:var(--muted2);margin:10px 0 0}.crumb a{color:var(--muted);text-decoration:none}"
        "h1{font-weight:900;font-size:clamp(30px,5vw,52px);line-height:1.05;margin:18px 0 12px}h1 .gold{color:var(--gold)}"
        ".lead{color:var(--muted);max-width:60ch}.lead a{color:var(--pitch);font-weight:700}"
        ".stats{display:flex;gap:30px;flex-wrap:wrap;margin:34px 0 10px}"
        '.stat .n{font-family:"Anton";font-size:clamp(30px,5vw,48px);color:var(--pitch);line-height:.9}'
        ".stat .n.gold{color:var(--gold)}"
        ".stat .l{font-size:11.5px;letter-spacing:.12em;text-transform:uppercase;color:var(--muted2);margin-top:7px}"
        ".card{background:var(--surface);border:1px solid var(--line);border-radius:16px;padding:24px 26px;margin:18px 0}"
        "h2{font-weight:900;font-size:20px;margin-bottom:16px}"
        ".sub{color:var(--muted);font-size:13.5px;margin:-8px 0 16px}"
        ".row{display:flex;align-items:center;gap:12px;margin:9px 0;font-size:14px}"
        ".lbl{flex:0 0 210px;color:var(--text)}"
        '.lbl em{color:var(--muted2);font-style:normal;font-size:11px;font-family:"Anton"}'
        ".bar{flex:1;height:9px;background:#0a0f0d;border-radius:6px;overflow:hidden}.bar i{display:block;height:100%}"
        ".val{flex:0 0 64px;text-align:right;color:var(--muted)}"
        "ol,ul{list-style:none}"
        "ol.videos li{padding:11px 0;border-bottom:1px solid var(--line)}"
        "ol.videos a{color:var(--text);text-decoration:none;border-bottom:1px solid rgba(22,224,138,.4)}"
        "ol.videos a:hover{color:var(--pitch)}"
        "ol.videos .meta{display:block;font-size:12px;color:var(--muted2);margin-top:4px}ol.videos .meta em{font-style:normal}"
        "ol.videos .blurb{margin:6px 0 0;font-size:13px;line-height:1.6;color:var(--muted)}"
        "p.muted,.muted{color:var(--muted);font-size:13.5px;margin:-8px 0 16px}"
        ".grid2{display:grid;grid-template-columns:1fr 1fr;gap:18px}"
        ".themes li{display:flex;justify-content:space-between;gap:12px;padding:7px 0;border-bottom:1px solid var(--line);font-size:14px}"
        ".themes.pos b{color:var(--pitch)}.themes.neg b{color:#ff6b5e}"
        ".credit{background:linear-gradient(180deg,var(--surface),var(--ink));border:1px solid var(--line);"
        "border-radius:16px;padding:26px;margin:26px 0;text-align:center}.credit a{color:var(--pitch);font-weight:700}"
        "footer{border-top:1px solid var(--line);margin-top:40px;padding:26px 0 60px;color:var(--muted2);"
        "font-size:12.5px;display:flex;gap:16px;flex-wrap:wrap;justify-content:space-between}footer a{color:var(--muted)}"
        + SHARED_UI_CSS
        + "@media(max-width:640px){.grid2{grid-template-columns:1fr}.lbl{flex-basis:120px}.stats{gap:22px}}"
    )

    crumb = (
        '<p class="crumb"><a href="../">'
        + tspan("← All dates", "← 日付一覧") + '</a> · '
        + tspan(f"snapshot {readable_date}", f"{readable_date} 時点") + '</p>'
    )

    h1_html = ('<h1>' + tspan("FIFA World Cup 2026 YouTube trends, ", "FIFAワールドカップ2026のYouTubeトレンドを、")
               + tspan("in data", "データで", cls="gold") + tspan(".", "。") + '</h1>')

    lead_html = (
        '<p class="lead">'
        + tspan("Open statistics from a cross-country dataset of FIFA World Cup 2026 YouTube football videos — "
                "what's getting watched, which teams are mentioned, and how fans react. Data & analysis by ",
                "FIFAワールドカップ2026に関連して各国で話題のYouTubeサッカー動画を横断的に集めたオープン統計 — "
                "何が観られ、どの代表チームが語られ、ファンがどう反応しているか。データ・分析：")
        + f'<a href="{TUBESAKU_URL}" rel="noopener"><strong>{esc(TUBESAKU_LABEL)}</strong></a>'
        + tspan(".", "。") + '</p>'
    )

    stats_block = (
        '<div class="stats">'
        f'<div class="stat"><div class="n num">{fmt(n_videos)}</div>'
        f'<div class="l">{tspan("videos analyzed","分析した動画")}</div></div>'
        f'<div class="stat"><div class="n num">{n_countries}</div>'
        f'<div class="l">{tspan("countries","対象国")}</div></div>'
        f'<div class="stat"><div class="n num gold">{fmt(total_views)}</div>'
        f'<div class="l">{tspan("total views","総再生回数")}</div></div>'
        f'<div class="stat"><div class="n num">{fmt(total_comments)}</div>'
        f'<div class="l">{tspan("total comments","総コメント数")}</div></div>'
        '</div>'
    )

    credit_html = (
        '<section class="credit">'
        + tspan("Data & analysis powered by ", "データ・分析：") + " "
        + f'<a href="{TUBESAKU_URL}" rel="noopener">{esc(TUBESAKU_LABEL)}</a>'
        + tspan(".", "。") + '<br>'
        + tspan("Search World Cup 2026 football videos and creators: ", "ワールドカップ2026関連のサッカー動画・クリエイターを検索：") + " "
        + f'<a href="{SEARCH_URL}" rel="noopener">{esc(SEARCH_LABEL)}</a>'
        + '</section>'
    )

    footer_html = (
        '<footer>'
        + tspan(f"Generated {readable_date} · built with Google ADK · Gemini · MongoDB Atlas Vector Search",
                f"{readable_date} 生成 · Google ADK · Gemini · MongoDB Atlas Vector Search 使用")
        + f'<span><a href="{TUBESAKU_URL}" rel="noopener">{esc(TUBESAKU_URL)}</a></span>'
        + '</footer>'
    )

    page = (
        '<!DOCTYPE html><html lang="en"><head>'
        + GTM_HEAD +
        '<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">'
        f"<title>{esc(title_en)}</title>"
        f'<meta name="description" content="{esc(desc_en)}">'
        + head_meta(title_en, desc_en, dated_url)
        + json_ld_html
        + jsonld_blocks(organization_jsonld(), website_jsonld(),
                        breadcrumb_jsonld(readable_date, dated_url))
        + ss_config_script(dated_url, title_en, title_ja, desc_en, desc_ja,
                           share_en, share_ja, cite_en, cite_ja)
        + '<link rel="preconnect" href="https://fonts.googleapis.com">'
        '<link href="https://fonts.googleapis.com/css2?family=Anton&family=Zen+Kaku+Gothic+New:wght@400;500;700;900&display=swap" rel="stylesheet">'
        f"<style>{css}</style></head><body><div class=\"wrap\">"
        '<header><div class="topbar"><div class="brand">SOCCER<span class="dot">·</span>SCOPE</div>'
        '<button id="langBtn" class="langtog">日本語</button></div>'
        + crumb + h1_html + lead_html + search_cta_html() + stats_block + '</header>'
        + '<section class="card"><h2>'
        + tspan(f"Most-talked-about teams (top {TOP_N})",
                f"最も語られている代表チーム（トップ{TOP_N}）")
        + '</h2>'
        + tspan("Ranked by how often national teams are mentioned in analyzed World Cup 2026 fan comments, "
                "with the overall sentiment lean.",
                "ワールドカップ2026関連の分析対象コメント内で各代表チームが言及された回数のランキング（感情の傾き付き）。",
                cls="muted", tag="p")
        + f'{team_rows}</section>'
        + '<section class="card"><h2>'
        + tspan("Most-watched trending videos", "最も再生されたトレンド動画")
        + f'</h2><ol class="videos">{video_rows}</ol></section>'
        + '<section class="card"><h2>'
        + tspan("Most cross-country trending videos", "最も多くの国でランクインした動画")
        + '</h2>'
        + tspan(f"World Cup 2026 football videos that surfaced in the most countries' YouTube trend searches (top {TOP_N}).",
                f"YouTube検索結果で最も多くの国にまたがって出現したワールドカップ2026関連サッカー動画（トップ{TOP_N}）。",
                cls="muted", tag="p")
        + f'<ol class="videos">{video_rows_by_reach}</ol></section>'
        + sentiment_html
        + themes_html
        + share_cite_section("Share & cite", "シェア・引用", cite_en)
        + credit_html
        + license_note_html()
        + footer_html
        + '</div>'
        + '<div id="toast" data-en="Copied!" data-ja="コピーしました">Copied!</div>'
        + PAGE_JS
        + "</body></html>"
    )

    # ---- 出力 ----
    day_dir = os.path.join(DOCS_DIR, date_str)
    os.makedirs(day_dir, exist_ok=True)
    out = os.path.join(day_dir, "index.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(page)
    stats_out = os.path.join(day_dir, "stats.json")
    with open(stats_out, "w", encoding="utf-8") as f:
        json.dump(stats_payload, f, ensure_ascii=False, indent=2)

    print(f"  書き出し: {out}")
    print(f"  書き出し: {stats_out}")
    return True


def process_date(date_str, files):
    """resolve_target_date_files() が返した1日分のファイルセットでページを生成する。"""
    phase7_path = files["phase7"]
    ca_path = files["comment_analysis"]
    phase2_path = files["phase2"]
    print(f"[{date_str}] videos: {os.path.basename(phase7_path)}"
          f" / comments: {os.path.basename(ca_path) if ca_path else '(なし)'}"
          f" / phase2: {os.path.basename(phase2_path)}")
    return build_day_page(date_str, phase7_path, ca_path, phase2_path)


def main() -> int:
    do_all = ("-all" in sys.argv) or ("--all" in sys.argv)

    by_target_date = resolve_target_date_files()
    if not by_target_date:
        print(f"ERROR: {DATA_DIR}/ 配下から収集対象日(--target-date)に対応する"
              f"phase2/phase7のセットが見つかりません。", file=sys.stderr)
        return 1

    all_dates = sorted(by_target_date.keys())
    targets = all_dates if do_all else [all_dates[-1]]  # 末尾＝target_dateが最も新しい日
    print(f"対象: {', '.join(targets)}" + (" (-all)" if do_all else " (最新)"))

    ok = 0
    for d in targets:
        if process_date(d, by_target_date[d]):
            ok += 1

    if ok == 0:
        print("ERROR: 生成できたページがありません。", file=sys.stderr)
        return 1

    # ルートの日付一覧を再生成（1回だけ）
    build_root_index()
    print(f"\n生成: {ok}/{len(targets)} 日分。docs/index.html（日付一覧）を再生成。")
    print("→ git add docs/ && commit && push、Settings>Pages を /docs に設定。")
    print("→ OGP画像は docs/images/soccerscope-ogp.png に配置（1200×630推奨）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
