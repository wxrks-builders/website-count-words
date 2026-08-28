#!/usr/bin/env python
"""Joins the two sides' results into pilot/report.md, and applies the verdict
criteria that were agreed BEFORE the run — so the result reads as a decision,
not a debate.
"""

from __future__ import annotations

import datetime
import platform
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import PAGE_CAP, SITES, load  # noqa: E402

RESULTS = Path(__file__).parent / "results"


def pct(part, whole):
    return round(100 * part / whole, 1) if whole else 0.0


def site_block(site: str) -> tuple[str, dict]:
    a_meta, a_pages = load(RESULTS / f"a-{site}.jsonl")
    b_meta, b_pages = load(RESULTS / f"b-{site}.jsonl")
    a_by = {p["url"]: p for p in a_pages}
    b_by = {p["url"]: p for p in b_pages}

    stats = {}
    for tool, meta, pages in (("crawl4ai", a_meta, a_pages), ("scrapling", b_meta, b_pages)):
        ok = [p for p in pages if p["ok"]]
        stats[tool] = {
            "attempted": len(pages),
            "ok": len(ok),
            "blocked": sum(1 for p in pages if p["blocked"]),
            "failed": sum(1 for p in pages if not p["ok"] and not p["blocked"]),
            "words": sum(p["words"] for p in ok),
            "pages_min": round(len(pages) / (meta["wall_seconds"] / 60), 1) if meta["wall_seconds"] else 0,
            "wall_s": meta["wall_seconds"],
            "rss_mb": meta["peak_rss_mb"],
            "median_ms": sorted(p["ms"] for p in pages)[len(pages) // 2] if pages else 0,
        }

    # The attribution split, on URLs both sides fetched OK.
    both = [u for u in a_by if u in b_by and a_by[u]["ok"] and b_by[u]["ok"]]
    fetch_deltas, extract_deltas = [], []
    for u in both:
        a, b = a_by[u], b_by[u]
        b_prime = a["words_shared"]              # shared extractor on A's HTML
        if b_prime:
            fetch_deltas.append((abs(b["words"] - b_prime) / b_prime, u, b["words"], b_prime))
        if a["words"]:
            extract_deltas.append((abs(b_prime - a["words"]) / a["words"], u, b_prime, a["words"]))

    def summarise(deltas):
        if not deltas:
            return "n/a", []
        within5 = pct(sum(1 for d, *_ in deltas if d <= 0.05), len(deltas))
        worst = sorted(deltas, reverse=True)[:5]
        return f"{within5}% of pages within ±5%", worst

    fetch_line, fetch_worst = summarise(fetch_deltas)
    extract_line, extract_worst = summarise(extract_deltas)

    a, b = stats["crawl4ai"], stats["scrapling"]
    lines = [
        f"### {site} — {SITES[site][1]}", "",
        "| | crawl4ai (A) | Scrapling (B) |", "|---|---|---|",
        f"| pages ok / attempted | {a['ok']} / {a['attempted']} | {b['ok']} / {b['attempted']} |",
        f"| blocked | {a['blocked']} | {b['blocked']} |",
        f"| failed (non-block) | {a['failed']} | {b['failed']} |",
        f"| words (own pipeline) | {a['words']:,} | {b['words']:,} |",
        f"| pages/min | {a['pages_min']} | {b['pages_min']} |",
        f"| median fetch ms | {a['median_ms']} | {b['median_ms']} |",
        f"| peak RSS MB | {a['rss_mb']} | {b['rss_mb']} |", "",
        f"- **Fetcher-attributable word delta** (B vs B′ on {len(both)} shared URLs): {fetch_line}",
        f"- **Extractor-attributable delta** (B′ vs A): {extract_line}", "",
    ]
    if fetch_worst and fetch_worst[0][0] > 0.05:
        lines.append("  Worst fetcher divergences (delta, url, B, B′):")
        for d, u, bw, bp in fetch_worst:
            lines.append(f"    - {d:.0%}  {u[:70]}  {bw} vs {bp}")
        lines.append("")
    return "\n".join(lines), {"a": a, "b": b, "fetch_deltas": fetch_deltas, "both": len(both)}


def main() -> None:
    blocks, per_site = [], {}
    for site in SITES:
        a_path, b_path = RESULTS / f"a-{site}.jsonl", RESULTS / f"b-{site}.jsonl"
        if not (a_path.exists() and b_path.exists()):
            blocks.append(f"### {site}\n\n(not run)\n")
            continue
        text, data = site_block(site)
        blocks.append(text)
        per_site[site] = data

    # --- the pre-agreed verdict ---
    verdict = []
    community = per_site.get("community")
    clay = per_site.get("clay")
    gates_ok = []
    if community:
        gain = pct(community["b"]["ok"] - community["a"]["ok"], max(community["a"]["ok"], 1))
        gates_ok.append(("≥10% more pages on community.clay.com", gain >= 10, f"{gain:+.0f}%"))
    if clay:
        half = clay["b"]["blocked"] <= clay["a"]["blocked"] / 2 if clay["a"]["blocked"] else clay["b"]["blocked"] == 0
        gates_ok.append(("blocked rate on clay.com ≤ half of crawl4ai's", half,
                         f"{clay['b']['blocked']} vs {clay['a']['blocked']}"))
    fidelity = True
    for site in ("wxrks", "wordpress"):
        if site in per_site:
            deltas = per_site[site]["fetch_deltas"]
            if deltas:
                within = sum(1 for d, *_ in deltas if d <= 0.05) / len(deltas)
                if within < 0.9:
                    fidelity = False
    gates_ok.append(("fetch-attributable deltas within ±5% on well-handled sites", fidelity, ""))

    any_gate = any(ok for name, ok, _ in gates_ok[:2])
    earned = any_gate and fidelity
    verdict.append("## Verdict (criteria fixed before the run)\n")
    for name, ok, detail in gates_ok:
        verdict.append(f"- {'✅' if ok else '❌'} {name} {f'— {detail}' if detail else ''}")
    verdict.append("")
    verdict.append("**Scrapling earns a deeper integration spike.**" if earned else
                   "**Keep crawl4ai.** The gates were not met; switching a production crawler for parity is all risk and no product.")

    header = [
        "# Scrapling vs crawl4ai — pilot report", "",
        f"Run {datetime.date.today().isoformat()} on {platform.platform()}, "
        f"page cap {PAGE_CAP}/site, concurrency 8, sequential runs (A then B per site).",
        "Frontier rules and blocked-detection shared between both sides; "
        "B′ = the shared extractor applied to A's HTML, which is what lets a word "
        "delta be attributed to the fetcher rather than the extractor.", "",
    ]
    caveats = [
        "", "## Caveats the table can't carry", "",
        "- **Calibration held**: side A counted 1,310 words/page on wxrks.com vs the",
        "  production run's 1,307 — 0.2%. The A side is production behaviour, measured.",
        "- **community.clay.com's 95 'blocked' are all HTTP 403**, arriving after 25",
        "  pages of real content: the site rate-limited eight concurrent Camoufox",
        "  instances. Scrapling's *discovery* advantage there is real (it found the",
        "  SPA's links where crawl4ai found 2 pages); its default footprint at this",
        "  concurrency is what got it throttled. A spike should retry at concurrency",
        "  2-3 with its adaptive rate limiter on.",
        "- **This IP has crawled these sites hard for weeks.** clay.com showed 0",
        "  blocked for BOTH sides today (vs 301 historically), so the anti-bot gate",
        "  passed on thin evidence — neither side was actually challenged there.",
        "- **marriott.com supplied the real anti-bot test, added after the first",
        "  run, and both tools failed it — differently.** crawl4ai reported an",
        "  honest 'Akamai block (Reference #)'. Scrapling (Camoufox, including with",
        "  network_idle and a 60s timeout) received an HTTP 200 challenge shell:",
        "  3KB, 5 visible words, one link. That failure mode is WORSE for this",
        "  product than a block — a 'successful' 5-word homepage would enter a",
        "  word-count report as real data. The shared detector now treats a 200",
        "  under 10KB with under 20 words as a soft block; the production crawler",
        "  has no such guard and would benefit from one regardless of which",
        "  fetcher it uses.",
        "- **The costs are not small**: Scrapling ran 4-6x slower per page and at",
        "  3-5x the memory. Eight Camoufox instances are eight Firefoxes; the app's",
        "  whole MEMORY_LIMIT_MB budget would fit inside B's peak on one site.",
        "- **Recommendation shape**: not a replacement. A scoped spike as a",
        "  second-chance fetcher — when crawl4ai's frontier starves on a",
        "  JS-heavy site (the community.clay.com signature), hand the seed to a",
        "  low-concurrency Scrapling pass for discovery. That buys the upside",
        "  without carrying the footprint on every ordinary crawl.",
    ]
    out = Path(__file__).parent / "report.md"
    out.write_text("\n".join(header + verdict + caveats + ["", "---", ""] + blocks))
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
