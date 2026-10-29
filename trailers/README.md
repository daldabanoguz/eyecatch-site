# Trailers

`trailers.json` (served at https://daldabanoguz.github.io/jikai-site/trailers.json) maps AniList IDs to an
official, English-friendly YouTube trailer. Jikai's Swipe plays that trailer if there is one, otherwise
AniList's own trailer, otherwise just the cover.

## How it's made

`update.py` runs every day on GitHub Actions (`.github/workflows/trailers.yml`):

1. Lists the newest uploads of the channels in `channels.json` (YouTube Data API, 1 quota unit per 50 videos; it never uses search, which costs 100).
2. Loads AniList's titles: this and next season every day, plus the 5,000 most popular once a week (and on full scans), and matches their titles (English, romaji, synonyms, season numbers) against trailer-looking video titles (Trailer / PV / Teaser, never Episode / Clip / Opening…).
3. Keeps only public, embeddable videos; about once a week re-checks the chosen ones and drops dead links.
4. Only adds: a show keeps its trailer unless a clearly better one appears.
5. Writes `trailers.json`, `state.json` (where each channel was left), `stats.json` (the last 90 runs' numbers) and `report.md` (summary + matches to check).

## Fixing a match

Edit `overrides.json` (on GitHub: open it, ✏️, save). The script reads it every run, never writes it, and it always wins:

```json
{
  "pin": { "154587": "tR8YH0G67Rk" },
  "block_videos": ["VIDEO_ID"],
  "block_shows": [12345]
}
```

`pin` uses that video for that show; `block_videos` never uses a video; `block_shows` makes a show fall back to AniList's trailer; `no_trailer` (a list of AniList IDs) gives a show no trailer at all, not even AniList's (the app shows the cover).

## Region limits and games

Some uploads only play in a few countries (Crunchyroll Dubs: US, UK, Canada, Australia, Ireland, New Zealand). Those lose to videos that play everywhere, and replace nothing that does; when one is kept anyway, its countries go into `trailers.json` (`"a"`: only these, `"b"`: all but these) and the app skips it in other countries, falling back to AniList's trailer. Game trailers (game, gameplay, launch, mobile, consoles…) are never matched.

A channel in `channels.json` with `"latin_only": true` only counts titles without Japanese or Chinese script (TOHO animation posts both). Run the workflow by hand (Actions → Trailers → Run workflow) to apply it straight away.

## Running it locally

```sh
YOUTUBE_API_KEY=... python3 trailers/update.py          # incremental
YOUTUBE_API_KEY=... python3 trailers/update.py --full   # whole history of every channel
python3 trailers/update.py --self-test                  # matching tests, no network
python3 trailers/update.py --bench                      # timing on the real AniList titles
```

## Budgets

| | Per day | Limit |
|---|---|---|
| YouTube quota | ~20–100 units (full scan ≈ 2,500, capped at 3,000) | 10,000 free |
| AniList | daily ~12 requests (~30 s); weekly ~112 (~5 min); paced at ~27/min | ~30/min |
| GitHub Actions | ~1 min daily, ~6 min weekly | unlimited for public repos |
| trailers.json | ~41 bytes per show (~200 KB for 5,000) | Pages 1 GB, files 100 MB |

`stats.json` records every run: quota used, calls, videos scanned, matches added / upgraded / kept / dropped, file size (raw and compressed) and run time.

The API key lives only in the repository's Actions secret `YOUTUBE_API_KEY` (and, for local runs, an ignored file). It's never committed.
