"""Minimal Prometheus exposition without introducing a process-global collector dependency."""
import re
import threading
import time
from collections import defaultdict


class Metrics:
    def __init__(self):
        self._lock = threading.Lock()
        self._requests = defaultdict(int)
        self._duration_sum = defaultdict(float)
        self._duration_count = defaultdict(int)

    @staticmethod
    def route(path: str) -> str:
        return re.sub(r"/[0-9a-f]{8}-[0-9a-f-]{27,35}", "/:id", path, flags=re.IGNORECASE)

    def observe(self, method: str, path: str, status: int, duration: float) -> None:
        key = (method, self.route(path), str(status))
        with self._lock:
            self._requests[key] += 1
            self._duration_sum[key] += duration
            self._duration_count[key] += 1

    def render(self) -> str:
        with self._lock:
            lines = ["# HELP softnix_http_requests_total Total HTTP responses", "# TYPE softnix_http_requests_total counter"]
            for key, value in sorted(self._requests.items()):
                labels = f'method="{key[0]}",path="{key[1]}",status="{key[2]}"'
                lines.append(f"softnix_http_requests_total{{{labels}}} {value}")
            lines.extend(["# HELP softnix_http_request_duration_seconds HTTP request duration", "# TYPE softnix_http_request_duration_seconds summary"])
            for key, value in sorted(self._duration_sum.items()):
                labels = f'method="{key[0]}",path="{key[1]}",status="{key[2]}"'
                lines.append(f"softnix_http_request_duration_seconds_sum{{{labels}}} {value:.6f}")
                lines.append(f"softnix_http_request_duration_seconds_count{{{labels}}} {self._duration_count[key]}")
        return "\n".join(lines) + "\n"


metrics = Metrics()


def now() -> float:
    return time.perf_counter()
