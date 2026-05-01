"""Local proxy server - exposes a unified API endpoint that routes to backends."""
import asyncio
import yaml
from aiohttp import web

from router import APIRouter


class ProxyServer:
    """aiohttp-based HTTP proxy that forwards requests through the APIRouter."""

    def __init__(self, api_router: APIRouter, host: str = "0.0.0.0", port: int = 8600):
        self.router = api_router
        self.host = host
        self.port = port
        self.app = web.Application()
        self._setup_routes()

    def _setup_routes(self):
        # Health endpoint for the proxy itself
        self.app.router.add_route("GET", "/router/health", self._health)
        # Stats endpoint
        self.app.router.add_route("GET", "/router/stats", self._stats)
        # Catch-all: forward everything else to backends
        self.app.router.add_route("*", "/{path:.*}", self._proxy)

    async def _health(self, request: web.Request) -> web.Response:
        stats = self.router.get_stats()
        available = sum(
            1 for b in stats["backends"] if b["status"] == "available"
        )
        return web.json_response({
            "status": "ok",
            "available_backends": available,
            "total_backends": len(stats["backends"]),
        })

    async def _stats(self, request: web.Request) -> web.Response:
        return web.json_response(self.router.get_stats())

    async def _proxy(self, request: web.Request) -> web.Response:
        # Read body
        body = await request.read()

        # Collect headers
        headers = dict(request.headers)

        # Build path
        path = f"/{request.match_info['path']}"
        query_string = request.query_string

        # Extract task hash for sticky routing (case-insensitive)
        task_hash = request.headers.get("X-QZ-Task-Hash")
        if not task_hash or task_hash == "-1":
            task_hash = None

        # Route through the APIRouter
        status, resp_headers, resp_body = await self.router.handle_request(
            method=request.method,
            path=path,
            headers=headers,
            body=body,
            query_string=query_string,
            task_hash=task_hash,
        )

        # Build response
        response = web.Response(
            status=status,
            body=resp_body,
        )

        # Copy relevant response headers
        for key in ("content-type", "x-request-id"):
            if key in resp_headers:
                response.headers[key] = resp_headers[key]

        return response

    async def start(self):
        runner = web.AppRunner(self.app)
        await runner.setup()
        site = web.TCPSite(runner, self.host, self.port)
        await site.start()
        return runner

    async def stop(self, runner):
        await runner.cleanup()
