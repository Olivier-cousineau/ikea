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


def norm_space(value: Optional[str]) -> str:
    return re.sub(r"\s+", " ", (value or "").strip())


def parse_price(text: str) -> Optional[str]:
    match = re.search(r"\$\s*[\d.,]+", text)
    if not match:
        return None
    return match.group(0).strip()


def close_overlays(page) -> None:
    selectors = [
        "button:has-text('Accept all')",
        "button:has-text('Accept')",
        "button:has-text('Tout accepter')",
        "button:has-text('Accepter')",
        "button:has-text('OK')",
        "button:has-text('Fermer')",
        "button:has-text('Continuer')",
        "[role='button']:has-text('Accept all')",
        "[role='button']:has-text('Accept')",
        "[role='button']:has-text('Tout accepter')",
        "[role='button']:has-text('Accepter')",
        "[role='button']:has-text('OK')",
        "[role='button']:has-text('Fermer')",
        "[role='button']:has-text('Continuer')",
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


def find_products_container(page):
    main = page.locator("main")
    if main.count() == 0:
        main = page.locator("body")

    candidates = main.locator(
        "xpath=.//*[self::div or self::section or self::ul or self::ol]"
        "[.//a[contains(@href,'/p/')]]"
    )
    best_candidate = None
    best_count = 0
    for i in range(min(candidates.count(), 25)):
        candidate = candidates.nth(i)
        try:
            count = candidate.locator("a[href*='/p/']").count()
        except Exception:
            continue
        if count > best_count:
            best_candidate = candidate
            best_count = count

    if best_candidate is not None and best_count >= 5:
        return best_candidate
    return None


def list_visible_product_urls(page, container) -> List[str]:
    handle = None
    if container is not None:
        try:
            handle = container.element_handle()
        except Exception:
            handle = None

    return page.evaluate(
        """
        (rootEl) => {
            const root =
                rootEl || document.querySelector("main") || document.body || document;
            const anchors = Array.from(root.querySelectorAll("a[href*='/p/']"));
            const urls = [];
            const seen = new Set();
            for (const anchor of anchors) {
                const rect = anchor.getBoundingClientRect();
                const style = window.getComputedStyle(anchor);
                const visible =
                    rect.width > 0 &&
                    rect.height > 0 &&
                    style &&
                    style.display !== "none" &&
                    style.visibility !== "hidden";
                if (!visible) {
                    continue;
                }
                const href = anchor.href || anchor.getAttribute("href");
                if (!href || !href.includes("/p/")) {
                    continue;
                }
                if (seen.has(href)) {
                    continue;
                }
                seen.add(href);
                urls.push(href);
            }
            return urls;
        }
        """,
        handle,
    )


def count_product_cards(page) -> int:
    selectors = [
        "[data-testid*='product']",
        "div[class*='plp-product']",
        "a[href*='/p/']",
    ]
    try:
        return page.evaluate(
            """
            (selectors) => {
                const isVisible = (el) => {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return (
                        rect.width > 0 &&
                        rect.height > 0 &&
                        style &&
                        style.display !== "none" &&
                        style.visibility !== "hidden"
                    );
                };
                const counts = selectors.map((selector) => {
                    const elements = Array.from(document.querySelectorAll(selector));
                    return elements.filter(isVisible).length;
                });
                return Math.max(...counts, 0);
            }
            """,
            selectors,
        )
    except Exception:
        return 0


def get_show_more_button(page):
    candidates = [
        page.locator("button:has-text('Montrer plus')").first,
        page.locator("button:has-text('Afficher plus')").first,
        page.get_by_role("button", name="Montrer plus").first,
        page.get_by_role("button", name="Show more").first,
        page.locator("[role='button']:has-text('Montrer plus')").first,
        page.locator("button:has-text('Show more')").first,
        page.locator("[role='button']:has-text('Show more')").first,
    ]
    for candidate in candidates:
        try:
            if candidate.count() > 0:
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
        button.click(timeout=5000)
    except Exception:
        try:
            handle = button.element_handle()
            if handle is None:
                return {"clicked": False, "text": btn_text, "found": True}
            page.evaluate("(el) => el.click()", handle)
        except Exception:
            return {"clicked": False, "text": btn_text, "found": True}

    try:
        page.wait_for_load_state("networkidle", timeout=10000)
    except PlaywrightTimeoutError:
        pass
    except Exception:
        pass

    return {"clicked": True, "text": btn_text, "found": True}


def log_visible_plus_texts(page, limit: int = 50) -> None:
    texts = page.evaluate(
        """
        (limit) => {
            const matcher = /plus/i;
            const elements = Array.from(document.querySelectorAll("body *"));
            const output = [];
            for (const el of elements) {
                const text = (el.innerText || "").trim();
                if (!text || !matcher.test(text)) {
                    continue;
                }
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                const visible =
                    rect.width > 0 &&
                    rect.height > 0 &&
                    style &&
                    style.display !== "none" &&
                    style.visibility !== "hidden";
                if (!visible) {
                    continue;
                }
                output.push(text);
            }
            return output.slice(-limit);
        }
        """,
        limit,
    )
    if texts:
        print("Derniers textes visibles contenant 'plus':")
        for entry in texts:
            print(f"- {entry}")


def load_all_products(page, debug_html: str, debug_screenshot: str) -> bool:
    stable_rounds = 0
    button_seen = False
    debug_dumped = False

    for i in range(1, 301):
        before = count_product_cards(page)
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(1200)

        close_overlays(page)
        click_info = click_show_more_if_possible(page)
        clicked_load_more = click_info["clicked"]
        btn_text = click_info["text"]
        if click_info["found"]:
            button_seen = True

        if clicked_load_more:
            try:
                page.wait_for_function(
                    """
                    (beforeCount) => {
                        const selectors = [
                            "[data-testid*='product']",
                            "div[class*='plp-product']",
                            "a[href*='/p/']",
                        ];
                        const isVisible = (el) => {
                            const rect = el.getBoundingClientRect();
                            const style = window.getComputedStyle(el);
                            return (
                                rect.width > 0 &&
                                rect.height > 0 &&
                                style &&
                                style.display !== "none" &&
                                style.visibility !== "hidden"
                            );
                        };
                        const counts = selectors.map((selector) => {
                            const elements = Array.from(
                                document.querySelectorAll(selector)
                            );
                            return elements.filter(isVisible).length;
                        });
                        const current = Math.max(...counts, 0);
                        return current > beforeCount;
                    }
                    """,
                    arg=before,
                    timeout=15000,
                )
            except Exception:
                pass

        after = count_product_cards(page)
        print(
            "Scroll/Load #{i}: before={before} after={after} "
            "clickedLoadMore={clicked} btnText={text}".format(
                i=i,
                before=before,
                after=after,
                clicked=clicked_load_more,
                text=btn_text or "None",
            )
        )

        if i >= 3 and not button_seen and not debug_dumped:
            html = page.content()
            Path(debug_html).write_text(html, encoding="utf-8")
            page.screenshot(path=debug_screenshot, full_page=True)
            log_visible_plus_texts(page)
            debug_dumped = True

        if after <= before:
            stable_rounds += 1
        else:
            stable_rounds = 0

        if stable_rounds >= 3:
            break

    page.mouse.wheel(0, 600)
    page.wait_for_timeout(800)
    total_links = count_product_cards(page)
    print(f"Scroll ended: totalItems={total_links}")
    return stable_rounds >= 3


def extract_products(page, base_url: str) -> List[Dict[str, Any]]:
    container = find_products_container(page)
    if container is not None:
        cards = container.locator("a[href*='/p/']")
    else:
        cards = page.locator("main a[href*='/p/']")
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

            item_container = anchor.locator(
                "xpath=ancestor::*[self::li or self::article or self::div][1]"
            )
            raw_text = ""
            if item_container.count():
                raw_text = item_container.inner_text(timeout=2000)
            if not raw_text:
                raw_text = anchor.inner_text(timeout=2000)
            text = norm_space(raw_text)

            name = None
            if text:
                name = text.split("$")[0].strip() or None

            image = None
            if item_container.count():
                try:
                    img = item_container.locator("img").first
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
        context = browser.new_context(locale="fr-CA", user_agent=DEFAULT_USER_AGENT)
        page = context.new_page()
        try:
            page.goto(args.url, wait_until="networkidle", timeout=60000)
        except PlaywrightTimeoutError:
            page.goto(args.url, wait_until="domcontentloaded", timeout=60000)

        close_overlays(page)
        warm_up_page(page)
        wait_for_products(page)
        stopped_by_stable = load_all_products(
            page,
            debug_html=args.debug_html,
            debug_screenshot=args.debug_screenshot,
        )
        container = find_products_container(page)
        total_links = len(list_visible_product_urls(page, container))
        if stopped_by_stable and total_links < 200:
            html = page.content()
            Path(args.debug_html).write_text(html, encoding="utf-8")
            page.screenshot(path=args.debug_screenshot, full_page=True)

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

        print(f"{len(products)} produits enregistrés dans {args.output}")
        context.close()
        browser.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
