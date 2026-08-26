from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Any

import requests

log = logging.getLogger(__name__)

DEFAULT_UA = "chance-seeker/0.1 (+https://github.com/ericweirdfish-gif/chance_seeker)"


class RateLimiter:
    """滑动窗口限流：保证任意 period 秒内不超过 max_calls 次调用。"""

    def __init__(self, max_calls: int, period: float = 60.0) -> None:
        self.max_calls = max(1, max_calls)
        self.period = period
        self._calls: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            while True:
                now = time.monotonic()
                while self._calls and now - self._calls[0] > self.period:
                    self._calls.popleft()
                if len(self._calls) < self.max_calls:
                    self._calls.append(now)
                    return
                sleep_for = self.period - (now - self._calls[0]) + 0.01
                time.sleep(max(sleep_for, 0.01))


class HttpClient:
    """带限流、重试、超时的薄封装。所有采集器共用。"""

    def __init__(
        self,
        name: str,
        rate_limit: int = 60,
        period: float = 60.0,
        timeout: float = 20.0,
        retries: int = 3,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.name = name
        self.timeout = timeout
        self.retries = retries
        self.limiter = RateLimiter(rate_limit, period)
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": DEFAULT_UA, "Accept": "application/json"})
        if headers:
            self.session.headers.update(headers)

    def get_json(self, url: str, params: dict[str, Any] | None = None, **kwargs: Any) -> Any | None:
        return self._request("GET", url, params=params, **kwargs)

    def post_json(self, url: str, json_body: dict[str, Any] | None = None, **kwargs: Any) -> Any | None:
        return self._request("POST", url, json=json_body, **kwargs)

    def post_status(self, url: str, json_body: dict[str, Any] | None = None, **kwargs: Any) -> int | None:
        """只关心成功与否的场景（例如 Discord webhook 返回 204 空响应）。"""
        self.limiter.acquire()
        try:
            resp = self.session.post(url, json=json_body, timeout=self.timeout, **kwargs)
        except requests.RequestException as exc:
            log.warning("[%s] POST %s 失败: %s", self.name, url, exc)
            return None
        if resp.status_code >= 400:
            log.warning("[%s] POST %s -> HTTP %s: %s", self.name, url, resp.status_code, resp.text[:200])
        return resp.status_code

    def _request(self, method: str, url: str, **kwargs: Any) -> Any | None:
        backoff = 1.0
        for attempt in range(1, self.retries + 1):
            self.limiter.acquire()
            try:
                resp = self.session.request(method, url, timeout=self.timeout, **kwargs)
            except requests.RequestException as exc:
                log.warning("[%s] 请求异常 %s (%d/%d): %s", self.name, url, attempt, self.retries, exc)
                if attempt == self.retries:
                    return None
                time.sleep(backoff)
                backoff *= 2
                continue

            if resp.status_code == 429 or resp.status_code >= 500:
                retry_after = _retry_after(resp)
                log.warning(
                    "[%s] HTTP %s from %s (%d/%d), %.1fs 后重试",
                    self.name, resp.status_code, url, attempt, self.retries, retry_after or backoff,
                )
                if attempt == self.retries:
                    return None
                time.sleep(retry_after or backoff)
                backoff *= 2
                continue

            if resp.status_code >= 400:
                log.warning("[%s] HTTP %s from %s: %s", self.name, resp.status_code, url, resp.text[:200])
                return None

            try:
                return resp.json()
            except ValueError:
                log.warning("[%s] 响应不是合法 JSON: %s", self.name, resp.text[:200])
                return None
        return None


def _retry_after(resp: requests.Response) -> float | None:
    raw = resp.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return min(float(raw), 60.0)
    except ValueError:
        return None
