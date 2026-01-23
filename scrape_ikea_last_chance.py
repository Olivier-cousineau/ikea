#!/usr/bin/env python3
"""Scrape IKEA Last Chance listings from the IKEA product list API."""

from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
import time
from pathlib import Path
from threading import Event
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen


DEFAULT_URL = "https://www.ikea.com/ca/fr/cat/last-chance/"
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
DEFAULT_HEADERS = {
    "User-Agent": DEFAULT_USER_AGENT,
    "Accept": "application/json",
    "Accept-Language": "fr-CA,fr;q=0.9,en;q=0.8",
    "Referer": DEFAULT_URL,
    "Origin": "https://www.ikea.com",
}


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
    primary_count = page.locator("div.plp-product-list_products > *").count()
    if primary_count:
        return primary_count
    return page.locator("a[href*='/p/']").count()


def load_more_until_done(page: Any) -> None:
    max_clicks = 50
    clicks = 0
    last_count = 0
    while clicks < max_clicks:
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(1500)
        more = page.locator('a[aria-label="Afficher plus de produits"]')
        button = more.first if more.count() else find_show_more(page)
        if not button:
            break
        try:
            if not button.is_visible():
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(1000)
            button.scroll_into_view_if_needed(timeout=2000)
            current_count = count_products(page)
            try:
                button.click(timeout=8000)
            except Exception:
                page.evaluate("(el) => el.click()", button)
            try:
                page.wait_for_function(
                    "(selector, before) => "
                    "document.querySelectorAll(selector).length > before",
                    ("a[href*='/p/']", current_count),
                    timeout=10000,
                )
            except Exception:
                pass
            updated_count = count_products(page)
            clicked_load_more = updated_count > current_count
            print(
                "Load more: before={before} after={after} clickedLoadMore={clicked}".format(
                    before=current_count,
                    after=updated_count,
                    clicked=clicked_load_more,
                )
            )
            if updated_count <= last_count and updated_count <= current_count:
                break
            last_count = updated_count
            clicks += 1
        except Exception:
            break


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

        load_more_until_done(page)

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
        page.wait_for_selector("a[href*='/p/']", timeout=45000)
        load_more_until_done(page)

        payloads: List[Any] = []
        next_data = page.evaluate("() => window.__NEXT_DATA__ || null")
        if next_data:
            payloads.append(next_data)
        nuxt_data = page.evaluate("() => window.__NUXT__ || null")
        if nuxt_data:
            payloads.append(nuxt_data)
        for script in page.locator(
            "script[type='application/ld+json']"
        ).all_inner_texts():
            try:
                payloads.append(json.loads(script))
            except json.JSONDecodeError:
                continue

        items: List[Dict[str, Any]] = []
        for payload in payloads:
            items = find_product_list(payload)
            if items:
                break

        products: List[Dict[str, Any]] = []
        if items:
            for item in items:
                products.append(build_product(item))

        if not products:
            link_locator = page.locator("a[href*='/p/']")
            link_count = link_locator.count()
            seen: set[str] = set()
            for idx in range(link_count):
                link = link_locator.nth(idx)
                href = link.get_attribute("href") or ""
                if not href:
                    continue
                if not href.startswith("http"):
                    href = f"https://www.ikea.com{href}"
                if href in seen:
                    continue
                seen.add(href)
                try:
                    text = link.inner_text().strip()
                except Exception:
                    text = ""
                name = None
                type_name = None
                if text:
                    lines = [line.strip() for line in text.splitlines() if line.strip()]
                    if lines:
                        name = lines[0]
                    if len(lines) > 1:
                        type_name = lines[1]
                image = None
                img_locator = link.locator("img").first
                if img_locator.count():
                    image = (
                        img_locator.get_attribute("src")
                        or img_locator.get_attribute("data-src")
                        or img_locator.get_attribute("data-srcset")
                    )
                products.append(
                    {
                        "name": name,
                        "typeName": type_name,
                        "price": None,
                        "url": href,
                        "image": image,
                    }
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
    target_value: int,
) -> List[Tuple[str, str]]:
    updated: List[Tuple[str, str]] = []
    for key, value in query_pairs:
        if key == target_key:
            updated.append((key, str(target_value)))
        else:
            updated.append((key, value))
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
        "--headed",
        action="store_true",
        help="Lance Playwright avec headless=False (ou HEADED=1).",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    headed = should_use_headed(args.headed)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if headed:
        try:
            products = scrape_products_from_page(args.url, headed=True)
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
            output = {
                "source": args.url,
                "count": len(products),
                "products": products,
            }
            with output_path.open("w", encoding="utf-8") as handle:
                json.dump(output, handle, ensure_ascii=False, indent=2)
            print(
                "{count} produits enregistrés dans {output}.".format(
                    count=len(products),
                    output=args.output,
                )
            )
            return 0

    try:
        api_url_template, captured_headers = capture_api_request(
            args.url, headed=headed
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
    else:
        products = scrape_products_from_page(args.url, headed=headed)
        print(f"Total produits collectés via DOM = {len(products)}")

    output = {
        "source": args.url,
        "count": len(products),
        "products": products,
    }

    with output_path.open("w", encoding="utf-8") as handle:
        json.dump(output, handle, ensure_ascii=False, indent=2)

    print(
        "{count} produits enregistrés dans {output}.".format(
            count=len(products),
            output=args.output,
        )
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())
