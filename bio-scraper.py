import time
import os
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as ec
from selenium.webdriver.chrome.service import Service as ChromeService
from webdriver_manager.chrome import ChromeDriverManager
from selenium.common.exceptions import NoSuchElementException
import re
import pandas as pd
# ----------------- BEGIN: cookie helpers (paste near imports) -----------------
import json
from typing import List, Dict

def load_cookies_from_env_or_file(env_name="COOKIES_SECRET", file_paths=None):
    """
    Returns a list of cookie dicts or None.
    Accepts either:
      - COOKIES_SECRET env with JSON array string of cookies OR
      - COOKIES_SECRET env equal to the raw sessionid string
      - file paths (list) to try reading JSON from repo
    """
    cookie_raw = os.environ.get(env_name)
    if cookie_raw:
        try:
            parsed = json.loads(cookie_raw)
            if isinstance(parsed, list):
                return parsed
            if isinstance(parsed, dict):
                for v in parsed.values():
                    if isinstance(v, list):
                        return v
        except Exception:
            # Not JSON — maybe raw sessionid string
            if cookie_raw and len(cookie_raw) > 10 and "=" not in cookie_raw:
                return [{"name": "sessionid", "value": cookie_raw, "domain": ".instagram.com", "path": "/"}]
    if file_paths:
        for p in file_paths:
            try:
                if os.path.exists(p):
                    parsed = json.load(open(p, encoding="utf-8"))
                    if isinstance(parsed, list):
                        return parsed
            except Exception:
                continue
    return None

def normalize_cookies_for_selenium(cookie_list: List[Dict]) -> List[Dict]:
    out = []
    for c in cookie_list:
        if not isinstance(c, dict):
            continue
        name = c.get("name"); value = c.get("value")
        if not name or value is None:
            continue
        cd = {"name": name, "value": str(value)}
        cd["domain"] = c.get("domain", ".instagram.com")
        cd["path"] = c.get("path", "/")
        if "expires" in c:
            try:
                cd["expiry"] = int(c["expires"])
            except Exception:
                pass
        out.append(cd)
    return out

def inject_cookies_into_driver(driver, cookie_list, base_url="https://www.instagram.com"):
    if not cookie_list:
        print("[cookies] no cookies to inject")
        return False
    try:
        driver.get(base_url)
        time.sleep(1.0)
    except Exception as e:
        print("[cookies] warning: initial GET failed:", e)
    selenium_cookies = normalize_cookies_for_selenium(cookie_list)
    added = 0
    for c in selenium_cookies:
        try:
            try:
                driver.delete_cookie(c["name"])
            except Exception:
                pass
            driver.add_cookie(c)
            added += 1
        except Exception as e:
            try:
                c2 = c.copy()
                if c2.get("domain","").startswith("."):
                    c2["domain"] = c2["domain"].lstrip(".")
                driver.add_cookie(c2)
                added += 1
            except Exception as e2:
                print("[cookies] failed to add cookie", c.get("name"), ":", e2)
    print(f"[cookies] injected {added} cookies")
    return added > 0
# ----------------- END: cookie helpers -----------------



def save_credentials(username, password):
    with open('credentials.txt', 'w') as file:
        file.write(f"{username}\n{password}")


def load_credentials():
    if not os.path.exists('credentials.txt'):
        return None

    with open('credentials.txt', 'r') as file:
        lines = file.readlines()
        if len(lines) >= 2:
            return lines[0].strip(), lines[1].strip()

    return None


def prompt_credentials():
    username = input("Enter your Instagram username: ")
    password = input("Enter your Instagram password: ")
    save_credentials(username, password)
    return username, password


def login(bot, username, password):
    bot.get('https://www.instagram.com/accounts/login/')
    time.sleep(2)

    # Check if cookies need to be accepted
    try:
        element = bot.find_element(By.XPATH, "/html/body/div[4]/div/div/div[3]/div[2]/button")
        element.click()
    except NoSuchElementException:
        print("[Info] - Instagram did not require to accept cookies this time.")

    print("[Info] - Logging in...")
    username_input = WebDriverWait(bot, 10).until(
        ec.element_to_be_clickable((By.CSS_SELECTOR, "input[name='username']")))
    password_input = WebDriverWait(bot, 10).until(
        ec.element_to_be_clickable((By.CSS_SELECTOR, "input[name='password']")))

    username_input.clear()
    username_input.send_keys(username)
    password_input.clear()
    password_input.send_keys(password)

    login_button = WebDriverWait(bot, 2).until(ec.element_to_be_clickable((By.CSS_SELECTOR, "button[type='submit']")))
    login_button.click()
    time.sleep(10)


def scrape_description(bot, username):
    bot.get(f'https://www.instagram.com/{username}/')

    print(f"[Info] - Scraping description for {username}...")
    time.sleep(5)

    user_description = dict()

    # check the account bio
    try:
        description = bot.find_element(By.TAG_NAME, 'h1').text.lower()
    except:
        description = ''

    # check the link in bio
    link = ''
    try:
        html_source = bot.page_source
        urls = re.findall(r'href=[\'"]?([^\'" >]+)', html_source)
        for url in urls:
            if re.match(r'https://l\.instagram\.com/\?u=(.*)', url):
                link = url
                break
    except:
        link = ''

    # words (in lower case) you need to find in the bio or link
    word_list = ['journalist', 'reporter', 'correspondent', 'editor', 'news', 'columnist', 'writer', 'commentator',
                 'blogger', 'reviewer']

    df_descriptions = pd.read_csv('descriptions.csv', encoding="utf-8")

    # look for words from the list in the bio or link, if there is a match, add that account to the csv file
    for word in word_list:
        if (word in description) or (word in link):
            user_description['username'] = [username]
            user_description['description'] = [description]
            user_description['link'] = [link]
            df_user = pd.DataFrame.from_dict(user_description)
            df_descriptions = pd.concat([df_descriptions, df_user])
            break

    print(f"[Info] - Saving descriptions for {username}...")

    df_descriptions.to_csv('descriptions.csv', encoding='utf-8', index=False)
    time.sleep(10)


def scrape():
    credentials = load_credentials()

    if credentials is None:
        username, password = prompt_credentials()
    else:
        username, password = credentials

   usernames_env = os.environ.get("TARGET_USERNAMES") or os.environ.get("TARGET_USERNAME")
if usernames_env:
    usernames = [u.strip() for u in usernames_env.split(",") if u.strip()]
else:
    usernames = input("Enter the Instagram usernames you want to scrape (separated by commas): ").split(",")


options = webdriver.ChromeOptions()
# run headless in Actions / CI
options.add_argument('--headless=new')   # if runner errors, use '--headless'
options.add_argument('--no-sandbox')
options.add_argument('--disable-dev-shm-usage')
options.add_argument('--disable-gpu')
options.add_argument('--window-size=1280,800')
mobile_emulation = {
    "userAgent": "Mozilla/5.0 (Linux; Android 10; SM-G970F) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/83.0.4103.106 Mobile Safari/537.36"
}
options.add_experimental_option("mobileEmulation", mobile_emulation)

bot = webdriver.Chrome(service=ChromeService(ChromeDriverManager().install()), options=options)

   bot = webdriver.Chrome(service=ChromeService(ChromeDriverManager().install()), options=options)

# Try cookie-based login first
cookie_list = load_cookies_from_env_or_file(
    env_name="COOKIES_SECRET",
    file_paths=["data/www.instagram.com.cookies.json","www.instagram.com.cookies.json","cookies/www.instagram.com.cookies.json"]
)
if cookie_list:
    print("[Info] - Found cookies in secret or files; injecting into browser")
    inject_cookies_into_driver(bot, cookie_list)
    time.sleep(2)
else:
    # fallback to username/password interactive login (existing behavior)
    login(bot, username, password)


    df = pd.DataFrame({'username': [], 'description': [], 'link': []})
    df.to_csv('descriptions.csv', encoding='utf-8', index=False)

    for user in usernames:
        user = user.strip()
        time.sleep(5)
        scrape_description(bot, user)

    bot.quit()


if __name__ == '__main__':
    TIMEOUT = 15
    scrape()
