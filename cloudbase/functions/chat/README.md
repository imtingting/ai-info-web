# CloudBase Chat Function

This HTTP function is deployed to CloudBase environment
`ai-info-web-dev-d0f70i51e551581f`. It accepts only `POST /api/chat`-style
requests and only answers for slugs present in the generated public catalog.

## Required CloudBase environment variables

Set these in the CloudBase console. Do not put values in this repository,
GitHub Actions logs, browser configuration, or a chat message.

| Variable | Purpose |
| --- | --- |
| `DEEPSEEK_API_KEY` | Server-side DeepSeek credential |
| `CHAT_IP_HASH_SALT` | Random secret used to hash source IP addresses |
| `CHAT_CATALOG_URL` | HTTPS URL to the deployed `data/products.json` |
| `TAVILY_API_KEY` | Optional Tavily key; required only after enabling the web-search pilot |

This environment's HTTP access service owns CORS for the configured static-site
domain. Do not add `CHAT_ALLOWED_ORIGINS` to this function or it will emit a
duplicate CORS response header.

Optional numeric limits have defaults: `CHAT_MAX_MESSAGE_CHARACTERS=1000`,
`CHAT_MAX_REQUESTS_PER_IP_PER_HOUR=20`, `CHAT_MONTHLY_BUDGET_CNY=10`,
`CHAT_RESERVATION_CNY=0.03`, and `CHAT_MAX_OUTPUT_TOKENS=400`. The optional
Tavily pilot defaults to `TAVILY_MAX_SEARCHES_PER_IP_PER_DAY=5`,
`TAVILY_MONTHLY_REQUEST_LIMIT=1000`, and `TAVILY_CACHE_TTL_HOURS=24`.

The function stores only hashed-IP hourly request counts, a monthly reserved
model budget, and hashed-IP daily web-search counts in
`ai_info_chat_usage`. It stores a hashed query key, bounded search snippets,
source links, and expiry time in `ai_info_chat_search_cache`; it does not store
raw IP addresses, the Tavily key, or the DeepSeek token. Create both collections
with server-side function access only.

## Deployment and validation

1. In CloudBase Console, create an **HTTP function** named `chat` by uploading
   an archive whose root contains `scf_bootstrap`, `server.js`, `index.js`,
   and `package.json`. This deployment path starts the server through the
   executable bootstrap file on port 9000 using the installed Node.js runtime.
   Keep `index.main` as the Console entry value when it is required. The deployment archive includes locked
   production dependencies, so disable the Console's online dependency install.
2. Add the required environment variables in the Console, then create the
   `ai_info_chat_usage` and `ai_info_chat_search_cache` collections with
   server-side function access only. For the pilot, add `TAVILY_API_KEY` only
   in this function's environment; never add it to GitHub or static-site variables.
3. Bind the returned HTTPS function URL to the static build as `CHAT_API_URL`.
   The site deliberately does not render an enabled form for an HTTP URL.
4. Verify one normal product request, one request with `use_web: true`, an
   unknown slug (404), an
   oversized message (400), rate limiting (429), and the monthly budget guard
   (503). Confirm logs show only the structured redacted fields.

References: [CloudBase HTTP functions](https://cloud.tencent.com/document/product/876/46899), [SCF bootstrap file](https://cloud.tencent.com/document/product/583/56126), [CloudBase Node SDK database](https://cloud.tencent.com/document/product/876/19362), [SCF environment variables](https://cloud.tencent.com/document/product/583/30228).
