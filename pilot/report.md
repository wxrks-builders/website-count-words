# Scrapling vs crawl4ai — pilot report

Run 2026-08-28 on macOS-26.3.1-arm64-arm-64bit, page cap 120/site, concurrency 8, sequential runs (A then B per site).
Frontier rules and blocked-detection shared between both sides; B′ = the shared extractor applied to A's HTML, which is what lets a word delta be attributed to the fetcher rather than the extractor.

## Verdict (criteria fixed before the run)

- ✅ ≥10% more pages on community.clay.com — +1150%
- ✅ blocked rate on clay.com ≤ half of crawl4ai's — 0 vs 0
- ✅ fetch-attributable deltas within ±5% on well-handled sites 

**Scrapling earns a deeper integration spike.**

## Caveats the table can't carry

- **Calibration held**: side A counted 1,310 words/page on wxrks.com vs the
  production run's 1,307 — 0.2%. The A side is production behaviour, measured.
- **community.clay.com's 95 'blocked' are all HTTP 403**, arriving after 25
  pages of real content: the site rate-limited eight concurrent Camoufox
  instances. Scrapling's *discovery* advantage there is real (it found the
  SPA's links where crawl4ai found 2 pages); its default footprint at this
  concurrency is what got it throttled. A spike should retry at concurrency
  2-3 with its adaptive rate limiter on.
- **This IP has crawled these sites hard for weeks.** clay.com showed 0
  blocked for BOTH sides today (vs 301 historically), so the anti-bot gate
  passed on thin evidence — neither side was actually challenged there.
- **marriott.com supplied the real anti-bot test, added after the first
  run, and both tools failed it — differently.** crawl4ai reported an
  honest 'Akamai block (Reference #)'. Scrapling (Camoufox, including with
  network_idle and a 60s timeout) received an HTTP 200 challenge shell:
  3KB, 5 visible words, one link. That failure mode is WORSE for this
  product than a block — a 'successful' 5-word homepage would enter a
  word-count report as real data. The shared detector now treats a 200
  under 10KB with under 20 words as a soft block; the production crawler
  has no such guard and would benefit from one regardless of which
  fetcher it uses.
- **The costs are not small**: Scrapling ran 4-6x slower per page and at
  3-5x the memory. Eight Camoufox instances are eight Firefoxes; the app's
  whole MEMORY_LIMIT_MB budget would fit inside B's peak on one site.
- **Recommendation shape**: not a replacement. A scoped spike as a
  second-chance fetcher — when crawl4ai's frontier starves on a
  JS-heavy site (the community.clay.com signature), hand the seed to a
  low-concurrency Scrapling pass for discovery. That buys the upside
  without carrying the footprint on every ordinary crawl.

---

### wxrks — Webflow — production ground truth: 824 pages / 1,076,780 words

| | crawl4ai (A) | Scrapling (B) |
|---|---|---|
| pages ok / attempted | 120 / 120 | 120 / 120 |
| blocked | 0 | 0 |
| failed (non-block) | 0 | 0 |
| words (own pipeline) | 157,202 | 135,536 |
| pages/min | 289.2 | 60.4 |
| median fetch ms | 967 | 6644 |
| peak RSS MB | 2454.3 | 10493.2 |

- **Fetcher-attributable word delta** (B vs B′ on 60 shared URLs): 98.3% of pages within ±5%
- **Extractor-attributable delta** (B′ vs A): 6.7% of pages within ±5%

  Worst fetcher divergences (delta, url, B, B′):
    - 226%  https://wxrks.com/testimonials  3228 vs 989
    - 2%  https://wxrks.com/signup  59 vs 60
    - 1%  https://wxrks.com/workshop  69 vs 70
    - 1%  https://wxrks.com/break-the-machine  144 vs 146
    - 1%  https://wxrks.com/blog  102 vs 103

### community — JS SPA — crawl4ai discovery collapsed here (5,579 -> 439)

| | crawl4ai (A) | Scrapling (B) |
|---|---|---|
| pages ok / attempted | 2 / 2 | 25 / 120 |
| blocked | 0 | 95 |
| failed (non-block) | 0 | 0 |
| words (own pipeline) | 23 | 17,610 |
| pages/min | 32.4 | 150.0 |
| median fetch ms | 1909 | 2218 |
| peak RSS MB | 1003.6 | 9168.8 |

- **Fetcher-attributable word delta** (B vs B′ on 2 shared URLs): 0.0% of pages within ±5%
- **Extractor-attributable delta** (B′ vs A): 0.0% of pages within ±5%

  Worst fetcher divergences (delta, url, B, B′):
    - 21900%  https://community.clay.com/  1540 vs 7
    - 21900%  https://community.clay.com  1540 vs 7

### clay — anti-bot gauntlet — 301 blocked pages in one production run

| | crawl4ai (A) | Scrapling (B) |
|---|---|---|
| pages ok / attempted | 120 / 120 | 120 / 120 |
| blocked | 0 | 0 |
| failed (non-block) | 0 | 0 |
| words (own pipeline) | 250,614 | 241,319 |
| pages/min | 79.6 | 22.4 |
| median fetch ms | 4042 | 15972 |
| peak RSS MB | 3419.3 | 11745.3 |

- **Fetcher-attributable word delta** (B vs B′ on 107 shared URLs): 97.2% of pages within ±5%
- **Extractor-attributable delta** (B′ vs A): 0.0% of pages within ±5%

  Worst fetcher divergences (delta, url, B, B′):
    - 146%  https://www.clay.com/careers  4190 vs 1701
    - 13%  https://www.clay.com/  1631 vs 1877
    - 7%  https://www.clay.com  2008 vs 1877
    - 5%  https://www.clay.com/claygent  1729 vs 1812
    - 1%  https://www.clay.com/experts  1388 vs 1403

### wordpress — boring WordPress control

| | crawl4ai (A) | Scrapling (B) |
|---|---|---|
| pages ok / attempted | 120 / 120 | 119 / 120 |
| blocked | 0 | 0 |
| failed (non-block) | 0 | 1 |
| words (own pipeline) | 99,167 | 122,454 |
| pages/min | 334.9 | 58.8 |
| median fetch ms | 878 | 4556 |
| peak RSS MB | 1889.4 | 8886.1 |

- **Fetcher-attributable word delta** (B vs B′ on 35 shared URLs): 97.1% of pages within ±5%
- **Extractor-attributable delta** (B′ vs A): 60.0% of pages within ±5%

  Worst fetcher divergences (delta, url, B, B′):
    - 288%  https://wordpress.org/gutenberg/  31 vs 8
    - 1%  https://wordpress.org/education/  391 vs 395
    - 1%  https://wordpress.org/showcase/  140 vs 139
    - 0%  https://wordpress.org/plugins/  485 vs 487
    - 0%  https://wordpress.org/news/2023/05/people-of-wordpress-stefano-cassone  1606 vs 1611

### marriott — Akamai-protected — the real anti-bot gauntlet

| | crawl4ai (A) | Scrapling (B) |
|---|---|---|
| pages ok / attempted | 0 / 1 | 0 / 1 |
| blocked | 1 | 1 |
| failed (non-block) | 0 | 0 |
| words (own pipeline) | 0 | 0 |
| pages/min | 46.2 | 21.4 |
| median fetch ms | 829 | 2755 |
| peak RSS MB | 533.5 | 1194.2 |

- **Fetcher-attributable word delta** (B vs B′ on 0 shared URLs): n/a
- **Extractor-attributable delta** (B′ vs A): n/a
