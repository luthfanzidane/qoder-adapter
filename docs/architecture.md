# Architecture

## Layers

| Layer | Path | Depends on |
|-------|------|------------|
| core | `src/qoder/core` | stdlib + cryptography only |
| upstream | `src/qoder/upstream` | core |
| adapters/pat | `src/qoder/adapters/pat` | core |
| http_api | `src/qoder/http_api` | upstream, adapters, core |

Request flow: `http_api` picks a slot from the PAT pool (`adapters/pat`), builds the Qoder
payload (`upstream/body`) and signs it (`core/cosy`), applies the `Encode=1` transform
(`core/encoding`), streams upstream chunks back, and normalizes usage (`core/usage`).

## Key behaviors

- **Fill-first pool with soft-disable.** Slots are ordered; a failed slot cools down, an
  exhausted (quota guard) slot is marked done and the pool walks to the next head.
- **JT lifecycle.** PATs are long-lived; JTs last ~24h. A background task re-exchanges
  PAT→JT within the refresh window so the pool stays warm.
- **Delayed response commit.** For SSE, the sidecar reads the first upstream chunk before
  calling `prepare()`. If the slot soft-fails, another slot can retry before 9router sees
  any bytes. After commit, failures end the stream gracefully instead of retrying.
- **No DB writes.** The sidecar only serves HTTP. 9router integration happens through its
  openai-compatible node API, or through the offline tools in `tools/pat/`.

## Prod parity notes

- `core/cosy.py` ports the upstream signing module (RSA-OAEP key wrap + AES body cipher,
  Cosy version constants from the shipped client build).
- `core/encoding.py` reproduces the `Encode=1` transform: standard base64 → 3-block
  rotation → custom 64-char alphabet with `=`→`$`.
- `upstream/body.py` mirrors the agent_chat payload shape, including session rotation.
