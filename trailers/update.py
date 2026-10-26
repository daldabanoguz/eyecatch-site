#!/usr/bin/env python3
"""Builds trailers.json: AniList ID -> an official, English-friendly YouTube trailer.

Lists the uploads of official channels (Crunchyroll, Netflix Anime, Muse Asia, Ani-One...) with the
YouTube Data API (1 quota unit per 50 videos; no searches, which cost 100 each), matches video titles
against AniList's anime titles, and writes the best trailer per show.

Runs daily on GitHub Actions (.github/workflows/trailers.yml); also by hand:
    YOUTUBE_API_KEY=... python3 trailers/update.py            # incremental: only new uploads
    YOUTUBE_API_KEY=... python3 trailers/update.py --full     # rescan every channel's history
    python3 trailers/update.py --self-test                    # matching tests, no network
    python3 trailers/update.py --bench                        # timing on the real AniList titles, no key

Rules:
- Only adds. A show keeps its trailer unless a clearly better one appears (e.g. an official trailer
  where there was only a teaser).
- overrides.json is never written by this script and always wins (pin / block).
- Only public, embeddable videos are used; about once a week the chosen ones are re-checked and dead
  ones dropped (pinned ones are only reported).
- Every run records its numbers in stats.json (last 90 runs) and report.md.

Python 3.9+, standard library only.
"""

import gzip
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
SITE = os.path.dirname(HERE)
OUTPUT = os.path.join(SITE, "trailers.json")
CHANNELS = os.path.join(HERE, "channels.json")
OVERRIDES = os.path.join(HERE, "overrides.json")
STATE = os.path.join(HERE, "state.json")
STATS = os.path.join(HERE, "stats.json")
REPORT = os.path.join(HERE, "report.md")

YOUTUBE = "https://www.googleapis.com/youtube/v3/"
ANILIST = "https://graphql.anilist.co"
DAILY_QUOTA = 10_000          # YouTube's free daily allowance
QUOTA_BUDGET = 3_000          # this script stops well before that
FULL_PAGES_PER_CHANNEL = 400  # 20,000 videos per channel on a full scan
NEW_PAGES_PER_CHANNEL = 10    # incremental runs: up to 500 new videos per channel
ANILIST_POPULAR_PAGES = 40    # top 2,000 anime by popularity, plus this and next season
VERIFY_EVERY_DAYS = 7
KEEP_RUNS = 90

TRAILER_WORDS = {"trailer": 4, "pv": 3, "teaser": 2, "promo": 2, "preview": 1}
SKIP_WORDS = {"episode", "ep", "clip", "clips", "opening", "ending", "op", "ed", "recap", "reaction",
              "shorts", "full", "song", "ost", "mv", "interview", "behind", "scene", "scenes",
              "highlight", "highlights", "compilation", "edit", "amv", "live", "concert", "event", "panel"}
GENERIC = {"the", "animation", "anime", "movie", "film", "tv", "series", "ova", "ona", "special", "specials",
           "season", "part", "new", "a", "an", "of", "no", "to", "and", "official", "trailer", "x", "2", "3"}
ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "final": 99}


# ---------------------------------------------------------------- small helpers

def now():
    return datetime.now(timezone.utc)


def load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def save(path, value, compact=False):
    with open(path, "w", encoding="utf-8") as f:
        if compact:
            json.dump(value, f, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        else:
            json.dump(value, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")


def normalize(text):
    """Lowercase ASCII-ish words: 'Frieren: Beyond Journey's End' -> 'frieren beyond journeys end'."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    text = text.replace("'", "").replace("’", "")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def season_number(normalized_title):
    """(season, part): (2, 2) for 'Season 2 Part 2', (2, None) for '2nd Season', (None, 2) for 'Part 2',
    (99, 3) for 'Final Season Part 3'. 'Cour 2' counts as a part."""
    t = normalized_title
    season = None
    for pattern in (r"\bseason (\d+)\b", r"\b(\d+)(?:st|nd|rd|th) season\b", r"\bs(\d+)\b"):
        m = re.search(pattern, t)
        if m:
            season = int(m.group(1))
            break
    if season is None:
        for word, number in ORDINALS.items():
            if re.search(rf"\b{word} season\b", t):
                season = number
                break
    part = None
    m = re.search(r"\b(?:part|cour) (\d+)\b", t) or re.search(r"\b(\d+)(?:st|nd|rd|th) (?:part|cour)\b", t)
    if m:
        part = int(m.group(1))
    return (season, part)


def strip_season(normalized_title):
    t = normalized_title
    for pattern in (r"\bseason \d+\b", r"\b\d+(?:st|nd|rd|th) season\b", r"\bs\d+\b", r"\bpart \d+\b",
                    r"\bcour \d+\b", r"\b(?:first|second|third|fourth|fifth|sixth|final) season\b"):
        t = re.sub(pattern, " ", t)
    return re.sub(r"\s+", " ", t).strip()


class Counter:
    """Every network call is counted: the numbers end up in stats.json."""

    def __init__(self):
        self.quota = 0
        self.youtube_calls = {}
        self.anilist_requests = 0
        self.anilist_seconds = 0.0
        self.anilist_retries = 0


COUNT = Counter()


def http_json(url, data=None, headers=None, attempts=4):
    for attempt in range(attempts):
        request = urllib.request.Request(url, data=data, headers=headers or {})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if error.code == 429 or error.code >= 500:
                wait = int(error.headers.get("Retry-After") or 0) or 10 * (attempt + 1)
                COUNT.anilist_retries += 1 if "anilist" in url else 0
                time.sleep(min(wait, 65))
                continue
            body = error.read().decode("utf-8", "replace")[:300]
            raise RuntimeError(f"HTTP {error.code} for {url.split('?')[0]}: {body}") from None
    raise RuntimeError(f"Gave up after {attempts} attempts: {url.split('?')[0]}")


# ---------------------------------------------------------------- YouTube

def youtube(endpoint, key, cost=1, **params):
    if COUNT.quota + cost > QUOTA_BUDGET:
        raise RuntimeError(f"Quota budget of {QUOTA_BUDGET} units reached")
    params["key"] = key
    COUNT.quota += cost
    COUNT.youtube_calls[endpoint] = COUNT.youtube_calls.get(endpoint, 0) + 1
    return http_json(YOUTUBE + endpoint + "?" + urllib.parse.urlencode(params))


def resolve_channel(handle, key):
    data = youtube("channels", key, part="id,contentDetails,snippet", forHandle=handle)
    items = data.get("items") or []
    if not items:
        raise RuntimeError(f"No channel for handle {handle}")
    item = items[0]
    return {"id": item["id"], "title": item["snippet"]["title"],
            "uploads": item["contentDetails"]["relatedPlaylists"]["uploads"]}


def uploads(playlist, key, stop_at, max_pages):
    """Newest first, until the last video seen last time (or max_pages)."""
    videos, token = [], None
    for _ in range(max_pages):
        params = {"part": "snippet,contentDetails", "playlistId": playlist, "maxResults": 50}
        if token:
            params["pageToken"] = token
        data = youtube("playlistItems", key, **params)
        for item in data.get("items", []):
            vid = item["contentDetails"]["videoId"]
            if vid == stop_at:
                return videos, True
            videos.append({"id": vid, "title": item["snippet"]["title"],
                           "published": item["contentDetails"].get("videoPublishedAt")
                           or item["snippet"]["publishedAt"]})
        token = data.get("nextPageToken")
        if not token:
            return videos, True
    return videos, False


def video_status(ids, key):
    """id -> True when public and embeddable (1 unit per 50 videos)."""
    result = {}
    for start in range(0, len(ids), 50):
        chunk = ids[start:start + 50]
        data = youtube("videos", key, part="status", id=",".join(chunk), maxResults=50)
        found = {item["id"]: item["status"] for item in data.get("items", [])}
        for vid in chunk:
            status = found.get(vid)
            result[vid] = bool(status and status.get("privacyStatus") == "public" and status.get("embeddable"))
    return result


# ---------------------------------------------------------------- AniList

ANIME_QUERY = """
query ($page: Int, $season: MediaSeason, $year: Int, $sort: [MediaSort]) {
  Page(page: $page, perPage: 50) {
    pageInfo { hasNextPage }
    media(type: ANIME, isAdult: false, season: $season, seasonYear: $year, sort: $sort) {
      id seasonYear format title { romaji english } synonyms
    }
  }
}
"""


def anilist_page(variables):
    started = time.time()
    body = json.dumps({"query": ANIME_QUERY, "variables": variables}).encode()
    data = http_json(ANILIST, data=body, headers={"Content-Type": "application/json", "Accept": "application/json",
                                                  "User-Agent": "Jikai-trailers/1.0"})
    COUNT.anilist_requests += 1
    COUNT.anilist_seconds += time.time() - started
    time.sleep(2.2)  # AniList allows about 30 requests a minute
    page = data["data"]["Page"]
    return page["media"], page["pageInfo"]["hasNextPage"]


def current_seasons(today):
    seasons = ["WINTER", "SPRING", "SUMMER", "FALL"]
    index = (today.month - 1) // 3
    this = (seasons[index], today.year)
    nxt = (seasons[(index + 1) % 4], today.year + (1 if index == 3 else 0))
    return [this, nxt]


def fetch_anime(today):
    shows = {}
    for page in range(1, ANILIST_POPULAR_PAGES + 1):
        media, more = anilist_page({"page": page, "sort": ["POPULARITY_DESC"]})
        for m in media:
            shows[m["id"]] = m
        if not more:
            break
    for season, year in current_seasons(today):
        page = 1
        while True:
            media, more = anilist_page({"page": page, "season": season, "year": year, "sort": ["POPULARITY_DESC"]})
            for m in media:
                shows[m["id"]] = m
            if not more or page >= 6:
                break
            page += 1
    return list(shows.values())


# ---------------------------------------------------------------- matching

def title_variants(show):
    names = [show["title"].get("english"), show["title"].get("romaji")] + (show.get("synonyms") or [])
    variants = set()
    for name in names:
        if not name:
            continue
        n = normalize(name)
        base = strip_season(n)
        words = base.split()
        # "The Animation", "TV Anime"… say nothing about which show it is
        if len(base) >= 4 and not base.isdigit() and not set(words) <= GENERIC:
            variants.add((base, season_number(n)))
    return variants


def build_index(shows):
    """First word -> [(variant words, season, show id)], so each video only checks a few titles."""
    index = {}
    for show in shows:
        for base, season in title_variants(show):
            words = base.split()
            if len(words) == 1 and len(words[0]) < 6:
                continue  # one short word ("Bleach" is fine, "K" or "Ao" is not) matches too much
            index.setdefault(words[0], []).append((words, season, show["id"]))
    return index


def trailer_score(normalized_title):
    words = set(normalized_title.split())
    if words & SKIP_WORDS:
        return 0
    score = max((weight for word, weight in TRAILER_WORDS.items() if word in words), default=0)
    if score and "official" in words:
        score += 1
    if score and ("main" in words or "final" in words):
        score += 1
    return score


def contains(words, phrase):
    n = len(phrase)
    return any(words[i:i + n] == phrase for i in range(len(words) - n + 1))


def match_video(video, index, shows_by_id):
    """[(show id, matched title length)] for a trailer video; longest titles win (sequels over originals)."""
    t = normalize(video["title"])
    if not trailer_score(t):
        return []
    words = t.split()
    season = season_number(t)
    found = {}
    for word in set(words):
        for phrase, show_season, show_id in index.get(word, []):
            if not contains(words, phrase):
                continue
            # Season and part must agree: "Season 2 Part 2" trailers go to that entry only
            if (season[0] or 1, season[1] or 1) != (show_season[0] or 1, show_season[1] or 1):
                continue
            found[show_id] = max(found.get(show_id, 0), len(phrase))
    if not found:
        return []
    best = max(found.values())
    return [(sid, length) for sid, length in found.items() if length == best]


def video_score(video, channel_priority, show):
    t = normalize(video["title"])
    score = trailer_score(t) * 10 + channel_priority
    if "english" in t.split() or "eng" in t.split() or "sub" in t.split():
        score += 2
    year = (video.get("published") or "0000")[:4]
    if show.get("seasonYear") and year.isdigit():
        gap = int(year) - show["seasonYear"]
        if gap < -1 or gap > 1:
            score -= 15  # a trailer from years before or after the show is probably another season
    return score


# ---------------------------------------------------------------- the run

def run(full=False):
    started = time.time()
    key = os.environ.get("YOUTUBE_API_KEY", "").strip()
    if not key:
        print("YOUTUBE_API_KEY is not set: nothing to do.")
        return 0

    channels = load(CHANNELS, {"channels": []})["channels"]
    overrides = load(OVERRIDES, {"pin": {}, "block_videos": [], "block_shows": []})
    state = load(STATE, {"channels": {}, "matches": {}, "verified_at": None})
    output = load(OUTPUT, {"version": 1, "trailers": {}})
    previous = dict(output.get("trailers", {}))
    matches = state.setdefault("matches", {})
    today = now()
    stats = {"started": today.isoformat(timespec="seconds"), "mode": "full" if full else "incremental",
             "channels": {}, "added": 0, "upgraded": 0, "kept": 0, "dropped": 0, "uncertain": 0}
    uncertain = []

    # 1. New uploads from each channel
    fresh = []
    for entry in channels:
        handle, priority = entry["handle"], entry.get("priority", 1)
        info = state["channels"].get(handle)
        if not info or "uploads" not in info:
            try:
                info = resolve_channel(handle, key)
            except RuntimeError as error:
                uncertain.append(f"- Channel {handle} skipped: {error}")
                stats["channels"][handle] = {"error": str(error)}
                continue
            state["channels"][handle] = info
        stop_at = None if full else info.get("last_seen")
        pages = FULL_PAGES_PER_CHANNEL if (full or not stop_at) else NEW_PAGES_PER_CHANNEL
        videos, complete = uploads(info["uploads"], key, stop_at, pages)
        if videos:
            info["last_seen"] = videos[0]["id"]
        stats["channels"][handle] = {"new_videos": len(videos), "complete": complete}
        for v in videos:
            v["channel"] = handle
            v["priority"] = priority
        fresh += videos
    stats["videos_scanned"] = len(fresh)

    # 2. AniList titles, matched against the trailer-looking videos
    shows = fetch_anime(today) if fresh else []
    shows_by_id = {s["id"]: s for s in shows}
    index = build_index(shows)
    blocked_videos = set(overrides.get("block_videos", []))
    blocked_shows = {str(s) for s in overrides.get("block_shows", [])}
    candidates = {}
    for video in fresh:
        if video["id"] in blocked_videos:
            continue
        hits = match_video(video, index, shows_by_id)
        if len(hits) > 1:
            # Same title for several shows (remakes, sequels): the one that aired within a year of the upload
            year = (video.get("published") or "0000")[:4]
            near = [h for h in hits if year.isdigit() and shows_by_id[h[0]].get("seasonYear")
                    and abs(int(year) - shows_by_id[h[0]]["seasonYear"]) <= 1]
            if len(near) == 1:
                hits = near
        if len(hits) > 1:
            uncertain.append(f"- `{video['id']}` “{video['title']}” matches several shows: "
                             + ", ".join(str(h[0]) for h in hits) + " (skipped)")
            continue
        for show_id, _ in hits:
            score = video_score(video, video["priority"], shows_by_id[show_id])
            best = candidates.get(show_id)
            if not best or score > best["score"]:
                candidates[show_id] = {**video, "score": score}
    stats["trailer_candidates"] = len(candidates)
    stats["anime_titles"] = len(shows)

    # 3. Only public, embeddable videos
    playable = video_status([c["id"] for c in candidates.values()], key) if candidates else {}

    for show_id, video in candidates.items():
        sid = str(show_id)
        if sid in blocked_shows or not playable.get(video["id"]):
            continue
        old = matches.get(sid)
        if old and old["v"] == video["id"]:
            continue
        if old and video["score"] < old.get("score", 0) + 10:
            stats["kept"] += 1  # not clearly better: the existing trailer stays
            continue
        if video["score"] < 20:
            uncertain.append(f"- {sid} “{video['title']}” (`{video['id']}`, score {video['score']}) is weak: check it")
        matches[sid] = {"v": video["id"], "s": video["channel"].lstrip("@").lower(), "score": video["score"],
                        "title": video["title"], "published": video["published"]}
        stats["upgraded" if old else "added"] += 1

    # 4. About once a week, drop chosen videos that went private, were deleted or stopped embedding
    last = state.get("verified_at")
    if matches and (not last or today - datetime.fromisoformat(last) > timedelta(days=VERIFY_EVERY_DAYS)):
        alive = video_status([m["v"] for m in matches.values()], key)
        for sid, m in list(matches.items()):
            if not alive.get(m["v"]):
                del matches[sid]
                stats["dropped"] += 1
        state["verified_at"] = today.isoformat(timespec="seconds")
        stats["verified"] = len(alive)

    # 5. Write trailers.json: the matches with overrides applied (pins always win, blocks always out)
    trailers = {sid: {"v": m["v"], "s": m["s"]} for sid, m in matches.items() if sid not in blocked_shows}
    for sid, vid in overrides.get("pin", {}).items():
        trailers[str(sid)] = {"v": vid, "s": "pinned"}
    if trailers != previous:
        output = {"version": 1, "updated": today.isoformat(timespec="seconds"), "trailers": trailers}
        save(OUTPUT, output, compact=True)
    save(STATE, state)

    # 6. The numbers
    raw = os.path.getsize(OUTPUT) if os.path.exists(OUTPUT) else 0
    gz = len(gzip.compress(open(OUTPUT, "rb").read())) if raw else 0
    stats.update({"seconds": round(time.time() - started, 1), "quota_used": COUNT.quota,
                  "quota_left_today_est": DAILY_QUOTA - COUNT.quota, "youtube_calls": COUNT.youtube_calls,
                  "anilist_requests": COUNT.anilist_requests, "anilist_seconds": round(COUNT.anilist_seconds, 1),
                  "anilist_retries": COUNT.anilist_retries, "entries": len(trailers),
                  "pinned": len(overrides.get("pin", {})), "uncertain": len(uncertain),
                  "file_bytes": raw, "file_gzip_bytes": gz, "changed": trailers != previous})
    history = load(STATS, {"runs": []})
    history["runs"] = (history.get("runs", []) + [stats])[-KEEP_RUNS:]
    save(STATS, history)
    write_report(stats, uncertain)
    print(json.dumps(stats, indent=2))
    return 0


def write_report(stats, uncertain):
    lines = [f"# Trailers report", "", f"Last run: {stats['started']} ({stats['mode']}, {stats['seconds']} s)", "",
             "| | |", "|---|---|",
             f"| Trailers in trailers.json | {stats['entries']} (pinned {stats['pinned']}) |",
             f"| Added / upgraded / kept / dropped | {stats['added']} / {stats['upgraded']} / {stats['kept']} / {stats['dropped']} |",
             f"| Videos scanned | {stats.get('videos_scanned', 0)} |",
             f"| Trailer candidates | {stats.get('trailer_candidates', 0)} |",
             f"| AniList titles checked | {stats.get('anime_titles', 0)} ({stats['anilist_requests']} requests, {stats['anilist_seconds']} s) |",
             f"| YouTube quota used | {stats['quota_used']} of {DAILY_QUOTA} per day |",
             f"| trailers.json | {stats['file_bytes'] / 1024:.1f} KB ({stats['file_gzip_bytes'] / 1024:.1f} KB compressed) |",
             "", "## Check these", ""]
    lines += uncertain[:200] or ["Nothing uncertain this run."]
    lines += ["", "Pin or block in `trailers/overrides.json`; this script never edits it."]
    with open(REPORT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


# ---------------------------------------------------------------- benchmark (AniList only, no YouTube key)

def bench():
    """Times the AniList fetch and the matching on the real title list with synthetic video titles."""
    started = time.time()
    shows = fetch_anime(now())
    fetched = time.time() - started
    index = build_index(shows)
    by_id = {s["id"]: s for s in shows}
    variants = sum(len(v) for v in index.values())
    names = [s["title"].get("english") or s["title"]["romaji"] for s in shows]
    formats = ["{} | OFFICIAL TRAILER", "{} Episode 4 | Clip", "{} - Opening", "{} Season 2 | Teaser Trailer",
               "Reaction to {}", "{} | Main Trailer [English Sub]"]
    videos = [{"title": f.format(n)} for n in names for f in formats]
    videos = (videos * (30_000 // max(len(videos), 1) + 1))[:30_000]
    t = time.time()
    matched = sum(1 for v in videos if match_video(v, index, by_id))
    match_seconds = time.time() - t
    sample = {"1": {"v": "tR8YH0G67Rk", "s": "crunchyroll"}}
    per_entry = len(json.dumps(sample, separators=(",", ":"))) - 2
    result = {"anime_titles": len(shows), "title_variants": variants, "anilist_requests": COUNT.anilist_requests,
              "anilist_seconds": round(fetched, 1), "videos_matched_against": len(videos),
              "videos_that_matched": matched, "match_seconds": round(match_seconds, 2),
              "videos_per_second": int(len(videos) / max(match_seconds, 0.001)),
              "bytes_per_entry": per_entry, "est_file_kb_5000_entries": round(per_entry * 5000 / 1024, 1)}
    print(json.dumps(result, indent=2))
    return 0


# ---------------------------------------------------------------- self-test (no network)

def self_test():
    shows = [
        {"id": 1, "seasonYear": 2023, "title": {"romaji": "Sousou no Frieren", "english": "Frieren: Beyond Journey's End"}, "synonyms": []},
        {"id": 2, "seasonYear": 2026, "title": {"romaji": "Sousou no Frieren 2nd Season", "english": "Frieren: Beyond Journey's End Season 2"}, "synonyms": []},
        {"id": 3, "seasonYear": 2026, "title": {"romaji": "Tempal: Item no Chikara", "english": "Overgeared"}, "synonyms": []},
        {"id": 4, "seasonYear": 2024, "title": {"romaji": "Ao no Hako", "english": "Blue Box"}, "synonyms": []},
        {"id": 5, "seasonYear": 2023, "title": {"romaji": "Mushoku Tensei II", "english": "Mushoku Tensei Season 2"}, "synonyms": []},
        {"id": 6, "seasonYear": 2024, "title": {"romaji": "Mushoku Tensei II Part 2", "english": "Mushoku Tensei Season 2 Part 2"}, "synonyms": []},
        {"id": 7, "seasonYear": 2015, "title": {"romaji": "Some Show", "english": None}, "synonyms": ["The Animation"]},
    ]
    by_id = {s["id"]: s for s in shows}
    index = build_index(shows)

    def hit(title):
        return [h[0] for h in match_video({"title": title}, index, by_id)]

    checks = [
        (hit("Frieren: Beyond Journey's End | OFFICIAL TRAILER"), [1]),
        (hit("Frieren: Beyond Journey's End Season 2 | Official Trailer"), [2]),
        (hit("Frieren Beyond Journey's End 2nd Season - Main Trailer"), [2]),
        (hit("Overgeared | OFFICIAL TEASER TRAILER"), [3]),
        (hit("Overgeared Episode 3 | Clip"), []),
        (hit("Blue Box Opening | Same Blue"), []),
        (hit("Blue Box | Official Trailer"), [4]),
        (trailer_score(normalize("Frieren | Official Trailer")) > trailer_score(normalize("Frieren | Teaser")), True),
        (season_number(normalize("2nd Season")), (2, None)),
        (season_number(normalize("Season 2 Part 2")), (2, 2)),
        (season_number(normalize("Final Season Part 3")), (99, 3)),
        (season_number(normalize("Season 3")), (3, None)),
        (hit("Mushoku Tensei Season 2 Part 2 | OFFICIAL TRAILER"), [6]),
        (hit("Mushoku Tensei Season 2 | OFFICIAL TRAILER"), [5]),
        (hit("Smile of the Arsnotoria the Animation | OFFICIAL TRAILER"), []),
    ]
    failed = [(got, want) for got, want in checks
              if (sorted(got) != sorted(want) if isinstance(got, list) else got != want)]
    for got, want in failed:
        print("FAIL: got", got, "want", want)
    print(f"{len(checks) - len(failed)}/{len(checks)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(self_test())
    if "--bench" in sys.argv:
        sys.exit(bench())
    sys.exit(run(full="--full" in sys.argv))
