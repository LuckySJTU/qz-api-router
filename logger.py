"""Logging module for API Router - records routing decisions and timing."""
import logging
import time
import json
import os
from datetime import datetime, timezone


class RoutingLogger:
    """Logs routing decisions: request time, target backend, response time."""

    def __init__(self, log_dir: str = "logs", quiet: bool = False):
        self.log_dir = log_dir
        os.makedirs(log_dir, exist_ok=True)

        # Routing log - detailed request/response tracking
        self.routing_logger = logging.getLogger("routing")
        self.routing_logger.setLevel(logging.INFO)
        self.routing_logger.propagate = False

        # Avoid duplicate handlers on re-init
        if not self.routing_logger.handlers:
            handler = logging.FileHandler(
                os.path.join(log_dir, "routing.log"),
                encoding="utf-8"
            )
            handler.setFormatter(logging.Formatter("%(message)s"))
            self.routing_logger.addHandler(handler)

        # Health check log
        self.health_logger = logging.getLogger("health")
        self.health_logger.setLevel(logging.INFO)
        self.health_logger.propagate = False

        if not self.health_logger.handlers:
            h = logging.FileHandler(
                os.path.join(log_dir, "health.log"),
                encoding="utf-8"
            )
            h.setFormatter(logging.Formatter("%(message)s"))
            self.health_logger.addHandler(h)

        # Console logger
        # When quiet=True (TUI mode), suppress stderr output to avoid
        # it flashing on screen and being overwritten by Textual redraws.
        self.console = logging.getLogger("console")
        self.console.setLevel(logging.INFO)
        if not self.console.handlers:
            if quiet:
                self.console.addHandler(logging.NullHandler())
            else:
                ch = logging.StreamHandler()
                ch.setFormatter(
                    logging.Formatter("[%(asctime)s] %(levelname)s %(message)s", datefmt="%H:%M:%S")
                )
                self.console.addHandler(ch)

    def log_request_start(self, request_id: str, method: str, path: str, backend_name: str, backend_url: str):
        """Log when a request is routed to a backend."""
        record = {
            "event": "request_start",
            "request_id": request_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "time_epoch": time.time(),
            "method": method,
            "path": path,
            "backend_name": backend_name,
            "backend_url": backend_url,
        }
        self.routing_logger.info(json.dumps(record))
        self.console.info(f"[{request_id}] -> {method} {path} -> {backend_name} ({backend_url})")

    def log_request_end(self, request_id: str, backend_name: str, status_code: int, response_time: float, success: bool, error: str = ""):
        """Log when a response is received from a backend."""
        record = {
            "event": "request_end",
            "request_id": request_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "time_epoch": time.time(),
            "backend_name": backend_name,
            "status_code": status_code,
            "response_time_ms": round(response_time * 1000, 1),
            "success": success,
            "error": error,
        }
        self.routing_logger.info(json.dumps(record))
        status_str = f"HTTP {status_code}" if success else f"ERROR: {error}"
        self.console.info(
            f"[{request_id}] <- {backend_name} {status_str} ({round(response_time * 1000)}ms)"
        )

    def log_health_change(self, backend_name: str, old_status: str, new_status: str):
        """Log when a backend's health status changes."""
        record = {
            "event": "health_change",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "backend_name": backend_name,
            "old_status": old_status,
            "new_status": new_status,
        }
        self.health_logger.info(json.dumps(record))
        self.console.warning(f"Health change: {backend_name} {old_status} -> {new_status}")

    def log_startup(self, backends: list, proxy_host: str, proxy_port: int):
        """Log router startup."""
        self.console.info(f"API Router started on {proxy_host}:{proxy_port}")
        for b in backends:
            self.console.info(f"  Backend: {b['name']} -> {b['url']}")
