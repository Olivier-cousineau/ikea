#!/usr/bin/env python3
"""Scrape IKEA Last Chance listings from a category URL."""

from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any, Dict, Iterable, List, Optional
from urllib.request import Request, urlopen


DEFAULT_URL = (
    "https://www.ikea.com/ca/fr/cat/last-chance/"
    "?filters=f-availability%3AAVAILABLE_IN_STORE"
)


def fetch_html(url: str) -> str:
    request = Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
        },
    )
    with urlopen(request) as response:
        return response.read().decode("utf-8")


def extract_next_data(html: str) -> Dict[str, Any]:
    match = re.search(
        r'__NEXT_DATA__" type="application/json">(.*?)</script>',
        html,
        re.DOTALL,
    )
    if not match:
        raise ValueError("Impossible de trouver __NEXT_DATA__ dans la page HTML.")
    return json.loads(match.group(1))


def iter_dicts(data: Any) -> Iterable[Dict[str, Any]]:
    if isinstance(data, dict):
        yield data
        for value in data.values():
            yield from iter_dicts(value)
    elif isinstance(data, list):
        for item in data:
            yield from iter_dicts(item)


def parse_price(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        for key in ("price", "priceValue", "priceNumeral", "currentPrice", "formatted"):
            if key in value:
                parsed = parse_price(value[key])
                if parsed:
                    return parsed
    return None


def build_product(candidate: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    merged = dict(candidate)
    if isinstance(candidate.get("product"), dict):
        merged.update(candidate["product"])

    name = merged.get("name") or merged.get("productName")
    url = merged.get("pipUrl") or merged.get("productUrl") or merged.get("url")
    if not (name and url):
        return None

    price = parse_price(
        merged.get("price")
        or merged.get("priceValue")
        or merged.get("priceNumeral")
        or merged.get("currentPrice")
    )

    return {
        "name": str(name).strip(),
        "url": str(url).strip(),
        "price": price,
        "typeName": merged.get("typeName") or merged.get("type"),
    }


def extract_products(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    products: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for entry in iter_dicts(data):
        product = build_product(entry)
        if not product:
            continue
        key = product["url"]
        if key in seen:
            continue
        seen.add(key)
        products.append(product)
    return products


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
    html = fetch_html(args.url)
    data = extract_next_data(html)
    products = extract_products(data)

    output = {
        "source": args.url,
        "count": len(products),
        "products": products,
    }

    with open(args.output, "w", encoding="utf-8") as handle:
        json.dump(output, handle, ensure_ascii=False, indent=2)

    print(f"{len(products)} produits enregistrés dans {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
