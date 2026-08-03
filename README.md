# AI Info Web

AI product intelligence workspace for Chinese AI practitioners.

## Local setup

This project uses the Python standard library for its initial database layer.
No credentials are required to initialize the local state database.

```bash
make init
make test
```

The default database path is `../ai-info-web-data/ai-info-web.sqlite3`, outside
the repository and outside static build output. Override it when needed:

```bash
make init DB_PATH=/private/path/ai-info-web.sqlite3
```

Runtime configuration is loaded from `config/default.json` and environment
variables. Secrets are read only in later provider tasks and must never be
placed in configuration files or static assets.

## GitHub collection

The GitHub provider requires a read-only token in the local environment; it is
not read from JSON configuration or stored by the application.

```bash
export GITHUB_TOKEN=github_pat_...
make fetch-github
```

When the token is missing or GitHub is unavailable, the run is marked `failed`
in the private SQLite `run_log` and does not produce a publishable batch.

Each configured topic also receives a `created:>=7 days ago` query, so the
weekly-new feed does not depend only on established high-star repositories.
README context is backfilled in a separate bounded task. It enriches previously
unchecked GitHub items with a structured README excerpt (8,000 characters),
README image links, and the official homepage's HTTPS `og:image`. These
requests are cached and never occupy the daily static-publication budget.

The non-critical GitHub Trending weekly observation is a separate command. It
reads the public `since=weekly` page, keeps only the first 20 structured
entries, then enriches them through the official repository API. It never uses
cookies or a token for the page request, does not store page HTML, and reports
`degraded` rather than failing the main pipeline when the page changes or is
unavailable.

```bash
PYTHONPATH=src python -m ai_info_web.cli observe-trending --db /private/path/ai-info-web.sqlite3
```

## Product Hunt collection

Product Hunt is disabled by default because V1 does not publish its data. Set
`ENABLE_PRODUCT_HUNT=true` and configure `PRODUCT_HUNT_DEVELOPER_TOKEN` only
for private validation after confirming the applicable API terms.

```bash
ENABLE_PRODUCT_HUNT=true PRODUCT_HUNT_DEVELOPER_TOKEN=... make fetch-product-hunt
```

Missing credentials, GraphQL errors, rate limits, and network failures produce
`degraded` status and do not block the GitHub data path.

## Curation

After collection, clean source records, apply each source's admission threshold,
conservatively merge only URL/domain matches, and rebuild products:

```bash
make curate
```

Potential name-only matches are never merged. They are written to the private
`weak_match_review.json` file beside the SQLite database for later review.

## Heat scoring

Calculate the independent hot-tab score after curation:

```bash
make score-heat
```

`score_breakdown` retains 7-day snapshot deltas, actual data-window days,
normalised values, weights, sources, and freshness decay. When no Product Hunt
records exist, scores use GitHub-only mode and are marked accordingly.

The ranking helpers apply the product rules directly: weekly-new uses the
GitHub `created_at` 7-day window and current stars (Top20); hot requires at
least two daily snapshots or a startup stargazer prefill (Top20). During the
first seven GitHub snapshot days, at most 30 curated high-star repositories
receive a timestamped stargazer query to seed their 7-day star delta. Each
prefill checks at most two pages; histories that cannot reach the seven-day
boundary within that limit are skipped instead of recording an incomplete
delta. GitHub does not provide timestamped fork history, so startup fork delta
is explicitly stored as zero. `rank_history` keeps the de-duplicated
historical union of weekly-new and hot membership for 30-item pagination.

The hosted **Daily AI product intelligence** workflow reserves its 20-minute
budget for collection, scoring, six uncached DeepSeek summaries, and static
publication. README/homepage enrichment and startup stargazer prefill are
excluded from that path. Use the manual **Backfill README context** workflow
to enrich two prioritized products and generate a fresh `cloudbase-static-site`
artifact; run it repeatedly to fill the backlog without blocking deployment.

## Chinese summaries

Summary generation reads `DEEPSEEK_API_KEY` only from the environment. It uses
the configured `deepseek-chat` endpoint, caches by the merged product input,
and records estimated token cost in the private SQLite ledger.

```bash
export DEEPSEEK_API_KEY=...
make summarize
```

When the key is absent, the feature is disabled, a budget is exhausted, or one
request fails, the affected product is marked as skipped or failed without
blocking other products. Never place the key in a config file or static output.

## Project chat API

Detail pages include a disabled chat form until `CHAT_API_URL` is configured as
an HTTPS endpoint. The browser sends only a published product slug and the
question; the API reloads that product's generated context from
`data/products.json` and rejects unknown slugs.

After the CloudBase function returns an HTTPS URL, set it as the non-secret
repository Actions variable `CHAT_API_URL`; the daily static build then enables
the form. Until that variable is set, publication remains functional and the
form stays disabled.

The deployable CloudBase HTTP function is in `cloudbase/functions/chat`. It
keeps `DEEPSEEK_API_KEY` and `CHAT_IP_HASH_SALT` in CloudBase environment
variables, applies a hashed-IP hourly limit and a monthly budget reservation in
the CloudBase database, and writes redacted structured logs only. Configure it
using [cloudbase/functions/chat/README.md](cloudbase/functions/chat/README.md);
never place `CHAT_IP_HASH_SALT` in static publication configuration or expose
either value to the browser.

## Daily static publication

Run the complete pipeline locally after setting the required `GITHUB_TOKEN`:

```bash
make run-daily DB_PATH=/private/tmp/ai-info-web.sqlite3
```

The command creates an isolated static batch, validates it, and only then
switches `public/`. If GitHub fails or its token is missing, it exits with code
2 and leaves the previous public batch untouched. Product Hunt and summaries
can degrade without blocking publication; their statuses are shown in the site.

GitHub Actions runs daily at 00:30 UTC. Configure its secrets without adding
them to this repository: `STATE_REPO`, `STATE_REPO_TOKEN`, `GH_PAT`, and
optionally `DEEPSEEK_API_KEY`. `GH_PAT` is the read-only GitHub token used by
collection; GitHub reserves the `GITHUB_` prefix, so it cannot be created as
an Actions secret. `STATE_REPO` must name a private repository used only for
the SQLite database.

Every successful workflow run publishes a seven-day Actions artifact named
`cloudbase-static-site`. Download and extract that artifact, then upload the
extracted directory through CloudBase **Static Website Hosting -> Website
Deployment -> Local project upload**. The artifact contains only the verified
static site; it never contains the SQLite state database or runtime secrets.

CloudBase supplies an HTTPS default domain for testing. After the first upload,
use that public root URL to configure the `chat` function's
`CHAT_CATALOG_URL` as `<root>/data/products.json` and
`CHAT_ALLOWED_ORIGINS` as `<root>` without a trailing slash.
