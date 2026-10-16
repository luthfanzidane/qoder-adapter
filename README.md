# qoder-adapter

OpenAI-compatible **loopback sidecar** for [Qoder](https://qoder.sh) **PAT → JT** accounts.

Runs next to a 9router deployment, exposes an OpenAI-shaped `/v1` API on `127.0.0.1`, and forwards
chat completions to Qoder's upstream Cosy endpoint using a rotating pool of PAT accounts.
It never writes 9router SQLite.

```
client → 9router (openai-compatible node, baseUrl http://127.0.0.1:18092/v1)
          └── qoder-adapter (this repo)
                ├── PAT pool (fill-first, quota guard, JT auto-refresh)
                ├── Cosy request signing (RSA + AES, prod parity)
                └── Encode=1 body transport (custom base64 alphabet + rotation)
                      └── https://api2.qoder.sh/... (upstream)
```

## Features

- **OpenAI-compatible surface** — `/v1/chat/completions` (stream + non-stream), `/v1/models`,
  `/usage`, `/health`; usage/cache/credits passed through in OpenAI shape.
- **PAT → JT pool** — PATs (from `QODER_KEYS`) are exchanged for short-lived job tokens (JT);
  JTs are auto re-exchanged before expiry; exhausted/quota-low slots soft-disable and walk on.
- **Cosy signing parity** — port of the upstream signing module (RSA-OAEP wrap + AES-GCM body,
  custom alphabet), plus the `Encode=1` request transform.
- **Stream safety** — an HTTP 200 is committed to 9router only after the first valid upstream
  event; pre-output slot failures are retryable on another account, committed-stream failures
  end the SSE gracefully.
- **No DB writes** — pure sidecar; 9router remains the only writer of its SQLite.

## Repo layout (clean architecture)

```
src/qoder/
├── core/           # pure, dependency-light building blocks
│   ├── config.py   # env-driven settings + URL builders
│   ├── cosy.py     # Cosy request signing (RSA/AES, prod chunk parity)
│   ├── encoding.py # Encode=1 body transform (custom base64 + rotation)
│   └── usage.py    # Qoder usage → OpenAI usage normalization
├── upstream/       # upstream-facing services (depend on core)
│   ├── body.py     # OpenAI messages → Qoder agent_chat payload
│   └── client.py   # chat / model-list calls, soft-error unwrap
├── adapters/pat/   # account/credential domain
│   └── pool.py     # PAT→JT slot pool (fill-first, cooldown, refresh)
├── http_api/       # delivery layer
│   └── server.py   # aiohttp app, routes, streaming, retry orchestration
└── __main__-style entry: python -m qoder.http_api.server
tools/pat/          # PAT lifecycle utilities (convert PAT → 9router connection)
deploy/             # systemd unit
docs/               # architecture notes + prod PAT insertion playbook
```

Dependency rule: `http_api → upstream/adapters → core`. `core` imports nothing from the
other layers.

## Quick start

```bash
python3 -m venv .venv && .venv/bin/pip install -e .
# or: .venv/bin/pip install -r requirements.txt

cat > .env <<'EOF'
QODER_KEYS=pt-xxxx,pt-yyyy          # comma-separated PATs
QODER_BIND_PORT=18092
EOF
chmod 600 .env

set -a && . ./.env && set +a
.venv/bin/python -m qoder.http_api.server
```

Register in 9router (via API, not raw SQLite) as an openai-compatible node:

- prefix `qoderkey`, `baseUrl: http://127.0.0.1:18092/v1`, dummy apiKey `qoder-local`

## PAT tools

- `tools/pat/inject_pat_to_9router.py` — convert a Qoder PAT (`pt-…`) into a 9router
  `providerConnections` row (`authType='pat'`): exchanges PAT→JT, fetches userinfo, writes
  the connection plus a `pat_connections.json` sidecar for refreshing.
- `tools/pat/refresh_pat_connections.py` — re-exchange PAT→JT before expiry and update the
  9router rows; idempotent, safe on a 30-min cron.

```bash
pip install requests
QODER_9ROUTER_DB=/path/to/data.sqlite python3 tools/pat/inject_pat_to_9router.py <PAT> [priority] [email]
QODER_9ROUTER_DB=/path/to/data.sqlite python3 tools/pat/refresh_pat_connections.py   # or --force
```

## Configuration

All settings are env vars (see `src/qoder/core/config.py`); key ones:

| Var | Default | Meaning |
|-----|---------|---------|
| `QODER_KEYS` | — | comma-separated PATs (required) |
| `QODER_BIND_HOST` / `QODER_BIND_PORT` | `127.0.0.1` / `18091` | loopback bind |
| `QODER_PREFER_HOST` | `api2.qoder.sh` | upstream chat host |
| `QODER_TIMEOUT` / `QODER_CONNECT_TIMEOUT` | `180` / `15` | upstream timeouts (s) |
| `QODER_CONCURRENCY` | `32` | max in-flight upstream requests |
| `QODER_KEY_COOLDOWN_S` | `30` | slot cooldown after failure |
| `QODER_JT_REFRESH_WINDOW_S` / `QODER_JT_REFRESH_INTERVAL_S` | `7200` / `300` | JT refresh policy |
| `QODER_QUOTA_GUARD_INTERVAL_S` / `QODER_QUOTA_SOFT_DISABLE_PCT` | `60` / `5.0` | quota guard |

## Deployment

`deploy/qoder-adapter-green.service` is the systemd unit shape used in prod
(`EnvironmentFile` + venv interpreter, `NoNewPrivileges`, `ProtectSystem=full`).

## Test policy

Develop and test locally or on a standby host. Obtain explicit approval before touching a
production 9router deployment.
