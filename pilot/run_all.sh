#!/bin/sh
# Sequential on purpose: concurrent runs would contend for CPU and poison the
# speed numbers. A then B per site, so network conditions stay comparable.
cd "$(dirname "$0")/.."
for site in wordpress wxrks community clay; do
  echo "=== A $site ==="
  .venv/bin/python pilot/run_a_crawl4ai.py "$site" 2>&1 | grep -vE "^\[(FETCH|SCRAPE|COMPLETE|INIT|ERROR|ANTIBOT)\]" | tail -2
  echo "=== B $site ==="
  pilot/.venv/bin/python pilot/run_b_scrapling.py "$site" 2>&1 | grep -v "INFO:" | tail -2
done
echo "ALL DONE"
