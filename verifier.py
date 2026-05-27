import logging
import re
import time

import requests
from rapidfuzz import fuzz

log = logging.getLogger(__name__)

_SEARCH_URL = "https://projects.propublica.org/nonprofits/api/v2/search.json"
_MIN_SCORE = 80
_AMBIGUOUS_GAP = 5


def _get_with_retry(url, params=None, max_retries=3):
    """Execute HTTP GET request with backoff retry for overloads & rate limits."""
    delay = 1.0
    last_resp = None
    for attempt in range(max_retries):
        resp = requests.get(url, params=params, timeout=10)
        if resp.status_code == 404:
            return resp
        if resp.status_code not in (429, 503):
            resp.raise_for_status()
            return resp
        last_resp = resp
        if attempt < max_retries - 1:
            log.warning("ProPublica rate limited (attempt %d/%d), retrying in %.0fs",
                        attempt + 1, max_retries, delay)
            time.sleep(delay)
            delay *= 2
    log.warning("ProPublica rate limited on final attempt (%d/%d), giving up",
                max_retries, max_retries)
    if last_resp is not None:
        last_resp.raise_for_status()
    raise RuntimeError(f"_get_with_retry called with max_retries=0 for {url}")


def verify_sample(records: list[dict]) -> None:
    """Verify a list of record dicts in-place against ProPublica Nonprofit Explorer.

    Populates verified_nonprofit, propublica_ein, and propublica_org_name for each record.
    """
    for record in records:
        name = record.get("scraped_org_name", "")
        try:
            match = _lookup(name)
        except Exception as exc:
            log.warning("ProPublica lookup failed for %r: %s", name, exc)
            record["verified_nonprofit"] = "false"
            time.sleep(0.3)
            continue

        if match:
            record["verified_nonprofit"] = "true"
            record["propublica_ein"] = match["ein"]
            record["propublica_org_name"] = match["name"]
        else:
            record["verified_nonprofit"] = "false"

        time.sleep(0.3)


_STRIP_TOKENS = {
    "incorporated", "inc", "llc", "corp", "corporation",
    "assoc", "assn", "ltd",
}

def _normalize(name: str) -> str:
    """Standardize organization names by converting them to lowercase, stripping common punctation & removing common suffixes."""
    name = re.sub(r"[.,']", "", name.lower())
    tokens = [t for t in name.split() if t not in _STRIP_TOKENS]
    return " ".join(tokens)


def _lookup(name: str) -> dict | None:
    """Query ProPublica and return the best-matching org, or None if no confident match."""
    # Submit a GET request to ProPublica API search endpoint
    resp = _get_with_retry(_SEARCH_URL, params={"q": name})
    if resp.status_code == 404:
        log.debug("No ProPublica results for %r (404)", name)
        return None
    resp.raise_for_status()

    # Extracts the list array of organization records
    orgs = resp.json().get("organizations", [])
    if not orgs:
        log.debug("No ProPublica results for %r", name)
        return None

    # Score and rank the every search result (normalized)
    norm_name = _normalize(name)
    scored = sorted(
        ((fuzz.token_sort_ratio(norm_name, _normalize(org.get("name", ""))), org) for org in orgs),
        reverse=True,
        key=lambda x: x[0],
    )

    best_score, best_org = scored[0]

    if best_score < _MIN_SCORE:
        log.debug("No confident match for %r (best score: %d)", name, best_score)
        return None

    if len(scored) > 1 and (best_score - scored[1][0]) < _AMBIGUOUS_GAP:
        log.info(
            "Ambiguous match for %r — %r (score %d) vs %r (score %d); picking top",
            name,
            best_org.get("name"),
            best_score,
            scored[1][1].get("name"),
            scored[1][0],
        )

    return {"ein": str(best_org.get("ein", "")), "name": best_org.get("name", "")}
