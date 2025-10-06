#!/usr/bin/env python3
# bio-scraper.py
# Headless Selenium bio extractor with cookie injection.
# Writes matching bios to descriptions.csv (creates if missing).
# Usage: TARGET_USERNAMES="user1,user2" python bio-scraper.py

import os
import time
import json
import traceback
import re
from typing import List, Dict

import pandas as pd
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as ec
from selenium.webdriver.chrome.service import Service as ChromeService
from webdriver_manager.chrome import ChromeDriverManager
from selenium.common.exceptions import NoSuchElementException

# ----- Config -----
COOKIE_PATHS = [
    "data/www.instagram.com.cookies.json",
    "www.instagram.com.cookies.json",
    "cookies/www.instagram.com.cookies.json",
]
TIMEOUT = 15

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
def get_target_usernames():
    env = os.environ.get("TARGET_USERNAMES") or os.environ.get("TARGET_USERNAME") or os.environ.get("SINGLE_USERNAME")
    if env:
        return [u.strip().lstrip("@") for u in env.split(",") if u.strip()]
    if os.path.exists("usernames.txt"):
        with open("usernames.txt", "r", encoding="utf-8") as fh:
            return [l.strip().lstrip("@") for l in fh if l.strip()]
    raw = input("Enter usernames (comma-separated): ")
    return [u.strip().lstrip("@") for u in raw.split(",") if u.strip()]

def ensure_descriptions_csv():
    if not os.path.exists('descriptions.csv'):
        df = pd.DataFrame(columns=['username', 'description', 'link'])
        df.to_csv('descriptions.csv', index=False, encoding='utf-8')

def save_debug(driver, name_prefix):
    try:
        os.makedirs("data", exist_ok=True)
        ts = int(time.time())
        safe = name_prefix.replace("/", "_")
        html_path = f"data/debug_{safe}_{ts}.html"
        png_path = f"data/debug_{safe}_{ts}.png"
        with open(html_path, "w", encoding="utf-8") as fh:
            fh.write(driver.page_source)
        driver.save_screenshot(png_path)
        print("Saved debug artifacts:", html_path, png_path)
    except Exception as e:
        print("Failed to save debug artifacts:", e)

# ---------------- login fallback ----------------
def save_credentials(username, password):
    try:
        with open('credentials.txt', 'w', encoding='utf-8') as fh:
            fh.write(username + "\n" + password + "\n")
    except Exception:
        pass

def load_credentials():
    if os.path.exists('credentials.txt'):
        try:
            with open('credentials.txt', 'r', encoding='utf-8') as fh:
                lines = [l.strip() for l in fh.readlines()]
                if len(lines) >= 2:
                    return lines[0], lines[1]
        except Exception:
            pass
    return None

def prompt_credentials():
    u = input("Enter Instagram username: ").strip()
    p = input("Enter Instagram password: ").strip()
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
        login_button = WebDriverWait(bot, 10).until(ec.element_to_be_clickable((By.CSS_SELECTOR, "button[type='submit']")))
        login_button.click()
        time.sleep(8)
    except Exception as e:
        print("Login error:", e)
        traceback.print_exc()

# ---------------- scraping logic ----------------
def extract_bio_from_profile(bot, username):
    url = f"https://www.instagram.com/{username}/"
    print("[bio] opening", url)
    bot.get(url)
    time.sleep(4)
    try:
        # full name or header h1 could be present
        bio_text = ""
        try:
            # Instagram DOM can vary; try a few selectors
            possible = bot.find_elements(By.CSS_SELECTOR, "header section div.-vDIg span") \
                       or bot.find_elements(By.CSS_SELECTOR, "header section div.-vDIg") \
                       or bot.find_elements(By.TAG_NAME, "h1")
            for el in possible:
                txt = el.text.strip()
                if txt:
                    bio_text = txt
                    break
        except Exception:
            pass

        # fallback: parse meta description content
        if not bio_text:
            try:
                meta = bot.find_element(By.CSS_SELECTOR, 'meta[name="description"]')
                bio_text = meta.get_attribute("content") or ""
            except Exception:
                pass

        # find bio link (link in bio often rewritten via l.instagram.com)
        link = ""
        try:
            src = bot.page_source
            urls = re.findall(r'href=[\'"]?([^\'" >]+)', src)
            for u in urls:
                if re.match(r'https://l\.instagram\.com/\?u=(.*)', u):
                    link = u
                    break
        except Exception:
            link = ""

        return bio_text.strip(), link
    except Exception as e:
        print("[bio] error extracting bio for", username, e)
        try:
            save_debug(bot, username)
        except Exception:
            pass
        return "", ""

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

def main():
    ensure_descriptions_csv = ensure_descriptions_csv  # alias for readability
    ensure_descriptions_csv()
    usernames = get_target_usernames()
    if not usernames:
        print("No usernames supplied")
        return

    try:
        driver = build_driver(headless=True)
    except Exception as e:
        print("Failed to build driver:", e)
        traceback.print_exc()
        return

    cookie_list = load_cookies_from_env_or_file()
    if cookie_list:
        print("[main] injecting cookies")
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
                login(driver, creds[0], creds[1])
            else:
                print("[main] no credentials saved; continuing (may not return full bios)")
    except Exception:
        pass

    df = pd.read_csv('descriptions.csv', encoding='utf-8') if os.path.exists('descriptions.csv') else pd.DataFrame(columns=['username','description','link'])

    # word list: edit as needed
    word_list = ['journalist', 'reporter', 'correspondent', 'editor', 'news', 'columnist', 'writer', 'commentator', 'blogger', 'reviewer']

    for user in usernames:
        user = user.strip()
        if not user:
            continue
        try:
            bio, link = extract_bio_from_profile(driver, user)
            found = False
            for word in word_list:
                if word.lower() in (bio or "").lower() or word.lower() in (link or "").lower():
                    found = True
                    break
            if found:
                df = pd.concat([df, pd.DataFrame([{'username': user, 'description': bio, 'link': link}])], ignore_index=True)
                print(f"[main] matched {user}")
            else:
                print(f"[main] no match for {user}")
            # polite small delay
            time.sleep(randint(1,3))
        except Exception as e:
            print("Error for", user, e)
            traceback.print_exc()
            try:
                save_debug(driver, user)
            except Exception:
                pass

    try:
        df.to_csv('descriptions.csv', index=False, encoding='utf-8')
        print("[main] saved descriptions.csv")
    except Exception as e:
        print("Failed to save descriptions.csv:", e)

    try:
        driver.quit()
    except Exception:
        pass

if __name__ == '__main__':
    main()
