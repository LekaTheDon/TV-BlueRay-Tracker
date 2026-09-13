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
OUTPUT_DIR = "output"

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
    r"season\s+\w+|the complete series|complete season|:\s*season|series\b",
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
    t = re.sub(
        r":\s*(the complete series|complete series|season\s+\w+|the complete season\s+\w+).*$",
        "",
        t,
        flags=re.IGNORECASE,
    )
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


def load_db():
    if os.path.exists(DB_PATH):
        with open(DB_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_db(db):
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with open(DB_PATH, "w", encoding="utf-8") as f:
        json.dump(db, f, indent=2, ensure_ascii=False)


def main():
    db = load_db()  # keyed by tmdb_id (string)
    entries = parse_items(fetch_rss())

    new_count = 0
    for entry in entries:
        if not looks_like_tv(entry["title"], entry["description"]):
            continue
        name = clean_title(entry["title"])
        if not name:
            continue

        try:
            result = search_tv(name)
        except Exception as e:
            print(f"TMDB search failed for '{name}': {e}", file=sys.stderr)
            continue
        if not result:
            continue

        tmdb_id = result["id"]
        key = str(tmdb_id)
        if key in db:
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
            "source_title": entry["title"],
        }
        new_count += 1
        time.sleep(0.3)  # be polite to TMDB's API

    save_db(db)
    print(f"Added {new_count} new TV entries this run. Total tracked: {len(db)}")

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
