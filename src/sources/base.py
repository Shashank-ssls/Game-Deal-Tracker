"""Abstract deal-source interface and shared HTTP plumbing.

Every source subclasses :class:`DealSource` and returns ``list[Deal]`` from
:meth:`fetch`. Per CLAUDE.md, a source must swallow its own errors (log a warning,
return ``[]``) and must never touch the DB or Discord.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from src.config import Settings
from src.models import Deal

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 10  # seconds
USER_AGENT = "game-deal-tracker/1.0 (+https://github.com/game-deal-tracker)"
_RETRY_STATUS = (429, 500, 502, 503, 504)


def build_session() -> requests.Session:
    """A ``requests`` session with 3 retries, exponential backoff, and a UA."""
    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=_RETRY_STATUS,
        allowed_methods=frozenset({"GET"}),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({"User-Agent": USER_AGENT})
    return session


def parse_iso(value: object) -> datetime | None:
    """Parse an ISO-8601 timestamp (``Z`` suffix allowed); ``None`` on failure."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


class DealSource(ABC):
    """Base class for one external deal API."""

    name: str = "base"

    def __init__(self, settings: Settings, session: requests.Session | None = None) -> None:
        self.settings = settings
        self.session = session if session is not None else build_session()

    @abstractmethod
    def fetch(self) -> list[Deal]:
        """Return current deals from this source; never raise (return [])."""

    def _get_json(self, url: str, params: dict | None = None) -> Any:
        """GET ``url`` and return parsed JSON. Raises on HTTP/parse error."""
        response = self.session.get(url, params=params, timeout=DEFAULT_TIMEOUT)
        response.raise_for_status()
        return response.json()
