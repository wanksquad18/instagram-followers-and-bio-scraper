#!/usr/bin/env python3
"""
followers-scraper.py

Playwright-based Instagram followers scraper. Designed to run inside GitHub Actions.
- Reads cookies from COOKIE_SECRET environment variable (a JSON string), or from
  COOKIE_FILE (path to JSON) if provided.
- Converts cookies safely for Playwright.
- Opens the target profile, clicks Followers (robust selectors), scrolls the followers
  modal to load followers, and saves follower usernames to CSV.

Usage in Actions (example env):
  TARGET=username COOKIE_FILE=www.instagram.com.cookies.json python followers-scraper.py
  or provide COOKIE_SECRET (raw JSON) in GitHub Actions secrets.

Notes:
 - This script tries to be tolerant of cookie formats (expires / expiry / max-age).
 - On failure it writes debug HTML and a small PNG to help debugging in CI artifacts.
"""

import os
import time
import json
import re
import traceback
from datetime import datetime
from typing import List, Dict

import pandas as pd
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ---------------------
# Cookie conversion helpers
# ---------------------
def load_cookies_from_env_or_file(cookie_file: str = "www.instagram.com.cookies.json"):
    """
    Return a list of cookie dicts. Tries:
    1) env COOKIE_SECRET (raw JSON string)
    2) path in COOKIE_FILE env or cookie_file param
    """
    if os.getenv("COOKIE_SECRET"):
        try:
            data = json.loads(os.getenv("COOKIE_SECRET"))
            # if a dict with key "cookies"
            if isinstance(data, dict) and data.get("cookies"):
                return data["cookies"]
            if isinstance(data, list):
                return data
        except Exception as e:
            print("Failed to parse COOKIE_SECRET:", e)

    file_path = os.getenv("COOKIE_FILE") or cookie_file
    if os.path.exists(file_path):
        try:
            with open(file_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
                if isinstance(data, dict) and data.get("cookies"):
                    return data["cookies"]
                if isinstance(data, list):
                    return data
        except Exception as e:
            print("Failed to parse cookie file:", e)

    print("No cookies found in env or file.")
    return []

def cookies_to_playwright(cookies: List[Dict]) -> List[Dict]:
    """
    Convert Chrome-style cookie dicts (or similar) to Playwright cookie format.
    Ensures expires is an int when present and strips leading '.' from domain.
    """
    out = []
    for c in cookies:
        if not c.get("name") or c.get("value") is None:
            continue
        ck = {"name": str(c.get("name")), "value": str(c.get("value"))}

        # normalize domain
        domain = c.get("domain") or c.get("host") or "www.instagram.com"
        if isinstance(domain, str):
            ck["domain"] = domain.lstrip(".")
        else:
            ck["domain"] = "www.instagram.com"

        # path
        if c.get("path"):
            ck["path"] = c.get("path")

        # expiry/expires parsing
        exp_val = None
        for k in ("expires", "expiry"):
            v = c.get(k)
            if v is None:
                continue
            try:
                if isinstance(v, str) and v.isdigit():
                    exp_val = int(v)
                else:
                    exp_val = int(float(v))
            except Exception:
                exp_val = None
            if exp_val:
                ck["expires"] = exp_val
                break

        # fallback: max-age
        if "expires" not in ck and c.get("max-age"):
            try:
                ma = int(c.get("max-age"))
                if ma > 0:
                    ck["expires"] = int(time.time()) + ma
            except Exception:
                pass

        if c.get("httpOnly") is not None:
            ck["httpOnly"] = bool(c.get("httpOnly"))
        if c.get("secure") is not None:
            ck["secure"] = bool(c.get("secure"))

        out.append(ck)
    return out

# ---------------------
# Debug helpers
# ---------------------
def save_debug_html(page, prefix="debug"):
    try:
        html = page.content()
        filename = f"{prefix}_{int(time.time())}.html"
        with open(filename, "w", encoding="utf-8") as fh:
            fh.write(html)
        print("Wrote debug HTML:", filename)
    except Exception as e:
        print("Failed to save debug HTML:", e)

def save_debug_screenshot(page, prefix="debug"):
    try:
        filename = f"{prefix}_{int(time.time())}.png"
        page.screenshot(path=filename, full_page=True)
        print("Wrote debug screenshot:", filename)
    except Exception as e:
        print("Failed to save debug screenshot:", e)

# ---------------------
# Followers extraction
# ---------------------
def extract_usernames_from_dialog(page) -> List[str]:
    """
    Given a page with the followers dialog open, try to extract usernames loaded
    in the modal. Returns unique usernames (strings).
    This function is defensive because IG's DOM changes a lot.
    """
    usernames = set()

    # We will look for list items inside dialog: 'div[role="dialog"] ul li'
    try:
        dialog_ul = page.locator('div[role="dialog"] ul')
        count = dialog_ul.count()
        if count == 0:
            # alternate path: some layouts use other containers
            dialog_ul = page.locator('div[role="dialog"]').locator('li')
        # Query all list items inside dialog
        items = page.locator('div[role="dialog"] ul li')
        n = items.count()
        for i in range(n):
            try:
                item = items.nth(i)
                # Try to find an anchor with href that looks like "/username/"
                a = item.locator('a').first
                if a.count() > 0:
                    href = a.get_attribute("href") or ""
                    # href may be full URL; extract username
                    m = re.search(r"instagram\.com\/([^\/\?]+)", href)
                    if m:
                        usernames.add(m.group(1))
                        continue
                    # fallback: a.textContent might include username or full name
                    text = a.inner_text().strip()
                    # username is often the first token without spaces and not containing spaces
                    if text and " " not in text:
                        usernames.add(text)
                        continue
                # fallback: sometimes username appears in span/div
                txt = item.inner_text().strip()
                # try to find @username-like token or first token that looks like a username
                m2 = re.search(r'@?([A-Za-z0-9._]{3,30})', txt)
                if m2:
                    usernames.add(m2.group(1))
            except Exception:
                continue
    except Exception as e:
        print("extract_usernames_from_dialog error:", e)

    return list(usernames)

def scroll_followers_modal(page, target_username: str, max_seconds=25):
    """
    Scroll inside followers modal to load more entries. Returns when timeout hit.
    """
    start = time.time()
    last_height = -1
    try:
        # locate the scrollable UL inside dialog
        ul = page.locator('div[role="dialog"] ul')
        if ul.count() == 0:
            # Sometimes the structure differs; try any scrollable container inside dialog
            ul = page.locator('div[role="dialog"]').locator('div').filter(has_text="followers").first
        # We'll repeatedly evaluate JS to scroll the container
        while time.time() - start < max_seconds:
            # run JS to scroll the first matching element
            page.evaluate(
                """() => {
                    const dlg = document.querySelector('div[role="dialog"] ul');
                    if (!dlg) return 0;
                    dlg.scrollTop = dlg.scrollHeight;
                    return dlg.scrollHeight;
                }"""
            )
            time.sleep(0.6)
    except Exception as e:
        # Non-fatal: if scroll fails, continue; we still try to extract whatever loaded
        print("scroll_followers_modal exception (non-fatal):", e)

# ---------------------
# Main scraping routine
# ---------------------
def scrape_followers_for(target: str, cookie_list: List[Dict], headless=True, max_followers=500):
    """
    Returns True on success (wrote CSV), False otherwise.
    """
    playwright_cookies = cookies_to_playwright(cookie_list)
    if not playwright_cookies:
        print("No valid cookies to set for Playwright. Aborting.")
        return False

    csv_path = f"{target}_followers.csv"
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=headless, args=["--no-sandbox"])
            context = browser.new_context()
            # set cookies in context (domain included in each cookie)
            try:
                context.add_cookies(playwright_cookies)
                print(f"Added {len(playwright_cookies)} cookies to Playwright context.")
            except Exception as e:
                print("context.add_cookies failed:", e)
                # still try: proceed without cookies (will likely show login)
            page = context.new_page()
            # go to profile directly
            profile_url = f"https://www.instagram.com/{target}/"
            print("Navigating to", profile_url)
            page.goto("https://www.instagram.com/", wait_until="domcontentloaded", timeout=15000)
            time.sleep(0.7)
            page.goto(profile_url, wait_until="domcontentloaded", timeout=20000)
            time.sleep(1.2)

            # detect login page heuristics
            title = page.title().lower()
            page_html = page.content().lower()
            if "log in" in title or "login" in title or "sign up" in title or "password" in page_html:
                print("Login page detected. Cookies invalid or blocked. Title:", page.title())
                save_debug_html(page, prefix=f"debug_{target}")
                save_debug_screenshot(page, prefix=f"debug_{target}")
                context.close()
                browser.close()
                return False

            # Try to click the followers link robustly.
            clicked = False
            try:
                # 1) href starting with /{target}/followers (covers trailing slash or not)
                selector_href = f'a[href^="/{target}/followers"]'
                if page.locator(selector_href).count() > 0:
                    page.locator(selector_href).first.click()
                    clicked = True
                else:
                    # 2) any anchor with visible text 'followers' (case-insensitive)
                    anchors = page.locator('a').all()
                    found = False
                    for i in range(page.locator('a').count()):
                        a = page.locator('a').nth(i)
                        try:
                            txt = a.inner_text().strip()
                        except Exception:
                            txt = ""
                        if re.search(r'(?i)followers', txt):
                            try:
                                a.click()
                                clicked = True
                                found = True
                                break
                            except Exception:
                                continue
                    if not found:
                        # 3) sometimes stats are in header as <li> elements - try clicking any element containing 'followers'
                        candidates = page.locator('header').locator('a, span, li')
                        for i in range(candidates.count()):
                            c = candidates.nth(i)
                            try:
                                txt = c.inner_text().strip()
                            except Exception:
                                txt = ""
                            if re.search(r'(?i)followers', txt):
                                try:
                                    c.click()
                                    clicked = True
                                    break
                                except Exception:
                                    continue

                if not clicked:
                    print("Followers link not found on profile page. Saving debug.")
                    save_debug_html(page, prefix=f"debug_{target}")
                    save_debug_screenshot(page, prefix=f"debug_{target}")
                    context.close()
                    browser.close()
                    return False
            except PWTimeout as e:
                print("Timeout while trying to locate/click followers link:", e)
                save_debug_html(page, prefix=f"debug_{target}")
                save_debug_screenshot(page, prefix=f"debug_{target}")
                context.close()
                browser.close()
                return False
            except Exception as e:
                print("Exception clicking followers link:", e)
                save_debug_html(page, prefix=f"debug_{target}")
                save_debug_screenshot(page, prefix=f"debug_{target}")
                context.close()
                browser.close()
                return False

            # At this point followers dialog should be open. Wait a bit.
            time.sleep(1.0)
            # Scroll modal to load followers
            scroll_followers_modal(page, target, max_seconds=20)

            # Extract usernames
            followers = []
            try:
                followers = extract_usernames_from_dialog(page)
            except Exception as e:
                print("Error extracting usernames:", e)

            # If we didn't find many, try a second pass after some scrolling
            if len(followers) < 10:
                scroll_followers_modal(page, target, max_seconds=10)
                more = extract_usernames_from_dialog(page)
                for u in more:
                    if u not in followers:
                        followers.append(u)

            # Trim to max_followers if requested
            if len(followers) > max_followers:
                followers = followers[:max_followers]

            # Save to CSV
            if followers:
                df = pd.DataFrame([{"username": u, "scraped_at": datetime.utcnow().isoformat()} for u in followers])
                df.to_csv(f"{target}_followers.csv", index=False)
                print(f"Wrote {len(followers)} followers to {target}_followers.csv")
                context.close()
                browser.close()
                return True
            else:
                print("No followers extracted. Saving debug.")
                save_debug_html(page, prefix=f"debug_{target}")
                save_debug_screenshot(page, prefix=f"debug_{target}")
                context.close()
                browser.close()
                return False

    except Exception as e:
        print("Unexpected exception in scrape_followers_for:", e)
        traceback.print_exc()
        return False

# ---------------------
# Main
# ---------------------
def main():
    target = os.getenv("TARGET") or os.getenv("TARGET_USERNAME") or os.getenv("TARGET_USER") or ""
    if not target:
        # support TARGET_USERNAMES comma list too; we'll use first
        tlist = os.getenv("TARGET_USERNAMES") or ""
        if tlist:
            target = tlist.split(",")[0].strip()

    if not target:
        print("Please set TARGET (or TARGET_USERNAMES) environment variable to the username you want to scrape.")
        return

    cookie_list = load_cookies_from_env_or_file()
    if isinstance(cookie_list, dict) and cookie_list.get("cookies"):
        cookie_list = cookie_list["cookies"]

    ok = scrape_followers_for(target, cookie_list, headless=True, max_followers=500)
    if ok:
        print("Scraping finished successfully.")
    else:
        print("Scraping failed. See debug artifacts.")

if __name__ == "__main__":
    main()
