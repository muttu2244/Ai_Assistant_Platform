from __future__ import annotations

import httpx


class CoreApiClient:
    def __init__(self, base_url: str, timeout_seconds: int = 20) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds

    async def readiness(self) -> dict:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.get(f"{self._base_url}/readiness")
            response.raise_for_status()
            return response.json()

    async def relay_generation(self, endpoint_path: str, payload: dict) -> dict:
        path = endpoint_path if endpoint_path.startswith("/") else f"/{endpoint_path}"
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(f"{self._base_url}{path}", json=payload)
            response.raise_for_status()
            return response.json()
