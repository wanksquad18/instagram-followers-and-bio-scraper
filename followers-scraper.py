# followers-scraper.py
# Requires: playwright
# Usage inside workflow: python followers-scraper.py
import os, sys, json, csv, time, re
from typing import List, Dict
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

COOKIE_PATHS = [
    os.environ.get("COOKIE_FILE", "data/www.instagram.com.cookies.json"),
    "www.instagram.com.cookies.json",
    "cookies/www.instagram.com.cookies.json",
]

OUTPUT_CSV = "data/results.csv"
DEBUG_PREFIX = "data/debug"

def load_cookies() -> List[Dict]:
    # try COOKIE_FILE env first, then common paths
    for p in COOKIE_PATHS:
        if p and os.path.exists(p):
            try:
                with open(p, encoding="utf-8") as fh:
                    j = json.load(fh)
                if isinstance(j, list):
                    return j
            except Exception as e:
                print("Failed parse cookie file", p, ":", e)
    # fallback: read COOKIES_SECRET env (string)
    cs = os.environ.get("COOKIES_SECRET")
    if cs:
        try:
            j = json.loads(cs)
            if isinstance(j, list):
                return j
        except Exception as e:
            print("Failed parse COOKIES_SECRET env:", e)
    raise RuntimeError("No cookie JSON found. Place cookie JSON file or set COOKIES_SECRET env.")

def cookies_to_playwright(cookies: List[Dict]) -> List[Dict]:
    out = []
    for c in cookies:
        cookie = {}
        cookie["name"] = c.get("name")
        cookie["value"] = c.get("value")
        # Playwright requires "domain" without leading dot works too
        domain = c.get("domain") or c.get("host") or "www.instagram.com"
        cookie["domain"] = domain
        # path, expires, httpOnly, secure
        if c.get("path"):
            cookie["path"] = c.get("path")
        if c.get("expires") and isinstance(c.get("expires"), (int, float)):
            cookie["expires"] = int(c.get("expires"))
        if c.get("httpOnly") is not None:
            cookie["httpOnly"] = bool(c.get("httpOnly"))
        if c.get("secure") is not None:
            cookie["secure"] = bool(c.get("secure"))
        out.append(cookie)
    return out

def save_debug_html_png(page, name):
    safe = re.sub(r'[^0-9A-Za-z._-]', '_', name)[:60]
    ts = int(time.time())
    os.makedirs("data", exist_ok=True)
    try:
        html = page.content()
        path_html = f"{DEBUG_PREFIX}_{safe}_{ts}.html"
        with open(path_html, "w", encoding="utf-8") as fh:
            fh.write(html)
        print("Wrote debug html:", path_html)
    except Exception as e:
        print("Failed to write debug html:", e)
    try:
        path_png = f"{DEBUG_PREFIX}_{safe}_{ts}.png"
        page.screenshot(path=path_png, full_page=True)
        print("Wrote debug png:", path_png)
    except Exception as e:
        print("Failed to screenshot page:", e)

def extract_user_from_href(href: str):
    # href like "/username/" or "/username"
    if not href:
        return None
    m = re.match(r"^/([^/]+)/?$", href)
    if m:
        return m.group(1)
    return None

def scrape_followers_of(target: str, max_followers: int = 500):
    print("Scraping followers for:", target, "limit:", max_followers)
    cookies = load_cookies()
    p_cookies = cookies_to_playwright(cookies)
    os.makedirs("data", exist_ok=True)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])
        context = browser.new_context()
        try:
            context.add_cookies(p_cookies)
        except Exception as e:
            print("Warning: context.add_cookies failed:", e)
        page = context.new_page()
        page.set_default_timeout(30000)
        profile_url = f"https://www.instagram.com/{target}/"
        try:
            page.goto(profile_url, wait_until="domcontentloaded")
        except Exception as e:
            print("Failed to open profile page:", e)
            save_debug_html_png(page, target)
            browser.close()
            return False

        # Heuristic: if login page shown -> cookies invalid
        title = ""
        try:
            title = page.title()
        except Exception:
            pass
        if "Log in" in title or "Login" in title or "Sign up" in title:
            print("Login page detected. Cookies invalid or blocked. Title:", title)
            save_debug_html_png(page, target)
            browser.close()
            return False

        # try click followers link
        try:
            # prefer exact href match
            selector_link = f'a[href="/{target}/followers/"]'
            if page.locator(selector_link).count() > 0:
                page.locator(selector_link).first.click()
            else:
                # fallback to link with 'followers' text
                candidate = page.locator('a').filter(has_text='followers').first
                if candidate.count() > 0:
                    candidate.click()
                else:
                    print("Followers link not found on page. Saving debug.")
                    save_debug_html_png(page, target)
                    browser.close()
                    return False
        except PWTimeout as e:
            print("Timeout clicking followers link:", e)
            save_debug_html_png(page, target)
            browser.close()
            return False
        except Exception as e:
            print("Exception clicking followers link:", e)
            save_debug_html_png(page, target)
            browser.close()
            return False

        # Wait for modal
        try:
            modal_selector = 'div[role="dialog"] ul'
            page.wait_for_selector(modal_selector, timeout=10000)
            modal = page.locator(modal_selector).first
        except Exception as e:
            print("Followers modal did not appear:", e)
            save_debug_html_png(page, target)
            browser.close()
            return False

        # Scroll modal and collect
        collected = {}
        last_len = -1
        scroll_tries = 0
        print("Begin scrolling modal to collect followers...")
        while len(collected) < max_followers and scroll_tries < 400:
            # collect anchors in modal and extract username + maybe full name
            anchors = modal.locator('a[href^="/"]')
            count = anchors.count()
            for i in range(count):
                try:
                    href = anchors.nth(i).get_attribute("href")
                    user = extract_user_from_href(href)
                    if not user:
                        continue
                    if user in collected:
                        continue
                    # attempt to also get visible display name/full name near the anchor
                    # The anchor usually contains inner text with username/full name; we'll try:
                    try:
                        # get text content of parent/ancestor for human-friendly name
                        parent_text = anchors.nth(i).inner_text().strip()
                        # inner_text sometimes contains username + full name, split by newline
                        parts = [p.strip() for p in parent_text.splitlines() if p.strip()]
                        full_name = parts[1] if len(parts) > 1 else ""
                    except Exception:
                        full_name = ""
                    collected[user] = full_name
                except Exception:
                    continue

            # scroll inside modal
            try:
                page.evaluate("""(sel) => {
                    const el = document.querySelector(sel);
                    if (!el) return 0;
                    el.scrollTop = el.scrollHeight;
                    return el.scrollHeight;
                }""", modal_selector)
            except Exception as e:
                print("Scroll evaluate error:", e)
            time.sleep(0.35)
            if len(collected) == last_len:
                scroll_tries += 1
            else:
                last_len = len(collected)
                scroll_tries = 0

        print("Collected usernames:", len(collected))
        # write out results
        with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["username","full_name","source_profile","collected_at"])
            for u, full in collected.items():
                w.writerow([u, full, target, int(time.time())])

        browser.close()
    return True

def main():
    targets = os.environ.get("TARGET_USERNAMES","thepreetjohal")
    limit = int(os.environ.get("LIMIT","500") or 500)
    targets_list = [t.strip() for t in targets.split(",") if t.strip()]
    if not targets_list:
        print("No targets provided. Set TARGET_USERNAMES env or input in workflow.")
        sys.exit(1)
    all_success = True
    # run per-target and append results (if multiple targets, combine)
    os.makedirs("data", exist_ok=True)
    overall_rows = []
    for t in targets_list:
        ok = scrape_followers_of(t, max_followers=limit)
        if not ok:
            print("Profile failed:", t)
            all_success = False
        # if scraper produced data/results.csv (per-run), merge
        csv_path = OUTPUT_CSV
        if os.path.exists(csv_path):
            # load and append to overall_rows, then move aside to avoid overwrite
            try:
                import csv as _csv
                with open(csv_path, newline="", encoding="utf-8") as f:
                    rdr = _csv.DictReader(f)
                    for r in rdr:
                        overall_rows.append(r)
                # rename last file to keep it (optional)
                os.rename(csv_path, f"data/results_{t}_{int(time.time())}.csv")
            except Exception as e:
                print("Error reading produced csv:", e)
    # write aggregated final results.csv
    if overall_rows:
        with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["username","full_name","source_profile","collected_at"])
            for r in overall_rows:
                w.writerow([r.get("username",""), r.get("full_name",""), r.get("source_profile",""), r.get("collected_at","")])
        print("Wrote aggregated", OUTPUT_CSV, "rows:", len(overall_rows))
    else:
        print("No rows collected. data/results.csv not created.")
    if not all_success:
        sys.exit(2)

if __name__ == "__main__":
    main()
