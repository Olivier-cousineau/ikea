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
DEFAULT_DEBUG_HTML = "debug_ikea.html"
DEFAULT_DEBUG_SCREENSHOT = "debug_ikea.png"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
NO_PRODUCTS_HTML = Path("outputs/debug/ikea_no_products.html")
NO_PRODUCTS_SCREENSHOT = Path("outputs/debug/ikea_no_products.png")


def norm_space(value: Optional[str]) -> str:
    return re.sub(r"\s+", " ", (value or "").replace("\xa0", " ").strip())


def close_overlays(page) -> None:
    selectors = [
        "button:has-text('Accept all')",
        "button:has-text('Accept')",
        "button:has-text('Tout accepter')",
        "button:has-text('Accepter')",
        "button:has-text('Accepter les cookies')",
        "button:has-text('OK')",
        "button:has-text('Fermer')",
        "button:has-text('Continuer')",
        "[role='button']:has-text('Accept all')",
        "[role='button']:has-text('Accept')",
        "[role='button']:has-text('Tout accepter')",
        "[role='button']:has-text('Accepter')",
        "[role='button']:has-text('Accepter les cookies')",
        "[role='button']:has-text('OK')",
        "[role='button']:has-text('Fermer')",
        "[role='button']:has-text('Continuer')",
        "button[aria-label*='close' i]",
        "button[aria-label*='fermer' i]",
        "button#onetrust-accept-btn-handler",
        "button[aria-label*='cookie' i]",
        "[id*='cookie' i] button",
        "[id*='consent' i] button",
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


def debug_dump_no_products(page) -> None:
    NO_PRODUCTS_HTML.parent.mkdir(parents=True, exist_ok=True)
    try:
        html = page.content()
    except Exception:
        html = ""
    NO_PRODUCTS_HTML.write_text(html, encoding="utf-8")
    try:
        page.screenshot(path=str(NO_PRODUCTS_SCREENSHOT), full_page=True)
    except Exception:
        pass
    if html:
        print("Debug HTML (3000 premiers caractères):")
        print(html[:3000])


def wait_for_product_list(page) -> bool:
    try:
        page.wait_for_selector("div.plp-product-list_products", timeout=45000)
        return True
    except PlaywrightTimeoutError:
        pass
    except Exception:
        pass

    try:
        page.wait_for_selector("a[href*='/p/']", timeout=45000)
        return True
    except PlaywrightTimeoutError:
        debug_dump_no_products(page)
        return False
    except Exception:
        debug_dump_no_products(page)
        return False


def get_total_count(page) -> Optional[int]:
    try:
        data_category = page.locator(".js-product-list").get_attribute(
            "data-category"
        )
    except Exception:
        data_category = None
    if not data_category:
        return None
    try:
        payload = json.loads(data_category)
    except json.JSONDecodeError:
        return None
    total_count = payload.get("totalCount")
    if isinstance(total_count, int):
        return total_count
    return None


def count_product_cards(page) -> int:
    primary = page.locator("div.plp-product-list_products > *").count()
    if primary:
        return primary
    fallback = page.locator("a[href*='/p/']").count()
    return fallback


def get_show_more_button(page):
    candidates = [
        page.locator(
            "div.plp-catalog-bottom-container button:has-text('Montrer plus')"
        ).first,
        page.locator(
            "div.plp-catalog-bottom-container button:has-text('Show more')"
        ).first,
        page.locator("[role='button']:has-text('Montrer plus')").first,
        page.locator("[role='button']:has-text('Show more')").first,
    ]
    for candidate in candidates:
        try:
            if candidate.count() > 0 and candidate.is_visible(timeout=1500):
                return candidate
        except Exception:
            continue
    return None


def click_show_more_if_possible(page) -> Dict[str, Optional[str]]:
    button = get_show_more_button(page)
    if button is None:
        return {"clicked": False, "text": None, "found": False}

    btn_text = None
    try:
        btn_text = norm_space(button.inner_text(timeout=1500))
    except Exception:
        btn_text = None

    try:
        if not button.is_visible(timeout=1500) or not button.is_enabled():
            return {"clicked": False, "text": btn_text, "found": True}
    except Exception:
        return {"clicked": False, "text": btn_text, "found": True}

    try:
        button.scroll_into_view_if_needed()
        button.click(timeout=5000)
    except Exception:
        try:
            handle = button.element_handle()
            if handle is None:
                return {"clicked": False, "text": btn_text, "found": True}
            page.evaluate("(el) => el.click()", handle)
        except Exception:
            return {"clicked": False, "text": btn_text, "found": True}

    return {"clicked": True, "text": btn_text, "found": True}


def load_all_products(page, total_count: Optional[int]) -> None:
    stable_rounds = 0
    for i in range(1, 301):
        before = count_product_cards(page)
        if total_count is not None and before >= total_count:
            print(
                "Load loop #{i}: before={before} target={target} reached".format(
                    i=i,
                    before=before,
                    target=total_count,
                )
            )
            break

        click_info = click_show_more_if_possible(page)
        clicked = bool(click_info["clicked"])
        btn_text = click_info["text"]

        if click_info["found"]:
            try:
                page.wait_for_timeout(800)
                page.wait_for_load_state("networkidle", timeout=10000)
            except PlaywrightTimeoutError:
                pass
            except Exception:
                pass

        after = count_product_cards(page)
        print(
            "Load loop #{i}: before={before} after={after} clicked={clicked} "
            "btnText={btn_text}".format(
                i=i,
                before=before,
                after=after,
                clicked=clicked,
                btn_text=btn_text,
            )
        )

        if after <= before:
            stable_rounds += 1
        else:
            stable_rounds = 0

        if stable_rounds >= 3:
            break


def parse_image_src(srcset: Optional[str]) -> Optional[str]:
    if not srcset:
        return None
    parts = [part.strip() for part in srcset.split(",") if part.strip()]
    if not parts:
        return None
    first = parts[0].split(" ")[0]
    return first or None


def extract_products(page, base_url: str) -> List[Dict[str, Any]]:
    cards = page.locator("div.plp-product-list_products > *")
    if cards.count() == 0:
        cards = page.locator("a[href*='/p/']")

    badge_labels = {"Meilleur vendeur", "Best seller"}
    items_by_url: Dict[str, Dict[str, Any]] = {}

    for card in cards.all():
        try:
            anchor = card.locator("a[href*='/p/']").first
            href = anchor.get_attribute("href") if anchor.count() else None
            if not href or "/p/" not in href:
                continue
            url = href if href.startswith("http") else urljoin(base_url, href)

            name = None
            try:
                name = norm_space(card.locator("h3").first.inner_text())
            except Exception:
                name = None
            if not name:
                try:
                    name = norm_space(
                        card.locator("[data-testid*='product-title']")
                        .first.inner_text()
                    )
                except Exception:
                    name = None
            if not name:
                try:
                    label = anchor.get_attribute("aria-label")
                except Exception:
                    label = None
                if label:
                    label = norm_space(label)
                    label = re.split(r"\s+\$|\s+[-|]\s+", label)[0].strip()
                    name = label or None

            if name in badge_labels:
                name = None

            type_name = None
            try:
                type_text = norm_space(card.locator("p").first.inner_text())
            except Exception:
                type_text = None
            if type_text and not re.search(r"Maintenant|Jamais|Now|never", type_text):
                type_name = type_text

            price = None
            try:
                price = norm_space(
                    card.locator("[data-testid*='price']").first.inner_text()
                )
            except Exception:
                price = None
            if not price:
                try:
                    price = norm_space(
                        card.locator("span:has-text('$')").first.inner_text()
                    )
                except Exception:
                    price = None

            image = None
            try:
                img = card.locator("img").first
                image = img.get_attribute("src") or parse_image_src(
                    img.get_attribute("srcset")
                )
            except Exception:
                image = None

            item = {
                "name": name,
                "url": url,
                "price": price,
                "typeName": type_name,
                "image": image,
            }

            existing = items_by_url.get(url)
            if existing is None:
                items_by_url[url] = item
            else:
                existing_score = int(bool(existing.get("name"))) + int(
                    bool(existing.get("price"))
                )
                new_score = int(bool(name)) + int(bool(price))
                if new_score > existing_score:
                    items_by_url[url] = item
        except Exception:
            continue

    return list(items_by_url.values())


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
        context = browser.new_context(locale="fr-CA", user_agent=DEFAULT_USER_AGENT)
        page = context.new_page()
        try:
            page.goto(args.url, wait_until="networkidle", timeout=60000)
        except PlaywrightTimeoutError:
            page.goto(args.url, wait_until="domcontentloaded", timeout=60000)

        close_overlays(page)
        warm_up_page(page)
        if not wait_for_product_list(page):
            print(
                "Liste produits introuvable après attente. "
                f"Debug dump: {NO_PRODUCTS_HTML} / {NO_PRODUCTS_SCREENSHOT}"
            )
            context.close()
            browser.close()
            return 1

        total_count = get_total_count(page)
        load_all_products(page, total_count)

        products = extract_products(page, base_url="https://www.ikea.com")
        if not products:
            html = page.content()
            Path(args.debug_html).write_text(html, encoding="utf-8")
            page.screenshot(path=args.debug_screenshot, full_page=True)
            context.close()
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

        print(
            "{count} produits enregistrés dans {output}. totalCount={total}.".format(
                count=len(products),
                output=args.output,
                total=total_count,
            )
        )
        context.close()
        browser.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
