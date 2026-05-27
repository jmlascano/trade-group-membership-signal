import time
import logging

import yaml
import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# ── Config & public API ──────────────────────────────────────────────────────

def load_config(source_key, path="sources.yaml"):
    """Read YAML config file."""
    with open(path) as f:
        data = yaml.safe_load(f)
    try:
        return data["sources"][source_key]
    except KeyError:
        available = list(data["sources"].keys())
        raise KeyError(f"Source '{source_key}' not found. Available: {available}")


def scrape(source_key, sources_path="sources.yaml"):
    """Centralized router for the scraping system."""
    config = load_config(source_key, sources_path)
    pagination_type = config["pagination_type"]

    if pagination_type == "algolia_api":
        yield from _scrape_algolia(config)
    elif pagination_type == "paginated":
        yield from _scrape_paginated(config)
    elif pagination_type == "alphabetical":
        yield from _scrape_alphabetical(config)
    elif pagination_type == "single_page":
        yield from _scrape_single_page(config)
    elif pagination_type == "js_rendered":
        yield from _scrape_js(config)
    else:
        raise ValueError(f"Unknown pagination_type: '{pagination_type}'")


# ── Schema ───────────────────────────────────────────────────────────────────

def _make_record(config, name, role):
    """Constructs & returns standardized data record dict."""
    return {
        "scraped_org_name": name,
        "source_type": config["source_type"],
        "source_name": config["name"],
        "role": role,
        "source_url": config["base_url"],
        "verified_nonprofit": "",
        "propublica_ein": "",
        "propublica_org_name": "",
    }


# ── HTTP helpers ─────────────────────────────────────────────────────────────

def _post_with_retry(url, headers, json_body, max_retries=3):
    """Execute HTTP POST request with backoff retry for overloads & rate limits."""
    delay = 1.0
    last_resp = None
    for attempt in range(max_retries):
        resp = requests.post(url, headers=headers, json=json_body)
        if resp.status_code not in (429, 503):
            resp.raise_for_status()
            return resp # exit the function on success
        last_resp = resp
        if attempt < max_retries - 1:
            logger.warning("Rate limited (attempt %d/%d), retrying in %.0fs", attempt + 1, max_retries, delay)
            time.sleep(delay)
            delay *= 2
    logger.warning("Rate limited on final attempt (%d/%d), giving up", max_retries, max_retries)
    if last_resp is not None:
        last_resp.raise_for_status()
    raise RuntimeError(f"_post_with_retry called with max_retries=0 for {url}")


def _get_with_retry(url, params=None, headers=None, max_retries=3):
    """Execute HTTP GET request with backoff retry for overloads & rate limits."""
    delay = 1.0
    last_resp = None
    for attempt in range(max_retries):
        resp = requests.get(url, params=params, headers=headers)
        if resp.status_code not in (429, 503):
            resp.raise_for_status()
            return resp
        last_resp = resp
        if attempt < max_retries - 1:
            logger.warning("Rate limited (attempt %d/%d), retrying in %.0fs", attempt + 1, max_retries, delay)
            time.sleep(delay)
            delay *= 2
    logger.warning("Rate limited on final attempt (%d/%d), giving up", max_retries, max_retries)
    if last_resp is not None:
        last_resp.raise_for_status()
    raise RuntimeError(f"_get_with_retry called with max_retries=0 for {url}")


# ── HTML parsing ─────────────────────────────────────────────────────────────

def _parse_members(soup, config, role, context=""):
    """Parses a BeautifulSoup HTML object to extract individual membrs."""
    member_selector = config["member_selector"]
    name_selector   = config["name_selector"]
    for member in soup.select(member_selector): # execute a CSS selector query against the document
        name_el = member.select_one(name_selector)
        if not name_el:
            logger.warning("Name element '%s' not found%s, skipping",
                           name_selector, f" ({context})" if context else "")
            continue
        name = name_el.get_text(strip=True)
        if not name:
            logger.warning("Empty name text%s, skipping",
                           f" ({context})" if context else "")
            continue
        yield _make_record(config, name, role)


# ── Scraper implementations ───────────────────────────────────────────────────

def _scrape_algolia(config):
    """Scrape member directories that use Algolia search API."""
    app_id = config["algolia_app_id"]
    api_key = config["algolia_api_key"]
    index = config["algolia_index"]
    page_size = config.get("algolia_page_size", 20)
    name_field = config.get("name_field", "name")
    role_field = config.get("role_field")
    delay = config.get("rate_limit_delay", 0.5)

    url = f"https://{app_id}-dsn.algolia.net/1/indexes/*/queries"
    headers = {
        "x-algolia-api-key": api_key,
        "x-algolia-application-id": app_id,
        "Content-Type": "application/json",
    }

    page = 0
    while True: # end when no pages remain
        body = {
            "requests": [{
                "indexName": index,
                "page": page,
                "query": "",
                "hitsPerPage": page_size,
            }]
        }

        resp = _post_with_retry(url, headers=headers, json_body=body)
        result = resp.json()["results"][0]
        hits = result.get("hits", [])
        nb_pages = result.get("nbPages", 1)

        for hit in hits:
            name = hit.get(name_field)
            if not name:
                logger.warning("Hit missing '%s' field (objectID=%s), skipping", name_field, hit.get("objectID"))
                continue

            role = hit.get(role_field, "member") if role_field else "member"
            yield _make_record(config, name, role)

        if page + 1 >= nb_pages:
            break

        page += 1
        time.sleep(delay)


def _scrape_paginated(config):
    """Scrape sources that use standard numeric pagination."""
    base_url = config["base_url"]
    role     = config.get("role", "member")
    delay    = config.get("rate_limit_delay", 1.0)

    page = 1
    while True:
        resp = _get_with_retry(base_url, params={"page": page})
        soup = BeautifulSoup(resp.text, "lxml")
        if not soup.select(config["member_selector"]):
            break
        yield from _parse_members(soup, config, role, context=f"page {page}")
        page += 1
        time.sleep(delay)


def _scrape_single_page(config):
    """Scrape sources that use a single page for their member directory."""
    resp = _get_with_retry(config["base_url"])
    soup = BeautifulSoup(resp.text, "lxml")
    yield from _parse_members(soup, config, config.get("role", "member"))


def _scrape_alphabetical(config):
    """Scrape sources that categorizes members alphabetically."""
    base_url     = config["base_url"]
    role         = config.get("role", "member")
    delay        = config.get("rate_limit_delay", 1.0)
    letter_param = config.get("letter_param", "letter")

    for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        resp = _get_with_retry(base_url, params={letter_param: letter})
        soup = BeautifulSoup(resp.text, "lxml")
        yield from _parse_members(soup, config, role, context=f"letter {letter}")
        time.sleep(delay)


def _scrape_js(config):
    """Scrape sources that rely on JS rendering (single page only)."""
    from playwright.sync_api import sync_playwright # lazy import so the project does not crash if the user does not install Playwright

    base_url = config["base_url"]
    role     = config.get("role", "member")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(base_url, timeout=30000)
        page.wait_for_selector(config["member_selector"])
        html = page.content()
        browser.close()

    soup = BeautifulSoup(html, "lxml")
    yield from _parse_members(soup, config, role, context="JS-rendered page")
