#!/usr/bin/env python3
# followers-scraper.py
# Headless Selenium follower scraper with cookie injection.
# Usage (locally): TARGET_USERNAMES="user1,user2" python followers-scraper.py
# In GitHub Actions we pass COOKIES_SECRET and TARGET_USERNAMES via env.

import os
import sys
import time
import json
import csv
import traceback
from random import randint

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as ec
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.chrome.service import Service as ChromeService
from webdriver_manager.chrome import ChromeDriverManager
from selenium.common.exceptions import NoSuchElementException, TimeoutException

# ----- Config -----
TIMEOUT = 15
COOKIE_PATHS = [
    "data/www.instagram.com.cookies.json",
    "www.instagram.com.cookies.json",
    "cookies/www.instagram.com.cookies.json",
]

# ---------------- cookie helpers ----------------
def load_cookies_from_env_or_file(env_name="COOKIES_SECRET", file_paths=None):
    file_paths = file_paths or COOKIE_PATHS
    raw = os.environ.get(env_name)
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return parsed
            if isinstance(parsed, dict):
                for v in parsed.values():
                    if isinstance(v, list):
                        return v
        except Exception:
            # treat raw as sessionid
            if raw and len(raw) > 10 and "=" not in raw:
                return [{"name": "sessionid", "value": raw, "domain": ".instagram.com", "path": "/"}]
    for p in file_paths:
        if os.path.exists(p):
            try:
                parsed = json.load(open(p, encoding="utf-8"))
                if isinstance(parsed, list):
                    return parsed
            except Exception:
                continue
    return None

def normalize_cookie_for_selenium(c):
    cd = {"name": c.get("name"), "value": str(c.get("value", ""))}
    domain = c.get("domain", ".instagram.com")
    cd["domain"] = domain
    cd["path"] = c.get("path", "/")
    if "expires" in c:
        try:
            cd["expiry"] = int(c["expires"])
        except Exception:
            pass
    elif "expiry" in c:
        try:
            cd["expiry"] = int(c["expiry"])
        except Exception:
            pass
    return cd

def inject_cookies_into_driver(driver, cookie_list, base_url="https://www.instagram.com/"):
    if not cookie_list:
        print("[cookies] no cookie list provided")
        return 0
    try:
        driver.get(base_url)
        time.sleep(1.0)
    except Exception as e:
        print("[cookies] warning: initial GET failed:", e)
    added = 0
    for raw in cookie_list:
        try:
            cookie = normalize_cookie_for_selenium(raw)
            if not cookie.get("name") or cookie.get("value") is None:
                continue
            try:
                driver.delete_cookie(cookie["name"])
            except Exception:
                pass
            try:
                driver.add_cookie(cookie)
                added += 1
            except Exception:
                c2 = cookie.copy()
                if c2.get("domain", "").startswith("."):
                    c2["domain"] = c2["domain"].lstrip(".")
                try:
                    driver.add_cookie(c2)
                    added += 1
                except Exception as e2:
                    print("[cookies] cannot add cookie", cookie.get("name"), ":", e2)
        except Exception as e:
            print("[cookies] normalize/add failed:", e)
    print(f"[cookies] injected {added} cookies")
    return added

# ---------------- utilities ----------------
def ensure_data_dir():
    os.makedirs("data", exist_ok=True)

def save_debug(driver, name_prefix):
    ensure_data_dir()
    ts = int(time.time())
    safe = name_prefix.replace("/", "_")
    html_path = f"data/debug_{safe}_{ts}.html"
    png_path = f"data/debug_{safe}_{ts}.png"
    try:
        with open(html_path, "w", encoding="utf-8") as fh:
            fh.write(driver.page_source)
    except Exception as e:
        print("Failed to save debug html:", e)
    try:
        driver.save_screenshot(png_path)
    except Exception as e:
        print("Failed to save debug png:", e)
    print("Saved debug artifacts:", html_path, png_path)
    return html_path, png_path

def get_target_usernames():
    env = os.environ.get("TARGET_USERNAMES") or os.environ.get("TARGET_USERNAME") or os.environ.get("SINGLE_USERNAME")
    if env:
        return [u.strip().lstrip("@") for u in env.split(",") if u.strip()]
    if os.path.exists("usernames.txt"):
        with open("usernames.txt", "r", encoding="utf-8") as fh:
            return [l.strip().lstrip("@") for l in fh if l.strip()]
    raw = input("Enter the Instagram usernames you want to scrape (comma-separated): ")
    return [u.strip().lstrip("@") for u in raw.split(",") if u.strip()]

# ---------------- fallback login helpers ----------------
def save_credentials(username, password):
    try:
        with open('credentials.txt', 'w', encoding='utf-8') as file:
            file.write(f"{username}\n{password}\n")
    except Exception:
        pass

def load_credentials():
    if not os.path.exists('credentials.txt'):
        return None
    try:
        with open('credentials.txt', 'r', encoding='utf-8') as file:
            lines = [l.strip() for l in file.readlines()]
            if len(lines) >= 2:
                return lines[0], lines[1]
    except Exception:
        pass
    return None

def prompt_credentials():
    u = input("Enter your Instagram username: ").strip()
    p = input("Enter your Instagram password: ").strip()
    save_credentials(u, p)
    return u, p

def login(bot, username, password):
    try:
        bot.get('https://www.instagram.com/accounts/login/')
        time.sleep(2)
        try:
            elt = bot.find_element(By.XPATH, "/html/body/div[4]/div/div/div[3]/div[2]/button")
            elt.click()
        except NoSuchElementException:
            pass
        username_input = WebDriverWait(bot, 10).until(ec.element_to_be_clickable((By.CSS_SELECTOR, "input[name='username']")))
        password_input = WebDriverWait(bot, 10).until(ec.element_to_be_clickable((By.CSS_SELECTOR, "input[name='password']")))
        username_input.clear()
        username_input.send_keys(username)
        password_input.clear()
        password_input.send_keys(password)
        submit = WebDriverWait(bot, 10).until(ec.element_to_be_clickable((By.CSS_SELECTOR, "button[type='submit']")))
        submit.click()
        time.sleep(8)
    except Exception as e:
        print("Login failed:", e)
        traceback.print_exc()

# ---------------- scraping logic ----------------
def scrape_followers_for_one(bot, username, max_followers=1000):
    try:
        url = f"https://www.instagram.com/{username}/"
        print("[scrape] opening", url)
        bot.get(url)
        time.sleep(3)

        try:
            WebDriverWait(bot, TIMEOUT).until(
                ec.presence_of_element_located((By.XPATH, "//a[contains(@href, '/followers')]"))
            ).click()
            time.sleep(2)
        except Exception as e:
            print("[scrape] Followers link/button not found:", e)
            save_debug(bot, username)
            return []

        try:
            modal_ul = WebDriverWait(bot, TIMEOUT).until(
                ec.presence_of_element_located((By.XPATH, "//div[@role='dialog']//ul"))
            )
        except Exception as e:
            print("[scrape] Followers modal did not appear:", e)
            save_debug(bot, username)
            return []

        collected = []
        seen = set()
        attempts = 0
        max_attempts = 400
        while len(collected) < max_followers and attempts < max_attempts:
            attempts += 1
            try:
                anchors = modal_ul.find_elements(By.XPATH, ".//a[contains(@href,'/')]")
            except Exception:
                anchors = []
            for a in anchors:
                try:
                    href = a.get_attribute("href") or ""
                    if href:
                        parts = href.rstrip("/").split("/")
                        if len(parts) >= 4:
                            uname = parts[3]
                        else:
                            uname = parts[-1]
                        uname = uname.strip()
                        if uname and uname not in seen and uname.lower() != username.lower():
                            seen.add(uname)
                            collected.append(uname)
                except Exception:
                    continue
            try:
                bot.execute_script("arguments[0].scrollTop = arguments[0].scrollTop + arguments[0].clientHeight;", modal_ul)
            except Exception:
                try:
                    ActionChains(bot).move_to_element(modal_ul).send_keys(Keys.END).perform()
                except Exception:
                    pass
            time.sleep(0.7)
            if attempts % 50 == 0:
                time.sleep(1.5)
            if len(collected) >= max_followers:
                break

        result = collected[:max_followers]
        print(f"[scrape] collected {len(result)} followers for {username}")
        ensure_data_dir()
        csv_path = f"data/followers_{username}.csv"
        txt_path = f"{username}_followers.txt"
        try:
            with open(csv_path, "w", encoding="utf-8", newline="") as fh:
                w = csv.writer(fh)
                w.writerow(["username"])
                for u in result:
                    w.writerow([u])
            with open(txt_path, "w", encoding="utf-8") as fh:
                fh.write("\n".join(result) + ("\n" if result else ""))
            print("[scrape] saved", csv_path, txt_path)
        except Exception as e:
            print("[scrape] failed to write results:", e)
        return result
    except Exception as e:
        print("[scrape] fatal error while scraping", username, ":", e)
        traceback.print_exc()
        try:
            save_debug(bot, username)
        except Exception:
            pass
        return []

def build_driver(headless=True):
    options = webdriver.ChromeOptions()
    if headless:
        options.add_argument('--headless=new')
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')
    options.add_argument('--disable-gpu')
    options.add_argument('--window-size=1280,800')
    service = ChromeService(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=options)
    return driver

def scrape():
    usernames = get_target_usernames()
    if not usernames:
        print("No usernames provided")
        sys.exit(2)
    try:
        maxf = int(os.environ.get("MAX_FOLLOWERS", "1000"))
    except Exception:
        maxf = 1000

    driver = None
    try:
        driver = build_driver(headless=True)
    except Exception as e:
        print("Failed to create driver:", e)
        traceback.print_exc()
        sys.exit(3)

    cookie_list = load_cookies_from_env_or_file()
    if cookie_list:
        print("[main] injecting cookies from secret/file")
        inject_cookies_into_driver(driver, cookie_list)
        time.sleep(1)
    else:
        print("[main] no cookies found; will try fallback login if credentials available")

    try:
        driver.get("https://www.instagram.com/")
        time.sleep(1.5)
        title = driver.title or ""
        if "Log in" in title or "Login" in title:
            print("[main] login page detected after cookie injection; cookies may be invalid")
            creds = load_credentials()
            if creds:
                print("[main] attempting login with saved credentials")
                login(driver, creds[0], creds[1])
            else:
                print("[main] no credentials saved; continuing (may fail)")
    except Exception:
        pass

    for u in usernames:
        if not u:
            continue
        print(f"[main] scraping followers for {u} (max {maxf})")
        try:
            scrape_followers_for_one(driver, u, max_followers=maxf)
        except Exception as e:
            print("Error scraping", u, e)
            traceback.print_exc()

    try:
        driver.quit()
    except Exception:
        pass

if __name__ == '__main__':
    scrape()
