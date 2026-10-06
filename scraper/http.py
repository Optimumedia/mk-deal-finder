"""Polite HTTP client: rate limited, robots.txt aware, gives up on bot walls.

If a site answers with a CAPTCHA / bot challenge we stop and report it —
we never try to get around it.
"""
from __future__ import annotations

import logging
import random
import time
import urllib.robotparser
from urllib.parse import urlparse

import requests

log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0 Mobile Safari/537.36"
)
_CHALLENGE_MARKERS = ("Just a moment...", "cf-chl-", "challenge-error-text", "Attention Required!", "captcha")


class Blocked(Exception):
    """The site served a bot challenge or refused us; stop scraping it this run."""


class PoliteSession:
    def __init__(self, delay: float = 2.5, jitter: float = 1.5, timeout: float = 25):
        self.delay, self.jitter, self.timeout = delay, jitter, timeout
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": USER_AGENT,
            "Accept-Language": "mk,en;q=0.8",
            "Accept": "text/html,application/xhtml+xml",
        })
        self._last = 0.0
        self._robots: dict[str, urllib.robotparser.RobotFileParser] = {}
        self.requests_made = 0

    def _allowed(self, url: str) -> bool:
        host = urlparse(url).netloc
        if host not in self._robots:
            rp = urllib.robotparser.RobotFileParser()
            try:
                r = self.s.get(f"https://{host}/robots.txt", timeout=self.timeout)
                rp.parse(r.text.splitlines() if r.ok else [])
            except requests.RequestException:
                rp.parse([])
            self._robots[host] = rp
        return self._robots[host].can_fetch(USER_AGENT, url)

    def _wait(self):
        pause = self.delay + random.uniform(0, self.jitter) - (time.monotonic() - self._last)
        if pause > 0:
            time.sleep(pause)

    def get(self, url: str) -> str:
        if not self._allowed(url):
            raise Blocked(f"robots.txt disallows {url}")
        for attempt in range(3):
            self._wait()
            self._last = time.monotonic()
            self.requests_made += 1
            try:
                r = self.s.get(url, timeout=self.timeout)
            except requests.RequestException as e:
                log.warning("request failed (%s): %s", attempt + 1, e)
                time.sleep(5 * (attempt + 1))
                continue
            head = r.text[:4000]
            if r.status_code in (403, 503) or any(m in head for m in _CHALLENGE_MARKERS[:4]):
                raise Blocked(f"{r.status_code} bot challenge at {url}")
            if r.status_code == 429:
                time.sleep(30 * (attempt + 1))
                continue
            if r.status_code >= 500:
                time.sleep(5 * (attempt + 1))
                continue
            r.raise_for_status()
            r.encoding = r.encoding or "utf-8"
            return r.text
        raise Blocked(f"gave up after retries: {url}")
