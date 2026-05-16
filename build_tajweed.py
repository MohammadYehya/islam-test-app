#!/usr/bin/env python3
"""
Build tajweed-annotated Quran JSON.

Sources:
  - Tajweed-marked Arabic text:  https://api.alquran.cloud/v1/surah/{N}/quran-tajweed
  - Word & ayah translations:    https://api.quran.com/api/v4/verses/by_chapter/{N}

Output:
  data/tajweed-rules.json                  - rule code -> {name, arabic, color, description}
  data/index.json                          - surah list + available translation editions
  data/surah/{N}.json                      - language-agnostic: Arabic + tajweed + transliteration
  data/translations/{key}/{N}.json         - per-language sidecar: ayah text + word translations

Usage:
  python scripts/build_tajweed.py --start 1 --end 114 --out data/
"""

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# ----------------------------------------------------------------------------
# Sources
# ----------------------------------------------------------------------------

TAJWEED_URL = "https://api.alquran.cloud/v1/surah/{n}/quran-tajweed"

# Quran.com v4 - per_page=300 covers the longest surah (Al-Baqarah, 286 ayahs).
# `language` controls the per-word translation language (not the ayah-level one).
# `translations` is a comma-separated list of full-ayah translation IDs.
QURAN_COM_URL = (
    "https://api.quran.com/api/v4/verses/by_chapter/{n}"
    "?words=true"
    "&language={lang}"
    "&word_fields=text_uthmani,transliteration"
    "&translations={tid}"
    "&per_page=300"
)

# ----------------------------------------------------------------------------
# Translation editions
# ----------------------------------------------------------------------------
#
# Each entry here produces data/translations/{slug}/{N}.json for all surahs.
# Look up translation IDs at:
#   https://api.quran.com/api/v4/resources/translations
#
# A few well-known IDs:
#   20  - Saheeh International (en)
#   131 - Dr. Mustafa Khattab, the Clear Quran (en)
#   85  - Abdul Haleem (en)
#   158 - Maulana Wahiduddin Khan (en)
#   54  - Fateh Muhammad Jalandhry (ur)
#   151 - Indonesian Islamic affairs ministry (id)
#   31  - Muhammad Hamidullah (fr)
#
# Add or remove entries here and rerun. The `language` field is the ISO code
# passed to Quran.com's `language=` parameter for per-word translations; if a
# given language has no word-by-word data, the sidecar's `words` array will
# be empty but the ayah-level `text` will still be present.

TRANSLATIONS = {
    "en-clear-quran": {
        "language": "en",
        "translator": "Dr. Mustafa Khattab, the Clear Quran",
        "translationId": 131,
    },
    # Uncomment / add as desired:
    # "en-saheeh":    {"language": "en", "translator": "Saheeh International",          "translationId": 20},
    # "ur-jalandhry": {"language": "ur", "translator": "Fateh Muhammad Jalandhry",      "translationId": 54},
    # "fr-hamidullah":{"language": "fr", "translator": "Muhammad Hamidullah",           "translationId": 31},
}

# Per-word transliteration is identical regardless of the `language=` param
# (Quran.com always returns Latin script). To avoid re-fetching, we just take
# transliteration from one configured edition's API response.
TRANSLITERATION_SOURCE_KEY = "en-clear-quran"

# ----------------------------------------------------------------------------
# Tajweed rules legend (also written to data/tajweed-rules.json)
# ----------------------------------------------------------------------------

TAJWEED_RULES = {
    "h": {"name": "Hamzat ul Wasl",        "arabic": "همزة الوصل",    "color": "#AAAAAA",
          "description": "The connecting hamza, only pronounced when starting on it."},
    "s": {"name": "Silent",                "arabic": "حرف صامت",      "color": "#AAAAAA",
          "description": "A letter written but not pronounced."},
    "l": {"name": "Laam Shamsiyyah",       "arabic": "لام شمسية",     "color": "#AAAAAA",
          "description": "The silent ل of ال before a sun letter."},
    "n": {"name": "Madd 2 Harakat",        "arabic": "مد طبيعي",      "color": "#537FFF",
          "description": "Natural prolongation of two counts."},
    "p": {"name": "Madd Munfasil 4-5",     "arabic": "مد منفصل",      "color": "#4050FF",
          "description": "Permissible four-to-five count prolongation across word boundaries."},
    "m": {"name": "Madd Lazim 6",          "arabic": "مد لازم",       "color": "#000EBC",
          "description": "Necessary six-count prolongation."},
    "q": {"name": "Qalqalah",              "arabic": "قلقلة",         "color": "#DD0008",
          "description": "Echoing sound on ق ط ب ج د when sukoon."},
    "o": {"name": "Madd Muttasil 4-5",     "arabic": "مد متصل",       "color": "#2144C1",
          "description": "Obligatory four-to-five count prolongation within a word."},
    "c": {"name": "Ikhfa Shafawi",         "arabic": "إخفاء شفوي",    "color": "#D500B7",
          "description": "Partial hiding of م before ب."},
    "f": {"name": "Ikhfa",                 "arabic": "إخفاء",         "color": "#9400A8",
          "description": "Partial hiding of ن ساكنة or tanwīn before 15 letters."},
    "w": {"name": "Idgham Shafawi",        "arabic": "إدغام شفوي",    "color": "#58B800",
          "description": "Merging of م with م."},
    "i": {"name": "Iqlab",                 "arabic": "إقلاب",         "color": "#26BFFD",
          "description": "Converting ن ساكنة / tanwīn into م before ب."},
    "a": {"name": "Idgham With Ghunnah",   "arabic": "إدغام بغنة",    "color": "#169200",
          "description": "Merging into ي ن م و with nasalization."},
    "u": {"name": "Idgham Without Ghunnah","arabic": "إدغام بلا غنة", "color": "#169200",
          "description": "Merging into ل ر without nasalization."},
    "d": {"name": "Idgham Mutajanisain",   "arabic": "إدغام متجانسين","color": "#A1A1A1",
          "description": "Merging of letters from the same articulation point."},
    "b": {"name": "Idgham Mutaqaribain",   "arabic": "إدغام متقاربين","color": "#A1A1A1",
          "description": "Merging of letters from nearby articulation points."},
    "g": {"name": "Ghunnah",               "arabic": "غنة",           "color": "#FF7E1E",
          "description": "Two-count nasalization on shaddah of ن or م."},
}

# ----------------------------------------------------------------------------
# Parsing
# ----------------------------------------------------------------------------

# Matches the source's inline markup:
#   [rule[arabic]            e.g.  [g[نّ]
#   [rule:id[arabic]         e.g.  [h:14659[ٱ]
TAJWEED_RE = re.compile(r"\[([a-z]+)(?::(\d+))?\[([^\]]+)\]")

# Quran.com translation text sometimes contains <sup foot_note=...>1</sup>
# footnote markers and other HTML. Strip them so the JSON contains plain text.
SUP_TAG_RE = re.compile(r"<sup\b[^>]*?>.*?</sup>", re.DOTALL | re.IGNORECASE)
HTML_TAG_RE = re.compile(r"<[^>]+>")


def parse_tajweed(marked: str) -> tuple[str, list[dict]]:
    """Parse a tajweed-marked string into (plain_text, segments)."""
    segments: list[dict] = []
    plain_parts: list[str] = []
    pos = 0
    for m in TAJWEED_RE.finditer(marked):
        if m.start() > pos:
            chunk = marked[pos:m.start()]
            segments.append({"text": chunk})
            plain_parts.append(chunk)
        rule, rid, text = m.group(1), m.group(2), m.group(3)
        seg: dict = {"text": text, "rule": rule}
        if rid is not None:
            seg["id"] = int(rid)
        segments.append(seg)
        plain_parts.append(text)
        pos = m.end()
    if pos < len(marked):
        rest = marked[pos:]
        segments.append({"text": rest})
        plain_parts.append(rest)
    return "".join(plain_parts), segments


def split_marked_words(marked: str) -> list[str]:
    """Split a tajweed-marked ayah string into per-word marked substrings.

    The source uses an unbalanced bracket format (`[rule[text]` has two opens
    and one close), so we tokenize into (marked | literal) chunks first,
    then split literals on whitespace while keeping marked chunks attached
    to whichever word they fall within.
    """
    tokens: list[tuple[str, str]] = []
    pos = 0
    for m in TAJWEED_RE.finditer(marked):
        if m.start() > pos:
            tokens.append(("lit", marked[pos:m.start()]))
        tokens.append(("mark", m.group(0)))
        pos = m.end()
    if pos < len(marked):
        tokens.append(("lit", marked[pos:]))

    words: list[str] = []
    buf: list[str] = []
    for kind, text in tokens:
        if kind == "mark":
            buf.append(text)
            continue
        for part in re.split(r"(\s+)", text):
            if not part:
                continue
            if part.isspace():
                if buf:
                    words.append("".join(buf))
                    buf = []
            else:
                buf.append(part)
    if buf:
        words.append("".join(buf))
    return words


def clean_text(text):
    """Strip HTML/footnotes from translation strings."""
    if not text:
        return text
    text = SUP_TAG_RE.sub("", text)
    text = HTML_TAG_RE.sub("", text)
    return text.strip() or None


# ----------------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------------

def http_get(url: str, retries: int = 4, backoff: float = 2.0) -> dict:
    last_err = None
    for i in range(retries):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "tajweed-builder/1.0"}
            )
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError,
                TimeoutError, json.JSONDecodeError) as e:
            last_err = e
            if i < retries - 1:
                time.sleep(backoff ** i)
    raise RuntimeError(f"GET {url} failed after {retries} attempts: {last_err}")


# ----------------------------------------------------------------------------
# Source fetchers
# ----------------------------------------------------------------------------

def fetch_quran_com(chapter_no: int, lang: str, translation_id: int) -> dict:
    """Returns {ayah_num: {transliterations, word_translations, ayah_translation}}."""
    url = QURAN_COM_URL.format(n=chapter_no, lang=lang, tid=translation_id)
    data = http_get(url)
    out: dict[int, dict] = {}
    for verse in data.get("verses", []):
        try:
            _, ayah_str = verse["verse_key"].split(":")
            ayah_num = int(ayah_str)
        except (KeyError, ValueError):
            continue
        translit_list: list = []
        wtrans_list: list = []
        for w in verse.get("words", []):
            # Skip the ayah-end glyph that Quran.com appends.
            if w.get("char_type_name") == "end":
                continue
            translit_list.append((w.get("transliteration") or {}).get("text"))
            wtrans_list.append(clean_text((w.get("translation") or {}).get("text")))
        ayah_trans_list = verse.get("translations") or []
        ayah_trans = clean_text(
            ayah_trans_list[0].get("text") if ayah_trans_list else None
        )
        out[ayah_num] = {
            "transliterations": translit_list,
            "word_translations": wtrans_list,
            "ayah_translation": ayah_trans,
        }
    return out


# ----------------------------------------------------------------------------
# Build
# ----------------------------------------------------------------------------

def build_surah(chapter_no: int) -> tuple[dict, dict[str, dict]]:
    """Returns (core_surah_json, {translation_slug: translation_json})."""
    tajweed = http_get(TAJWEED_URL.format(n=chapter_no))
    if tajweed.get("code") != 200:
        raise RuntimeError(
            f"alquran.cloud non-200 for surah {chapter_no}: {tajweed.get('status')}"
        )
    sdata = tajweed["data"]

    # Fetch each configured translation edition.
    qc_per_slug: dict[str, dict] = {}
    for slug, cfg in TRANSLATIONS.items():
        try:
            qc_per_slug[slug] = fetch_quran_com(
                chapter_no, cfg["language"], cfg["translationId"]
            )
        except Exception as e:
            print(f"WARN: translation {slug} failed for surah {chapter_no}: {e}",
                  file=sys.stderr)
            qc_per_slug[slug] = {}

    # Pick a transliteration source.
    if TRANSLITERATION_SOURCE_KEY in qc_per_slug and qc_per_slug[TRANSLITERATION_SOURCE_KEY]:
        translit_slug = TRANSLITERATION_SOURCE_KEY
    else:
        translit_slug = next(
            (k for k, v in qc_per_slug.items() if v), None
        )

    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # --- Core surah file: Arabic + tajweed + transliteration only ---
    core_ayahs = []
    for ayah in sdata["ayahs"]:
        marked_words = split_marked_words(ayah["text"])
        translits = (
            qc_per_slug[translit_slug]
            .get(ayah["numberInSurah"], {})
            .get("transliterations", [])
            if translit_slug else []
        )
        if translits and len(translits) != len(marked_words):
            print(
                f"WARN: word-count mismatch surah {chapter_no} ayah "
                f"{ayah['numberInSurah']}: tajweed={len(marked_words)} "
                f"transliteration={len(translits)}",
                file=sys.stderr,
            )

        words = []
        for i, wm in enumerate(marked_words):
            wp, wsegs = parse_tajweed(wm)
            entry: dict = {"plain": wp, "segments": wsegs}
            tl = translits[i] if i < len(translits) else None
            if tl:
                entry["transliteration"] = tl
            words.append(entry)

        core_ayahs.append({
            "ayahNumber": ayah["numberInSurah"],
            "globalNumber": ayah["number"],
            "juz": ayah["juz"],
            "manzil": ayah["manzil"],
            "page": ayah["page"],
            "ruku": ayah["ruku"],
            "hizbQuarter": ayah["hizbQuarter"],
            "sajda": ayah["sajda"],
            "words": words,
        })

    core = {
        "metadata": {
            "surahNumber": sdata["number"],
            "name": {
                "arabic": sdata["name"],
                "transliteration": sdata["englishName"],
                "translation": sdata["englishNameTranslation"],
            },
            "revelationType": sdata["revelationType"],
            "numberOfAyahs": sdata["numberOfAyahs"],
            "source": {
                "tajweed": "api.alquran.cloud (quran-tajweed)",
                "transliteration": (
                    f"api.quran.com v4 ({translit_slug})" if translit_slug else None
                ),
                "generatedAt": now_iso,
            },
        },
        "ayahs": core_ayahs,
    }

    # --- Translation sidecar files (one per edition per surah) ---
    translation_files: dict[str, dict] = {}
    for slug, cfg in TRANSLATIONS.items():
        t_ayahs = []
        for ayah in sdata["ayahs"]:
            ad = qc_per_slug.get(slug, {}).get(ayah["numberInSurah"], {})
            t_ayahs.append({
                "ayahNumber": ayah["numberInSurah"],
                "text": ad.get("ayah_translation"),
                "words": ad.get("word_translations", []),
            })
        translation_files[slug] = {
            "key": slug,
            "language": cfg["language"],
            "translator": cfg["translator"],
            "translationId": cfg["translationId"],
            "surahNumber": sdata["number"],
            "numberOfAyahs": sdata["numberOfAyahs"],
            "source": {
                "api": "api.quran.com v4",
                "generatedAt": now_iso,
            },
            "ayahs": t_ayahs,
        }

    return core, translation_files


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=int, default=1)
    parser.add_argument("--end", type=int, default=114)
    parser.add_argument("--out", type=Path, default=Path("data"))
    parser.add_argument("--sleep", type=float, default=0.4,
                        help="Pause between surahs (kindness to upstream APIs).")
    args = parser.parse_args()

    if not (1 <= args.start <= args.end <= 114):
        print("--start and --end must be in 1..114 with start <= end",
              file=sys.stderr)
        return 2

    out_root: Path = args.out
    surah_dir = out_root / "surah"
    trans_root = out_root / "translations"
    surah_dir.mkdir(parents=True, exist_ok=True)
    trans_root.mkdir(parents=True, exist_ok=True)
    for slug in TRANSLATIONS:
        (trans_root / slug).mkdir(parents=True, exist_ok=True)

    (out_root / "tajweed-rules.json").write_text(
        json.dumps(TAJWEED_RULES, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    built_entries = []
    for n in range(args.start, args.end + 1):
        print(f"[{n:3d}/114] building...", flush=True)
        core, t_files = build_surah(n)
        (surah_dir / f"{n}.json").write_text(
            json.dumps(core, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        for slug, tdata in t_files.items():
            (trans_root / slug / f"{n}.json").write_text(
                json.dumps(tdata, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        built_entries.append({
            "number": core["metadata"]["surahNumber"],
            "name": core["metadata"]["name"],
            "revelationType": core["metadata"]["revelationType"],
            "numberOfAyahs": core["metadata"]["numberOfAyahs"],
            "file": f"surah/{n}.json",
        })
        time.sleep(args.sleep)

    # Merge into existing index when only a range was built.
    index_path = out_root / "index.json"
    if index_path.exists() and (args.start > 1 or args.end < 114):
        existing = json.loads(index_path.read_text(encoding="utf-8"))
        by_num = {e["number"]: e for e in existing.get("surahs", [])}
        for e in built_entries:
            by_num[e["number"]] = e
        merged = [by_num[k] for k in sorted(by_num.keys())]
    else:
        merged = built_entries

    index_path.write_text(
        json.dumps({
            "count": len(merged),
            "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "translations": [
                {
                    "key": k,
                    "language": v["language"],
                    "translator": v["translator"],
                    "translationId": v["translationId"],
                    "path": f"translations/{k}/{{N}}.json",
                }
                for k, v in TRANSLATIONS.items()
            ],
            "surahs": merged,
        }, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print(
        f"Done. Wrote {len(built_entries)} surah(s) and "
        f"{len(TRANSLATIONS)} translation edition(s)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
