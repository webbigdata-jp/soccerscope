#!/usr/bin/env python3
"""
build_stats_page.py — Generates statistics pages (docs/<date>/index.html) from data.

Reads phase7_with_buzz_score_*.json (video metadata + stats + buzz) and
comment_analysis_*.json (sentiment and themes) under data/<YYYYMMDD>/, aggregates
statistics for buzzing soccer videos, and writes a self-contained HTML page for
GitHub Pages.

Specification:
- The output date is determined from YYYYMMDD folder names under data/ rather than
  the execution date.
- No arguments: process the latest YYYYMMDD folder only.
- -all: process all YYYYMMDD folders under data/ and generate each docs/<date>/ page.
- Exit with an error if no YYYYMMDD folder exists.
- phase7 and comment_analysis are read from files in the same date folder.
- Pages default to English and provide a language toggle without reload, persisted
  through localStorage.
- Citation and sharing support: OGP, Twitter Card, canonical, JSON-LD, citation copy,
  X/Facebook/LINE sharing, Web Share API, link copy, and mobile optimization.
- No external libraries are required; the standard library is enough.

Run:
    python build_stats_page.py        # Latest date folder
    python build_stats_page.py -all   # All date folders
"""

import os
import re
import sys
import glob
import json
import html
from collections import Counter, defaultdict

# ==== Edit points: customize these URLs ====
TUBESAKU_URL = "https://tubesaku.com"            # Data source
TUBESAKU_LABEL = "TubeSaku — YouTube creator and video trend analysis"
SEARCH_URL = "https://soccer.tubesaku.com"       # Search page, replacing the old live agent
SEARCH_LABEL = "SoccerScope World Cup 2026 Search"
PAGE_URL = "https://webbigdata-jp.github.io/soccerscope/"  # Public URL for OGP, canonical, and JSON-LD
OGP_IMAGE_URL = PAGE_URL.rstrip("/") + "/images/soccerscope-ogp.png"  # Place this under docs/images/
TOP_N = 10
WORLD_CUP_EN = "FIFA World Cup 2026"
WORLD_CUP_JA = "FIFA World Cup 2026"
SEO_KEYWORDS = [
    "FIFA World Cup 2026", "World Cup 2026", "2026 FIFA World Cup",
    "football video trends", "soccer video trends", "YouTube football trends",
    "viral soccer videos", "fan reactions", "sentiment analysis", "TubeSaku",
    "World Cup 2026", "FIFA World Cup 2026", "soccer videos", "YouTube trends",
    "international soccer videos", "fan comment analysis", "soccer video analysis",
]
# ==== Schema.org / license-related settings ====
ORG_NAME = "TubeSaku"
# Search page brand name and CTA button destination.
SEARCH_BRAND = "SOCCER·SCOPE"
# Organization logo. Prefer a square 112x112 or larger image under docs/images/. Use the OGP image as a fallback if absent.
LOGO_URL = PAGE_URL.rstrip("/") + "/images/soccerscope-logo.png"
# Dataset license: CC BY 4.0 for aggregated and analyzed statistics, attributed to TubeSaku.
DATA_LICENSE_URL = "https://creativecommons.org/licenses/by/4.0/"
# Internal anchor for the human-readable license scope note.
DATA_USAGE_INFO = PAGE_URL.rstrip("/") + "/#data-license"

# Narration (Gemini) settings. Automatically skipped when no API key is available.
GEMINI_MODEL = os.environ.get("SOCCER_NARRATE_MODEL", "gemini-3.1-flash-lite")
NARRATE_TOP_N = 10           # Number of videos to narrate
NARRATE_COMMENTS_PER_VIDEO = 0  # Do not pass raw comments by default; use existing analysis results.


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

# Allow narration step 2 to load an API key from .env; optional and safe to omit.
# If python-dotenv is unavailable, do nothing and use shell environment variables.
try:  # noqa: SIM105
    from dotenv import load_dotenv as _load_dotenv
    for _p in (".env", os.path.join("git", "soccer_agent", ".env"), os.path.join("..", ".env")):
        if os.path.exists(_p):
            _load_dotenv(_p)
except Exception:  # noqa: BLE001
    pass


def esc(s):
    """Escape HTML for both attribute values and text."""
    return html.escape("" if s is None else str(s), quote=True)


def tspan(en, ja, cls=None, tag="span"):
    """Return a toggle-ready element with English/Japanese text in data attributes; English is shown initially."""
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
    """Return YYYYMMDD folder names directly under data/ in ascending order.
    This remains for backward compatibility, but the main process_date logic uses
    resolve_target_date_files() because folder names are execution dates and may
    not match collection target dates (--target-date)."""
    if not os.path.isdir(DATA_DIR):
        return []
    out = []
    for name in os.listdir(DATA_DIR):
        if len(name) == 8 and name.isdigit() and os.path.isdir(os.path.join(DATA_DIR, name)):
            out.append(name)
    return sorted(out)


def find_in_dir(dirpath, *patterns):
    """Return the latest filename-matching file in the specified folder, sorted by filename."""
    hits = []
    for pat in patterns:
        hits += glob.glob(os.path.join(dirpath, pat))
    return max(hits, key=lambda p: os.path.basename(p)) if hits else None


# ---- File mapping based on target_date, the collection target date ----------------------
#
# data/<execution-date>/ folders are named after the date the process was run.
# When run_backfill.sh or similar scripts recollect past dates (--target-date),
# one execution-date folder can contain files for multiple collection target dates.
# In addition, rerunning 3_analyze_comments.py, for example to retroactively apply
# is_soccer_related judgments, can create multiple comment_analysis_*.json files
# for the same collection set at separate times.
#
# Therefore, guessing "probably the same set" from nearby filename timestamps does
# not work in this workflow because reanalysis can run many hours later. Instead,
# follow the source_file fields in each intermediate JSON deterministically:
# comment_analysis -> phase4 -> phase7 -> phase3 -> phase2. Do not use ambiguous
# guessing at all.

def _index_files_by_basename(*filename_globs):
    """Search across data/ directly and one-level-down YYYYMMDD folders, then build a
    mapping from filename basename to full path. If the same basename exists in
    multiple locations, keep the file with the newer mtime. This normally should
    not happen, but it is the safer behavior."""
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
        print(f"  WARN: failed to read {path}: {e}", file=sys.stderr)
        return None


def resolve_target_date_files():
    """Return a dictionary sorted by target_date ascending:
    target_date -> {"phase7": path, "comment_analysis": path|None, "phase2": path}.

    Mapping is done by reliably following the source_file chain:
      comment_analysis.source_file -> phase4 file
      phase4.source_file           -> phase7 file
      phase7.source_file           -> phase3 file
      phase3.source_file           -> phase2 file
      phase2.target_date           -> collection target date
        For older phase2 files without a target_date field, read target_date from
        the filename phase2_video_ids_{target_date}_{ts}.json. If it was run
        without --target-date, the filename is only phase2_video_ids_{ts}.json, so
        use the date portion of ts. In every case, this is deterministic
        information read from the filename rather than a guess based on timestamp
        proximity.

    If multiple comment_analysis files exist for one target_date due to repeated
    reanalysis, adopt the one whose comment_analysis file has the newest generated_at,
    rather than looking inside the analyses contents.
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
        # If the target_date field is absent, read it from the filename.
        m = re.match(r'phase2_video_ids_(\d{8})_\d{8}_\d{6}\.json$', phase2_name)
        if m:
            return m.group(1)
        m2 = re.match(r'phase2_video_ids_(\d{8})_\d{6}\.json$', phase2_name)
        if m2:
            return m2.group(1)
        return None

    # Resolve target_date for every phase2 file in advance.
    phase2_target_date_by_name = {}
    for name, path in phase2_index.items():
        td = phase2_target_date(path, name)
        if td:
            phase2_target_date_by_name[name] = td
        else:
            print(f"  WARN: {name}  could not be resolved to a target_date.", file=sys.stderr)

    # comment_analysis -> phase4 -> phase7 -> phase3 -> phase2 -> target_date
    # Follow the chain and build target_date -> [(generated_at, ca_path, phase7_path, phase2_path), ...]
    by_target_date = {}
    for ca_name, ca_path in ca_index.items():
        ca_data = _safe_load_json(ca_path)
        if not ca_data:
            continue
        phase4_name = ca_data.get("source_file")
        phase4_path = phase4_index.get(phase4_name) if phase4_name else None
        if not phase4_path:
            print(f"  WARN: {ca_name} source_file='{phase4_name}'  was not found. Skipping.",
                  file=sys.stderr)
            continue

        phase4_data = _safe_load_json(phase4_path)
        if not phase4_data:
            continue
        phase7_name = phase4_data.get("source_file")
        phase7_path = phase7_index.get(phase7_name) if phase7_name else None
        if not phase7_path:
            print(f"  WARN: {phase4_name} source_file='{phase7_name}'  was not found. Skipping.",
                  file=sys.stderr)
            continue

        phase7_data = _safe_load_json(phase7_path)
        if not phase7_data:
            continue
        phase3_name = phase7_data.get("source_file")
        phase3_path = phase3_index.get(phase3_name) if phase3_name else None
        if not phase3_path:
            print(f"  WARN: {phase7_name} source_file='{phase3_name}'  was not found. Skipping.",
                  file=sys.stderr)
            continue

        phase3_data = _safe_load_json(phase3_path)
        if not phase3_data:
            continue
        phase2_name = phase3_data.get("source_file")
        if not phase2_name or phase2_name not in phase2_target_date_by_name:
            print(f"  WARN: {phase3_name} source_file='{phase2_name}' "
                  f"has no matching target_date. Skipping.", file=sys.stderr)
            continue
        target_date = phase2_target_date_by_name[phase2_name]
        phase2_path = phase2_index[phase2_name]

        generated_at = ca_data.get("generated_at", "")  # Determine newest/oldest through string comparison; ISO 8601 is assumed.
        by_target_date.setdefault(target_date, []).append(
            (generated_at, ca_path, phase7_path, phase2_path)
        )

    # Also pick up days where phase2 exists but comment_analysis cannot be traced,
    # likely because comment analysis has not been run yet, if they can be found
    # through the phase2 -> phase3 -> phase7 chain alone. Page generation can still
    # proceed with comment_analysis set to None.
    phase3_by_phase2name = {}
    for p3_name, p3_path in phase3_index.items():
        p3_data = _safe_load_json(p3_path)
        if p3_data and p3_data.get("source_file"):
            phase3_by_phase2name.setdefault(p3_data["source_file"], []).append((p3_name, p3_path))

    result = {}
    for target_date, candidates in by_target_date.items():
        # Adopt the newest generated_at value, i.e. the latest reanalysis result.
        candidates.sort(key=lambda c: c[0])
        _generated_at, ca_path, phase7_path, phase2_path = candidates[-1]
        if len(candidates) > 1:
            print(f"  INFO: target_date={target_date} comment_analysis entries: "
                  f"{len(candidates)} entries found. Using the latest: {os.path.basename(ca_path)}")
        result[target_date] = {"phase7": phase7_path, "comment_analysis": ca_path, "phase2": phase2_path}

    # Fill target_dates not found through the comment_analysis chain using only phase2 -> phase3 -> phase7.
    for phase2_name, target_date in phase2_target_date_by_name.items():
        if target_date in result:
            continue
        phase2_path = phase2_index[phase2_name]
        p3_candidates = phase3_by_phase2name.get(phase2_name, [])
        if not p3_candidates:
            continue
        # If multiple exist, use the first found; name collisions are not expected normally.
        _p3_name, p3_path = p3_candidates[0]
        p3_data = _safe_load_json(p3_path)
        if not p3_data:
            continue
        # phase7 references the phase3 source_file, so a reverse lookup is required.
        # Search phase7_index for the item whose source_file == p3_name.
        matched_phase7 = None
        for p7_name, p7_path in phase7_index.items():
            p7_data = _safe_load_json(p7_path)
            if p7_data and p7_data.get("source_file") == _p3_name:
                matched_phase7 = p7_path
                break
        if not matched_phase7:
            print(f"  WARN: target_date={target_date}  has no generated comment_analysis, and "
                  f"the matching phase7 was not found, so it will be skipped.", file=sys.stderr)
            continue
        print(f"  INFO: target_date={target_date}  has no generated comment_analysis. "
              f"Generating the page with video data only.")
        result[target_date] = {"phase7": matched_phase7, "comment_analysis": None, "phase2": phase2_path}

    return result


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def attach_countries_from_phase2(videos, phase2_path):
    """
    (b) Many-to-many: attach every country where each video appeared based on phase2
    search results. Use only phase2_video_ids_*.json from the same date folder;
    do not call the API or MongoDB.

    Adds the following to each video:
      v["countries"]: list of country codes where it appeared, sorted by ranking
                      order in each country's search results
      v["reach"]    : number of countries where it appeared
    Videos absent from phase2 fall back to a single-element v["country"] value if present.

    Returns (name_en, name_ja, lang_by_code), dictionaries mapping code to display
    name and primary language.
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
            countries = [code for _rank, code in sorted(lst)]  # Countries with better ranks first
        else:
            c = v.get("country")
            countries = [c] if c else []
            if c:  # Also keep display names for fallback videos.
                name_en.setdefault(c, v.get("country_name_en") or v.get("country_name_ja") or c)
                name_ja.setdefault(c, v.get("country_name_ja") or v.get("country_name_en") or c)
                lang_by_code.setdefault(c, v.get("primary_lang", ""))
        v["countries"] = countries
        v["reach"] = len(countries)
    return name_en, name_ja, lang_by_code


def aggregate_team_mentions(analyses):
    """Aggregate mentioned_teams from each comment_analysis video and return them in
    descending order by mention count. Each analysis is expected to have
    mentioned_teams: [{team, sentiment(positive/neutral/negative), mention_count}].
    If absent, return an empty list so the section is hidden.
    Returns [{team, mentions, positive, neutral, negative, lean}] sorted by mentions descending.
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
    """Add short English/Japanese blurbs to top videos with Gemini, step 2.
    If the API key is unset, the SDK is unavailable, or generation fails, return {}
    and skip narration while continuing page generation. To prevent fabrication,
    assume comments are one global pool and prohibit inventing per-nationality
    opinions. Use only facts such as countries where the video appeared, languages,
    overall sentiment, and mentioned teams.
    Returns {video_id: {"en": str, "ja": str}}.
    """
    if not (os.environ.get("GEMINI_API_KEY")):
        print("  Narration: API key is not set, skipping", file=sys.stderr)
        return {}
    try:
        from google import genai
        from google.genai import types
    except Exception as e:  # noqa: BLE001
        print(f"  Narration: google-genai is not installed, skipping ({e})", file=sys.stderr)
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
        print(f"  Narration: generation failed, skipping ({e})", file=sys.stderr)
        return {}

    out = {}
    for d in (data if isinstance(data, list) else []):
        vid = d.get("video_id")
        if vid:
            out[vid] = {"en": (d.get("blurb_en") or "").strip(),
                        "ja": (d.get("blurb_ja") or "").strip()}
    print(f"  Narration: {len(out)}/{len(items)} generated")
    return out


# ---- Shared client-side toggle and sharing JS. Raw string; do not make it an f-string. ----
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
    if(btn) btn.textContent = (l==='en') ? 'Japanese' : 'English';
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
    """Pass language-specific title, description, share text, and citation text to JS using XSS-safe json.dumps."""
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
    """OGP, Twitter Card, and canonical metadata. Crawlers receive English as the default."""
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


# --- Shared Schema.org entities and UI components -------------------------------------

def world_cup_event():
    """Return a complete SportsEvent for World Cup 2026. This satisfies required
    Google Event fields (name/startDate/location) and recommended fields
    (endDate/eventStatus/eventAttendanceMode/organizer/description), avoiding
    structured data errors."""
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
    """Publisher organization, TubeSaku, for search logo display and Knowledge Graph use."""
    return {
        "@context": "https://schema.org", "@type": "Organization",
        "name": ORG_NAME, "url": TUBESAKU_URL, "logo": LOGO_URL,
        "sameAs": [SEARCH_URL, TUBESAKU_URL],
    }


def website_jsonld():
    """WebSite entity representing the entire site."""
    return {
        "@context": "https://schema.org", "@type": "WebSite",
        "name": "SoccerScope",
        "alternateName": SEARCH_BRAND,
        "url": PAGE_URL.rstrip("/") + "/",
        "inLanguage": ["en", "ja"],
        "publisher": {"@type": "Organization", "name": ORG_NAME, "url": TUBESAKU_URL},
    }


def breadcrumb_jsonld(readable_date, dated_url):
    """Breadcrumbs for daily pages, Home -> date, compatible with Google breadcrumb rich results."""
    return {
        "@context": "https://schema.org", "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "SoccerScope",
             "item": PAGE_URL.rstrip("/") + "/"},
            {"@type": "ListItem", "position": 2, "name": readable_date, "item": dated_url},
        ],
    }


def jsonld_blocks(*objs):
    """Combine multiple JSON-LD objects into script blocks."""
    return "".join('<script type="application/ld+json">'
                   + json.dumps(o, ensure_ascii=False) + "</script>" for o in objs)


def search_cta_html():
    """Prominent CTA button to the search page, SOCCER·SCOPE / soccer.tubesaku.com."""
    return (
        '<div class="cta-wrap">'
        f'<a class="cta" href="{SEARCH_URL}" rel="noopener">'
        + tspan("⚽ Search World Cup 2026 videos & creators",
                "⚽ Search World Cup 2026 videos and creators")
        + '<span class="cta-arrow num">&rarr;</span></a>'
        + tspan(f"Open {SEARCH_BRAND} — search engine for World Cup 2026 football videos, creators & posts",
                f"Open {SEARCH_BRAND} — search World Cup 2026 soccer videos, creators, and posts",
                cls="cta-sub")
        + '</div>'
    )


def license_note_html():
    """Human-readable license note for the footer, covering CC BY 4.0 and excluding YouTube assets."""
    return (
        '<p class="license" id="data-license">'
        + tspan("Statistics & analysis by TubeSaku are licensed under ",
                "Statistics and analysis by TubeSaku are licensed under ")
        + f'<a href="{DATA_LICENSE_URL}" rel="noopener license">CC BY 4.0</a>'
        + tspan(". Underlying YouTube videos, titles, thumbnails and comments remain the property "
                "of YouTube and their respective creators, and are not covered by this license.",
                " The underlying YouTube videos, titles, thumbnails, comments, and similar assets remain the property of "
                "YouTube and their respective creators, and are not covered by this license.")
        + '</p>'
    )


def share_cite_section(heading_en, heading_ja, cite_en):
    """Sharing buttons plus a citation block. The citation starts in English and is swapped from __SS__ on language change."""
    return (
        '<section class="card">'
        '<h2>' + tspan(heading_en, heading_ja) + '</h2>'
        '<div class="share">'
        + tspan("Share", "Share", cls="sbtn primary", tag="button").replace("<button", '<button id="shareBtn"')
        + '<a href="#" data-share="x" class="sbtn">X</a>'
        + '<a href="#" data-share="facebook" class="sbtn">Facebook</a>'
        + '<a href="#" data-share="line" class="sbtn">LINE</a>'
        + tspan("Copy link", "Copy link", cls="sbtn", tag="button").replace("<button", '<button id="copyLinkBtn"')
        + '</div>'
        '<div class="cite">'
        + tspan("Cite this snapshot", "Cite this snapshot", cls="cite-h")
        + f'<code id="citeText">{esc(cite_en)}</code>'
        + tspan("Copy citation", "Copy citation", cls="sbtn", tag="button").replace("<button", '<button id="copyCiteBtn"')
        + '</div>'
        '</section>'
    )


# Shared styles for the language toggle, sharing, citation, and toast. Raw string.
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
    # --- CTA to the search page ---
    ".cta-wrap{margin:22px 0 6px;display:flex;flex-direction:column;gap:9px;align-items:flex-start}"
    ".cta{display:inline-flex;align-items:center;gap:12px;background:var(--pitch);color:#06231a;"
    "font-weight:800;font-size:16px;letter-spacing:.01em;padding:15px 26px;border-radius:999px;"
    "text-decoration:none;box-shadow:0 6px 22px rgba(22,224,138,.28);"
    "transition:transform .15s,box-shadow .2s,filter .2s}"
    ".cta:hover{transform:translateY(-2px);box-shadow:0 10px 30px rgba(22,224,138,.42);filter:brightness(1.05);color:#06231a}"
    ".cta .cta-arrow{font-size:18px}"
    ".cta-sub{font-size:12px;color:var(--muted2);letter-spacing:.02em}"
    # --- License note ---
    ".license{color:var(--muted2);font-size:11.5px;line-height:1.65;margin:20px 0 0;max-width:72ch}"
    ".license a{color:var(--muted)}"
    "@media(max-width:640px){.cta{width:100%;justify-content:center;text-align:center}.cta-wrap{align-items:stretch}}"
)


# ============================ Root index page ============================

def build_root_index():
    """Scan date folders under docs/ and build docs/index.html as a list of links to each daily page."""
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
    days.sort(key=lambda x: x[0], reverse=True)  # Newest first

    rows = []
    for name, s in days:
        bits_en, bits_ja = [], []
        if s.get("videos_analyzed") is not None:
            bits_en.append(f"{fmt(s['videos_analyzed'])} videos")
            bits_ja.append(f"{fmt(s['videos_analyzed'])} videos")
        if s.get("countries") is not None:
            bits_en.append(f"{s['countries']} countries")
            bits_ja.append(f"{s['countries']} countries")
        if (s.get("totals") or {}).get("views"):
            bits_en.append(f"{fmt(s['totals']['views'])} views")
            bits_ja.append(f"{fmt(s['totals']['views'])} views")
        meta_en = " · ".join(bits_en)
        meta_ja = " · ".join(bits_ja)
        rows.append(
            f'<a class="day" href="{name}/"><span class="d num">{readable(name)}</span>'
            f'<span class="m" data-en="{esc(meta_en)}" data-ja="{esc(meta_ja)}">{esc(meta_en)}</span>'
            f'<span class="go num">&rarr;</span></a>'
        )
    days_html = "\n".join(rows) if rows else (
        '<p class="lead">' + tspan("No snapshots yet.", "No snapshots yet.") + "</p>"
    )

    page_url = PAGE_URL.rstrip("/") + "/"
    title_en = "SoccerScope — FIFA World Cup 2026 YouTube Trends & Fan Reactions"
    title_ja = "SoccerScope — FIFA World Cup 2026 YouTube Trend Analysis"
    desc_en = ("Daily FIFA World Cup 2026 statistics on YouTube football videos trending worldwide — "
               "views, countries, fan sentiment, teams, and themes. Data & analysis by TubeSaku.")
    desc_ja = ("Daily statistics on YouTube soccer videos trending worldwide for FIFA World Cup 2026 — "
               "views, countries, fan sentiment, mentioned national teams, and trending topics. Data and analysis by TubeSaku.")
    share_en = ("⚽ Daily FIFA World Cup 2026 YouTube football trends — "
                "viral videos, fan sentiment & themes. SoccerScope by TubeSaku")
    share_ja = ("⚽ Daily YouTube soccer video trends for FIFA World Cup 2026 — "
                "views, sentiment, and topics. SoccerScope by TubeSaku")
    cite_en = f"SoccerScope by TubeSaku — daily FIFA World Cup 2026 YouTube football trend snapshots. {page_url}"
    cite_ja = f"SoccerScope by TubeSaku — daily FIFA World Cup 2026 YouTube soccer video trend snapshots. {page_url}"

    json_ld = {
        "@context": "https://schema.org", "@type": "Dataset",
        "name": "SoccerScope — FIFA World Cup 2026 YouTube Football Trends (daily snapshots)",
        "alternateName": ["World Cup 2026 YouTube Trends", "World Cup 2026 Soccer Video Trends"],
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

    h1_html = ('<h1>' + tspan("FIFA World Cup 2026 YouTube trends, ", "FIFA World Cup 2026 YouTube trends, ")
               + tspan("in data", "in data", cls="gold") + tspan(".", ".") + '</h1>')

    lead_html = (
        '<p class="lead">'
        + tspan("Daily snapshots of FIFA World Cup 2026 football videos trending on YouTube across countries — "
                "views, fan sentiment, mentioned teams and themes. Data & analysis by ",
                "Daily snapshots of YouTube soccer videos trending across countries for FIFA World Cup 2026 — "
                "views, fan sentiment, national-team mentions, and trending topics. Data and analysis: ")
        + f'<a href="{TUBESAKU_URL}" rel="noopener"><strong>{esc(TUBESAKU_LABEL)}</strong></a>'
        + tspan(".", ".") + '</p>'
    )


    faq_html = (
        '<section class="card"><h2>'
        + tspan("What makes this World Cup data useful?", "What makes this World Cup data useful?") + '</h2>'
        + '<p class="lead">'
        + tspan("The pages combine YouTube football video trend signals, country coverage, view counts, fan-comment sentiment and mentioned national teams. This makes SoccerScope useful for World Cup 2026 content planning, football creator discovery, media research and sponsor research.",
                "The pages combine YouTube soccer video trend signals, country coverage, view counts, fan-comment sentiment, and mentioned national teams. SoccerScope is useful for World Cup 2026 content planning, soccer creator discovery, media research, and sponsor research.")
        + '</p></section>'
    )

    credit_html = (
        '<section class="credit">'
        + tspan("Data & analysis powered by ", "Data and analysis: ") + " "
        + f'<a href="{TUBESAKU_URL}" rel="noopener">{esc(TUBESAKU_LABEL)}</a>'
        + tspan(".", ".") + '<br>'
        + tspan("Search World Cup 2026 football videos and creators: ", "Search World Cup 2026 soccer videos and creators: ") + " "
        + f'<a href="{SEARCH_URL}" rel="noopener">{esc(SEARCH_LABEL)}</a>'
        + '</section>'
    )

    footer_html = (
        '<footer>'
        + tspan("Updated daily during the FIFA World Cup 2026", "Updated daily during the FIFA World Cup 2026")
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
        '<button id="langBtn" class="langtog">Japanese</button></div>'
        + h1_html + lead_html + search_cta_html() + '</header>'
        + '<h2>' + tspan("Daily snapshots", "Daily snapshots") + '</h2>'
        + days_html
        + share_cite_section("Share & cite", "Share & cite", cite_en)
        + faq_html
        + credit_html
        + license_note_html()
        + footer_html
        + '</div>'
        + '<div id="toast" data-en="Copied!" data-ja="Copied!">Copied!</div>'
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
    print(f"  Wrote: {os.path.join(DOCS_DIR, 'sitemap.xml')}")
    print(f"  Wrote: {os.path.join(DOCS_DIR, 'robots.txt')}")
    print(f"  Wrote: {os.path.join(DOCS_DIR, 'llms.txt')}")


# ============================ Daily page ============================

def build_day_page(date_str, phase7_path, ca_path, phase2_path):
    """Aggregate one day of data and write docs/<date_str>/{index.html, stats.json}."""
    videos = load_json(phase7_path).get("videos", [])
    if not videos:
        print(f"  WARN: {phase7_path} has an empty videos list. Skipping.", file=sys.stderr)
        return False

    # ---- Preload comment_analysis and exclude videos whose is_soccer_related is False ----
    # 4_load_comment_analysis.py removes videos from MongoDB using the same judgment,
    # but build_stats_page.py reads the intermediate JSON under data/ directly.
    # Therefore, the same filter must also be applied here independently of MongoDB
    # deletion; otherwise, non-soccer videos already removed from MongoDB would
    # remain only on the statistics page. Videos without comment_analysis yet,
    # meaning unanalyzed videos, are not excluded unless there is confirmed false
    # information, to avoid over-excluding by mistake.
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
        print(f"  Excluded as non-soccer-related: {excluded_count} items "
              f"(is_soccer_related=false in {os.path.basename(ca_path)})")
    videos = filtered_videos
    if not videos:
        print(f"  WARN: {phase7_path} videos were all excluded as non-soccer-related. Skipping.",
              file=sys.stderr)
        return False

    # Also restrict analyses used for team-mention aggregation and narration to
    # video_ids that remain in the filtered videos, so mentioned_teams from excluded
    # cricket videos or similar items do not enter the team ranking.
    remaining_ids = {v.get("video_id") for v in videos}
    analyses = {vid: a for vid, a in analyses.items() if vid in remaining_ids}

    # (b) Many-to-many: attach every country where each video appeared from phase2.
    # Do not call the API or MongoDB. If phase2 is absent, fall back to the single
    # country in phase7. Use the phase2_path mapped to the same collection set by
    # resolve_target_date_files(); do not search for the latest file in the same
    # directory because one directory can contain multiple days.
    country_en, country_ja, lang_by_code = attach_countries_from_phase2(videos, phase2_path)
    if phase2_path:
        print(f"  countries attached (b): {len(videos)} items "
              f"(phase2={os.path.basename(phase2_path)})")
    else:
        print(f"  WARN: phase2 is absent, so falling back to the single country value.", file=sys.stderr)

    # ---- Video-related aggregation ----
    n_videos = len(videos)

    # Country-level quantitative rankings are removed because countries sharing the
    # same language share the same viral items, and view counts are one global
    # value, so any metric ties and becomes meaningless. Keep "which countries it
    # trended in" as a video-level fact.
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

    # Sort by reach, the number of countries where it appeared, descending.
    # attach_countries_from_phase2 has already run, so each video has reach.
    # Break ties stably by view count.
    top_by_reach = sorted(
        videos,
        key=lambda x: (int(x.get("reach", 0) or 0),
                       int(x.get("stats", {}).get("view_count", 0) or 0)),
        reverse=True,
    )[:TOP_N]

    # ---- Sentiment and theme aggregation ----
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

    # ---- Hero metric: most-mentioned national teams, reliable because it is comment-derived and independent of country bias ----
    top_teams = aggregate_team_mentions(analyses)
    max_team = max((t["mentions"] for t in top_teams), default=1) or 1

    # ---- Step 2: English/Japanese narration for top videos based on countries, languages, and mentioned teams ----
    narration = narrate_top_videos(top_by_views, analyses, country_en, lang_by_code)

    # ---- URL / date ----
    readable_date = readable(date_str)
    page_url = PAGE_URL.rstrip("/") + "/"
    dated_url = page_url + date_str + "/"

    # ---- Machine-readable data, stats.json ----
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
        "alternateName": [f"World Cup 2026 YouTube Trends {readable_date}", f"World Cup 2026 Soccer Video Trends {readable_date}"],
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

    # ---- Rows ----
    # Team-mention ranking. Bars show mention-count ratios, and color shows sentiment lean.
    _lean_color = {"positive": "var(--pitch)", "negative": "#d64545", "neutral": "#9aa0a6"}
    _lean_ja = {"positive": "Positive", "negative": "Negative", "neutral": "Neutral"}
    team_rows = "\n".join(
        f'<div class="row"><span class="lbl">{esc(t["team"])} '
        f'<em data-en="{t["lean"]}" data-ja="{_lean_ja[t["lean"]]}">{t["lean"]}</em></span>'
        f'{bar(100*t["mentions"]/max_team, color=_lean_color.get(t["lean"], "var(--pitch)"))}'
        f'<span class="val num">{fmt(t["mentions"])}</span></div>'
        for t in top_teams[:TOP_N]
    ) or (
        '<p class="muted" data-en="No team-mention data for this day."'
        ' data-ja="No team-mention data for this day.">No team-mention data for this day.</p>'
    )

    def _country_label(v):
        codes = v.get("countries", [])
        reach = v.get("reach", 0)
        head = ", ".join(country_en.get(c, c) for c in codes[:3])
        head_ja = ", ".join(country_ja.get(c, c) for c in codes[:3])
        if reach > 3:
            return (f"{esc(head)} +{reach-3}", f"{esc(head_ja)} +{reach-3} countries")
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
                f'<span data-en="countries" data-ja="countries">countries</span>'
                f' · <b class="num">{fmt(v.get("stats",{}).get("view_count",0))}</b> '
                f'<span data-en="views" data-ja="views">views</span>'
            )
        else:
            metric_html = (
                f'<b class="num">{fmt(v.get("stats",{}).get("view_count",0))}</b> '
                f'<span data-en="views" data-ja="views">views</span>'
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
            f"Average sentiment across {n_analyzed} videos and {fmt(total_comments_analyzed)} analyzed comments.",
            cls="sub")
        sentiment_html = (
            '<section class="card">'
            + '<h2>' + tspan("How fans feel", "How fans feel") + '</h2>'
            + sub
            + f'<div class="row"><span class="lbl">{tspan("Positive","Positive")}</span>'
              f'{bar(sentiment["positive"], "var(--pitch)")}<span class="val num">{sentiment["positive"]:.0f}%</span></div>'
            + f'<div class="row"><span class="lbl">{tspan("Neutral","Neutral")}</span>'
              f'{bar(sentiment["neutral"], "#6b7b73")}<span class="val num">{sentiment["neutral"]:.0f}%</span></div>'
            + f'<div class="row"><span class="lbl">{tspan("Negative","Negative")}</span>'
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
            + '<div class="card"><h2>' + tspan("Top positive themes", "Top positive themes")
            + f'</h2><ul class="themes pos">{pos_li}</ul></div>'
            + '<div class="card"><h2>' + tspan("Top negative themes", "Top negative themes")
            + f'</h2><ul class="themes neg">{neg_li}</ul></div>'
            + '</section>'
        )

    # ---- Share and citation text ----
    title_en = f"FIFA World Cup 2026 YouTube Trends — {readable_date} | SoccerScope"
    title_ja = f"FIFA World Cup 2026 YouTube Video Trends — {readable_date} | SoccerScope"
    desc_en = (f"{readable_date} snapshot of FIFA World Cup 2026 YouTube football videos going viral across {n_countries} countries — "
               "views, fan sentiment, mentioned teams and trending themes. Data & analysis by TubeSaku.")
    desc_ja = (f"FIFA World Cup 2026 YouTube soccer video trends as of {readable_date}. Analyzes buzzing videos across {n_countries} countries, views, fan sentiment, national-team mentions, and topics. Data and analysis by TubeSaku.")
    share_en = (f"⚽ FIFA World Cup 2026 YouTube trends: {fmt(n_videos)} football videos from {n_countries} countries · "
                f"{fmt(total_views)} views — see fan reactions. SoccerScope by TubeSaku")
    share_ja = (f"⚽ FIFA World Cup 2026 YouTube trends: {fmt(n_videos)} soccer videos across {n_countries} countries with {fmt(total_views)} total views. "
                "Fan reactions in data. SoccerScope by TubeSaku")
    cite_en = f"SoccerScope by TubeSaku — FIFA World Cup 2026 YouTube football trends snapshot {readable_date}. {dated_url}"
    cite_ja = f"SoccerScope by TubeSaku — FIFA World Cup 2026 YouTube soccer video trends as of {readable_date}. {dated_url}"

    # ---- CSS, retaining the existing design plus shared UI. Concatenated as raw strings. ----
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
        + tspan("← All dates", "← All dates") + '</a> · '
        + tspan(f"snapshot {readable_date}", f"snapshot {readable_date}") + '</p>'
    )

    h1_html = ('<h1>' + tspan("FIFA World Cup 2026 YouTube trends, ", "FIFA World Cup 2026 YouTube trends, ")
               + tspan("in data", "in data", cls="gold") + tspan(".", ".") + '</h1>')

    lead_html = (
        '<p class="lead">'
        + tspan("Open statistics from a cross-country dataset of FIFA World Cup 2026 YouTube football videos — "
                "what's getting watched, which teams are mentioned, and how fans react. Data & analysis by ",
                "Open statistics that collect YouTube soccer videos trending across countries for FIFA World Cup 2026 — "
                "what is being watched, which national teams are discussed, and how fans react. Data and analysis: ")
        + f'<a href="{TUBESAKU_URL}" rel="noopener"><strong>{esc(TUBESAKU_LABEL)}</strong></a>'
        + tspan(".", ".") + '</p>'
    )

    stats_block = (
        '<div class="stats">'
        f'<div class="stat"><div class="n num">{fmt(n_videos)}</div>'
        f'<div class="l">{tspan("videos analyzed","videos analyzed")}</div></div>'
        f'<div class="stat"><div class="n num">{n_countries}</div>'
        f'<div class="l">{tspan("countries","countries")}</div></div>'
        f'<div class="stat"><div class="n num gold">{fmt(total_views)}</div>'
        f'<div class="l">{tspan("total views","total views")}</div></div>'
        f'<div class="stat"><div class="n num">{fmt(total_comments)}</div>'
        f'<div class="l">{tspan("total comments","total comments")}</div></div>'
        '</div>'
    )

    credit_html = (
        '<section class="credit">'
        + tspan("Data & analysis powered by ", "Data and analysis: ") + " "
        + f'<a href="{TUBESAKU_URL}" rel="noopener">{esc(TUBESAKU_LABEL)}</a>'
        + tspan(".", ".") + '<br>'
        + tspan("Search World Cup 2026 football videos and creators: ", "Search World Cup 2026 soccer videos and creators: ") + " "
        + f'<a href="{SEARCH_URL}" rel="noopener">{esc(SEARCH_LABEL)}</a>'
        + '</section>'
    )

    footer_html = (
        '<footer>'
        + tspan(f"Generated {readable_date} · built with Google ADK · Gemini · MongoDB Atlas Vector Search",
                f"Generated {readable_date} · Google ADK · Gemini · MongoDB Atlas Vector Search")
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
        '<button id="langBtn" class="langtog">Japanese</button></div>'
        + crumb + h1_html + lead_html + search_cta_html() + stats_block + '</header>'
        + '<section class="card"><h2>'
        + tspan(f"Most-talked-about teams (top {TOP_N})",
                f"Most-mentioned national teams (top {TOP_N})")
        + '</h2>'
        + tspan("Ranked by how often national teams are mentioned in analyzed World Cup 2026 fan comments, "
                "with the overall sentiment lean.",
                "Ranking of how often each national team was mentioned in analyzed World Cup 2026-related comments, with sentiment lean.",
                cls="muted", tag="p")
        + f'{team_rows}</section>'
        + '<section class="card"><h2>'
        + tspan("Most-watched trending videos", "Most-watched trending videos")
        + f'</h2><ol class="videos">{video_rows}</ol></section>'
        + '<section class="card"><h2>'
        + tspan("Most cross-country trending videos", "Most cross-country trending videos")
        + '</h2>'
        + tspan(f"World Cup 2026 football videos that surfaced in the most countries' YouTube trend searches (top {TOP_N}).",
                f"World Cup 2026-related soccer videos that appeared across the most countries in YouTube search results (top {TOP_N}).",
                cls="muted", tag="p")
        + f'<ol class="videos">{video_rows_by_reach}</ol></section>'
        + sentiment_html
        + themes_html
        + share_cite_section("Share & cite", "Share & cite", cite_en)
        + credit_html
        + license_note_html()
        + footer_html
        + '</div>'
        + '<div id="toast" data-en="Copied!" data-ja="Copied!">Copied!</div>'
        + PAGE_JS
        + "</body></html>"
    )

    # ---- Output ----
    day_dir = os.path.join(DOCS_DIR, date_str)
    os.makedirs(day_dir, exist_ok=True)
    out = os.path.join(day_dir, "index.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(page)
    stats_out = os.path.join(day_dir, "stats.json")
    with open(stats_out, "w", encoding="utf-8") as f:
        json.dump(stats_payload, f, ensure_ascii=False, indent=2)

    print(f"  Wrote: {out}")
    print(f"  Wrote: {stats_out}")
    return True


def process_date(date_str, files):
    """Generate a page from one day of file sets returned by resolve_target_date_files()."""
    phase7_path = files["phase7"]
    ca_path = files["comment_analysis"]
    phase2_path = files["phase2"]
    print(f"[{date_str}] videos: {os.path.basename(phase7_path)}"
          f" / comments: {os.path.basename(ca_path) if ca_path else '(none)'}"
          f" / phase2: {os.path.basename(phase2_path)}")
    return build_day_page(date_str, phase7_path, ca_path, phase2_path)


def main() -> int:
    do_all = ("-all" in sys.argv) or ("--all" in sys.argv)

    by_target_date = resolve_target_date_files()
    if not by_target_date:
        print(f"ERROR: Could not find a phase2/phase7 set under {DATA_DIR}/ "
              f"that matches the collection target date (--target-date).", file=sys.stderr)
        return 1

    all_dates = sorted(by_target_date.keys())
    targets = all_dates if do_all else [all_dates[-1]]  # Last item is the newest target_date.
    print(f"Targets: {', '.join(targets)}" + (" (-all)" if do_all else " (latest)"))

    ok = 0
    for d in targets:
        if process_date(d, by_target_date[d]):
            ok += 1

    if ok == 0:
        print("ERROR: No pages were generated.", file=sys.stderr)
        return 1

    # Regenerate the root date list once.
    build_root_index()
    print(f"\nGenerated: {ok}/{len(targets)} days. Regenerated docs/index.html (date list).")
    print("-> git add docs/ && commit && push; set Settings > Pages to /docs.")
    print("-> Place the OGP image at docs/images/soccerscope-ogp.png; 1200x630 recommended.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
