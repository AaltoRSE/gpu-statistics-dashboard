"""Minimal synchronous Prometheus HTTP API v1 client with a small TTL cache."""

import base64
import threading
import time

import httpx


class PrometheusError(Exception):
    pass


class PromClient:
    def __init__(self, api_base, username="", password="", timeout=30):
        self.api_base = api_base
        self.timeout = timeout
        token = "%s:%s" % (username, password)
        self.headers = {
            "Authorization": "Basic " + base64.b64encode(token.encode()).decode()
        }
        self._client = httpx.Client(timeout=timeout)
        self._cache = {}
        self._lock = threading.Lock()
    def request(self, endpoint, params, ttl):
        key = (endpoint, tuple(sorted(params.items())))
        with self._lock:
            hit = self._cache.get(key)
            if hit and hit[0] > time.monotonic():
                return hit[1]
        url = self.api_base + endpoint
        try:
            resp = self._client.get(url, params=params, headers=self.headers)
        except httpx.HTTPError as exc:
            raise PrometheusError("cannot reach Prometheus: %s" % exc) from exc
        if resp.status_code in (401, 403):
            raise PrometheusError("Prometheus authentication failed (%s)" % resp.status_code)
        if resp.status_code != 200:
            raise PrometheusError("Prometheus HTTP error %s" % resp.status_code)
        try:
            payload = resp.json()
        except ValueError as exc:
            raise PrometheusError("Prometheus returned invalid JSON") from exc
        if payload.get("status") != "success":
            message = payload.get("error") or payload.get("errorType") or "unknown error"
            raise PrometheusError("Prometheus query failed: %s" % message)
        data = payload.get("data", {})
        with self._lock:
            if len(self._cache) > 512:
                self._cache.clear()
            self._cache[key] = (time.monotonic() + ttl, data)
        return data

    def query_range(self, query, start, end, step):
        data = self.request(
            "/query_range",
            {"query": query, "start": int(start), "end": int(end), "step": int(step)},
            ttl=60,
        )
        return data.get("result", [])

    def query_instant(self, query, time=None):
        params = {"query": query}
        if time is not None:
            params["time"] = int(time)
        data = self.request("/query", params, ttl=20)
        return data.get("result", [])

    def label_values(self, name):
        data = self.request("/label/%s/values" % name, {}, ttl=300)
        return data.get("result", [])

    def close(self):
        self._client.close()
