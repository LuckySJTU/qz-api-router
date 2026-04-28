"""Entry point - starts proxy server and optionally the TUI dashboard."""
import argparse
import asyncio
import signal
import sys

import yaml

from router import APIRouter
from proxy import ProxyServer


def load_config(path: str) -> dict:
    with open(path, "r") as f:
        return yaml.safe_load(f)


async def run_with_tui(config: dict):
    """Run proxy server + TUI dashboard together."""
    from tui import RouterDashboard

    api_router = APIRouter(config)
    await api_router.start()

    proxy = ProxyServer(
        api_router,
        host=config["proxy"]["host"],
        port=config["proxy"]["port"],
    )
    runner = await proxy.start()

    host = config["proxy"]["host"]
    port = config["proxy"]["port"]
    bind_display = "localhost" if host == "0.0.0.0" else host

    app = RouterDashboard(
        api_router,
        refresh_interval=config.get("tui", {}).get("refresh_interval", 1.0),
    )
    try:
        await app.run_async()
    finally:
        await proxy.stop(runner)
        await api_router.stop()


async def run_headless(config: dict):
    """Run proxy server only (no TUI) for headless / CI environments."""
    api_router = APIRouter(config)
    await api_router.start()

    proxy = ProxyServer(
        api_router,
        host=config["proxy"]["host"],
        port=config["proxy"]["port"],
    )
    runner = await proxy.start()

    host = config["proxy"]["host"]
    port = config["proxy"]["port"]
    bind_display = "localhost" if host == "0.0.0.0" else host
    print(f"API Router listening on http://{bind_display}:{port}")
    print(f"  Proxy endpoint:  http://{bind_display}:{port}/v1/chat/completions")
    print(f"  Health check:    http://{bind_display}:{port}/router/health")
    print(f"  Stats:           http://{bind_display}:{port}/router/stats")
    print("Press Ctrl+C to stop.")

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig_name in ("SIGINT", "SIGTERM"):
        try:
            loop.add_signal_handler(getattr(signal, sig_name), stop_event.set)
        except (NotImplementedError, AttributeError):
            pass

    await stop_event.wait()
    await proxy.stop(runner)
    await api_router.stop()


def main():
    parser = argparse.ArgumentParser(description="API Router - load-balancing proxy for LLM endpoints")
    parser.add_argument(
        "-c", "--config",
        default="config.yaml",
        help="Path to config.yaml (default: config.yaml)",
    )
    parser.add_argument(
        "--no-tui",
        action="store_true",
        help="Run without TUI dashboard (headless mode)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=None,
        help="Override request timeout in seconds (for debugging)",
    )
    args = parser.parse_args()

    config = load_config(args.config)

    # Allow CLI timeout override for debugging
    if args.timeout is not None:
        config.setdefault("request", {})["timeout"] = args.timeout

    if args.no_tui:
        asyncio.run(run_headless(config))
    else:
        asyncio.run(run_with_tui(config))


if __name__ == "__main__":
    main()
