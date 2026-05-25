import time
import logging

import yaml
import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

def load_config(source_key, path="sources.yaml"):
    with open(path) as f:
        data = yaml.safe_load(f)
    try:
        return data["sources"][source_key]
    except KeyError:
        available = list(data["sources"].keys())
        raise KeyError(f"Source '{source_key}' not found. Available: {available}")


def scrape(source_key, sources_path="sources.yaml"):
    config = load_config(source_key, sources_path)
    pagination_type = config["pagination_type"]

    if pagination_type == "algolia_api":
        yield from _scrape_algolia(config)
    elif pagination_type == "paginated":
        yield from _scrape_paginated(config)
    elif pagination_type == "alphabetical":
        yield from _scrape_alphabetical(config)
    elif pagination_type == "js_rendered":
        yield from _scrape_js(config)
    else:
        raise ValueError(f"Unknown pagination_type: '{pagination_type}'")


def _make_record(config, name, role):
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


def _post_with_retry(url, headers, json_body, max_retries=3):
    delay = 1.0
    for attempt in range(max_retries):
        resp = requests.post(url, headers=headers, json=json_body)
        if resp.status_code in (429, 503):
            logger.warning("Rate limited (attempt %d/%d), retrying in %.0fs", attempt + 1, max_retries, delay)
            time.sleep(delay)
            delay *= 2
            continue
        resp.raise_for_status()
        return resp
    resp.raise_for_status()


def _get_with_retry(url, params=None, max_retries=3):
    delay = 1.0
    for attempt in range(max_retries):
        resp = requests.get(url, params=params)
        if resp.status_code in (429, 503):
            logger.warning("Rate limited (attempt %d/%d), retrying in %.0fs", attempt + 1, max_retries, delay)
            time.sleep(delay)
            delay *= 2
            continue
        resp.raise_for_status()
        return resp
    resp.raise_for_status()


def _scrape_algolia(config):
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
    while True:
        body = {
            "requests": [{
                "indexName": index,
                "page": page,
                "query": "",
                "hitsPerPage": page_size,
            }]
        }

        resp = _post_with_retry(url, headers=headers, json_body=body)
        hits = resp.json()["results"][0].get("hits", [])

        for hit in hits:
            name = hit.get(name_field)
            if not name:
                logger.warning("Hit missing '%s' field (objectID=%s), skipping", name_field, hit.get("objectID"))
                continue

            role = hit.get(role_field, "member") if role_field else "member"
            yield _make_record(config, name, role)

        if len(hits) < page_size:
            break

        page += 1
        time.sleep(delay)


def _scrape_paginated(config):
    base_url = config["base_url"]
    member_selector = config["member_selector"]
    name_selector = config["name_selector"]
    role = config.get("role", "member")
    delay = config.get("rate_limit_delay", 1.0)

    page = 1
    while True:
        resp = _get_with_retry(base_url, params={"page": page})
        soup = BeautifulSoup(resp.text, "lxml")
        members = soup.select(member_selector)

        if not members:
            break

        for member in members:
            name_el = member.select_one(name_selector)
            if not name_el:
                logger.warning("Name element '%s' not found on page %d, skipping", name_selector, page)
                continue
            name = name_el.get_text(strip=True)
            if not name:
                logger.warning("Empty name text on page %d, skipping", page)
                continue
            yield _make_record(config, name, role)

        page += 1
        time.sleep(delay)


def _scrape_alphabetical(config):
    base_url = config["base_url"]
    member_selector = config["member_selector"]
    name_selector = config["name_selector"]
    role = config.get("role", "member")
    letter_param = config.get("letter_param", "letter")
    delay = config.get("rate_limit_delay", 1.0)

    for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        resp = _get_with_retry(base_url, params={letter_param: letter})
        soup = BeautifulSoup(resp.text, "lxml")

        for member in soup.select(member_selector):
            name_el = member.select_one(name_selector)
            if not name_el:
                logger.warning("Name element '%s' not found for letter %s, skipping", name_selector, letter)
                continue
            name = name_el.get_text(strip=True)
            if not name:
                logger.warning("Empty name text for letter %s, skipping", letter)
                continue
            yield _make_record(config, name, role)

        time.sleep(delay)


def _scrape_js(config):
    from playwright.sync_api import sync_playwright

    base_url = config["base_url"]
    member_selector = config["member_selector"]
    name_selector = config["name_selector"]
    role = config.get("role", "member")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(base_url)
        page.wait_for_selector(member_selector)
        html = page.content()
        browser.close()

    soup = BeautifulSoup(html, "lxml")
    for member in soup.select(member_selector):
        name_el = member.select_one(name_selector)
        if not name_el:
            logger.warning("Name element '%s' not found in JS-rendered page, skipping", name_selector)
            continue
        name = name_el.get_text(strip=True)
        if not name:
            logger.warning("Empty name text in JS-rendered page, skipping")
            continue
        yield _make_record(config, name, role)
