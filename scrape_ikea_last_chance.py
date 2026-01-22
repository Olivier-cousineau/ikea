#!/usr/bin/env python3
"""Scrape IKEA Last Chance listings from the IKEA product list API."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.error import HTTPError
from urllib.request import Request, urlopen


DEFAULT_URL = "https://www.ikea.com/ca/fr/cat/last-chance/"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
API_BASE = "https://sik.search.blue.cdtapps.com/ca/fr/product-list-page/more-products"
API_URL_TEMPLATE = (
    "{base}?category={category}&sort=RELEVANCE&start={start}&end={end}&c=lf&v={version}"
)
CATEGORY = "last-chance"
API_VERSIONS = ("20211021", "20240101", "20240601")
PAGE_SIZE = 60


def build_request_url(
    start: int,
    end: int,
    category: str,
    version: str,
) -> str:
    return API_URL_TEMPLATE.format(
        base=API_BASE,
        category=category,
        start=start,
        end=end,
        version=version,
    )


def fetch_page(
    start: int,
    end: int,
    category: str = CATEGORY,
) -> Dict[str, Any]:
    last_error: Optional[HTTPError] = None
    for version in API_VERSIONS:
        url = build_request_url(start, end, category, version)
        request = Request(
            url,
            headers={
                "User-Agent": DEFAULT_USER_AGENT,
                "Accept": "application/json",
                "Accept-Language": "fr-CA,fr;q=0.9,en;q=0.8",
            },
        )
        try:
            with urlopen(request, timeout=30) as response:
                return json.load(response)
        except HTTPError as exc:
            if exc.code == 400:
                print(
                    f"HTTP 400 for URL: {url}",
                    file=sys.stderr,
                )
                last_error = exc
                continue
            raise
    if last_error is not None:
        raise last_error
    raise RuntimeError("Aucune version d'API disponible.")


def extract_price(product: Dict[str, Any]) -> Optional[Any]:
    price = product.get("price")
    if isinstance(price, dict):
        return price.get("current") or price.get("price")
    return None


def build_product(product: Dict[str, Any]) -> Dict[str, Any]:
    link_url = product.get("linkUrl") or product.get("pipUrl") or ""
    if link_url and not link_url.startswith("http"):
        link_url = f"https://www.ikea.com{link_url}"
    return {
        "name": product.get("name"),
        "typeName": product.get("typeName"),
        "price": extract_price(product),
        "url": link_url,
        "image": product.get("imageUrl") or (product.get("image") or {}).get("url"),
    }


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

    products: List[Dict[str, Any]] = []
    start = 0
    end = PAGE_SIZE
    total_count: Optional[int] = None

    while True:
        payload = fetch_page(start, end)
        items = payload.get("items") or payload.get("products") or []
        if total_count is None:
            total_count = payload.get("totalCount")
        print(f"Fetch start={start} end={end} → items {len(items)}")
        if not items:
            break
        for item in items:
            if isinstance(item, dict):
                products.append(build_product(item))
        if total_count is not None and len(products) >= total_count:
            break
        start += PAGE_SIZE
        end += PAGE_SIZE

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
