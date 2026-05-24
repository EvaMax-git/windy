"""Simple in-memory rate limiter for API endpoints (A5 MVP).

Uses a sliding-window counter per client IP. For production,
replace with Redis-backed implementation.
"""

from __future__ import annotations

import time
from collections import defaultdict
from typing import Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

_MAX_TRACKED_IPS = 10_000


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Simple rate limiter middleware.

    Limits requests per client IP per window. Returns 429 when exceeded.
    """

    def __init__(
        self,
        app,
        max_requests: int = 60,
        window_seconds: int = 60,
        path_prefix: str = "/api/v4",
    ):
        super().__init__(app)
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.path_prefix = path_prefix
        self._requests: dict[str, list[float]] = defaultdict(list)
        self._last_eviction: float = time.time()

    def _evict_stale(self) -> None:
        """Remove IPs with no recent activity. Runs at most once per window."""
        now = time.time()
        if now - self._last_eviction < self.window_seconds:
            return
        self._last_eviction = now
        cutoff = now - self.window_seconds * 2
        stale_ips = [
            ip for ip, timestamps in self._requests.items()
            if not timestamps or timestamps[-1] < cutoff
        ]
        for ip in stale_ips:
            del self._requests[ip]

        # Hard cap: if still too many IPs, drop oldest half
        if len(self._requests) > _MAX_TRACKED_IPS:
            sorted_ips = sorted(
                self._requests.items(),
                key=lambda kv: kv[1][-1] if kv[1] else 0,
            )
            for ip, _ in sorted_ips[: len(sorted_ips) // 2]:
                del self._requests[ip]

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        # Only rate-limit API endpoints
        if not request.url.path.startswith(self.path_prefix):
            return await call_next(request)

        # Skip rate limiting for health checks
        if request.url.path.startswith("/health"):
            return await call_next(request)

        client_ip = request.client.host if request.client else "unknown"
        now = time.time()
        window_start = now - self.window_seconds

        # Periodic eviction
        self._evict_stale()

        # Clean old entries for this IP
        self._requests[client_ip] = [
            t for t in self._requests[client_ip] if t > window_start
        ]

        # Check limit
        if len(self._requests[client_ip]) >= self.max_requests:
            from fastapi.responses import JSONResponse
            return JSONResponse(
                status_code=429,
                content={
                    "error_code": "rate_limit_exceeded",
                    "message": f"请求过于频繁，请 {self.window_seconds} 秒后再试",
                    "retry_after": self.window_seconds,
                },
                headers={"Retry-After": str(self.window_seconds)},
            )

        # Record request
        self._requests[client_ip].append(now)

        response = await call_next(request)
        # Add rate limit headers
        remaining = max(0, self.max_requests - len(self._requests[client_ip]))
        response.headers["X-RateLimit-Limit"] = str(self.max_requests)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        response.headers["X-RateLimit-Reset"] = str(int(now + self.window_seconds))

        return response
