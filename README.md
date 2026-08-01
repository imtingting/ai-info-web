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
