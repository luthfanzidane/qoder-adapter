# PAT insertion playbook (prod pattern, last verified 2026-10-15)

How a new batch of Qoder PATs is added to the running sidecar on the prod host
(`tencent-2`, blue env). Follow this exactly; the pool is only read at startup, so a
restart of the correct unit is required for new PATs to load.

## `QODER_KEYS` formats

`load_pool_from_env` accepts, in order of preference:

1. **JSON array** (the format prod uses): a single line, newest PATs **appended at the end**.

   ```
   QODER_KEYS=[{"pat":"pt-..."},{"pat":"pt-..."}]
   ```

   Each item may optionally carry `"email"`, `"name"`, `"user_id"`, `"machine_id"`,
   `"plan_status"`. Anything that does not start with `pt-` is skipped, so a stray
   comma/paste error degrades to "PAT missing" instead of corrupting the list.

2. Whitespace-separated bare PATs: `QODER_KEYS=pt-aaa pt-bbb` (split on whitespace only,
   not commas).

3. A single bare PAT string.

## Prod steps (blue env, port 18092)

```bash
# 1. Backup the env file first — always, with a timestamped name:
cp ~/qoder-adapter/.env.blue-qoder-vwg6-20260826 \
   ~/qoder-adapter/.env.blue-qoder-vwg6-20260826.bak-addpat-$(date -u +%Y%m%dT%H%M%SZ)

# 2. Edit QODER_KEYS in that file: parse the JSON array, append the new
#    {"pat":"pt-..."} objects at the END, keep it one line, keep file otherwise intact.

# 3. Validate the line before restarting (a broken JSON line kills the pool at startup):
python3 -c 'import json,os; json.loads(open(os.path.expanduser("~/qoder-adapter/.env.blue-qoder-vwg6-20260826")).read().split("QODER_KEYS=",1)[1].split("\n",1)[0].strip())'

# 4. Restart the matching unit (blue prod):
sudo systemctl restart qoder-adapter-blue-vwg6.service

# 5. Verify:
systemctl status qoder-adapter-blue-vwg6 --no-pager
curl -s http://127.0.0.1:18092/health | head
```

Rules:

- Restart **only the unit that reads that env file**. Green (`qoder-adapter-green.service`)
  reads `~/qoder-adapter/.env`; blue reads `.env.blue-qoder-vwg6-20260826`. Restarting green
  does nothing for the prod pool at 18092.
- The list is **fill-first**: the pool walks from the first slot; order in the array is
  consumption order. Append new PATs at the end unless you deliberately want them first.
- The restart is a sidecar restart, distinct from the Amanai gateway blue/green procedure;
  9router needs no restart (it reads its SQLite per request).
