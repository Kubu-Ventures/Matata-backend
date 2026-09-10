"""Async load driver.

Authenticates a pool of anonymous sessions, submits the planned reports at a
target concurrency, and records per-request timing + HTTP outcome. Photo-less
plans that carry ``has_photo`` still exercise the metadata path.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx

from simulation.scenario import PlannedReport


@dataclass
class RequestResult:
    idx: int
    identity: int
    http_status: int
    report_id: Optional[str]
    t_submit_start: float
    latency_ms: float
    error: Optional[str] = None
    moderation_blocked: bool = False


@dataclass
class RunOutput:
    results: list[RequestResult] = field(default_factory=list)
    wall_start: float = 0.0
    wall_end: float = 0.0
    token_count: int = 0

    @property
    def submitted_ok(self) -> list[RequestResult]:
        return [r for r in self.results if r.report_id]

    @property
    def throughput_rps(self) -> float:
        dur = max(1e-6, self.wall_end - self.wall_start)
        return len(self.submitted_ok) / dur


async def _get_tokens(base: str, n: int, client: httpx.AsyncClient) -> list[str]:
    async def one() -> Optional[str]:
        try:
            r = await client.post(f"{base}/api/v1/auth/anonymous")
            r.raise_for_status()
            d = r.json()
            return d.get("session_token") or d.get("token")
        except Exception:
            return None

    toks = await asyncio.gather(*[one() for _ in range(n)])
    return [t for t in toks if t]


def _multipart(plan: PlannedReport):
    meta = {
        "crisis_type": plan.crisis_type,
        "infrastructure_type": plan.infrastructure_type,
        "damage_severity": plan.damage_severity,
        "lat": plan.lat,
        "lng": plan.lng,
        "gps_accuracy_m": plan.gps_accuracy_m,
        "landmark_description": plan.landmark_description,
        "electricity_status": plan.electricity_status,
        "health_services_status": plan.health_services_status,
        "most_pressing_needs": plan.most_pressing_needs or None,
        "debris_clearing_needed": plan.debris_clearing_needed,
    }
    meta = {k: v for k, v in meta.items() if v is not None}
    data = {"metadata": json.dumps(meta)}
    files = None
    if plan.has_photo and plan.image_bytes:
        files = {"photo": (f"sim_{plan.idx}.jpg", plan.image_bytes, "image/jpeg")}
    return data, files


async def run_load(
    *,
    base: str,
    plans: list[PlannedReport],
    concurrency: int,
    token_pool: int = 40,
    lang_in_query: bool = True,
) -> RunOutput:
    limits = httpx.Limits(max_connections=concurrency + 10,
                          max_keepalive_connections=concurrency + 10)
    out = RunOutput()
    async with httpx.AsyncClient(timeout=60.0, limits=limits) as client:
        tokens = await _get_tokens(base, token_pool, client)
        if not tokens:
            raise RuntimeError("could not obtain any anonymous session tokens")
        out.token_count = len(tokens)

        sem = asyncio.Semaphore(concurrency)

        async def submit(i: int, plan: PlannedReport) -> RequestResult:
            token = tokens[i % len(tokens)]
            data, files = _multipart(plan)
            url = f"{base}/api/v1/reports"
            if lang_in_query:
                url += f"?lang={plan.lang}"
            async with sem:
                t0 = time.perf_counter()
                ts = time.time()
                try:
                    r = await client.post(
                        url,
                        headers={"Authorization": f"Bearer {token}"},
                        data=data,
                        files=files,
                    )
                    dt = (time.perf_counter() - t0) * 1000
                    rid = None
                    blocked = False
                    if r.status_code == 201:
                        rid = r.json().get("id")
                    elif r.status_code == 422:
                        # could be moderation block or validation; distinguish
                        body = r.text.lower()
                        blocked = "reject" in body or "could not be accepted" in body \
                            or "image" in body
                    return RequestResult(
                        idx=plan.idx, identity=plan.identity,
                        http_status=r.status_code, report_id=rid,
                        t_submit_start=ts, latency_ms=dt,
                        moderation_blocked=blocked,
                        error=None if r.status_code in (201, 422, 429) else r.text[:200],
                    )
                except Exception as e:  # noqa: BLE001
                    dt = (time.perf_counter() - t0) * 1000
                    return RequestResult(
                        idx=plan.idx, identity=plan.identity, http_status=0,
                        report_id=None, t_submit_start=ts, latency_ms=dt,
                        error=f"{type(e).__name__}: {e}",
                    )

        out.wall_start = time.time()
        tasks = [asyncio.create_task(submit(i, p)) for i, p in enumerate(plans)]
        out.results = list(await asyncio.gather(*tasks))
        out.wall_end = time.time()

    out.results.sort(key=lambda r: r.idx)
    return out
