#!/usr/bin/env python3
"""
Refresh PAT-derived 9router connections before JT expires.

For each entry in pat_connections.json:
  - If JT expires within next 2 hours: re-exchange PAT -> new JT, update DB.

Idempotent — safe to run on cron every 30 min.

Usage:
  QODER_9ROUTER_DB=/path/to/data.sqlite python3 refresh_pat_connections.py [--force]
"""
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

import requests

OPENAPI = "https://openapi.qoder.sh"
UA = "qodercli/1.0.47"
TIMEOUT = 20
REFRESH_WINDOW_S = 2 * 3600  # refresh if JT expires within 2h

HERE = Path(__file__).resolve().parent
SIDECAR = HERE / "pat_connections.json"


def exchange_pat(pat: str) -> dict:
    r = requests.post(
        f"{OPENAPI}/api/v1/jobToken/exchange",
        headers={"Authorization": f"Bearer {pat}", "Content-Type": "application/json", "User-Agent": UA},
        json={"personal_token": pat},
        timeout=TIMEOUT,
    )
    if r.status_code != 200:
        raise RuntimeError(f"exchange failed: HTTP {r.status_code}: {r.text[:200]}")
    d = r.json()
    return {
        "jt": d["token"],
        "jrt": d.get("refresh_token", ""),
        "jt_expires_at_iso": d.get("expires_at"),
        "jt_expires_in_ms": d.get("expires_in", 86400000),
    }


def update_db(db_path: str, conn_id: str, email: str, jt: str, jrt: str, jt_expires_iso: str, jt_expires_ms: int):
    """Read current data, update accessToken/refreshToken/expiresAt, write back."""
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT data FROM providerConnections WHERE id=?", (conn_id,))
    row = cur.fetchone()
    if not row:
        raise RuntimeError(f"connection id {conn_id} not found in DB")
    data = json.loads(row[0])
    data["accessToken"] = jt
    data["refreshToken"] = jrt
    data["expiresAt"] = jt_expires_iso
    data["expiresIn"] = jt_expires_ms // 1000
    if "providerSpecificData" not in data:
        data["providerSpecificData"] = {}
    data["providerSpecificData"]["jtExpiresAt"] = int(time.time() * 1000) + jt_expires_ms
    data["providerSpecificData"]["jtExpiresAtIso"] = jt_expires_iso
    cur.execute(
        "UPDATE providerConnections SET data=?, updatedAt=? WHERE id=?",
        (json.dumps(data), int(time.time() * 1000), conn_id),
    )
    conn.commit()
    conn.close()
    return data


def main():
    db_path = os.environ.get("QODER_9ROUTER_DB")
    if not db_path:
        print("[!] QODER_9ROUTER_DB not set", file=sys.stderr)
        sys.exit(2)
    force = "--force" in sys.argv

    if not SIDECAR.exists():
        print(f"[!] no sidecar at {SIDECAR}")
        sys.exit(0)

    entries = json.load(open(SIDECAR))
    if not isinstance(entries, list):
        entries = [entries]

    now_ms = int(time.time() * 1000)
    refreshed = 0
    failed = 0

    for e in entries:
        email = e.get("email", "?")
        conn_id = e.get("conn_id")
        pat = e.get("pat")
        jt_expires_ms = e.get("jt_expires_ms", 0)
        time_left_s = (jt_expires_ms - now_ms) / 1000

        if not force and time_left_s > REFRESH_WINDOW_S:
            print(f"[~] {email}: JT still valid ({time_left_s/3600:.1f}h left) — skipping")
            continue

        if time_left_s < 0:
            print(f"[!] {email}: JT EXPIRED {abs(time_left_s)/3600:.1f}h ago — refreshing now")
        else:
            print(f"[~] {email}: JT expires in {time_left_s/3600:.1f}h — refreshing")

        try:
            ex = exchange_pat(pat)
            data = update_db(
                db_path,
                conn_id,
                email,
                ex["jt"],
                ex["jrt"],
                ex["jt_expires_at_iso"],
                ex["jt_expires_in_ms"],
            )
            e["jt"] = ex["jt"]
            e["jrt"] = ex["jrt"]
            e["jt_expires_at_iso"] = ex["jt_expires_at_iso"]
            e["jt_expires_ms"] = now_ms + ex["jt_expires_in_ms"]
            print(f"    [+] new JT: {ex['jt']}")
            print(f"    [+] expires: {ex['jt_expires_at_iso']}")
            refreshed += 1
        except Exception as exc:
            print(f"    [!] refresh failed: {exc}")
            failed += 1

    # write back updated sidecar
    with open(SIDECAR, "w") as f:
        json.dump(entries, f, indent=2)

    print()
    print(f"=== SUMMARY: refreshed={refreshed} failed={failed} total={len(entries)} ===")


if __name__ == "__main__":
    main()
