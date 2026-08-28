#!/usr/bin/env python
"""Side B: Scrapling's StealthyFetcher (Camoufox), same frontier as side A.

    .venv/bin/python run_b_scrapling.py <site>       # pilot venv, not the app's

The loop mirrors run_a_crawl4ai.py deliberately: same frontier, same page cap,
same concurrency, same blocked-detector. Only the fetcher differs — that is
the experiment.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from urllib.parse import urljoin

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (CONCURRENCY, PAGE_CAP, SITES, Frontier, PageResult,  # noqa: E402
                    RssSampler, RunResult, looks_blocked, shared_words)

from scrapling.fetchers import StealthyFetcher  # noqa: E402


async def run(site: str) -> None:
    seed, note = SITES[site]
    frontier = Frontier(seed)
    result = RunResult(tool="scrapling", site=site, seed=seed)

    started = time.perf_counter()
    with RssSampler() as rss:
        semaphore = asyncio.Semaphore(CONCURRENCY)

        async def fetch(url: str) -> PageResult:
            async with semaphore:
                t0 = time.perf_counter()
                try:
                    page = await StealthyFetcher.async_fetch(url, headless=True, timeout=30000)
                except Exception as exc:
                    return PageResult(url=url, ok=False, error=str(exc)[:200],
                                      ms=int((time.perf_counter() - t0) * 1000))
                ms = int((time.perf_counter() - t0) * 1000)
                html = page.html_content or ""
                status = page.status
                blocked = looks_blocked(html, status)
                if status != 200 or blocked:
                    return PageResult(url=url, ok=False, blocked=blocked, status=status,
                                      ms=ms, error=f"HTTP {status}",
                                      words_shared=shared_words(html))
                words = shared_words(html)   # B's own count IS the shared extractor
                hrefs = [str(h) for h in page.css("a::attr(href)")]
                for href in hrefs:
                    frontier.offer(urljoin(url, href))
                return PageResult(url=url, ok=True, status=status, words=words,
                                  words_shared=words, links_found=len(hrefs), ms=ms)

        while len(result.pages) < PAGE_CAP:
            batch = frontier.next_batch(min(CONCURRENCY, PAGE_CAP - len(result.pages)))
            if not batch:
                break
            result.pages.extend(await asyncio.gather(*(fetch(u) for u in batch)))
            print(f"  [{site}] {len(result.pages)} pages, "
                  f"{sum(1 for p in result.pages if p.ok)} ok, queue {len(frontier.queue)}", flush=True)

        result.wall_seconds = round(time.perf_counter() - started, 1)
        result.peak_rss_mb = rss.peak_mb

    out = Path(__file__).parent / "results" / f"b-{site}.jsonl"
    result.write(out)
    ok = sum(1 for p in result.pages if p.ok)
    print(f"B/{site}: {ok}/{len(result.pages)} ok, {result.wall_seconds}s, "
          f"peak {result.peak_rss_mb}MB -> {out.name}")


if __name__ == "__main__":
    asyncio.run(run(sys.argv[1]))
