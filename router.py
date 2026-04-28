"""Core API Router - load balancing, health checking, request routing."""
import asyncio
import random
import time
import uuid
from typing import Optional, List

import aiohttp

from models import Backend, BackendStatus
from logger import RoutingLogger


class LoadBalancer:
    """Selects the least-loaded schedulable backend.
    
    Strategy:
    - Filter to only schedulable (available) backends.
    - Pick the one with the lowest current_load.
    - If all have the same load, pick randomly among them.
    """

    @staticmethod
    def select_backend(backends: List[Backend]) -> Optional[Backend]:
        schedulable = [b for b in backends if b.is_schedulable]
        if not schedulable:
            return None

        min_load = min(b.current_load for b in schedulable)
        min_load_backends = [b for b in schedulable if b.current_load == min_load]

        if len(min_load_backends) == 1:
            return min_load_backends[0]
        # All same load or multiple with min load -> random
        return random.choice(min_load_backends)


class HealthChecker:
    """Periodically checks backend health and updates status.
    
    - Sends GET /v1/models to each backend as a health probe.
    - If a backend returns consecutive failures >= failure_threshold -> mark UNAVAILABLE.
    - If an UNAVAILABLE backend starts responding again -> mark RECOVERING, then AVAILABLE
      after recovery_threshold consecutive successes.
    """

    def __init__(self, backends: List[Backend], api_key: str, config: dict,
                 logger: RoutingLogger, proxy_url: str = ""):
        self.backends = backends
        self.api_key = api_key
        self.interval = config.get("interval", 10)
        self.timeout = config.get("timeout", 5)
        self.failure_threshold = config.get("failure_threshold", 5)
        self.recovery_threshold = config.get("recovery_threshold", 2)
        self.logger = logger
        self.proxy_url = proxy_url or None
        self._task: Optional[asyncio.Task] = None
        self._session: Optional[aiohttp.ClientSession] = None

    async def start(self):
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.timeout)
        )
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._session:
            await self._session.close()

    async def _run_loop(self):
        while True:
            try:
                await asyncio.sleep(self.interval)
                await self._check_all()
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.console.error(f"Health check loop error: {e}")

    async def _check_all(self):
        tasks = [self._check_one(b) for b in self.backends]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _check_one(self, backend: Backend):
        old_status = backend.status
        try:
            headers = {"Authorization": f"Bearer {self.api_key}"}
            async with self._session.get(
                f"{backend.url}/v1/models",
                headers=headers,
                proxy=self.proxy_url,
            ) as resp:
                if resp.status < 500:
                    self._on_success(backend)
                else:
                    self._on_failure(backend, f"HTTP {resp.status}")
        except Exception as e:
            self._on_failure(backend, str(e))

        new_status = backend.status
        if old_status != new_status:
            self.logger.log_health_change(
                backend.name, old_status.value, new_status.value
            )

    def _on_success(self, backend: Backend):
        if backend.status == BackendStatus.UNAVAILABLE:
            backend.mark_recovering()
            backend.consecutive_successes = 1
        elif backend.status == BackendStatus.RECOVERING:
            backend.consecutive_successes += 1
            if backend.consecutive_successes >= self.recovery_threshold:
                backend.mark_available()
        else:
            backend.consecutive_failures = 0
            backend.consecutive_successes += 1

    def _on_failure(self, backend: Backend, error: str):
        backend.consecutive_successes = 0
        backend.consecutive_failures += 1
        if backend.consecutive_failures >= self.failure_threshold:
            if backend.status != BackendStatus.UNAVAILABLE:
                backend.mark_unavailable()


class APIRouter:
    """Main API Router that ties together load balancing, health checking, and proxying."""

    def __init__(self, config: dict):
        self.config = config
        self.api_key = config["api_key"]
        self.timeout = config["request"].get("timeout", 120)
        self.max_retries = config["request"].get("max_retries", 2)
        self.proxy_url = config.get("http_proxy", "") or None

        # Initialize backends
        self.backends: List[Backend] = []
        for bc in config["backends"]:
            self.backends.append(Backend(name=bc["name"], url=bc["url"]))

        # Initialize components
        self.logger = RoutingLogger(log_dir=config.get("log_dir", "logs"))
        self.load_balancer = LoadBalancer()
        self.health_checker = HealthChecker(
            self.backends, self.api_key, config["health_check"],
            self.logger, proxy_url=self.proxy_url,
        )

        # Stats
        self.total_proxied_requests = 0
        self.total_proxied_success = 0
        self.total_proxied_fail = 0

    async def start(self):
        await self.health_checker.start()
        self.logger.log_startup(
            [{"name": b.name, "url": b.url} for b in self.backends],
            self.config["proxy"]["host"],
            self.config["proxy"]["port"],
        )
        if self.proxy_url:
            self.logger.console.info(f"Using HTTP proxy: {self.proxy_url}")

    async def stop(self):
        await self.health_checker.stop()

    async def handle_request(
        self,
        method: str,
        path: str,
        headers: dict,
        body: bytes,
        query_string: str = "",
    ) -> tuple:
        """Route a request to the best available backend.

        Returns (status_code, response_headers, response_body_bytes).
        Retries on a different backend up to max_retries times on failure.
        """
        request_id = str(uuid.uuid4())[:8]
        self.total_proxied_requests += 1

        attempts = 0
        last_error = ""

        while attempts <= self.max_retries:
            backend = self.load_balancer.select_backend(self.backends)
            if backend is None:
                return (
                    503,
                    {"Content-Type": "application/json"},
                    b'{"error": "No available backends"}',
                )

            backend.record_request_sent()
            start_time = time.time()
            self.logger.log_request_start(request_id, method, path, backend.name, backend.url)

            try:
                status, resp_headers, resp_body = await self._proxy_to_backend(
                    backend, method, path, headers, body, query_string
                )
                elapsed = time.time() - start_time
                success = 200 <= status < 500
                backend.record_response_received(elapsed, success)
                self.logger.log_request_end(
                    request_id, backend.name, status, elapsed, success
                )

                if success:
                    self.total_proxied_success += 1
                    return status, resp_headers, resp_body
                else:
                    last_error = f"HTTP {status}"
                    # 4xx errors are client errors, don't retry
                    if 400 <= status < 500:
                        self.total_proxied_fail += 1
                        return status, resp_headers, resp_body
            except Exception as e:
                elapsed = time.time() - start_time
                backend.record_response_received(elapsed, False, str(e))
                self.logger.log_request_end(
                    request_id, backend.name, 0, elapsed, False, str(e)
                )
                last_error = str(e)

            attempts += 1

        self.total_proxied_fail += 1
        return (
            502,
            {"Content-Type": "application/json"},
            f'{{"error": "All backends failed after retries. Last error: {last_error}"}}'.encode(),
        )

    async def _proxy_to_backend(
        self,
        backend: Backend,
        method: str,
        path: str,
        headers: dict,
        body: bytes,
        query_string: str,
    ) -> tuple:
        """Send the actual HTTP request to a backend."""
        url = f"{backend.url}{path}"
        if query_string:
            url = f"{url}?{query_string}"

        # Build clean headers
        fwd_headers = {}
        skip = {"host", "content-length", "transfer-encoding"}
        for k, v in headers.items():
            if k.lower() not in skip:
                fwd_headers[k] = v

        # Override Authorization with the shared key
        fwd_headers["Authorization"] = f"Bearer {self.api_key}"

        timeout = aiohttp.ClientTimeout(total=self.timeout)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.request(
                method=method,
                url=url,
                headers=fwd_headers,
                data=body if body else None,
                proxy=self.proxy_url,
            ) as resp:
                resp_body = await resp.read()
                resp_headers = dict(resp.headers)
                # Clean response headers
                resp_headers.pop("transfer-encoding", None)
                resp_headers.pop("content-encoding", None)
                return resp.status, resp_headers, resp_body

    def get_stats(self) -> dict:
        """Get current router statistics including all backend snapshots."""
        return {
            "total_proxied": self.total_proxied_requests,
            "total_success": self.total_proxied_success,
            "total_fail": self.total_proxied_fail,
            "backends": [b.get_snapshot() for b in self.backends],
        }
