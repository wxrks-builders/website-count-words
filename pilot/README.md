# Scrapling pilot

A disconnected comparison of [Scrapling](https://github.com/D4Vinci/Scrapling)
against crawl4ai, the app's production crawler. Nothing in `app/` imports
anything here; delete the whole directory and the product is untouched.

## Why these two venvs

- **Side A runs in the app's own venv** (`../.venv`) — parity by definition:
  same crawl4ai build (0.9.2, which is not on PyPI), same browser, same
  markdown pipeline, config lifted from `app/crawler.py`.
- **Side B runs in `pilot/.venv`** with `scrapling[fetchers]` and Camoufox.

Both import the app's frontier rules and word counter read-only, so this is a
comparison of fetchers — not of URL policies or extractors.

## The B′ trick

Every A-side page also records the word count of the **shared** lxml extractor
run on A's HTML (`words_shared`). That is B′: `B − B′` is what the fetchers saw
differently; `B′ − A` is how the extractors count differently. Without it, a
word delta is unattributable and the pilot decides nothing.

## Run

    pilot/run_all.sh            # all four sites, A then B each, sequential
    pilot/.venv/bin/python pilot/compare.py   # -> pilot/report.md

`PILOT_CAP=n` caps pages per site for smoke tests.

## Verdict criteria — fixed before the first run

Scrapling earns a deeper spike only if it reaches ≥10% more of
community.clay.com (crawl4ai's documented discovery hole) OR at most half the
blocked pages on clay.com, while staying within ±5% fetch-attributable word
delta on the sites crawl4ai already handles well. See `compare.py`.
