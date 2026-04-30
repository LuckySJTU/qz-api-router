"""Data models for API Router."""
import time
import threading
from dataclasses import dataclass, field
from enum import Enum
from collections import deque


class BackendStatus(Enum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    RECOVERING = "recovering"


@dataclass
class Backend:
    """Represents a single backend API endpoint."""
    name: str
    url: str
    status: BackendStatus = BackendStatus.AVAILABLE
    
    # Load tracking
    current_load: int = 0          # in-flight requests
    total_requests_sent: int = 0   # total requests sent
    total_responses_received: int = 0  # total responses received
    total_success: int = 0          # responses with status < 500
    total_fail: int = 0              # responses with status >= 500 or exception
    
    # Health tracking
    consecutive_failures: int = 0
    consecutive_successes: int = 0
    
    # Response time tracking (rolling window)
    response_times: deque = field(default_factory=lambda: deque(maxlen=100))
    
    # Error log (recent)
    recent_errors: deque = field(default_factory=lambda: deque(maxlen=20))
    
    # Lock for thread-safe operations
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def is_schedulable(self) -> bool:
        return self.status == BackendStatus.AVAILABLE

    @property
    def avg_response_time(self) -> float:
        if not self.response_times:
            return 0.0
        return sum(self.response_times) / len(self.response_times)

    def record_request_sent(self):
        with self._lock:
            self.current_load += 1
            self.total_requests_sent += 1

    def record_response_received(self, response_time: float, success: bool, error_msg: str = ""):
        with self._lock:
            self.current_load = max(0, self.current_load - 1)
            self.total_responses_received += 1
            self.response_times.append(response_time)
            if success:
                self.total_success += 1
                self.consecutive_failures = 0
                self.consecutive_successes += 1
                if self.status == BackendStatus.RECOVERING:
                    # Will be set to AVAILABLE by health checker based on threshold
                    pass
            else:
                self.consecutive_successes = 0
                self.consecutive_failures += 1
                self.total_fail += 1
                if error_msg:
                    self.recent_errors.append({
                        "time": time.time(),
                        "error": error_msg
                    })

    def mark_unavailable(self):
        with self._lock:
            self.status = BackendStatus.UNAVAILABLE

    def mark_available(self):
        with self._lock:
            self.status = BackendStatus.AVAILABLE
            self.consecutive_failures = 0

    def mark_recovering(self):
        with self._lock:
            self.status = BackendStatus.RECOVERING

    def get_snapshot(self) -> dict:
        """Thread-safe snapshot of backend state."""
        with self._lock:
            return {
                "name": self.name,
                "url": self.url,
                "status": self.status.value,
                "current_load": self.current_load,
                "total_requests_sent": self.total_requests_sent,
                "total_responses_received": self.total_responses_received,
                "total_success": self.total_success,
                "total_fail": self.total_fail,
                "consecutive_failures": self.consecutive_failures,
                "avg_response_time_ms": round(self.avg_response_time * 1000, 1),
                "recent_errors": list(self.recent_errors)[-5:],
            }
