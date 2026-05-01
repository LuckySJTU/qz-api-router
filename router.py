"""Core API Router - load balancing, health checking, sticky routing, request routing."""
import asyncio
import random
import time
import uuid
import json
from typing import Optional, List, Dict

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


class StickyRouter:
    """Maps task hashes to fixed backends for session affinity.
    
    - First request from a task_hash picks a backend via load balancer and records it.
    - Subsequent requests with the same task_hash reuse the same backend.
    - If the assigned backend becomes unavailable, reassign on next request.
    - Only used when X-QZ-Task-Hash header is present.
    """

    def __init__(self, backends: List[Backend]):
        self._backends = backends
        self._mapping: Dict[str, str] = {}  # task_hash -> backend_name
        self._lock = asyncio.Lock()

    async def get_backend(self, task_hash: str, lb: LoadBalancer) -> Optional[Backend]:
        """Get the backend assigned to this task_hash, or assign a new one."""
        async with self._lock:
            backend_name = self._mapping.get(task_hash)
            if backend_name:
                # Look up the backend object and check if still schedulable
                for b in self._backends:
                    if b.name == backend_name and b.is_schedulable:
                        return b
                # Backend became unavailable, remove mapping and reassign
                del self._mapping[task_hash]

            # Assign a new backend via load balancer
            backend = lb.select_backend(self._backends)
            if backend:
                self._mapping[task_hash] = backend.name
            return backend

    async def release_backend(self, task_hash: str) -> None:
        """Remove the mapping for a task_hash (e.g. after task completes)."""
        async with self._lock:
            self._mapping.pop(task_hash, None)

    @property
    def active_mappings(self) -> int:
        return len(self._mapping)

    def get_mapping_snapshot(self) -> Dict[str, str]:
        """Return a copy of current mappings for stats."""
        return dict(self._mapping)


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
        self.interval = config.get("interval", 5)
        self.timeout = config.get("timeout", 3)
        self.failure_threshold = config.get("failure_threshold", 3)
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
    """Main API Router that ties together load balancing, sticky routing, health checking, and proxying."""

    def __init__(self, config: dict):
        self.config = config
        self.api_key = config["api_key"]

        # Timeout settings (fine-grained)
        req_cfg = config.get("request", {})
        self.timeout = req_cfg.get("timeout", 120)
        self.connect_timeout = req_cfg.get("connect_timeout", 5)
        self.max_retries = req_cfg.get("max_retries", 2)
        self.retry_delay = req_cfg.get("retry_delay", 0.3)

        self.proxy_url = config.get("http_proxy", "") or None

        # Connection pool settings
        pool_cfg = config.get("connection_pool", {})
        self.pool_limit = pool_cfg.get("limit", 200)
        self.pool_limit_per_host = pool_cfg.get("limit_per_host", 50)
        self.pool_keepalive = pool_cfg.get("keepalive_timeout", 30)

        # Initialize backends
        self.backends: List[Backend] = []
        for bc in config["backends"]:
            self.backends.append(Backend(name=bc["name"], url=bc["url"]))

        # Initialize components
        self.logger = RoutingLogger(log_dir=config.get("log_dir", "logs"), quiet=config.get("quiet_console", False))
        self.load_balancer = LoadBalancer()
        self.sticky_router = StickyRouter(self.backends)
        self.health_checker = HealthChecker(
            self.backends, self.api_key, config["health_check"],
            self.logger, proxy_url=self.proxy_url,
        )

        # Shared session + connector for connection pooling — created in start()
        self._connector: Optional[aiohttp.TCPConnector] = None
        self._session: Optional[aiohttp.ClientSession] = None

        # Stats
        self.total_proxied_requests = 0
        self.total_proxied_success = 0
        self.total_proxied_fail = 0

    async def start(self):
        # Create a single shared connector with connection pool
        self._connector = aiohttp.TCPConnector(
            limit=self.pool_limit,
            limit_per_host=self.pool_limit_per_host,
            keepalive_timeout=self.pool_keepalive,
            ttl_dns_cache=300,
            enable_cleanup_closed=True,
        )
        self._session = aiohttp.ClientSession(connector=self._connector)

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
        if self._session:
            await self._session.close()
        if self._connector:
            await self._connector.close()

    async def _select_backend(self, task_hash: Optional[str]) -> Optional[Backend]:
        """Select a backend: sticky routing if task_hash present, else load balancer."""
        if task_hash:
            return await self.sticky_router.get_backend(task_hash, self.load_balancer)
        return self.load_balancer.select_backend(self.backends)

    async def handle_request(
        self,
        method: str,
        path: str,
        headers: dict,
        body: bytes,
        query_string: str = "",
        task_hash: Optional[str] = None,
    ) -> tuple:
        """Route a request to the best available backend.

        If task_hash is provided, the same backend is reused for that task.
        Returns (status_code, response_headers, response_body_bytes).
        Retries on a different backend up to max_retries times on failure.
        """
        request_id = str(uuid.uuid4())[:8]
        self.total_proxied_requests += 1

        attempts = 0
        last_error = ""

        while attempts <= self.max_retries:
            backend = await self._select_backend(task_hash)
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
                    resp_body = self._inject_backend(resp_body, resp_headers, backend)
                    return status, resp_headers, resp_body
                else:
                    last_error = f"HTTP {status}"
                    # 4xx errors are client errors, don't retry
                    if 400 <= status < 500:
                        self.total_proxied_fail += 1
                        resp_body = self._inject_backend(resp_body, resp_headers, backend)
                        return status, resp_headers, resp_body
            except asyncio.TimeoutError as e:
                elapsed = time.time() - start_time
                backend.record_response_received(elapsed, False, f"Timeout: {e}")
                self.logger.log_request_end(
                    request_id, backend.name, 0, elapsed, False, f"Timeout: {e}"
                )
                last_error = f"Timeout: {e}"
            except aiohttp.ClientConnectorError as e:
                elapsed = time.time() - start_time
                backend.record_response_received(elapsed, False, f"Connection error: {e}")
                self.logger.log_request_end(
                    request_id, backend.name, 0, elapsed, False, f"Connection error: {e}"
                )
                last_error = f"Connection error: {e}"
            except aiohttp.ClientResponseError as e:
                elapsed = time.time() - start_time
                backend.record_response_received(elapsed, False, f"Response error: {e}")
                self.logger.log_request_end(
                    request_id, backend.name, e.status, elapsed, False, f"Response error: {e}"
                )
                last_error = f"Response error: {e}"
                # Don't retry on 4xx
                if 400 <= e.status < 500:
                    self.total_proxied_fail += 1
                    return e.status, {}, str(e).encode()
            except Exception as e:
                elapsed = time.time() - start_time
                backend.record_response_received(elapsed, False, str(e))
                self.logger.log_request_end(
                    request_id, backend.name, 0, elapsed, False, str(e)
                )
                last_error = str(e)

            attempts += 1
            # Short delay before retry to avoid hammering a struggling backend
            if attempts <= self.max_retries:
                await asyncio.sleep(self.retry_delay)

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
        """Send the actual HTTP request to a backend using the shared session."""
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

        # Fine-grained timeout: connect + total
        timeout = aiohttp.ClientTimeout(
            connect=self.connect_timeout,
            total=self.timeout,
        )
        async with self._session.request(
            method=method,
            url=url,
            headers=fwd_headers,
            data=body if body else None,
            proxy=self.proxy_url,
            timeout=timeout,
        ) as resp:
            resp_body = await resp.read()
            resp_headers = dict(resp.headers)
            # Clean response headers
            resp_headers.pop("transfer-encoding", None)
            resp_headers.pop("content-encoding", None)
            return resp.status, resp_headers, resp_body

    def _inject_backend(self, body: bytes, headers: dict, backend: Backend) -> bytes:
        """Inject router_api_backend field into JSON response body if possible."""
        content_type = headers.get("Content-Type", headers.get("content-type", ""))
        if "json" not in content_type:
            return body
        try:
            data = json.loads(body)
            if isinstance(data, dict):
                data["router_api_backend"] = backend.name
                return json.dumps(data).encode("utf-8")
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass
        return body

    def get_stats(self) -> dict:
        """Get current router statistics including all backend snapshots."""
        return {
            "total_proxied": self.total_proxied_requests,
            "total_success": self.total_proxied_success,
            "total_fail": self.total_proxied_fail,
            "sticky_mappings": self.sticky_router.active_mappings,
            "backends": [b.get_snapshot() for b in self.backends],
        }
