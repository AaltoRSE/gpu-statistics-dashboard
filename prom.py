"""Minimal synchronous Prometheus HTTP API v1 client with a small TTL cache."""

import base64

import httpx

from cache import TtlCache


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
        self._cache = TtlCache(max_size=512)

    def request(self, endpoint, params, ttl):
        key = (endpoint, tuple(sorted(params.items())))

        def fetch():
            url = self.api_base + endpoint
            try:
                resp = self._client.get(url, params=params, headers=self.headers)
            except httpx.HTTPError as exc:
                raise PrometheusError("cannot reach Prometheus: %s" % exc) from exc
            if resp.status_code in (401, 403):
                raise PrometheusError(
                    "Prometheus authentication failed (%s)" % resp.status_code)
            if resp.status_code != 200:
                raise PrometheusError("Prometheus HTTP error %s" % resp.status_code)
            try:
                payload = resp.json()
            except ValueError as exc:
                raise PrometheusError("Prometheus returned invalid JSON") from exc
            if payload.get("status") != "success":
                message = (payload.get("error") or payload.get("errorType")
                           or "unknown error")
                raise PrometheusError("Prometheus query failed: %s" % message)
            return payload.get("data", {})

        # A failed fetch is never cached: get_or_set only stores the result
        # once fetch() returns, so an error propagates on every call until
        # a request actually succeeds.
        return self._cache.get_or_set(key, ttl, fetch)

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

    def clear_cache(self):
        """Drop the response cache; forced-refresh paths call this."""
        self._cache.clear()

    def close(self):
        self._client.close()
