import os
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Literal, Optional

import httpx


StatusLiteral = Literal["healthy", "unhealthy", "unknown"]


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _normalize_base_url(base_url: str) -> str:
    u = (base_url or "").strip()
    if not u:
        return ""
    return u.rstrip("/")


def _models_url(base_url: str) -> str:
    """
    Best-effort to construct an OpenAI-compatible /v1/models URL.
    Accepts base_url like:
    - https://api.openai.com
    - https://api.openai.com/v1
    - https://poloai.top/v1
    """
    u = _normalize_base_url(base_url)
    if not u:
        return ""
    if u.endswith("/v1"):
        return f"{u}/models"
    return f"{u}/v1/models"


@dataclass
class HealthStatus:
    name: str
    status: StatusLiteral
    latency_ms: Optional[float]
    error: Optional[str]
    last_check: str
    quota_remaining: Optional[float] = None
    base_url: Optional[str] = None
    model: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class ModelHealthChecker:
    """
    Lightweight health checker for OpenAI-compatible endpoints.
    Uses GET /v1/models to validate:
      - DNS/network reachability
      - auth header accepted (non-401/403)
      - service responds within timeout
    """

    def __init__(self, *, timeout_s: int = 5):
        self.timeout_s = max(1, int(timeout_s))
        self._cache: Dict[str, HealthStatus] = {}

    def get_config(self) -> Dict[str, Dict[str, str]]:
        return {
            "openai": {
                "api_key_set": "true" if bool(os.getenv("OPENAI_API_KEY")) else "false",
                "base_url": os.getenv("OPENAI_BASE_URL", ""),
                "model": os.getenv("OPENAI_MODEL", ""),
            },
            "deepseek": {
                "api_key_set": "true" if bool(os.getenv("DEEPSEEK_API_KEY")) else "false",
                "base_url": os.getenv("DEEPSEEK_BASE_URL", ""),
                "model": os.getenv("DEEPSEEK_MODEL", ""),
            },
            "qwenvl": {
                "api_key_set": "true" if bool(os.getenv("QWENVL_API_KEY")) else "false",
                "base_url": os.getenv("QWENVL_BASE_URL", ""),
                "model": os.getenv("QWENVL_MODEL", ""),
            },
        }

    def get_cached(self) -> Dict[str, Dict[str, Any]]:
        return {k: v.to_dict() for k, v in self._cache.items()}

    async def check_one(self, name: str, *, base_url: str, api_key: str, model: str) -> HealthStatus:
        if not api_key:
            st = HealthStatus(
                name=name,
                status="unknown",
                latency_ms=None,
                error="api_key not set",
                last_check=_now_iso(),
                base_url=base_url or None,
                model=model or None,
            )
            self._cache[name] = st
            return st

        if not base_url and name != "openai":
            st = HealthStatus(
                name=name,
                status="unknown",
                latency_ms=None,
                error="base_url not set",
                last_check=_now_iso(),
                base_url=None,
                model=model or None,
            )
            self._cache[name] = st
            return st

        url = _models_url(base_url) or "https://api.openai.com/v1/models"
        headers = {"Authorization": f"Bearer {api_key}"}
        t0 = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                resp = await client.get(url, headers=headers)
            latency = (time.monotonic() - t0) * 1000
            if resp.status_code in (401, 403):
                st = HealthStatus(
                    name=name,
                    status="unhealthy",
                    latency_ms=latency,
                    error=f"auth failed ({resp.status_code})",
                    last_check=_now_iso(),
                    base_url=base_url or None,
                    model=model or None,
                )
            elif resp.status_code >= 400:
                st = HealthStatus(
                    name=name,
                    status="unhealthy",
                    latency_ms=latency,
                    error=f"http {resp.status_code}",
                    last_check=_now_iso(),
                    base_url=base_url or None,
                    model=model or None,
                )
            else:
                st = HealthStatus(
                    name=name,
                    status="healthy",
                    latency_ms=latency,
                    error=None,
                    last_check=_now_iso(),
                    base_url=base_url or None,
                    model=model or None,
                )
        except Exception as e:
            latency = (time.monotonic() - t0) * 1000
            st = HealthStatus(
                name=name,
                status="unhealthy",
                latency_ms=latency,
                error=f"{type(e).__name__}: {e}",
                last_check=_now_iso(),
                base_url=base_url or None,
                model=model or None,
            )

        self._cache[name] = st
        return st

    async def check_all(self) -> Dict[str, HealthStatus]:
        import asyncio

        cfg = self.get_config()
        key_map = {
            "openai": "OPENAI_API_KEY",
            "deepseek": "DEEPSEEK_API_KEY",
            "qwenvl": "QWENVL_API_KEY",
        }

        async def run_one(name: str, c: Dict[str, str]) -> tuple[str, HealthStatus]:
            api_key = os.getenv(key_map.get(name, ""), "")
            st = await self.check_one(
                name,
                base_url=c.get("base_url", ""),
                api_key=api_key,
                model=c.get("model", ""),
            )
            return name, st

        pairs = await asyncio.gather(*(run_one(name, c) for name, c in cfg.items()))
        return {k: v for k, v in pairs}
