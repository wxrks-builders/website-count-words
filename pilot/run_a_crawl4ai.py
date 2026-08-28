#!/usr/bin/env python
"""Side A: crawl4ai, configured exactly as production configures it.

Runs in the APP's venv (../.venv), which is the parity argument itself: same
library build, same browser, same markdown pipeline. The only differences from
production are the shared pilot frontier (so both sides attempt the same URL
universe) and the page cap.

    ../.venv/bin/python run_a_crawl4ai.py <site>     # site key from common.SITES
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (CONCURRENCY, PAGE_CAP, SITES, Frontier, PageResult,  # noqa: E402
                    RssSampler, RunResult, looks_blocked, shared_words)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from crawl4ai import (AsyncWebCrawler, BrowserConfig, CrawlerRunConfig,  # noqa: E402
                      DefaultMarkdownGenerator, PruningContentFilter)
from app.crawler import _markdown_text, clean_markdown_for_counting  # noqa: E402
from app.word_count import count_words  # noqa: E402


async def run(site: str) -> None:
    seed, note = SITES[site]
    frontier = Frontier(seed)
    result = RunResult(tool="crawl4ai", site=site, seed=seed)

    # Production's exact page config — lifted from app/crawler.py, not retyped
    # from memory of it.
    config = CrawlerRunConfig(
        page_timeout=30000,
        markdown_generator=DefaultMarkdownGenerator(content_filter=PruningContentFilter()),
        excluded_tags=["nav", "footer", "aside", "form"],
        word_count_threshold=10,
    )
    browser = BrowserConfig(avoid_css=True, avoid_ads=True, memory_saving_mode=True,
                            max_pages_before_recycle=500)

    started = time.perf_counter()
    with RssSampler() as rss:
        async with AsyncWebCrawler(config=browser) as crawler:
            semaphore = asyncio.Semaphore(CONCURRENCY)

            async def fetch(url: str) -> PageResult:
                async with semaphore:
                    t0 = time.perf_counter()
                    try:
                        page = await crawler.arun(url, config=config)
                    except Exception as exc:
                        return PageResult(url=url, ok=False, error=str(exc)[:200],
                                          ms=int((time.perf_counter() - t0) * 1000))
                    ms = int((time.perf_counter() - t0) * 1000)
                    html = getattr(page, "html", "") or ""
                    error = getattr(page, "error_message", "") or ""
                    blocked = error.startswith("Blocked by anti-bot protection:") or looks_blocked(html, getattr(page, "status_code", None))
                    if not page.success or blocked:
                        return PageResult(url=url, ok=False, blocked=blocked,
                                          status=getattr(page, "status_code", None),
                                          ms=ms, error=error[:200],
                                          words_shared=shared_words(html))
                    words = count_words(clean_markdown_for_counting(_markdown_text(page)))
                    links = [l.get("href") for l in (page.links or {}).get("internal", [])]
                    for href in links:
                        if href:
                            frontier.offer(href)
                    return PageResult(url=url, ok=True, status=getattr(page, "status_code", None),
                                      words=words, words_shared=shared_words(html),
                                      links_found=len(links), ms=ms)

            while len(result.pages) < PAGE_CAP:
                batch = frontier.next_batch(min(CONCURRENCY, PAGE_CAP - len(result.pages)))
                if not batch:
                    break
                result.pages.extend(await asyncio.gather(*(fetch(u) for u in batch)))
                done = len(result.pages)
                print(f"  [{site}] {done} pages, {sum(1 for p in result.pages if p.ok)} ok, "
                      f"queue {len(frontier.queue)}", flush=True)

        result.wall_seconds = round(time.perf_counter() - started, 1)
        result.peak_rss_mb = rss.peak_mb

    out = Path(__file__).parent / "results" / f"a-{site}.jsonl"
    result.write(out)
    ok = sum(1 for p in result.pages if p.ok)
    print(f"A/{site}: {ok}/{len(result.pages)} ok, {result.wall_seconds}s, "
          f"peak {result.peak_rss_mb}MB -> {out.name}")


if __name__ == "__main__":
    asyncio.run(run(sys.argv[1]))
