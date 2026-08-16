# Deploying

The app is a single Docker container. Environment variables are listed in
`.env.example`, which is the authoritative set. Everything below is a
requirement that *isn't* an environment variable, and each one has a real
failure mode attached — these were previously implied by a Render blueprint and
are written down here so they survive the move to any other host.

## One worker. Not a default — a requirement.

```
uvicorn app.main:app --workers 1 --proxy-headers --forwarded-allow-ips='*'
```

Crawl state lives in process memory (`app/job_store.py`): the running job
registry, the queue of waiting crawls, and the subscriber list that feeds live
progress. A second worker gets its own copy of all of it.

What breaks with more than one: live progress returns 404 whenever the browser
lands on the worker that isn't running the crawl; "Proceed with crawl" 404s the
same way; and the three-concurrent-crawl limit becomes three *per worker*, each
sized against the full memory budget.

This matters because uvicorn falls back to `$WEB_CONCURRENCY` when it's set, and
several hosts set it automatically from the instance size. Pass `--workers 1`
explicitly.

`--proxy-headers` is what makes the app see the real external scheme and host
behind a load balancer. Without it the OAuth callback is built as `http://`
against an internal hostname and sign-in fails.

## A persistent volume, mounted before the app starts

`DB_PATH` is a SQLite file, and its parent directory also holds saved Markdown
(`MARKDOWN_DIR` defaults to `<DB_PATH parent>/markdown`). Both need to survive a
restart and a redeploy.

- **Size**: budget ~10 GB. The database alone is small, but Markdown for one
  very large crawl is 600-700 MB, and `MARKDOWN_MAX_TOTAL_MB` defaults to 6 GB.
- **One instance only.** The volume cannot be shared. Two instances against the
  same disk will fight over SQLite, and the startup sweep in `lifespan` deletes
  Markdown for any run id missing from *its* database — so a second instance
  wipes the first one's archives on boot.
- If the volume is missing, SQLite is created in the container filesystem and
  every run disappears on the next deploy, silently.

## Crawl speed and the memory ceiling

How long a crawl takes is almost entirely one number: how many pages it fetches
at once (`app/plans.py`). Free gets `CRAWL_CONCURRENCY_FREE`, paid accounts get
`CRAWL_CONCURRENCY_PRO`, and `CRAWL_PAGE_BUDGET` caps the total in flight across
every running crawl at once.

That last one is not optional. crawl4ai builds a separate dispatcher per crawl,
so the per-crawl numbers can't see each other — without the budget,
`MAX_CONCURRENT_CRAWLS` paid crawls together would put
`MAX_CONCURRENT_CRAWLS x CRAWL_CONCURRENCY_PRO` Chromium pages on the box and
thrash the CPU long before `MEMORY_LIMIT_MB` noticed.

Size them to the host. On 6 vCPU / 16 GB the shipped defaults are 4 / 16 / 24,
with `MEMORY_LIMIT_MB=12000`. Rough rule: budget about 4 pages per vCPU, and set
`MEMORY_LIMIT_MB` to roughly three quarters of the container's limit — the
crawler polls its own RSS and stops a crawl before reaching it rather than being
OOM-killed.

The speedup is real but sublinear: fetching is only part of the work, and
rendering and text extraction compete for CPU. Measured against a local server
with fixed latency, 120 pages: 2 wide took 34.8s, 4 wide 19.4s, 16 wide 7.0s —
so 4 -> 16 is 2.8x, not 4x. `CRAWL_SCALING_EFFICIENCY` is what keeps the figure
quoted to customers in line with that; re-measure it if the host changes.

## Billing (Stripe) — optional

Leave `STRIPE_SECRET_KEY` blank and billing does not exist: `/pricing`,
`/billing/*` and `/stripe/webhook` all 404, the UI never links to them, and
everyone crawls at the free speed. Nothing else changes.

To turn it on, set `STRIPE_SECRET_KEY`, `STRIPE_PRICE_ID` (the recurring
`price_...`, not a `prod_...`) and `STRIPE_WEBHOOK_SECRET`, then register the
endpoint in Stripe:

```
https://<host>/stripe/webhook
```

subscribed to `checkout.session.completed`, `customer.subscription.created`,
`customer.subscription.updated` and `customer.subscription.deleted`.

**The webhook is the only thing that grants Pro.** The checkout success URL
deliberately grants nothing — a browser can be pointed at it without paying — so
if `STRIPE_WEBHOOK_SECRET` is wrong, every event is rejected and paying
customers stay on the free tier while their card is charged. Verify it after
any deploy that changes the endpoint:

```
stripe listen --forward-to https://<host>/stripe/webhook   # prints the secret
stripe trigger customer.subscription.updated               # expect 200, not 400
```

Both hostnames share one account (`app/surfaces.py`), so one subscription covers
both products. Only one Stripe price is needed.

## Health check

`GET /login` — public, cheap, and doesn't touch the database. `GET /` is also
public now (it serves the landing page) but does hit the database when signed
in, so `/login` remains the better probe.

## HTTPS

Set `SECURE_COOKIES=true` (the default) anywhere the app is served over HTTPS.
It marks the session cookie `Secure`.

This used to be derived from a host-provided variable, which meant it silently
became false when the app moved hosts. It now defaults to on and only an
explicit `SECURE_COOKIES=false` disables it — set that locally, and nowhere else.

## Hostnames

Both products are served by this one container, chosen by the `Host` header
(`app/surfaces.py`). Adding a hostname means:

1. Point the DNS record at this same service. Do not create a second one — see
   the volume note above for why a second instance is not safe.
2. Set `COUNTER_HOST` / `MARKDOWN_HOST` to match.
3. **Add `https://<host>/auth/callback` to the Google OAuth client's authorised
   redirect URIs.** The callback is built from the incoming request host, so
   sign-in fails on the new hostname until this exists. This is the step most
   likely to cost an afternoon.

## Build notes

The image installs Playwright's Chromium and runs `crawl4ai-setup` — the crawler
renders pages in a real browser, which is why JavaScript-built content is
counted. Expect a large image and a slow first build.

`requirements-dev.txt` is test-only and is deliberately not installed in the
image.

## Checks after a deploy

```
curl -sf https://<host>/login                     # 200, health check
curl -s  https://<host>/ | grep -o '<title>[^<]*' # the right product name
```

Then sign in — that is what proves the OAuth redirect URI is registered — and
run a small crawl to confirm the volume is writable and live progress streams.

If billing is configured, also send one real event (`stripe trigger
customer.subscription.updated`) and confirm it returns 200. A 400 means the
signing secret is wrong, and nobody who pays will actually become Pro.
