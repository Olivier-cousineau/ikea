#!/usr/bin/env python3
"""Scrape IKEA Last Chance listings from the IKEA product list API."""

from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import sys
import time
from pathlib import Path
from threading import Event
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen


DEFAULT_URL = "https://www.ikea.com/ca/fr/cat/last-chance/"
FILTERED_LAST_CHANCE_URL = (
    "https://www.ikea.com/ca/fr/cat/last-chance/"
    "?filters=f_availability%3AAVAILABLE_IN_STORE"
)
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
DEFAULT_LOCALE = "fr-CA"
DEFAULT_TIMEZONE = "America/Toronto"
DEFAULT_VIEWPORT = {"width": 1280, "height": 800}
DEBUG_SCREENSHOT = "playwright_debug.png"
DEBUG_HTML = "playwright_debug.html"
DEBUG_REQUESTS = "playwright_debug_requests.txt"
DEBUG_RESPONSES = "network_responses_filtered.json"
DEBUG_LINKS = "playwright_debug_links.txt"
STORE_DEBUG_SCREENSHOT = "store_debug.png"
STORE_DEBUG_HTML = "store_debug.html"
STORE_DEBUG_TEXT = "store_debug.txt"
DEFAULT_HEADERS = {
    "User-Agent": DEFAULT_USER_AGENT,
    "Accept": "application/json",
    "Accept-Language": "fr-CA,fr;q=0.9,en;q=0.8",
    "Referer": DEFAULT_URL,
    "Origin": "https://www.ikea.com",
}
STORE_VALIDATION_SAMPLE_SIZE = 30
STORE_VALIDATION_MIN_MATCHES = 5
MAX_PAGINATION_PAGES = 200
STORE_SEARCH_SELECTORS = [
    "input[type='search']",
    "input[placeholder*='Rechercher']",
    "input[aria-label*='Rechercher']",
    "input[placeholder*='Search']",
    "input[aria-label*='Search']",
]
STORE_MODAL_TITLE = "Choisir un magasin"


def should_use_headed(flag: bool) -> bool:
    if flag:
        return True
    return os.getenv("HEADED", "").strip().lower() in {"1", "true", "yes", "on"}


def create_context(playwright: Any, headed: bool) -> Tuple[Any, Any]:
    browser = playwright.chromium.launch(headless=not headed)
    context = browser.new_context(
        user_agent=DEFAULT_USER_AGENT,
        locale=DEFAULT_LOCALE,
        timezone_id=DEFAULT_TIMEZONE,
        viewport=DEFAULT_VIEWPORT,
    )
    return browser, context


def dismiss_consent(page: Any) -> None:
    for label in ("Accepter", "Tout accepter", "OK", "Fermer", "Continuer"):
        locator = page.locator(f"text={label}")
        if locator.count():
            try:
                if locator.first.is_visible():
                    locator.first.click(timeout=1500)
            except Exception:
                continue


def click_first_visible(
    page: Any, selectors: List[str], timeout: int = 5000
) -> bool:
    for selector in selectors:
        locator = page.locator(selector)
        if not locator.count():
            continue
        for idx in range(locator.count()):
            candidate = locator.nth(idx)
            try:
                if candidate.is_visible():
                    candidate.click(timeout=timeout)
                    return True
            except Exception:
                continue
    return False


def store_modal_is_open(page: Any) -> bool:
    dialog = page.locator("[role=dialog]")
    for idx in range(dialog.count()):
        try:
            if dialog.nth(idx).is_visible():
                return True
        except Exception:
            continue
    for selector in STORE_SEARCH_SELECTORS:
        locator = page.locator(selector)
        if locator.count():
            try:
                if locator.first.is_visible():
                    return True
            except Exception:
                continue
    title_locator = page.locator(f"text={STORE_MODAL_TITLE}")
    if title_locator.count():
        try:
            if title_locator.first.is_visible():
                return True
        except Exception:
            pass
    return False


def capture_store_debug(
    page: Any,
    error: Exception,
    store_query: Optional[str],
    expected_label: Optional[str],
) -> None:
    try:
        page.screenshot(path=STORE_DEBUG_SCREENSHOT, full_page=True)
    except Exception:
        pass
    try:
        html = page.content()
        Path(STORE_DEBUG_HTML).write_text(html, encoding="utf-8")
    except Exception:
        pass
    try:
        details = [
            f"Error: {error}",
            f"URL: {page.url}",
            f"Title: {page.title()}",
            f"Store query: {store_query or ''}",
            f"Expected label: {expected_label or ''}",
            f"Modal visible: {store_modal_is_open(page)}",
        ]
        Path(STORE_DEBUG_TEXT).write_text(
            "\n".join(details), encoding="utf-8"
        )
    except Exception:
        pass


def open_store_modal(page: Any) -> None:
    selectors = [
        "button:has-text('Magasin')",
        "button:has-text('Store')",
        "a:has-text('Magasin')",
        "a:has-text('Store')",
        "button:has-text('Choisir un magasin')",
        "button:has-text('Choose a store')",
        "[data-testid*='store']",
        "[aria-label*='Magasin']",
        "[aria-label*='Store']",
    ]
    header_selectors = [
        "header button:has-text('Magasin')",
        "header button:has-text('Store')",
        "header a:has-text('Magasin')",
        "header a:has-text('Store')",
        "header [aria-label*='Magasin']",
        "header [aria-label*='Store']",
    ]

    def attempt_open(selector: str, use_trial: bool) -> bool:
        locator = page.locator(selector)
        if not locator.count():
            return False
        for idx in range(locator.count()):
            candidate = locator.nth(idx)
            try:
                if not candidate.is_visible():
                    continue
                if use_trial:
                    candidate.click(trial=True, timeout=1500)
                candidate.click(timeout=2000)
                page.wait_for_timeout(500)
                if store_modal_is_open(page):
                    return True
            except Exception:
                continue
        return False

    for selector in selectors:
        if attempt_open(selector, use_trial=False):
            return
    for selector in header_selectors:
        if attempt_open(selector, use_trial=True):
            return

    if not store_modal_is_open(page):
        raise RuntimeError("Impossible d'ouvrir le modal Magasin/Store.")


def set_store(
    page: Any, store_query: str, expected_label: Optional[str]
) -> None:
    page.goto("https://www.ikea.com/ca/fr/", wait_until="domcontentloaded")
    dismiss_consent(page)
    try:
        open_store_modal(page)

        search_input = None
        for selector in STORE_SEARCH_SELECTORS:
            locator = page.locator(selector).first
            if locator.count():
                search_input = locator
                break
        if not search_input:
            raise RuntimeError("Champ de recherche du magasin introuvable.")

        search_input.fill(store_query)
        page.wait_for_timeout(750)

        target_label = expected_label or store_query
        escaped_label = re.escape(target_label)
        label_locator = page.locator(f"text=/{escaped_label}/i").first
        if label_locator.count():
            choose_button = label_locator.locator(
                "button:has-text('Choisir'), "
                "button:has-text('Sélectionner'), "
                "button:has-text('Select'), "
                "button:has-text('Confirmer'), "
                "button:has-text('Enregistrer')"
            )
            if choose_button.count():
                choose_button.first.click()
            else:
                label_locator.click()
        else:
            fallback_selectors = [
                "button:has-text('Choisir')",
                "button:has-text('Sélectionner')",
                "button:has-text('Select')",
            ]
            if not click_first_visible(page, fallback_selectors):
                raise RuntimeError(
                    f"Magasin introuvable pour la requête: {store_query}"
                )

        confirm_selectors = [
            "button:has-text('Confirmer')",
            "button:has-text('Enregistrer')",
            "button:has-text('Continuer')",
            "button:has-text('Save')",
            "button:has-text('Continue')",
        ]
        click_first_visible(page, confirm_selectors)
        page.wait_for_timeout(500)
    except Exception as exc:
        capture_store_debug(page, exc, store_query, expected_label)
        raise


def find_show_more(page: Any) -> Optional[Any]:
    selectors = [
        "a[aria-label*='Afficher plus']",
        "a.plp-btn.plp-btn--secondary",
        "a.plp-btn:has-text('Montrer plus')",
        "a:has-text('Montrer plus')",
        "a:has-text('Afficher plus de produits')",
        "a[aria-label*='Show more'], a:has-text('Show more')",
        "button:has-text('Montrer plus')",
        "button:has-text('Show more')",
        "[role=button]:has-text('Montrer plus')",
        "[role=button]:has-text('Show more')",
    ]
    for selector in selectors:
        locator = page.locator(selector).first
        if locator.count():
            return locator
    return None


def count_products(page: Any) -> int:
    primary_count = page.locator("div.plp-product-list__products > *").count()
    if primary_count:
        return primary_count
    return page.locator("a[href*='/p/']").count()


def normalize_page_url(url: str) -> str:
    split_url = urlsplit(url)
    filtered_query = [
        (key, value)
        for key, value in parse_qsl(split_url.query, keep_blank_values=True)
        if key.lower() == "page"
    ]
    normalized_query = urlencode(filtered_query, doseq=True)
    return urlunsplit(
        split_url._replace(query=normalized_query, fragment="")
    )


def pick_image_source(img_locator: Any) -> Optional[str]:
    for attr in ("src", "data-src", "srcset", "data-srcset"):
        value = img_locator.get_attribute(attr)
        if not value:
            continue
        if attr.endswith("srcset"):
            first = value.split(",")[0].strip()
            if first:
                return first.split(" ")[0]
        else:
            return value
    return None


def extract_text(locator: Any) -> Optional[str]:
    try:
        text = locator.inner_text().strip()
    except Exception:
        return None
    return text or None


def pick_name_and_type(card: Any) -> Tuple[Optional[str], Optional[str]]:
    candidates = [
        card.locator("a[href*='/p/']").first,
        card.locator("h3").first,
        card.locator("h2").first,
    ]
    for candidate in candidates:
        if not candidate.count():
            continue
        text = extract_text(candidate)
        if text:
            parts = [line.strip() for line in text.splitlines() if line.strip()]
            if not parts:
                continue
            name = parts[0]
            type_name = parts[1] if len(parts) > 1 else None
            return name, type_name
    return None, None


def pick_price(card: Any) -> Optional[str]:
    selectors = [
        "[data-testid*='price']",
        "[data-testid*='Price']",
        "[data-testid*='product-price']",
        "[aria-label*='$']",
        "[aria-label*='CAD']",
        "span:has-text('$')",
    ]
    for selector in selectors:
        locator = card.locator(selector).first
        if locator.count():
            text = extract_text(locator)
            if text:
                return text
    return None


def normalize_store_label(label: str) -> str:
    return " ".join(label.split())


def parse_in_stock_label(label: str) -> Optional[str]:
    match = re.search(r"En stock\s*:\s*(.+)", label, flags=re.IGNORECASE)
    if not match:
        return None
    store = normalize_store_label(match.group(1))
    return store or None


def extract_in_stock_store(card: Any) -> Optional[str]:
    locator = card.locator("text=/En stock\\s*:/")
    if not locator.count():
        return None
    for idx in range(locator.count()):
        text = extract_text(locator.nth(idx))
        if not text:
            continue
        store = parse_in_stock_label(text)
        if store:
            return store
    return None


def extract_product_from_card(card: Any) -> Dict[str, Optional[str]]:
    link_locator = card.locator("a[href*='/p/']").first
    href = link_locator.get_attribute("href") if link_locator.count() else ""
    if href and not href.startswith("http"):
        href = f"https://www.ikea.com{href}"

    image = None
    img_locator = card.locator("img").first
    if img_locator.count():
        image = pick_image_source(img_locator)

    name, type_name = pick_name_and_type(card)
    price = pick_price(card)
    in_stock_store = extract_in_stock_store(card)

    return {
        "name": name,
        "typeName": type_name,
        "price": price,
        "url": href or None,
        "image": image,
        "inStockStore": in_stock_store,
    }


def collect_products_across_pages(page: Any) -> List[Dict[str, Any]]:
    max_pages = MAX_PAGINATION_PAGES
    pages = 0
    seen_pages: set[str] = set()
    products_map: Dict[str, Dict[str, Any]] = {}

    while pages < max_pages:
        normalized_current = normalize_page_url(page.url)
        if normalized_current in seen_pages:
            print(
                "Pagination stop: page repeat detected -> {page}".format(
                    page=normalized_current
                )
            )
            break
        seen_pages.add(normalized_current)

        cards = page.locator("div.plp-product-list__products > *")
        card_count = cards.count()
        for idx in range(card_count):
            card = cards.nth(idx)
            product_data = extract_product_from_card(card)
            href = product_data.get("url") or ""
            if not href:
                continue
            product = products_map.get(
                href,
                {
                    "name": None,
                    "typeName": None,
                    "price": None,
                    "url": href,
                    "image": None,
                    "inStockStore": None,
                },
            )
            for key, value in product_data.items():
                if value and not product.get(key):
                    product[key] = value
            products_map[href] = product

        more = page.locator('a[aria-label="Afficher plus de produits"]')
        link = more.first if more.count() else find_show_more(page)
        if not link:
            print("Pagination stop: no show more link found.")
            break
        try:
            href = link.get_attribute("href")
        except Exception:
            href = None
        if not href:
            print("Pagination stop: empty href.")
            break
        next_url = urljoin(page.url, href)
        normalized_next = normalize_page_url(next_url)
        if normalized_next in seen_pages:
            print(
                "Pagination stop: next page already seen -> {page}".format(
                    page=normalized_next
                )
            )
            break
        print(
            "Pagination: [PAGE]={page} [NEXT]={next} [NAV]=goto".format(
                page=page.url, next=next_url
            )
        )
        page.goto(next_url, wait_until="domcontentloaded")
        dismiss_consent(page)
        try:
            page.wait_for_selector("a[href*='/p/']", timeout=10000)
        except Exception:
            pass
        pages += 1

    return list(products_map.values())


def capture_api_request(page_url: str, headed: bool) -> Tuple[str, Dict[str, str]]:
    from playwright.sync_api import sync_playwright

    captured_url: Optional[str] = None
    captured_headers: Dict[str, str] = {}
    fallback_url: Optional[str] = None
    fallback_headers: Dict[str, str] = {}
    captured_event = Event()
    request_log: List[str] = []
    response_log: List[str] = []
    response_filtered: List[str] = []

    capture_patterns = (
        "product-list-page",
        "more-products",
        "pip",
        "sik.search.blue.cdtapps.com",
    )

    def url_matches(url: str) -> bool:
        return any(pattern in url for pattern in capture_patterns)

    def maybe_capture(url: str, headers: Dict[str, str]) -> None:
        nonlocal captured_url, captured_headers, fallback_url, fallback_headers
        if "product-list-page/more-products" in url:
            captured_url = url
            captured_headers = extract_headers(headers)
            captured_event.set()
            return
        if "product-list-page" in url and fallback_url is None:
            fallback_url = url
            fallback_headers = extract_headers(headers)

    def handle_request(request: Any) -> None:
        request_url = request.url
        if url_matches(request_url):
            request_log.append(request_url)
            if not captured_event.is_set():
                headers = request.headers or {}
                maybe_capture(request_url, headers)

    def handle_response(response: Any) -> None:
        response_url = response.url
        if "product-list-page" in response_url or (
            "sik.search.blue.cdtapps.com" in response_url
        ):
            response_filtered.append(response_url)
        if url_matches(response_url):
            response_log.append(response_url)
            if not captured_event.is_set():
                headers = response.request.headers or {}
                maybe_capture(response_url, headers)

    with sync_playwright() as playwright:
        browser, context = create_context(playwright, headed=headed)
        page = context.new_page()
        page.on("request", handle_request)
        page.on("response", handle_response)
        page.goto(page_url, wait_until="domcontentloaded")

        dismiss_consent(page)

        page.wait_for_selector("a[href*='/p/']", timeout=45000)

        collect_products_across_pages(page)

        start_time = time.monotonic()
        timeout_seconds = 60
        while not captured_event.is_set():
            if time.monotonic() - start_time >= timeout_seconds:
                break
            page.wait_for_timeout(250)

        if not captured_event.is_set():
            page.screenshot(path=DEBUG_SCREENSHOT, full_page=True)
            html = page.content()
            Path(DEBUG_HTML).write_text(html, encoding="utf-8")
            Path(DEBUG_REQUESTS).write_text(
                "\n".join(request_log + response_log), encoding="utf-8"
            )
            Path(DEBUG_RESPONSES).write_text(
                json.dumps(response_filtered, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            product_links = page.locator("a[href*='/p/']")
            link_count = min(product_links.count(), 10)
            link_details: List[str] = []
            for idx in range(link_count):
                link = product_links.nth(idx)
                href = link.get_attribute("href") or ""
                try:
                    text = link.inner_text().strip()
                except Exception:
                    text = ""
                link_details.append(f"{idx + 1}. {text} ({href})")
            Path(DEBUG_LINKS).write_text(
                "\n".join(link_details), encoding="utf-8"
            )
        context.close()
        browser.close()

    if not captured_url and fallback_url:
        captured_url = fallback_url
        captured_headers = fallback_headers

    if not captured_url:
        raise RuntimeError(
            "Aucune requête capturée après 60s. "
            f"Debug: {DEBUG_SCREENSHOT}, {DEBUG_HTML}, {DEBUG_REQUESTS}"
        )

    return captured_url, captured_headers


def scrape_products_from_page(
    page_url: str,
    headed: bool,
) -> List[Dict[str, Any]]:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser, context = create_context(playwright, headed=headed)
        page = context.new_page()
        page.goto(page_url, wait_until="domcontentloaded")
        dismiss_consent(page)
        page.wait_for_selector(
            "div.plp-product-list__products > *", timeout=45000
        )
        products = collect_products_across_pages(page)

        print(
            "Total unique product urls = {count}".format(
                count=len(products)
            )
        )

        context.close()
        browser.close()

    return products


def extract_headers(headers: Dict[str, str]) -> Dict[str, str]:
    base_headers = {}
    for header_name in (
        "user-agent",
        "accept",
        "accept-language",
        "referer",
        "origin",
    ):
        for key, value in headers.items():
            if key.lower() == header_name:
                base_headers[header_name.title()] = value
                break
    return base_headers


def merge_headers(captured_headers: Dict[str, str]) -> Dict[str, str]:
    merged = dict(DEFAULT_HEADERS)
    for key, value in captured_headers.items():
        if value:
            merged[key] = value
    merged["Accept-Encoding"] = "gzip"
    return merged


def detect_pagination_params(query_pairs: List[Tuple[str, str]]) -> Tuple[str, str]:
    candidates = [
        ("start", "end"),
        ("offset", "limit"),
        ("from", "to"),
        ("startindex", "endindex"),
    ]
    lowered = [(key.lower(), key, value) for key, value in query_pairs]
    for start_key, end_key in candidates:
        start_actual = next(
            (original for lower, original, _ in lowered if lower == start_key),
            None,
        )
        end_actual = next(
            (original for lower, original, _ in lowered if lower == end_key),
            None,
        )
        if start_actual and end_actual:
            return start_actual, end_actual
    raise ValueError("Impossible d'identifier les paramètres start/end.")


def detect_page_param(query_pairs: List[Tuple[str, str]]) -> Tuple[str, int]:
    candidates = ("page", "pagenumber", "pageindex")
    lowered = [(key.lower(), key, value) for key, value in query_pairs]
    for candidate in candidates:
        match = next(
            (original for lower, original, _ in lowered if lower == candidate),
            None,
        )
        if match:
            value = next(value for key, value in query_pairs if key == match)
            return match, int(value)
    raise ValueError("Impossible d'identifier un paramètre de page.")


def update_query_params(
    query_pairs: List[Tuple[str, str]],
    start_key: str,
    end_key: str,
    start: int,
    end: int,
) -> List[Tuple[str, str]]:
    updated: List[Tuple[str, str]] = []
    for key, value in query_pairs:
        if key == start_key:
            updated.append((key, str(start)))
        elif key == end_key:
            updated.append((key, str(end)))
        else:
            updated.append((key, value))
    return updated


def update_single_query_param(
    query_pairs: List[Tuple[str, str]],
    target_key: str,
    target_value: str,
) -> List[Tuple[str, str]]:
    updated: List[Tuple[str, str]] = []
    found = False
    for key, value in query_pairs:
        if key == target_key:
            updated.append((key, str(target_value)))
            found = True
        else:
            updated.append((key, value))
    if not found:
        updated.append((target_key, str(target_value)))
    return updated


def fetch_json(url: str, headers: Dict[str, str]) -> Dict[str, Any]:
    request = Request(url, headers=headers)
    with urlopen(request, timeout=30) as response:
        raw = response.read()
        if response.headers.get("Content-Encoding", "").lower() == "gzip":
            raw = gzip.decompress(raw)
        return json.loads(raw.decode("utf-8"))


def extract_price(product: Dict[str, Any]) -> Optional[Any]:
    price = product.get("price")
    if isinstance(price, dict):
        return price.get("current") or price.get("price") or price.get("now")
    return price


def build_product(product: Dict[str, Any]) -> Dict[str, Any]:
    link_url = product.get("linkUrl") or product.get("pipUrl") or ""
    if link_url and not link_url.startswith("http"):
        link_url = f"https://www.ikea.com{link_url}"
    image = product.get("imageUrl")
    if not image and isinstance(product.get("image"), dict):
        image = product.get("image", {}).get("url")
    return {
        "name": product.get("name"),
        "typeName": product.get("typeName"),
        "price": extract_price(product),
        "url": link_url,
        "image": image,
        "inStockStore": None,
    }


def find_product_list(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, dict):
        for key in ("products", "productList", "items", "moreProducts"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        for value in payload.values():
            found = find_product_list(value)
            if found:
                return found
    elif isinstance(payload, list):
        if payload and all(isinstance(item, dict) for item in payload):
            for item in payload:
                if any(
                    field in item for field in ("pipUrl", "linkUrl", "name")
                ):
                    return payload
        for item in payload:
            found = find_product_list(item)
            if found:
                return found
    return []


def parse_pagination_values(
    query_pairs: List[Tuple[str, str]],
    start_key: str,
    end_key: str,
) -> Tuple[int, int]:
    start_value = next(value for key, value in query_pairs if key == start_key)
    end_value = next(value for key, value in query_pairs if key == end_key)
    return int(start_value), int(end_value)


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scraper IKEA Last Chance (Canada FR)."
    )
    parser.add_argument("--url", default=DEFAULT_URL, help="URL de la page IKEA")
    parser.add_argument(
        "--output",
        default="ikea_last_chance.json",
        help="Chemin du fichier JSON de sortie",
    )
    parser.add_argument(
        "--out-base",
        default="public/ikea",
        help="Répertoire de base pour data.json par magasin.",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Lance Playwright avec headless=False (ou HEADED=1).",
    )
    parser.add_argument(
        "--locations",
        help="Liste de magasins (séparés par des virgules) à scraper.",
    )
    parser.add_argument(
        "--locations-file",
        help="Chemin d'un fichier contenant une liste de magasins (1 par ligne).",
    )
    parser.add_argument(
        "--store-ids",
        help=(
            "Chemin d'un fichier JSON associant nom de magasin -> storeId "
            "(ex: {\"IKEA Montréal\": \"123\"})."
        ),
    )
    parser.add_argument(
        "--store-param",
        default="storeId",
        help="Nom du paramètre de query pour le storeId.",
    )
    parser.add_argument(
        "--store-query",
        help="Requête de recherche pour sélectionner le magasin.",
    )
    parser.add_argument(
        "--expected-store-label",
        help=(
            "Libellé exact du magasin attendu dans les cartes "
            "(ex: IKEA Montréal)."
        ),
    )
    parser.add_argument(
        "--store-display-name",
        help="Nom d'affichage du magasin (ex: IKEA Montréal).",
    )
    parser.add_argument(
        "--store-city",
        help="Ville du magasin (ex: Montréal).",
    )
    parser.add_argument(
        "--store-province",
        help="Province du magasin (ex: QC).",
    )
    parser.add_argument(
        "--store-slug",
        help="Slug du magasin (ex: montreal-qc).",
    )
    return parser.parse_args(argv)


def parse_locations(args: argparse.Namespace) -> List[str]:
    locations: List[str] = []
    if args.locations:
        locations.extend(
            [item.strip() for item in args.locations.split(",") if item.strip()]
        )
    if args.locations_file:
        location_path = Path(args.locations_file)
        for line in location_path.read_text(encoding="utf-8").splitlines():
            cleaned = line.strip()
            if cleaned and not cleaned.startswith("#"):
                locations.append(cleaned)
    unique_locations: List[str] = []
    seen = set()
    for location in locations:
        if location not in seen:
            unique_locations.append(location)
            seen.add(location)
    return unique_locations


def load_store_ids(path: Optional[str]) -> Dict[str, str]:
    if not path:
        return {}
    store_path = Path(path)
    if not store_path.exists():
        raise FileNotFoundError(f"Fichier store-ids introuvable: {path}")
    data = json.loads(store_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Le fichier store-ids doit contenir un objet JSON.")
    return {
        str(key): str(value)
        for key, value in data.items()
        if value is not None
    }


def build_location_url(base_url: str, store_id: Optional[str], param: str) -> str:
    if not store_id:
        return base_url
    parsed_url = urlsplit(base_url)
    query_pairs = parse_qsl(parsed_url.query, keep_blank_values=True)
    updated = update_single_query_param(query_pairs, param, store_id)
    return urlunsplit(parsed_url._replace(query=urlencode(updated, doseq=True)))


def scrape_url(url: str, headed: bool) -> List[Dict[str, Any]]:
    if headed:
        try:
            products = scrape_products_from_page(url, headed=True)
        except Exception as exc:
            print(
                f"Erreur Playwright headed, fallback API: {exc}",
                file=sys.stderr,
            )
            products = []
        if products:
            print(
                f"Total produits collectés via Playwright = {len(products)}"
            )
            return products

    try:
        api_url_template, captured_headers = capture_api_request(
            url, headed=headed
        )
        use_api = True
    except Exception as exc:
        print(
            f"[WARN] Capture API impossible: {exc} -> fallback DOM 'Montrer plus'"
        )
        use_api = False

    if use_api:
        print(f"Captured API URL: {api_url_template}")

        parsed_url = urlsplit(api_url_template)
        query_pairs = parse_qsl(parsed_url.query, keep_blank_values=True)
        headers = merge_headers(captured_headers)
        products: List[Dict[str, Any]] = []

        try:
            start_key, end_key = detect_pagination_params(query_pairs)
            start_value, end_value = parse_pagination_values(
                query_pairs, start_key, end_key
            )
            step = max(end_value - start_value, 1)
            print(f"step size: {step}")

            start = start_value
            end = end_value

            while True:
                updated_query = update_query_params(
                    query_pairs, start_key, end_key, start, end
                )
                updated_url = urlunsplit(
                    parsed_url._replace(query=urlencode(updated_query, doseq=True))
                )
                payload = fetch_json(updated_url, headers)
                items = find_product_list(payload)
                print(f"Fetch start={start} end={end} -> items {len(items)}")
                if not items:
                    break
                for item in items:
                    products.append(build_product(item))
                start += step
                end += step
        except ValueError:
            page_key, page_value = detect_page_param(query_pairs)
            seen_urls: set[str] = set()
            page = page_value
            max_pages = 200

            while page <= max_pages:
                updated_query = update_single_query_param(
                    query_pairs, page_key, page
                )
                updated_url = urlunsplit(
                    parsed_url._replace(query=urlencode(updated_query, doseq=True))
                )
                if updated_url in seen_urls:
                    break
                seen_urls.add(updated_url)
                payload = fetch_json(updated_url, headers)
                items = find_product_list(payload)
                print(f"Fetch page={page} -> items {len(items)}")
                if not items:
                    break
                for item in items:
                    products.append(build_product(item))
                page += 1

        print(f"Total produits collectés = {len(products)}")
        return products

    products = scrape_products_from_page(url, headed=headed)
    print(f"Total produits collectés via DOM = {len(products)}")
    return products


def is_store_output_path(path: Path) -> bool:
    parts = path.parts
    return (
        len(parts) >= 4
        and parts[-4] == "public"
        and parts[-3] == "ikea"
        and parts[-1] == "data.json"
    )


def build_store_payload(args: argparse.Namespace) -> Optional[Dict[str, str]]:
    if not any(
        [
            args.store_display_name,
            args.store_city,
            args.store_province,
            args.store_slug,
        ]
    ):
        return None
    return {
        "displayName": args.store_display_name,
        "city": args.store_city,
        "province": args.store_province,
        "slug": args.store_slug,
    }


def validate_store_sample(
    products: List[Dict[str, Any]],
    expected_store_label: str,
    sample_size: int = 20,
    min_matches: int = 5,
) -> Dict[str, int]:
    normalized_expected = normalize_store_label(expected_store_label)
    sample = products[:sample_size]
    matched = 0
    for product in sample:
        in_stock_store = product.get("inStockStore")
        if (
            in_stock_store
            and normalize_store_label(in_stock_store) == normalized_expected
        ):
            matched += 1
    result = {"checked": len(sample), "matched": matched}
    if matched < min_matches:
        raise RuntimeError(
            "Validation du magasin échouée: "
            f"{matched}/{len(sample)} cartes correspondent à "
            f"{expected_store_label}."
        )
    return result


def validate_store_on_page(
    page: Any,
    expected_store_label: str,
    sample_size: int = STORE_VALIDATION_SAMPLE_SIZE,
    min_matches: int = STORE_VALIDATION_MIN_MATCHES,
) -> Dict[str, int]:
    normalized_expected = normalize_store_label(expected_store_label)
    cards = page.locator("div.plp-product-list__products > *")
    checked = 0
    matched = 0
    card_count = min(cards.count(), sample_size)
    for idx in range(card_count):
        card = cards.nth(idx)
        in_stock_store = extract_in_stock_store(card)
        if (
            in_stock_store
            and normalize_store_label(in_stock_store) == normalized_expected
        ):
            matched += 1
        checked += 1
    result = {"checked": checked, "matched": matched}
    if matched < min_matches:
        raise RuntimeError(
            "Validation du magasin échouée: "
            f"{matched}/{checked} cartes correspondent à "
            f"{expected_store_label}."
        )
    return result


def scrape_store_products(
    store_query: str,
    expected_store_label: str,
    headed: bool,
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser, context = create_context(playwright, headed=headed)
        page = context.new_page()
        set_store(page, store_query, expected_store_label)
        page.goto(FILTERED_LAST_CHANCE_URL, wait_until="domcontentloaded")
        dismiss_consent(page)
        page.wait_for_selector(
            "div.plp-product-list__products > *", timeout=45000
        )

        store_match_sample = validate_store_on_page(
            page, expected_store_label
        )
        products = collect_products_across_pages(page)

        print(
            "Total unique product urls = {count}".format(
                count=len(products)
            )
        )

        context.close()
        browser.close()

    return products, store_match_sample


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    headed = should_use_headed(args.headed)
    locations = parse_locations(args)
    store_ids = load_store_ids(args.store_ids)

    if args.store_query:
        if not args.expected_store_label or not args.store_slug:
            print(
                "Erreur: --store-query requiert --expected-store-label "
                "et --store-slug.",
                file=sys.stderr,
            )
            return 2
        output_path = (
            Path(args.out_base) / args.store_slug / "data.json"
        )
        products, store_match_sample = scrape_store_products(
            args.store_query,
            args.expected_store_label,
            headed=headed,
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output = {
            "source": FILTERED_LAST_CHANCE_URL,
            "count": len(products),
            "products": products,
            "expectedStore": args.expected_store_label,
            "storeMatchSample": store_match_sample,
            "store": {
                "query": args.store_query,
                "slug": args.store_slug,
            },
        }
        with output_path.open("w", encoding="utf-8") as handle:
            json.dump(output, handle, ensure_ascii=False, indent=2)
        print(
            "{count} produits enregistrés dans {output}.".format(
                count=len(products),
                output=output_path,
            )
        )
        return 0

    if not locations:
        expected_store = args.expected_store_label
        if expected_store:
            if not args.store_slug:
                print(
                    "Erreur: --store-slug est requis avec "
                    "--expected-store-label.",
                    file=sys.stderr,
                )
                return 2
            output_path = (
                Path(args.out_base) / args.store_slug / "data.json"
            )
            products = scrape_products_from_page(args.url, headed=headed)
            store_match_sample = validate_store_sample(
                products, expected_store
            )
        else:
            output_path = Path(args.output)
            if is_store_output_path(output_path):
                print(
                    "Erreur: --expected-store-label requis pour valider "
                    "public/ikea/<store>/data.json.\n"
                    "Exemple:\n"
                    "python scrape_ikea_last_chance.py "
                    "--expected-store-label \"IKEA Montréal\" "
                    "--store-slug \"montreal-qc\" "
                    "--out-base \"public/ikea\"",
                    file=sys.stderr,
                )
                return 2
            products = scrape_url(args.url, headed)
            store_match_sample = None
        output_path.parent.mkdir(parents=True, exist_ok=True)
        store_payload = build_store_payload(args)
        output = {
            "source": args.url,
            "count": len(products),
            "products": products,
        }
        if store_payload:
            output["store"] = store_payload
        if expected_store:
            output["expectedStore"] = expected_store
        if store_match_sample:
            output["storeMatchSample"] = store_match_sample
        with output_path.open("w", encoding="utf-8") as handle:
            json.dump(output, handle, ensure_ascii=False, indent=2)
        print(
            "{count} produits enregistrés dans {output}.".format(
                count=len(products),
                output=args.output,
            )
        )
        return 0

    results = []
    for location in locations:
        store_id = store_ids.get(location)
        if not store_id:
            print(
                f"[WARN] Aucun storeId pour {location}. URL de base utilisée."
            )
        location_url = build_location_url(args.url, store_id, args.store_param)
        products = scrape_url(location_url, headed)
        results.append(
            {
                "location": location,
                "source": location_url,
                "count": len(products),
                "products": products,
            }
        )

    output = {
        "source": args.url,
        "locations": results,
    }

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as handle:
        json.dump(output, handle, ensure_ascii=False, indent=2)

    print(
        "{count} emplacements enregistrés dans {output}.".format(
            count=len(results),
            output=args.output,
        )
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())
