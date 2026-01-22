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


def find_load_more_button(page):
    primary = page.get_by_role(
        "button",
        name=re.compile(
            r"Montrer plus|Afficher plus|Voir plus|Show more|Load more",
            re.IGNORECASE,
        ),
    ).first
    if primary.count() > 0:
        return primary

    fallback_selectors = [
        "[role='button']:has-text('Montrer plus')",
        "[role='button']:has-text('Afficher plus')",
        "[role='button']:has-text('Voir plus')",
        "[role='button']:has-text('Show more')",
        "[role='button']:has-text('Load more')",
    ]
    fallback = page.locator(", ".join(fallback_selectors)).first
    if fallback.count() > 0:
        return fallback

    return (
        page.locator("text=/Montrer plus|Show more|Load more/i")
        .locator("xpath=ancestor-or-self::*[self::button or @role='button'][1]")
        .first
    )


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
    container = find_products_container(page)
    main_locator = page.locator("main")
    main_handle = None
    if main_locator.count() > 0:
        try:
            main_handle = main_locator.first.element_handle()
        except Exception:
            main_handle = None
    button_seen = False
    debug_dumped = False

    def count_products() -> int:
        if main_handle is not None:
            return len(list_visible_product_urls(page, main_locator.first))
        return len(list_visible_product_urls(page, container))

    for i in range(1, 301):
        before = count_products()
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(1200)

        clicked_load_more = False
        button = find_load_more_button(page)
        btn_text = None
        try:
            if button.count() > 0:
                button_seen = True
                btn_text = norm_space(button.inner_text(timeout=1500))
            if (
                button.count() > 0
                and button.is_visible(timeout=1500)
                and button.is_enabled()
            ):
                button.click(timeout=5000)
                clicked_load_more = True
                page.wait_for_timeout(1200)
                try:
                    root_handle = None
                    if main_handle is not None:
                        root_handle = main_handle
                    elif container is not None:
                        try:
                            root_handle = container.element_handle()
                        except Exception:
                            root_handle = None
                    page.wait_for_function(
                        """
                        (rootEl, beforeCount) => {
                            const root =
                                rootEl ||
                                document.querySelector("main") ||
                                document.body ||
                                document;
                            const anchors = Array.from(
                                root.querySelectorAll("a[href*='/p/']")
                            );
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
                                seen.add(href);
                            }
                            return seen.size > beforeCount;
                        }
                        """,
                        arg=(root_handle, before),
                        timeout=15000,
                    )
                except Exception:
                    pass
        except PlaywrightTimeoutError:
            pass
        except Exception:
            pass

        after = count_products()
        print(
            "Scroll #{i}: before={before} after={after} "
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

        if after == before:
            stable_rounds += 1
        else:
            stable_rounds = 0

        if stable_rounds >= 8:
            break

    page.mouse.wheel(0, 600)
    page.wait_for_timeout(800)
    total_links = count_products()
    print(f"Scroll ended: totalLinks={total_links}")
    return stable_rounds >= 8


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

        close_popups(page)
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
