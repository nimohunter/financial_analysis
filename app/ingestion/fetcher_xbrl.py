"""Fetch XBRL companyfacts JSON from SEC EDGAR, with local file cache."""
from __future__ import annotations

import logging
from pathlib import Path

from app.ingestion.edgar_client import DATA_BASE_URL, edgar_client

logger = logging.getLogger(__name__)

# Cache directory relative to project root
_CACHE_DIR = Path(__file__).parent.parent.parent / "data" / "raw" / "xbrl"


async def fetch_companyfacts(cik: str) -> bytes:
    """
    Download the companyfacts JSON for a company.
    Saves to data/raw/xbrl/{cik}.json on first fetch; reads from file on subsequent calls.
    """
    cik_padded = cik.zfill(10)
    cache_file = _CACHE_DIR / f"{cik_padded}.json"

    if cache_file.exists():
        logger.info("Loading companyfacts from cache: %s", cache_file)
        return cache_file.read_bytes()

    url = f"{DATA_BASE_URL}/api/xbrl/companyfacts/CIK{cik_padded}.json"
    logger.info("Fetching companyfacts: %s", url)
    raw = await edgar_client.get(url)
    logger.info("Fetched %d bytes — saving to %s", len(raw), cache_file)

    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file.write_bytes(raw)

    return raw
