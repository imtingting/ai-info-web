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
