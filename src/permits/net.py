"""
net.py — one HTTP transport for every source adapter, resilient to a moving proxy.

Outbound traffic here goes through a local agent proxy on 127.0.0.1. Its port is
not stable: the sandbox restarts the proxy and hands the new port to newly
launched processes through the environment. A process that is already running
never sees that update — `os.environ` was snapshotted at exec — so a long sweep
keeps dialing a dead port and every request fails with ECONNREFUSED. That is
what killed the first standard-permit sweep after 70 of 1,200 entities:
rebuilding `ProxyHandler()` per request was not enough, because the value it
reads is stale in exactly the case that matters.

So the port is treated as something to *discover* rather than something to be
told. When the configured proxy refuses a connection, `_discover()` walks the
listening TCP sockets in /proc/net and asks each one whether it is the agent
proxy (`/__agentproxy/status`). The one that answers becomes the new proxy for
the rest of the run, and the failed request is retried against it.

`get()` and `post()` are the entry points; both retry with backoff, and treat a
refused connection as a signal to re-discover before the next attempt.
"""

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

USER_AGENT = "viirs-nightfire-tool/permits (+https://github.com/tim03bo-ux/viirs-nightfire-tool)"

STATUS_PATH = "/__agentproxy/status"

# Backoff between attempts. Long enough at the tail to outlast a proxy restart,
# which is the failure this exists for.
RETRY_DELAYS = (2, 5, 15, 30, 60)

_PROXY = None       # cached proxy URL, or "" for "no proxy, go direct"
_PROXY_CHECKED = False


def _env_proxy():
    for key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        value = os.environ.get(key)
        if value:
            return value
    return ""


def _listening_ports():
    """Local TCP ports in LISTEN state, newest-looking first."""
    ports = set()
    for path in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            lines = open(path).read().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            # st == 0A is TCP_LISTEN; local_address is hex "IP:PORT".
            if len(fields) > 3 and fields[3] == "0A":
                try:
                    ports.add(int(fields[1].split(":")[1], 16))
                except (IndexError, ValueError):
                    continue
    return sorted(ports, reverse=True)


def _is_agent_proxy(port, timeout=3):
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{port}{STATUS_PATH}", timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", errors="replace")).get("enabled")
    except Exception:
        return False


def _discover():
    """Find the agent proxy's current port by asking every listener."""
    for port in _listening_ports():
        if _is_agent_proxy(port):
            return f"http://127.0.0.1:{port}"
    return ""


def current_proxy(rediscover=False):
    """The proxy URL to use now. Discovers a new port when the cached one died."""
    global _PROXY, _PROXY_CHECKED
    if rediscover or not _PROXY_CHECKED:
        found = _discover()
        if found:
            _PROXY = found
        elif not _PROXY_CHECKED:
            # Nothing answered the status probe: either there is no agent proxy
            # in this environment, or it is a plain proxy. Trust the environment.
            _PROXY = _env_proxy()
        _PROXY_CHECKED = True
    return _PROXY


def _opener(proxy):
    handler = urllib.request.ProxyHandler(
        {"http": proxy, "https": proxy} if proxy else {}
    )
    return urllib.request.build_opener(handler, urllib.request.HTTPCookieProcessor())


def new_jar():
    """A cookie-carrying opener for sessions that need one (TCEQ ColdFusion apps).

    Only the cookie jar is reused; the proxy is re-resolved per request, so a
    jar handed to a long sweep does not pin it to a dead port.
    """
    return urllib.request.HTTPCookieProcessor()


def _refused(exc):
    text = str(exc)
    return "Connection refused" in text or "Errno 111" in text


def request(url, data=None, headers=None, timeout=60, retries=None, jar=None,
            method=None):
    """Fetch a URL, re-discovering the proxy when the connection is refused.

    `jar` is an optional HTTPCookieProcessor to carry session cookies across
    calls. HTTP status errors (4xx/5xx) are raised immediately — retrying a 403
    only wastes the remote's time.
    """
    delays = RETRY_DELAYS if retries is None else RETRY_DELAYS[:retries]
    all_headers = {"User-Agent": USER_AGENT}
    if data is not None:
        all_headers["Content-Type"] = "application/x-www-form-urlencoded"
    all_headers.update(headers or {})

    last = None
    for attempt in range(len(delays) + 1):
        proxy = current_proxy()
        handlers = [urllib.request.ProxyHandler(
            {"http": proxy, "https": proxy} if proxy else {}
        )]
        handlers.append(jar or urllib.request.HTTPCookieProcessor())
        opener = urllib.request.build_opener(*handlers)
        req = urllib.request.Request(url, data=data, headers=all_headers,
                                     method=method)
        try:
            with opener.open(req, timeout=timeout) as response:
                return response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError:
            raise
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            last = exc
            if _refused(exc):
                current_proxy(rediscover=True)
            if attempt < len(delays):
                time.sleep(delays[attempt])
    raise last


def request_bytes(url, headers=None, timeout=120, retries=None, jar=None):
    """Same as `request`, for binary payloads (permit PDFs)."""
    delays = RETRY_DELAYS if retries is None else RETRY_DELAYS[:retries]
    all_headers = {"User-Agent": USER_AGENT}
    all_headers.update(headers or {})

    last = None
    for attempt in range(len(delays) + 1):
        proxy = current_proxy()
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy} if proxy else {}),
            jar or urllib.request.HTTPCookieProcessor(),
        )
        req = urllib.request.Request(url, headers=all_headers)
        try:
            with opener.open(req, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError:
            raise
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            last = exc
            if _refused(exc):
                current_proxy(rediscover=True)
            if attempt < len(delays):
                time.sleep(delays[attempt])
    raise last


def get(url, params=None, **kwargs):
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    return request(url, **kwargs)


def post(url, fields, **kwargs):
    return request(url, data=urllib.parse.urlencode(fields).encode("utf-8"), **kwargs)
