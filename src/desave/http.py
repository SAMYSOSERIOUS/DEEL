"""One shared way to download things: polite, with retries and a timeout."""
from __future__ import annotations

import logging
import os
import time

import requests

log = logging.getLogger(__name__)

USER_AGENT = "desave/1.0 (research project; German electricity savings)"


class DownloadError(RuntimeError):
    pass


_session: requests.Session | None = None


def session() -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers["User-Agent"] = USER_AGENT
    return _session


def get(url: str, params: dict | None = None, timeout: float = 60, retries: int = 4,
        pause: float | None = None, ok_404: bool = False) -> requests.Response | None:
    """GET with retries. Returns None for a 404 when ``ok_404`` is set."""
    last = None
    for attempt in range(retries):
        try:
            r = session().get(url, params=params, timeout=timeout)
            if r.status_code == 404 and ok_404:
                return None
            if r.status_code in (429, 500, 502, 503, 504):
                raise DownloadError(f"{r.status_code} from {url}")
            r.raise_for_status()
            wait_s = float(os.environ.get("DESAVE_HTTP_PAUSE", "0.2")) if pause is None else pause
            if wait_s:
                time.sleep(wait_s)
            return r
        except (requests.RequestException, DownloadError) as e:
            last = e
            wait = 2 ** attempt
            log.warning("Download failed (%s), try %d/%d, waiting %ss", e, attempt + 1, retries, wait)
            time.sleep(wait)
    raise DownloadError(f"Could not download {url}: {last}")
