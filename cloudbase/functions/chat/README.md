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
| `CHAT_ALLOWED_ORIGINS` | Comma-separated HTTPS static-site origins |

Optional numeric limits have defaults: `CHAT_MAX_MESSAGE_CHARACTERS=1000`,
`CHAT_MAX_REQUESTS_PER_IP_PER_HOUR=20`, `CHAT_MONTHLY_BUDGET_CNY=10`,
`CHAT_RESERVATION_CNY=0.03`, and `CHAT_MAX_OUTPUT_TOKENS=400`.

The function stores only hashed-IP hourly request counts and a monthly reserved
budget in the CloudBase collection `ai_info_chat_usage`. It does not store raw
IP addresses, user questions, answers, or the DeepSeek token.

## Deployment and validation

1. In CloudBase Console, create an HTTP function named `chat` from this
   directory and install the declared npm dependency.
2. Add the four required environment variables in the Console, then create the
   `ai_info_chat_usage` collection with server-side function access only.
3. Bind the returned HTTPS function URL to the static build as `CHAT_API_URL`.
   The site deliberately does not render an enabled form for an HTTP URL.
4. Verify one valid published product request, an unknown slug (404), an
   oversized message (400), rate limiting (429), and the monthly budget guard
   (503). Confirm logs show only the structured redacted fields.

References: [CloudBase function deployment](https://cloud.tencent.com/document/product/876/46899), [CloudBase Node SDK database](https://cloud.tencent.com/document/product/876/19362), [SCF environment variables](https://cloud.tencent.com/document/product/583/30228).
