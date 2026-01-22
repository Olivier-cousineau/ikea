#!/usr/bin/env python3
"""Scrape IKEA Last Chance listings from the IKEA product list API."""

from __future__ import annotations

import argparse
import gzip
import json
import sys
import time
from pathlib import Path
from threading import Event
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen


DEFAULT_URL = "https://www.ikea.com/ca/fr/cat/last-chance/"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
DEBUG_SCREENSHOT = "playwright_debug.png"
DEBUG_HTML = "playwright_debug.html"
DEBUG_REQUESTS = "playwright_debug_requests.txt"
DEFAULT_HEADERS = {
    "User-Agent": DEFAULT_USER_AGENT,
    "Accept": "application/json",
    "Accept-Language": "fr-CA,fr;q=0.9,en;q=0.8",
    "Referer": DEFAULT_URL,
    "Origin": "https://www.ikea.com",
}


def capture_api_request(page_url: str) -> Tuple[str, Dict[str, str]]:
    from playwright.sync_api import sync_playwright

    captured_url: Optional[str] = None
    captured_headers: Dict[str, str] = {}
    captured_event = Event()
    request_log: List[str] = []

    def handle_request(request: Any) -> None:
        nonlocal captured_url, captured_headers
        request_url = request.url
        if "product-list-page/more-products" in request_url:
            if not captured_event.is_set():
                captured_url = request_url
                headers = request.headers or {}
                captured_headers = extract_headers(headers)
                captured_event.set()
        if (
            "product-list-page" in request_url
            or "sik.search.blue.cdtapps.com" in request_url
        ):
            request_log.append(request_url)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(user_agent=DEFAULT_USER_AGENT)
        page = context.new_page()
        page.on("request", handle_request)
        page.goto(page_url, wait_until="domcontentloaded")

        start_time = time.monotonic()
        timeout_seconds = 30
        while not captured_event.is_set():
            if time.monotonic() - start_time >= timeout_seconds:
                break
            page.wait_for_timeout(250)

        if not captured_event.is_set():
            page.screenshot(path=DEBUG_SCREENSHOT, full_page=True)
            html = page.content()
            Path(DEBUG_HTML).write_text(html, encoding="utf-8")
            Path(DEBUG_REQUESTS).write_text(
                "\n".join(request_log), encoding="utf-8"
            )
        context.close()
        browser.close()

    if not captured_url:
        raise RuntimeError(
            "Aucune requête capturée après 30s. "
            f"Debug: {DEBUG_SCREENSHOT}, {DEBUG_HTML}, {DEBUG_REQUESTS}"
        )

    return captured_url, captured_headers


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
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    api_url_template, captured_headers = capture_api_request(args.url)
    print(f"Captured API URL: {api_url_template}")

    parsed_url = urlsplit(api_url_template)
    query_pairs = parse_qsl(parsed_url.query, keep_blank_values=True)
    start_key, end_key = detect_pagination_params(query_pairs)
    start_value, end_value = parse_pagination_values(
        query_pairs, start_key, end_key
    )
    step = max(end_value - start_value, 1)
    print(f"step size: {step}")

    headers = merge_headers(captured_headers)

    products: List[Dict[str, Any]] = []
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

    print(f"Total produits collectés = {len(products)}")

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
