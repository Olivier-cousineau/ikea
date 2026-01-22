#!/usr/bin/env python3
"""Scrape IKEA Last Chance listings from a category URL."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


DEFAULT_URL = (
    "https://www.ikea.com/ca/fr/cat/last-chance/"
    "?filters=f-availability%3AAVAILABLE_IN_STORE"
)
DEFAULT_DEBUG_HTML = "debug_ikea_last_chance.html"
DEFAULT_DEBUG_SCREENSHOT = "debug_ikea_last_chance.png"


def norm_space(value: Optional[str]) -> str:
    return re.sub(r"\s+", " ", (value or "").strip())


def parse_price(text: str) -> Optional[str]:
    match = re.search(r"\$\s*[\d.,]+", text)
    if not match:
        return None
    return match.group(0).strip()


def close_popups(page) -> None:
    selectors = [
        "button:has-text('Accept all')",
        "button:has-text('Accept')",
        "button:has-text('Tout accepter')",
        "button:has-text('Accepter')",
        "button[aria-label*='close' i]",
        "button[aria-label*='fermer' i]",
    ]
    for selector in selectors:
        try:
            button = page.locator(selector).first
            if button.is_visible(timeout=1500):
                button.click(timeout=1500)
        except PlaywrightTimeoutError:
            continue
        except Exception:
            continue


def warm_up_page(page) -> None:
    try:
        page.wait_for_load_state("networkidle", timeout=10000)
    except PlaywrightTimeoutError:
        pass
    except Exception:
        pass

    for _ in range(3):
        page.mouse.wheel(0, 2500)
        page.wait_for_timeout(800)


def wait_for_products(page) -> None:
    try:
        page.wait_for_selector("a[href*='/p/']", timeout=60000)
        return
    except PlaywrightTimeoutError:
        pass
    except Exception:
        pass

    try:
        page.wait_for_function(
            "document.querySelectorAll(\"a[href*='/p/']\").length >= 5",
            timeout=60000,
        )
    except Exception:
        pass


def load_all_products(page) -> None:
    button_selector = (
        "button:has-text('Show more'), button:has-text('Voir plus'), "
        "button:has-text('Afficher plus')"
    )
    attempts_without_growth = 0
    product_links = page.locator("a[href*='/p/']")

    for i in range(1, 201):
        button = page.locator(button_selector).first
        try:
            if button.count() == 0:
                break
            if not button.is_visible(timeout=1500):
                break
            if not button.is_enabled():
                break
        except PlaywrightTimeoutError:
            break
        except Exception:
            break

        before = product_links.count()
        try:
            button.click(timeout=5000)
        except PlaywrightTimeoutError:
            break
        except Exception:
            break

        page.wait_for_timeout(800)
        page.mouse.wheel(0, 2500)
        page.wait_for_timeout(800)

        try:
            page.wait_for_function(
                "prev => document.querySelectorAll(\"a[href*='/p/']\").length > prev",
                arg=before,
                timeout=10000,
            )
        except Exception:
            pass

        after = product_links.count()
        print(f"Load more click #{i}: before={before} after={after}")

        if after <= before:
            attempts_without_growth += 1
        else:
            attempts_without_growth = 0

        if attempts_without_growth >= 3:
            break

    total_links = product_links.count()
    print(f"Load more ended: totalLinks={total_links}")


def extract_products(page, base_url: str) -> List[Dict[str, Any]]:
    cards = page.locator("a[href*='/p/']")
    count = cards.count()
    if count == 0:
        return []

    items: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for i in range(min(count, 5000)):
        try:
            anchor = cards.nth(i)
            href = anchor.get_attribute("href") or ""
            if "/p/" not in href:
                continue
            url = href if href.startswith("http") else urljoin(base_url, href)
            if url in seen:
                continue
            seen.add(url)

            container = anchor.locator(
                "xpath=ancestor::*[self::li or self::article or self::div][1]"
            )
            raw_text = ""
            if container.count():
                raw_text = container.inner_text(timeout=2000)
            if not raw_text:
                raw_text = anchor.inner_text(timeout=2000)
            text = norm_space(raw_text)

            name = None
            if text:
                name = text.split("$")[0].strip() or None

            image = None
            if container.count():
                try:
                    img = container.locator("img").first
                    image = img.get_attribute("src") or img.get_attribute("data-src")
                except Exception:
                    image = None

            items.append(
                {
                    "name": name,
                    "url": url,
                    "price": parse_price(text),
                    "typeName": None,
                    "image": image,
                }
            )
        except Exception:
            continue

    return items


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
        "--debug-html",
        default=DEFAULT_DEBUG_HTML,
        help="Chemin du snapshot HTML en cas d'echec",
    )
    parser.add_argument(
        "--debug-screenshot",
        default=DEFAULT_DEBUG_SCREENSHOT,
        help="Chemin du screenshot en cas d'echec",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(locale="fr-CA")
        page.goto(args.url, wait_until="domcontentloaded", timeout=60000)

        close_popups(page)
        warm_up_page(page)
        wait_for_products(page)
        load_all_products(page)

        products = extract_products(page, base_url="https://www.ikea.com")
        if not products:
            html = page.content()
            Path(args.debug_html).write_text(html, encoding="utf-8")
            page.screenshot(path=args.debug_screenshot, full_page=True)
            browser.close()
            raise RuntimeError(
                "Aucun produit trouvé. "
                f"Snapshot HTML sauvegardé: {args.debug_html}. "
                f"Screenshot sauvegardé: {args.debug_screenshot}"
            )

        output = {
            "source": args.url,
            "count": len(products),
            "products": products,
        }

        with output_path.open("w", encoding="utf-8") as handle:
            json.dump(output, handle, ensure_ascii=False, indent=2)

        print(f"{len(products)} produits enregistrés dans {args.output}")
        browser.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
