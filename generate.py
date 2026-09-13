import difflib
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

TMDB_API_KEY = os.environ["TMDB_API_KEY"]
TMDB_BASE = "https://api.themoviedb.org/3"
RSS_URL = "https://www.blu-ray.com/rss/newreleasesfeed.xml"
DB_PATH = "data/database.json"
OVERRIDES_PATH = "data/overrides.json"
NEEDS_REVIEW_PATH = "data/needs_review.json"
OUTPUT_DIR = "output"

# Below this similarity between our cleaned search title and TMDB's returned
# title, we don't trust the match - better to flag it for review than to
# confidently link the wrong show.
MATCH_CONFIDENCE_THRESHOLD = 0.6

# TMDB TV genre ids (there is no dedicated "Horror" TV genre in TMDB's
# vocabulary, so Horror is detected separately via keyword tagging below).
GENRE_MAP = {
    "Drama": 18,
    "Crime": 80,
    "Sci-Fi": 10765,   # TMDB combines this as "Sci-Fi & Fantasy" for TV
    "Mystery": 9648,
    "Comedy": 35,
    "Action": 10759,   # TMDB combines this as "Action & Adventure" for TV
}

TV_HINT_PATTERN = re.compile(
    r"season\s+\w+|complete\s+(original\s+)?series|complete\s+collection|complete season|series\b",
    re.IGNORECASE,
)

# Matches a trailing "season"/"complete series"/"complete collection" chunk
# regardless of what punctuation (colon, hyphen, or nothing) precedes it, and
# regardless of extra words in between (e.g. "Complete Original Series").
TITLE_TRIM_PATTERN = re.compile(
    r"\s*[:\-]?\s*(the\s+)?complete(\s+\w+)?\s+(series|collection)\b.*$"
    r"|\s*[:\-]?\s*season\s+\w+.*$",
    re.IGNORECASE,
)

# Common disc-marketing suffixes that don't help (and often hurt) a TMDB
# title search.
TRAILING_CRUFT_PATTERN = re.compile(
    r"\s*\d+(st|nd|rd|th)\s+anniversary.*$|\s*anniversary\s+edition.*$|\s*limited\s+edition.*$",
    re.IGNORECASE,
)


def fetch_rss():
    req = urllib.request.Request(RSS_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def parse_items(xml_bytes):
    root = ET.fromstring(xml_bytes)
    items = []
    for item in root.iter("item"):
        items.append(
            {
                "title": item.findtext("title") or "",
                "link": item.findtext("link") or "",
                "description": item.findtext("description") or "",
                "pubDate": item.findtext("pubDate") or "",
            }
        )
    return items


def looks_like_tv(title, description):
    text = f"{title} {description}"
    return bool(TV_HINT_PATTERN.search(text))


def clean_title(title):
    t = title
    t = re.sub(r"\s*4K\s*(Ultra HD)?", "", t, flags=re.IGNORECASE)
    t = TITLE_TRIM_PATTERN.sub("", t)
    t = TRAILING_CRUFT_PATTERN.sub("", t)
    t = re.sub(r"\s*\(.*?\)\s*$", "", t)
    return t.strip(" :-")


def tmdb_get(path, params):
    qs = urllib.parse.urlencode(params)
    url = f"{TMDB_BASE}{path}?api_key={TMDB_API_KEY}&{qs}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read())


def search_tv(title):
    data = tmdb_get("/search/tv", {"query": title})
    results = data.get("results") or []
    return results[0] if results else None


def get_external_ids(tmdb_id):
    return tmdb_get(f"/tv/{tmdb_id}/external_ids", {})


def has_horror_keyword(tmdb_id):
    data = tmdb_get(f"/tv/{tmdb_id}/keywords", {})
    keywords = data.get("results") or []
    return any("horror" in (k.get("name") or "").lower() for k in keywords)


def normalize_for_compare(s):
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return s.strip()


def title_similarity(a, b):
    return difflib.SequenceMatcher(None, normalize_for_compare(a), normalize_for_compare(b)).ratio()


def load_json(path, default):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path, data):
    dirname = os.path.dirname(path)
    if dirname:
        os.makedirs(dirname, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def load_db():
    return load_json(DB_PATH, {})


def save_db(db):
    save_json(DB_PATH, db)


def main():
    db = load_db()  # keyed by tmdb_id (string)
    # Manual overrides: source_title -> explicit tmdb_id, for stubborn cases
    # that don't auto-match cleanly. Edit data/overrides.json by hand to add one.
    overrides = load_json(OVERRIDES_PATH, {})
    # Anything that failed to match (or matched with low confidence) gets
    # logged here instead of silently dropped, keyed by source_title so a
    # re-run doesn't pile up duplicate entries for the same release.
    needs_review = load_json(NEEDS_REVIEW_PATH, {})

    entries = parse_items(fetch_rss())

    new_count = 0
    for entry in entries:
        if not looks_like_tv(entry["title"], entry["description"]):
            continue
        source_title = entry["title"]
        name = clean_title(source_title)
        print(f"[TV candidate] '{source_title}' -> search query: '{name}'")
        if not name:
            print("  skipped: cleaned title was empty")
            continue

        override_id = overrides.get(source_title)
        result = None
        confidence = None

        if override_id:
            print(f"  using manual override: tmdb id {override_id}")
            try:
                result = tmdb_get(f"/tv/{override_id}", {})
                result["id"] = override_id
                # /tv/{id} returns full genre objects ({"id":18,"name":"Drama"}),
                # not the flat "genre_ids" list /search/tv returns - normalize
                # so downstream genre-list matching works the same either way.
                result["genre_ids"] = [g["id"] for g in result.get("genres") or []]
            except Exception as e:
                print(f"  override lookup failed: {e}", file=sys.stderr)
                continue
        else:
            try:
                result = search_tv(name)
            except Exception as e:
                print(f"  TMDB search failed: {e}", file=sys.stderr)
                continue
            if not result:
                print("  no TMDB match found - logged for review")
                needs_review[source_title] = {
                    "search_query": name,
                    "reason": "no_match",
                    "checked_date": time.strftime("%Y-%m-%d"),
                }
                continue

            confidence = title_similarity(name, result.get("name") or "")
            if confidence < MATCH_CONFIDENCE_THRESHOLD:
                print(
                    f"  low-confidence match ({confidence:.2f}): "
                    f"'{result.get('name')}' (id={result['id']}) - logged for review, not added"
                )
                needs_review[source_title] = {
                    "search_query": name,
                    "reason": "low_confidence",
                    "candidate_title": result.get("name"),
                    "candidate_tmdb_id": result.get("id"),
                    "confidence": round(confidence, 2),
                    "checked_date": time.strftime("%Y-%m-%d"),
                }
                continue

        print(
            f"  matched TMDB: '{result.get('name')}' (id={result['id']})"
            + (f" confidence={confidence:.2f}" if confidence is not None else " (override)")
        )

        # A successful match means this release is resolved - clear any
        # stale review entry left over from a previous run.
        needs_review.pop(source_title, None)

        tmdb_id = result["id"]
        key = str(tmdb_id)
        if key in db:
            print("  already tracked, skipping")
            continue  # already tracked from a previous run

        try:
            ext = get_external_ids(tmdb_id)
        except Exception:
            ext = {}
        try:
            is_horror = has_horror_keyword(tmdb_id)
        except Exception:
            is_horror = False

        release_year = None
        if result.get("first_air_date"):
            release_year = result["first_air_date"][:4]

        db[key] = {
            "id": tmdb_id,
            "title": result.get("name") or name,
            "imdb_id": ext.get("imdb_id"),
            "mediatype": "show",
            "release_year": release_year,
            "vote_average": result.get("vote_average"),
            "genre_ids": result.get("genre_ids") or [],
            "is_horror": is_horror,
            "detected_date": time.strftime("%Y-%m-%d"),
            "source_title": source_title,
        }
        new_count += 1
        time.sleep(0.3)  # be polite to TMDB's API

    save_db(db)
    save_json(NEEDS_REVIEW_PATH, needs_review)
    print(f"Added {new_count} new TV entries this run. Total tracked: {len(db)}")
    print(f"Items awaiting manual review: {len(needs_review)}")

    generate_outputs(db)


def slim(item, rank):
    return {
        "id": item["id"],
        "rank": rank,
        "title": item["title"],
        "imdb_id": item.get("imdb_id"),
        "mediatype": "show",
        "release_year": item.get("release_year"),
    }


def write_json(path, items_in_order):
    ranked = [slim(item, i + 1) for i, item in enumerate(items_in_order)]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(ranked, f, indent=2, ensure_ascii=False)


def generate_outputs(db):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    items = list(db.values())

    write_json(f"{OUTPUT_DIR}/all.json", items)

    latest_sorted = sorted(items, key=lambda i: i.get("detected_date", ""), reverse=True)
    write_json(f"{OUTPUT_DIR}/latest.json", latest_sorted)

    rated_sorted = sorted(items, key=lambda i: (i.get("vote_average") or 0), reverse=True)
    write_json(f"{OUTPUT_DIR}/highest-rated.json", rated_sorted)

    for name, gid in GENRE_MAP.items():
        matches = [i for i in items if gid in (i.get("genre_ids") or [])]
        write_json(f"{OUTPUT_DIR}/{name.lower().replace(' ', '-')}.json", matches)

    horror_matches = [i for i in items if i.get("is_horror")]
    write_json(f"{OUTPUT_DIR}/horror.json", horror_matches)


if __name__ == "__main__":
    main()
