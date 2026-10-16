"""OpenAI-compatible Qoder PAT sidecar (kiro-adapter shape; prod Cosy/Encode path)."""
from __future__ import annotations

import asyncio
import json
import logging
import time

import httpx
from aiohttp import web

from qoder.core import config as config
from qoder.upstream import client as qoder_client
from qoder.upstream.client import UpstreamError
from qoder.adapters.pat.pool import load_pool_from_env, bootstrap_slot, refresh_if_needed

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("qoder.server")


class Adapter:
    def __init__(self):
        self.http = httpx.AsyncClient(
            timeout=httpx.Timeout(config.UPSTREAM_TIMEOUT, connect=config.CONNECT_TIMEOUT),
            http2=False,
        )
        self.pool = load_pool_from_env()
        self._sem: asyncio.Semaphore | None = None
        self._refresh_task: asyncio.Task | None = None
        self._quota_guard_task: asyncio.Task | None = None
        self._models_cache: list[dict] | None = None
        self._models_cache_at: float = 0.0

    @property
    def sem(self) -> asyncio.Semaphore:
        if self._sem is None:
            self._sem = asyncio.Semaphore(config.CONCURRENCY)
        return self._sem

    async def bootstrap(self) -> None:
        for slot in self.pool.all_slots():
            try:
                await bootstrap_slot(self.http, slot)
                log.info("bootstrapped %s user=%s", slot.email or slot.pat[:12], slot.user_id[:12] if slot.user_id else "?")
            except Exception as e:
                log.error("bootstrap failed %s: %s", slot.pat[:12], e)
                self.pool.advance(slot, f"bootstrap:{e}")

    async def refresh_loop(self) -> None:
        while True:
            await asyncio.sleep(config.JT_REFRESH_INTERVAL_S)
            for slot in self.pool.all_slots():
                if slot.exhausted:
                    continue
                try:
                    refreshed = await refresh_if_needed(self.http, slot)
                    if refreshed:
                        log.info("refreshed JT for %s", slot.email or slot.pat[:12])
                except Exception as e:
                    log.warning("refresh failed %s: %s", slot.pat[:12], e)
                    self.pool.advance(slot, f"refresh:{e}")

    async def _soft_disable_low_quota_heads(self) -> int:
        """Exhaust fill-first heads while remaining/total < soft-disable pct.

        Walks the current active head repeatedly so a streak of depleted PATs
        is skipped without waiting another guard interval.
        """
        disabled = 0
        # Cap walks to pool size to avoid pathological loops.
        for _ in range(len(self.pool.all_slots())):
            active = self.pool.active_slots()
            if not active:
                break
            slot = active[0]
            try:
                q = await qoder_client.fetch_quota_usage(self.http, slot)
            except Exception as e:
                log.warning(
                    "quota guard fetch failed head=%s: %s",
                    slot.email or slot.pat[:12],
                    e,
                )
                break
            uq = q.get("userQuota") if isinstance(q, dict) else None
            if not isinstance(uq, dict):
                break
            try:
                total = float(uq.get("total") or 0)
                remaining = float(uq.get("remaining") or 0)
            except (TypeError, ValueError):
                break
            if total <= 0:
                break
            pct = (remaining / total) * 100.0
            if pct >= config.QUOTA_SOFT_DISABLE_PCT:
                break
            reason = f"quota_soft_disable:{remaining:g}/{total:g}={pct:.2f}%<{config.QUOTA_SOFT_DISABLE_PCT:g}%"
            self.pool.mark_exhausted(slot, reason)
            disabled += 1
            log.warning(
                "quota guard exhausted head email=%s remaining=%s total=%s pct=%.2f",
                slot.email or slot.pat[:12],
                remaining,
                total,
                pct,
            )
        return disabled

    async def quota_guard_loop(self) -> None:
        # First pass soon after bootstrap so a depleted index-0 is not sticky.
        await asyncio.sleep(min(15, config.QUOTA_GUARD_INTERVAL_S))
        while True:
            try:
                await self._soft_disable_low_quota_heads()
            except Exception:
                log.exception("quota guard loop error")
            await asyncio.sleep(config.QUOTA_GUARD_INTERVAL_S)

    async def close(self) -> None:
        for task in (self._refresh_task, self._quota_guard_task):
            if task:
                task.cancel()
        await self.http.aclose()


def create_app() -> web.Application:
    ad = Adapter()

    async def on_start(_app: web.Application) -> None:
        await ad.bootstrap()
        ad._refresh_task = asyncio.create_task(ad.refresh_loop())
        ad._quota_guard_task = asyncio.create_task(ad.quota_guard_loop())

    async def on_cleanup(_app: web.Application) -> None:
        await ad.close()

    async def health(_req: web.Request) -> web.Response:
        return web.json_response(
            {
                "status": "ok",
                "prefer_host": config.PREFER_HOST,
                "key_count": len(ad.pool.all_slots()),
                "active": len(ad.pool.active_slots()),
                "pool": ad.pool.report(),
            }
        )

    async def models(_req: web.Request) -> web.Response:
        now = time.time()
        if ad._models_cache and (now - ad._models_cache_at) < 3600:
            return web.json_response({"object": "list", "data": ad._models_cache})
        rows = None
        for slot in ad.pool.active_slots():
            try:
                rows = await qoder_client.list_openai_models(ad.http, slot)
                break
            except Exception as e:
                log.warning("models fetch failed: %s", e)
        if not rows:
            rows = [
                {"id": m, "object": "model", "owned_by": "qoder"}
                for m in config.FALLBACK_MODELS
            ]
        ad._models_cache = rows
        ad._models_cache_at = now
        return web.json_response({"object": "list", "data": rows})

    async def usage(_req: web.Request) -> web.Response:
        """Per-slot trial credits (openapi quota/usage) — kiro /usage analogue."""
        out = []
        for slot in ad.pool.all_slots():
            entry = {
                "email": slot.email,
                "status": "exhausted" if slot.exhausted else ("active" if slot.jt_valid else "no_jt"),
                "plan_status": slot.plan_status,
            }
            if not slot.jt_valid:
                entry["error"] = "no valid JT"
                out.append(entry)
                continue
            try:
                q = await qoder_client.fetch_quota_usage(ad.http, slot)
                uq = q.get("userQuota") if isinstance(q, dict) else None
                entry["userQuota"] = uq
            except Exception as e:
                entry["error"] = str(e)[:200]
            out.append(entry)
        return web.json_response({"slots": out})

    async def chat(req: web.Request) -> web.StreamResponse | web.Response:
        try:
            body = await req.json()
        except web.HTTPRequestEntityTooLarge:
            return web.json_response({"error": {"message": "request body too large"}}, status=413)
        except Exception:
            return web.json_response({"error": {"message": "invalid json"}}, status=400)
        model = str(body.get("model") or "qmodel_latest")
        stream = bool(body.get("stream"))

        slots = ad.pool.active_slots()
        if not slots:
            return web.json_response(
                {"error": {"message": "no active qoder PAT/JT", "type": "no_capacity"}},
                status=503,
            )

        tried: set[int] = set()
        last_err: Exception | None = None
        for _ in range(len(ad.pool.all_slots())):
            slot = next((s for s in ad.pool.active_slots() if id(s) not in tried), None)
            if slot is None:
                break
            tried.add(id(slot))
            async with ad.sem:
                if req.transport is None or req.transport.is_closing():
                    raise ConnectionResetError("downstream closed before dispatch")
                response_committed = False
                try:
                    if stream:
                        # Do not commit HTTP 200 to 9router until the selected
                        # slot has produced a valid upstream event. Otherwise a
                        # pre-output slot failure followed by an account retry
                        # calls prepare() again on a closing aiohttp transport.
                        upstream = qoder_client.chat_stream(ad.http, slot, body, model)
                        try:
                            try:
                                first_chunk = await anext(upstream)
                            except StopAsyncIteration as e:
                                raise UpstreamError("qoder stream ended before first event", status=502) from e

                            # Qoder soft errors are OpenAI-shaped SSE chunks. They
                            # are retryable only before the downstream response is
                            # committed.
                            first_error = None
                            for first_line in first_chunk.decode("utf-8", errors="replace").splitlines():
                                if not first_line.startswith("data:"):
                                    continue
                                first_data = first_line[5:].strip()
                                if not first_data or first_data == "[DONE]":
                                    continue
                                try:
                                    first_event = json.loads(first_data)
                                except Exception:
                                    continue
                                if isinstance(first_event, dict) and first_event.get("error"):
                                    first_error = first_event["error"]
                                    break
                            if first_error is not None:
                                await upstream.aclose()
                                raise UpstreamError(str(first_error)[:500], status=502)

                            resp = web.StreamResponse(
                                status=200,
                                headers={
                                    "Content-Type": "text/event-stream",
                                    "Cache-Control": "no-cache",
                                    "Connection": "keep-alive",
                                },
                            )
                            await resp.prepare(req)
                            response_committed = True
                            try:
                                await resp.write(first_chunk)
                                async for chunk in upstream:
                                    await resp.write(chunk)
                            except (ConnectionResetError, asyncio.CancelledError):
                                raise
                            except Exception:
                                # A committed request cannot switch slots safely or
                                # prepare a second response. End the SSE stream best
                                # effort and let the caller retry the whole request.
                                log.exception("committed stream failed slot=%s", slot.email or slot.pat[:12])
                                try:
                                    err = {"error": {"type": "upstream_error", "message": "qoder stream interrupted"}}
                                    await resp.write(("data: " + json.dumps(err, separators=(",", ":")) + "\n\n").encode())
                                    await resp.write(b"data: [DONE]\n\n")
                                    await resp.write_eof()
                                except Exception:
                                    pass
                                return resp
                            ad.pool.record_success(slot)
                            await resp.write_eof()
                            return resp
                        finally:
                            try:
                                await upstream.aclose()
                            except Exception:
                                log.exception("upstream cleanup failed")
                    result = await qoder_client.chat_nonstream(ad.http, slot, body, model)
                    if isinstance(result, dict) and result.get("error"):
                        ad.pool.advance(slot, str(result["error"])[:120])
                        # try next slot
                        last_err = UpstreamError(str(result["error"]), status=502)
                        continue
                    ad.pool.record_success(slot)
                    return web.json_response(result)
                except (ConnectionResetError, asyncio.CancelledError):
                    # Downstream cancellation must not consume another upstream attempt.
                    raise
                except UpstreamError as e:
                    if response_committed:
                        raise
                    last_err = e
                    ad.pool.advance(slot, str(e)[:120])
                    if not e.retryable:
                        break
                except Exception as e:
                    if response_committed:
                        raise
                    last_err = e
                    ad.pool.advance(slot, str(e)[:120])
                    log.exception("chat failed slot=%s", slot.email or slot.pat[:12])

        msg = str(last_err) if last_err else "all qoder slots failed"
        return web.json_response({"error": {"message": msg}}, status=502)

    # Long agent transcripts can exceed aiohttp's 1 MiB default. Keep a
    # finite cap so large requests are accepted without unbounded buffering.
    app = web.Application(client_max_size=8 * 1024 * 1024)
    app.on_startup.append(on_start)
    app.on_cleanup.append(on_cleanup)
    app.router.add_get("/health", health)
    app.router.add_get("/v1/models", models)
    app.router.add_get("/usage", usage)
    app.router.add_post("/v1/chat/completions", chat)
    app["adapter"] = ad
    return app


def main() -> None:
    log.info("listening on %s:%s prefer_host=%s", config.BIND_HOST, config.BIND_PORT, config.PREFER_HOST)
    app = create_app()
    web.run_app(app, host=config.BIND_HOST, port=config.BIND_PORT)


if __name__ == "__main__":
    main()
