#!/usr/bin/env python3
"""Scrape IKEA Last Chance listings from the IKEA product list API."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.request import Request, urlopen


DEFAULT_URL = "https://www.ikea.com/ca/fr/cat/last-chance/"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
API_URL_TEMPLATE = (
    "https://sik.search.blue.cdtapps.com/ca/fr/product-list-page"
    "?category={category}&page={page}"
)
CATEGORY = "last-chance"


def fetch_page(page: int, category: str = CATEGORY) -> Dict[str, Any]:
    url = API_URL_TEMPLATE.format(category=category, page=page)
    request = Request(
        url,
        headers={
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept": "application/json",
        },
    )
    with urlopen(request, timeout=30) as response:
        return json.load(response)


def extract_price(product: Dict[str, Any]) -> Optional[Any]:
    price = product.get("price")
    if isinstance(price, dict):
        return price.get("current") or price.get("price")
    return None


def build_product(product: Dict[str, Any]) -> Dict[str, Any]:
    link_url = product.get("linkUrl") or ""
    return {
        "name": product.get("name"),
        "typeName": product.get("typeName"),
        "price": extract_price(product),
        "url": f"https://www.ikea.com{link_url}",
        "image": (product.get("image") or {}).get("url"),
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
    page = 1

    while True:
        payload = fetch_page(page)
        items = payload.get("products") or []
        print(f"Fetch page {page} → items {len(items)}")
        if not items:
            break
        for item in items:
            if isinstance(item, dict):
                products.append(build_product(item))
        page += 1

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
