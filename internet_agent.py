from __future__ import annotations
import json
import re
import time
from dataclasses import dataclass
from typing import List, Dict, Optional, Callable
import base64
import binascii
import os
from pathlib import Path
from urllib.parse import urlparse, unquote_plus
import threading
import queue
import tkinter as tk
from tkinter import scrolledtext
from tkinter import filedialog
from io import BytesIO
import datetime

from PIL import Image, ImageTk
import pytesseract

import requests
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, InvalidSelectorException
from webdriver_manager.chrome import ChromeDriverManager


OLLAMA_MODEL = "kimi-k2:1t-cloud"
OLLAMA_URL = "http://localhost:11434/api/chat"
OLLAMA_CONNECT_TIMEOUT = int(os.environ.get("OLLAMA_CONNECT_TIMEOUT", "30"))
OLLAMA_READ_TIMEOUT = int(os.environ.get("OLLAMA_READ_TIMEOUT", "60"))
OLLAMA_MAX_RETRIES = int(os.environ.get("OLLAMA_MAX_RETRIES", "4"))
OLLAMA_RETRY_PAUSE = float(os.environ.get("OLLAMA_RETRY_PAUSE", "15.0"))
OLLAMA_RETRY_BACKOFF = float(os.environ.get("OLLAMA_RETRY_BACKOFF", "5.0"))
STEP_PAUSE_SECONDS = float(os.environ.get("STEP_PAUSE_SECONDS", "1.0"))
VISIT_PAUSE_SECONDS = float(os.environ.get("VISIT_PAUSE_SECONDS", "2.0"))
EXTRACT_PAUSE_SECONDS = float(os.environ.get("EXTRACT_PAUSE_SECONDS", "1.5"))
MAX_STEPS = 640
MIN_STEPS_BEFORE_DONE = 4
CHROMEDRIVER_LOG = os.environ.get("CHROMEDRIVER_LOG", "/tmp/chromedriver.log")
# Default lokalni cesta k driveru, pokud neni nastaven CHROMEDRIVER.
DEFAULT_LOCAL_DRIVER = "/usr/lib/chromium-browser/chromedriver"
DEFAULT_SNAP_DRIVER = "/snap/chromium/current/usr/lib/chromium-browser/chromedriver"
DEFAULT_SNAP_CHROME = "/snap/chromium/current/usr/lib/chromium-browser/chrome"
MAX_SOCIAL_ITEMS = int(os.environ.get("MAX_SOCIAL_ITEMS", "20"))
HISTORY_MAX_ITEMS = int(os.environ.get("HISTORY_MAX_ITEMS", "140"))
HISTORY_MAX_CHARS = int(os.environ.get("HISTORY_MAX_CHARS", "48000"))
IMAGE_REF_LIMIT = int(os.environ.get("IMAGE_REF_LIMIT", "30"))
IMAGE_QUEUE_LIMIT = int(os.environ.get("IMAGE_QUEUE_LIMIT", "64"))
SOCIAL_UI_MAX_LINES = int(os.environ.get("SOCIAL_UI_MAX_LINES", "400"))


SESSION = requests.Session()


def call_ollama(messages: List[Dict[str, str]], model: str = OLLAMA_MODEL) -> str:
    payload = {"model": model, "messages": messages, "stream": False, "temperature": 0.2}
    last_err: Optional[Exception] = None
    for attempt in range(OLLAMA_MAX_RETRIES + 1):
        try:
            resp = SESSION.post(
                OLLAMA_URL,
                json=payload,
                timeout=(OLLAMA_CONNECT_TIMEOUT, OLLAMA_READ_TIMEOUT),
            )
            if resp.status_code == 429:
                # Respect Retry-After if provided; otherwise exponential backoff.
                retry_after = resp.headers.get("Retry-After")
                if attempt < OLLAMA_MAX_RETRIES:
                    pause = float(retry_after) if retry_after else OLLAMA_RETRY_PAUSE * (OLLAMA_RETRY_BACKOFF ** attempt)
                    time.sleep(pause)
                    continue
                resp.raise_for_status()
            resp.raise_for_status()
        except (requests.Timeout, requests.RequestException) as exc:  # noqa: PERF203
            last_err = exc
            if attempt < OLLAMA_MAX_RETRIES:
                time.sleep(OLLAMA_RETRY_PAUSE * (OLLAMA_RETRY_BACKOFF ** attempt))
                continue
            raise

        try:
            data = resp.json()
        except json.JSONDecodeError:
            # Ollama muze vratit vice JSON radku (stream), nebo plain text pri chybe.
            text = resp.text.strip()
            return text

        return data.get("message", {}).get("content", "") or resp.text.strip()

    if last_err:
        raise last_err
    raise RuntimeError("Neznama chyba pri volani Ollama")


def wait_dom_ready(driver, timeout: int = 10) -> None:
    try:
        WebDriverWait(driver, timeout).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )
    except TimeoutException:
        pass


def detect_social_platform(url: str) -> Optional[str]:
    host = urlparse(url).hostname or ""
    host = host.lower()
    if any(h in host for h in ("instagram.com", "cdninstagram.com")):
        return "instagram"
    if "facebook.com" in host:
        return "facebook"
    if any(h in host for h in ("x.com", "twitter.com")):
        return "x"
    if "tiktok.com" in host:
        return "tiktok"
    if "linkedin.com" in host:
        return "linkedin"
    if "reddit.com" in host:
        return "reddit"
    return None


def extract_social_posts(driver: webdriver.Chrome, platform: str, image_hook: Optional[Callable[[str], None]] = None) -> List[Dict[str, object]]:
    """Heuristicke vycucnuti prispevku a fotek z hlavnich socialnich siti."""
    selectors = {
        "instagram": {
            "post": "article, div[role='presentation'] section",
            "text": "h1, h2, span",
            "image": "img[src*='cdninstagram.com'], img[src*='fbcdn.net']",
            "time": "time",
            "link": "a[href*='/p/'], a[href*='/reel/']",
        },
        "facebook": {
            "post": "div[role='article']",
            "text": "div[dir='auto']",
            "image": "img[src*='fbcdn.net']",
            "time": "a[aria-label][role='link'] abbr, abbr",
            "link": "a[role='link'][href*='/posts/']",
        },
        "x": {
            "post": "article",
            "text": "div[data-testid='tweetText']",
            "image": "img[src*='twimg.com']",
            "time": "time",
            "link": "a[href*='/status/']",
        },
        "tiktok": {
            "post": "div[data-e2e='video-item'], div[data-e2e='recommended-user-item']",
            "text": "div[data-e2e='video-desc'], div[data-e2e='recommended-user-description']",
            "image": "img[src*='tiktokcdn.com']",
            "time": "span[data-e2e='video-ugc-date']",
            "link": "a[href*='video']",
        },
        "linkedin": {
            "post": "div.feed-shared-update-v2, div.feed-shared-news-module",
            "text": "span.break-words, p",
            "image": "img[src*='media.licdn.com']",
            "time": "span.visually-hidden",
            "link": "a[href*='activity']",
        },
        "reddit": {
            "post": "div[data-testid='post-container']",
            "text": "h3, p",
            "image": "img[src*='preview.redd.it'], img[src*='i.redd.it']",
            "time": "a[data-click-id='timestamp']",
            "link": "a[data-click-id='body']",
        },
    }.get(platform)

    if not selectors:
        return []

    posts: List[Dict[str, object]] = []
    try:
        elems = driver.find_elements(By.CSS_SELECTOR, selectors["post"])
    except InvalidSelectorException:
        return []

    for elem in elems[:MAX_SOCIAL_ITEMS]:
        text_parts = []
        if selectors.get("text"):
            for t in elem.find_elements(By.CSS_SELECTOR, selectors["text"]):
                txt = t.text.strip()
                if txt:
                    text_parts.append(txt)
        text = "\n".join(text_parts)[:800]
        decoded: List[str] = []
        if text:
            decoded.extend(decode_suspect_value(text))

        images: List[str] = []
        if selectors.get("image"):
            for im in elem.find_elements(By.CSS_SELECTOR, selectors["image"]):
                src = im.get_attribute("src")
                if src and src.startswith("http") and "data:" not in src:
                    images.append(src)
                    if image_hook:
                        image_hook(src)

        ts_val = None
        if selectors.get("time"):
            ts = elem.find_elements(By.CSS_SELECTOR, selectors["time"])
            if ts:
                ts_val = ts[0].get_attribute("datetime") or ts[0].text

        href_val = None
        if selectors.get("link"):
            links = elem.find_elements(By.CSS_SELECTOR, selectors["link"])
            if links:
                href_val = links[0].get_attribute("href")
                decoded.extend(decode_suspect_value(href_val or ""))
        if not href_val:
            href_val = driver.current_url

        if text or images:
            contacts = extract_contacts_and_links(text)
            posts.append(
                {
                    "text": text,
                    "images": images,
                    "time": ts_val,
                    "link": href_val,
                    "decoded": list(dict.fromkeys(d for d in decoded if d)),
                    "struct": contacts,
                }
            )

    return posts


@dataclass
class Action:
    name: str
    args: Optional[str] = None


def parse_action(text: str) -> Action:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for line in lines:
        up = line.upper()
        if up.startswith("VISIT "):
            return Action("VISIT", line[6:].strip())
        if up.startswith("EXTRACT "):
            return Action("EXTRACT", line[8:].strip())
        if up.startswith("OCR "):
            return Action("OCR", line[4:].strip())
        if up.startswith("CLICK "):
            return Action("CLICK", line[6:].strip())
        if up.startswith("TYPE "):
            return Action("TYPE", line[5:].strip())
        if up.startswith("REPORT "):
            return Action("REPORT", line[7:].strip())
        if up.startswith("DONE"):
            return Action("DONE")
    return Action("DONE")


def sanitize_selector(selector: str) -> str:
    selector = selector.strip()
    if not selector:
        return ""

    # Pokud model vrati JSON tvar {"css": "..."} nebo {"selector": "..."}, vytahni hodnotu.
    if selector.startswith("{"):
        try:
            payload = json.loads(selector)
            if isinstance(payload, dict):
                for key in ("css", "selector"):
                    val = payload.get(key)
                    if isinstance(val, str) and val.strip():
                        return val.strip()
        except json.JSONDecodeError:
            # ignore and fall through to plain sanitization
            pass
        # pokud JSON parsing selze, odrizni vnejsi slozene zavorky
        selector = selector.lstrip("{").rstrip("}").strip()

    # Odstrani vse za '{' pokud model poslal cele CSS ruleset, a osekava mezery
    if "{" in selector:
        selector = selector.split("{", 1)[0]
    selector = selector.strip()
    # Odstran zaverecne '}' pokud zbyla
    if selector.endswith("}"):
        selector = selector[:-1].strip()
    return selector


def decode_suspect_value(value: str) -> List[str]:
    """Zkusi dekodovat base64/URL-encode/hex retezce a vrati kandidaty."""
    val = value.strip()
    if not val or len(val) > 4096:
        return []

    decoded: List[str] = []

    # URL decode
    try:
        url_dec = unquote_plus(val)
        if url_dec and url_dec != val:
            decoded.append(url_dec[:500])
    except Exception:
        pass

    # Base64 decode (s doplnenim paddingu)
    b64_chars = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=")
    if set(val).issubset(b64_chars) and len(val) >= 8:
        padded = val + "=" * ((4 - len(val) % 4) % 4)
        try:
            raw = base64.b64decode(padded, validate=True)
            text = raw.decode("utf-8", "ignore")
            if text and text != val:
                decoded.append(text[:500])
        except (binascii.Error, ValueError):
            pass

    # Hex decode
    hex_chars = set("0123456789abcdefABCDEF")
    if len(val) % 2 == 0 and set(val).issubset(hex_chars) and len(val) >= 10:
        try:
            raw = bytes.fromhex(val)
            text = raw.decode("utf-8", "ignore")
            if text and text != val:
                decoded.append(text[:500])
        except ValueError:
            pass

    # Deduplicate zachovanim poradi
    seen = set()
    uniq: List[str] = []
    for d in decoded:
        if d not in seen:
            seen.add(d)
            uniq.append(d)
    return uniq


def log_decoded_preview(log_hook: Callable[[str], None], step: int, label: str, decoded: List[str], max_items: int = 3) -> None:
    """Vytiskne kratkou ukazku dekodovanych hodnot pro snazsi debug."""
    if not decoded:
        return
    sample = decoded[:max_items]
    preview = "; ".join(sample)
    log_hook(f"[{step}] {label} DECODED -> {len(decoded)} nalezeno, ukazka: {preview}")


def extract_contacts_and_links(text: str) -> Dict[str, List[str]]:
    """Heuristicky vytahne URL, emaily a telefonni cisla z volneho textu."""
    if not text:
        return {"urls": [], "emails": [], "phones": []}

    urls = re.findall(r"https?://[^\s]+", text)
    emails = re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text)
    phones = re.findall(r"\+?\d[\d\s-]{7,}\d", text)

    def uniq_list(seq: List[str]) -> List[str]:
        seen = set()
        out: List[str] = []
        for s in seq:
            if s not in seen:
                seen.add(s)
                out.append(s)
        return out

    return {
        "urls": uniq_list(urls)[:10],
        "emails": uniq_list(emails)[:10],
        "phones": uniq_list(phones)[:10],
    }


def trim_history(history: List[Dict[str, str]], max_items: int = HISTORY_MAX_ITEMS, max_chars: int = HISTORY_MAX_CHARS) -> None:
    """Omezuje historii pro model, aby nerostla do pameti a nezpomalovala proces."""
    if len(history) <= 2:
        return

    head = history[:2]
    tail = history[2:]
    total_chars = sum(len(msg.get("content", "")) for msg in head)
    kept: List[Dict[str, str]] = []

    for msg in reversed(tail):
        total_chars += len(msg.get("content", ""))
        kept.append(msg)
        if len(head) + len(kept) >= max_items or total_chars >= max_chars:
            break

    kept.reverse()
    history[:] = head + kept[-(max_items - len(head)) :]


def make_browser(headless: bool = True) -> webdriver.Chrome:
    opts = Options()
    if headless:
        opts.add_argument("--headless=new")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-setuid-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--remote-debugging-pipe")
    # Snap/Wayland/ARM: pouzij fake display a vypni GPU, aby Chrome nepoustel capabilities.
    opts.add_argument("--use-gl=swiftshader")
    opts.add_argument("--disable-software-rasterizer")
    opts.add_argument("--window-size=1280,720")
    opts.set_capability("pageLoadStrategy", "none")

    # Umoznuje rucne nastavit binarku prohlizece (CHROME_BIN) a driver (CHROMEDRIVER).
    chrome_bin = os.environ.get("CHROME_BIN")
    if not chrome_bin and Path(DEFAULT_SNAP_CHROME).exists():
        chrome_bin = DEFAULT_SNAP_CHROME
    if chrome_bin:
        opts.binary_location = chrome_bin

    # Preferuj systemovy driver, jinak auto-download. Na odlisnych architekturach (ARM) pouzij CHROMEDRIVER.
    driver_path = os.environ.get("CHROMEDRIVER")
    force_local = os.environ.get("FORCE_LOCAL_DRIVER") == "1" or os.environ.get("FORCE_LOCAL_DRIVER") is None

    # Pokud CHROMEDRIVER neni nastaven, zkus nejdrive snap cestu, pak systemovou.
    if not driver_path and Path(DEFAULT_SNAP_DRIVER).exists():
        driver_path = DEFAULT_SNAP_DRIVER
    if not driver_path and Path(DEFAULT_LOCAL_DRIVER).exists():
        driver_path = DEFAULT_LOCAL_DRIVER

    if driver_path:
        service = Service(driver_path, log_output=CHROMEDRIVER_LOG)
    elif force_local:
        raise RuntimeError("Neni nastaven CHROMEDRIVER a FORCE_LOCAL_DRIVER=1 zakazuje auto-download. Nastav cestu k driveru.")
    else:
        # Auto-download (může stáhnout špatnou architekturu, pokud nejde o amd64).
        service = Service(ChromeDriverManager().install(), log_output=CHROMEDRIVER_LOG)

    driver = webdriver.Chrome(service=service, options=opts)
    driver.set_page_load_timeout(20)
    return driver


def run_agent(
    goal: str,
    headless: bool = True,
    log_hook: Optional[Callable[[str], None]] = None,
    image_hook: Optional[Callable[[str], None]] = None,
    report_hook: Optional[Callable[[str], None]] = None,
    social_hook: Optional[Callable[[str, List[Dict[str, object]]], None]] = None,
    stop_event: Optional[threading.Event] = None,
) -> None:
    system_prompt = (
        "Jsi autonomni webovy OSINT agent, ktery se pri prubeznem prohledavani inteligentne uci a vylepsuje dalsi dotazy. Odpovidej POUZE jednim radkem, bez komentaru.\n"
        "Cil: najdi informace o osobe (profil, aliasy, organizace, kontakty), nasbirej odkazy a obrazky a info pro reporty. Pri kazdem kroku zhodnot, co uz vis, a adaptuj dalsi dotaz/akci.\n"
        "Google/Brave/Bing triky: \"exact phrase\", OR, +must, -exclude, site:, intitle:, inurl:, intext:, filetype:/ext:, before:YYYY, after:YYYY, cache:, related:, source:.\n"
        "Google dorky (pouzivej a rotuj v dotazech): site:, intitle:, inurl:, intext:, allintitle:, allinurl:, allintext:, filetype:, ext:, cache:, info:, related:, source:, daterange:, before:, after:, numrange:, link:, map:, weather:, stocks:, define:, movie:, music:, imagesize:, safesearch:, phonebook:, location:, source:, inanchor:, allinanchor:, author:, group:, insubject:, \"+\"/\"-\" povinne/zakazane terminy, \"phrase\" presna fraze, * wildcards, OR, ( ), around(N), lang:, country:, source:, site:*.domain.tld, inurl:login|signin|admin, intitle:index.of, filetype:pdf|doc|xls|ppt|csv, ext:sql|db|bak|log, intext:password|passwd, intext:confidential, inurl:wp-admin|wp-login, inurl:?id=, inurl:view.php?id=, intitle:\"index of\" parent directory, inurl:gitlab|github + token, site:pastebin.com + email/phone, site:drive.google.com open?id, site:dropbox.com/s/, site:onedrive.live.com, site:mega.nz, site:docs.google.com/spreadsheets, site:slack.com messages, site:trello.com board, site:figma.com file, site:bitbucket.org snippet, site:s3.amazonaws.com bucket, site:storage.googleapis.com, filetype:pcap|har, filetype:json|yaml secrets, filetype:env, filetype:pkcs12|pfx, filetype:ppk, filetype:key, inurl:/backup/, inurl:/old/, inurl:/test/, inurl:/dev/, inurl:/staging/, intitle:\"error log\", intitle:\"phpinfo()\", intext:\"Internal Server Error\", site:cdninstagram.com {alias}, site:fbcdn.net {alias}.\n"
        "Instagram OSINT: site:instagram.com intext:{alias}; site:instagram.com/p/ +timestamp; site:cdninstagram.com {alias} (obrazky); hashtag + mesto; inurl:/stories/ {alias}; sleduj bio, link v bio, followings/followers z profilove stranky.\n"
        "Facebook OSINT: site:facebook.com/public {jmeno}; site:facebook.com/people {jmeno}; site:facebook.com inurl:/groups/ {tema|firma}; site:facebook.com inurl:/events/ {mesto}; kombinuj \"Work at\" \"Lives in\" ve vyhledavani; filtruj fotky pres photos tab a archivace ve Wayback.\n"
        "Phone OSINT: \"+420\" {cislo} site:facebook.com|instagram.com|x.com; intext:{cislo} AND (email|contact); site:pastebin.com {cislo}; site:github.com intext:{cislo}; zkus reverzni lookup pres numverify/opencnam API, pripadne telegram t.me/s {cislo} nebo {alias}.\n"
        "IP OSINT: whois/rdap {ip}; ipinfo.io/{ip}; virustotal.com/gui/ip-address/{ip}; abuseipdb.com/check/{ip}; shodan.io/host/{ip}; censys.io/ipv4?q={ip}; bgp.he.net/{ip}; vyhledej inurl:{ip} log nebo paste.\n"
        "OSINT hub vyhledavace: osintframework.com strom; publicwww.com k nalezeni kodu/emailu; crt.sh pro certifikaty; ahmia.fi pro Tor obsah; startpage/duckduckgo jako alternativni SERP.\n"
        "Stridej startovni dotazy (rotuj): site:linkedin.com/in {jmeno}; \"{jmeno}\" github; \"{jmeno}\" cv filetype:pdf; intitle:\"resume\" \"{jmeno}\"; \"{jmeno}\" email; site:pastebin.com {alias}; \"{jmeno}\" +\"phone\"; site:facebook.com \"{jmeno}\"; site:x.com {alias}; site:youtube.com \"{jmeno}\"; site:instagram.com {alias}; site:vk.com {alias}; site:reddit.com {alias}; site:medium.com \"{jmeno}\"; site:github.com {alias} orgs; whois/rdap <domena>; site:archive.org <url>.\n"
        "Zahrnuj casto vyuzivane platformy/databaze: Google/Bing/Brave, LinkedIn, GitHub, Twitter/X, Facebook, Instagram, YouTube, Reddit, Pastebin, Wayback Machine, Shodan/Censys (metadata), whois/rdap, company registries.\n"
        "Typicky zacni VISIT https://www.google.com/search?q=<dotaz> nebo podobnym, pak podle nalezu iteruj a rozsiruj.\n"
        "Povolene akce:\n"
        "VISIT <url>\n"
        "EXTRACT <css>\n"
        "OCR <css>\n"
        "CLICK <css>\n"
        "TYPE <css> |text|\n"
        "REPORT <shrnuty_osint_report>\n"
        "DONE\n"
        "Pokud narazis na zakodovane retezce (URL-encode/base64/hex), zkus je dekodovat a sdilej obsah; hash hodnoty jsou jednosmerne (nelze dekodovat).\n"
        "Pouzivej absolutni URL. Pokud uz mas fakta, pouzij REPORT pro kratky OSINT souhrn (kdo/co, aliasy, organizace, socialy/zdroje s URL, obrazky URL, datum/zdroj, jiste/nejiste).\n"
        "REPORT struktura: Souhrn; Zdroje (URL + co obsahuje); Scraping kody/dotazy (vyhledavaci operatory pro vyse uvedene platformy)."
    )

    if log_hook is None:
        log_hook = print
    if report_hook is None:
        report_hook = log_hook
    if stop_event is None:
        stop_event = threading.Event()

    driver = make_browser(headless=headless)
    history: List[Dict[str, str]] = [{"role": "system", "content": system_prompt}]
    history.append({"role": "user", "content": f"Ukolej: {goal}"})
    report_done = False
    finished_with_done = False
    last_visit_ts: Optional[float] = None
    decoded_seen: set[str] = set()
    decoded_agg: List[str] = []

    def record_decoded(values: List[str]) -> None:
        for v in values:
            if not v or v in decoded_seen:
                continue
            decoded_seen.add(v)
            decoded_agg.append(v)
            if len(decoded_agg) > 200:
                removed = decoded_agg.pop(0)
                decoded_seen.discard(removed)

    trim_history(history)

    try:
        for step in range(1, MAX_STEPS + 1):
            try:
                if stop_event.is_set():
                    log_hook("[STOP] preruseno uzivatelem")
                    break
                reply = call_ollama(history)
                log_hook(f"MODEL: {reply}")
                history.append({"role": "assistant", "content": reply})
                action = parse_action(reply)

                if action.name == "DONE":
                    if step < MIN_STEPS_BEFORE_DONE and not report_done:
                        prompt_msg = "Pokračuj ve sběru informací, ještě neshrnuto."
                        history.append({"role": "user", "content": prompt_msg})
                        log_hook(f"[WARN] DONE prilis brzy (krok {step}), vyzadano pokracovani")
                        continue
                    log_hook(f"Konec: {reply}")
                    finished_with_done = True
                    break

                if action.name == "VISIT" and action.args:
                    # Udrzuj odstup mezi navstevami kvuli plynulosti a rate limitum.
                    if last_visit_ts is not None:
                        gap = time.monotonic() - last_visit_ts
                        if gap < VISIT_PAUSE_SECONDS:
                            pause = VISIT_PAUSE_SECONDS - gap
                            log_hook(f"[{step}] VISIT pauza {pause:.1f}s (omezovani pozadavku)")
                            time.sleep(pause)
                    try:
                        driver.get(action.args)
                    except TimeoutException:
                        log_hook(f"[{step}] VISIT timeout (loadStrategy=none), pokracuji")
                    wait_dom_ready(driver, timeout=12)
                    time.sleep(0.6)
                    last_visit_ts = time.monotonic()
                    obs = {
                        "url": driver.current_url,
                        "title": driver.title,
                    }
                    history.append({"role": "user", "content": f"OBSERVE: {json.dumps(obs)}"})
                    log_hook(f"[{step}] VISIT -> {driver.current_url}")
                    report_hook(f"Krok {step}: navstiveno {driver.current_url}")

                    platform = detect_social_platform(driver.current_url)
                    if platform:
                        social_posts = extract_social_posts(driver, platform, image_hook=image_hook)
                        if social_posts:
                            payload = json.dumps(social_posts)
                            history.append({"role": "user", "content": f"SOCIAL_POSTS {platform}: {payload}"})
                            log_hook(f"[{step}] SOCIAL -> {platform}: {len(social_posts)} polozek")
                            decoded_social = [d for post in social_posts for d in post.get("decoded", [])]
                            log_decoded_preview(log_hook, step, "SOCIAL", decoded_social)
                            record_decoded(decoded_social)
                            if social_hook:
                                social_hook(platform, social_posts)
                            report_hook(f"Krok {step}: SOCIAL {platform} {len(social_posts)} polozek")
                        else:
                            log_hook(f"[{step}] SOCIAL -> {platform}: nic nenalezeno")
                    continue

                if action.name == "EXTRACT" and action.args:
                    selector = sanitize_selector(action.args)
                    if not selector:
                        history.append({"role": "user", "content": "EXTRACT_FAILED_INVALID_SELECTOR"})
                        log_hook(f"[{step}] EXTRACT -> prazdny selector po sanitizaci")
                        continue
                    time.sleep(EXTRACT_PAUSE_SECONDS)  # zpomaleni, aby se URL zbytecne nezatezovala
                    try:
                        elems = driver.find_elements(By.CSS_SELECTOR, selector)
                        # DuckDuckGo/Brave: .result__url nema href, link je na .result__a; zkus fallbacky.
                        if not elems and ".result__url" in selector:
                            alt_selector = selector.replace(".result__url", "a.result__a")
                            elems = driver.find_elements(By.CSS_SELECTOR, alt_selector)
                        if not elems and ".result__url" in selector:
                            alt_selector2 = selector.replace(".result__url", "a")
                            elems = driver.find_elements(By.CSS_SELECTOR, alt_selector2)
                    except InvalidSelectorException:
                        history.append({"role": "user", "content": "EXTRACT_FAILED_INVALID_SELECTOR"})
                        log_hook(f"[{step}] EXTRACT -> invalid selector: {selector}")
                        continue
                    extracted = []
                    decoded_payloads: List[str] = []
                    for e in elems[:5]:
                        item = {
                            "text": e.text[:500],
                            "href": e.get_attribute("href"),
                        }
                        src = e.get_attribute("src")
                        if src:
                            item["src"] = src
                            if image_hook:
                                image_hook(src)
                        # Heuristicky dopln dalsi mozna URL pro vysledky vyhledavacu (data-href, parent <a>...).
                        if not item.get("href"):
                            for attr in ("data-href", "data-url", "data-src", "cite"):
                                val = e.get_attribute(attr)
                                if val:
                                    item["href"] = val
                                    break
                        if not item.get("href"):
                            try:
                                ancestor_link = e.find_element(By.XPATH, "ancestor-or-self::a[@href][1]")
                                item["href"] = ancestor_link.get_attribute("href")
                            except Exception:
                                pass
                        for key in ("text", "href", "src"):
                            if item.get(key):
                                decoded_candidates = decode_suspect_value(item[key])
                                if decoded_candidates:
                                    item.setdefault("decoded", []).extend(decoded_candidates)
                                    decoded_payloads.extend(decoded_candidates)
                        extracted.append(item)
                    history.append({"role": "user", "content": f"EXTRACTED: {json.dumps(extracted)}"})
                    log_hook(f"[{step}] EXTRACT -> {len(extracted)} prvku")
                    report_hook(f"Krok {step}: extrahovano {len(extracted)} prvku z '{selector}'")
                    if decoded_payloads:
                        history.append({"role": "user", "content": f"DECODED: {json.dumps(decoded_payloads[:6])}"})
                        log_decoded_preview(log_hook, step, "EXTRACT", decoded_payloads)
                        record_decoded(decoded_payloads)
                    continue

                if action.name == "OCR" and action.args:
                    selector = sanitize_selector(action.args)
                    try:
                        elems = driver.find_elements(By.CSS_SELECTOR, selector)
                    except InvalidSelectorException:
                        history.append({"role": "user", "content": "OCR_FAILED_INVALID_SELECTOR"})
                        log_hook(f"[{step}] OCR -> invalid selector: {selector}")
                        continue
                    if elems:
                        img_bytes = elems[0].screenshot_as_png
                        text = pytesseract.image_to_string(Image.open(BytesIO(img_bytes)))
                        ocr_text = text.strip()[:1000]
                        history.append({"role": "user", "content": f"OCR: {ocr_text}"})
                        log_hook(f"[{step}] OCR -> {len(ocr_text)} znaku")
                        report_hook(f"Krok {step}: OCR {len(ocr_text)} znaku ze selectoru '{selector}'")
                        decoded_from_ocr = decode_suspect_value(ocr_text)
                        if decoded_from_ocr:
                            history.append({"role": "user", "content": f"OCR_DECODED: {json.dumps(decoded_from_ocr)}"})
                            log_decoded_preview(log_hook, step, "OCR", decoded_from_ocr)
                            record_decoded(decoded_from_ocr)
                        extracted_struct = extract_contacts_and_links(ocr_text)
                        if any(extracted_struct.values()):
                            history.append({"role": "user", "content": f"OCR_STRUCT: {json.dumps(extracted_struct)}"})
                            log_hook(
                                f"[{step}] OCR_STRUCT -> urls:{len(extracted_struct['urls'])} emails:{len(extracted_struct['emails'])} phones:{len(extracted_struct['phones'])}"
                            )
                    else:
                        history.append({"role": "user", "content": "OCR_FAILED"})
                        log_hook(f"[{step}] OCR -> 0 prvku")
                        report_hook(f"Krok {step}: OCR selhal, nic nenalezeno pro '{selector}'")
                    continue

                if action.name == "CLICK" and action.args:
                    selector = sanitize_selector(action.args)
                    try:
                        elems = driver.find_elements(By.CSS_SELECTOR, selector)
                    except InvalidSelectorException:
                        history.append({"role": "user", "content": "CLICK_FAILED_INVALID_SELECTOR"})
                        log_hook(f"[{step}] CLICK -> invalid selector: {selector}")
                        continue
                    if elems:
                        elems[0].click()
                        time.sleep(1.0)
                        history.append({"role": "user", "content": f"CLICKED: {selector}"})
                        log_hook(f"[{step}] CLICK -> {selector}")
                        report_hook(f"Krok {step}: klik na '{selector}'")
                    else:
                        history.append({"role": "user", "content": "CLICK_FAILED"})
                    continue

                if action.name == "TYPE" and action.args:
                    if "|" in action.args:
                        selector, text = action.args.split("|", 1)
                        selector = sanitize_selector(selector)
                        text = text.strip()
                        try:
                            elems = driver.find_elements(By.CSS_SELECTOR, selector)
                        except InvalidSelectorException:
                            history.append({"role": "user", "content": "TYPE_FAILED_INVALID_SELECTOR"})
                            log_hook(f"[{step}] TYPE -> invalid selector: {selector}")
                            continue
                        if elems:
                            box = elems[0]
                            box.clear()
                            box.send_keys(text)
                            box.send_keys(Keys.ENTER)
                            time.sleep(1.0)
                            history.append({"role": "user", "content": f"TYPED: {selector}"})
                            log_hook(f"[{step}] TYPE -> {selector}")
                            report_hook(f"Krok {step}: zadany text do '{selector}'")
                        else:
                            history.append({"role": "user", "content": "TYPE_FAILED"})
                    continue

                if action.name == "REPORT" and action.args:
                    report_text = action.args.strip()
                    history.append({"role": "user", "content": f"REPORT: {report_text}"})
                    log_hook(f"[{step}] REPORT -> {len(report_text)} znaku")
                    report_hook(f"Krok {step}: mezivysledek reportu ({len(report_text)} znaku)")
                    report_done = True
                    # po REPORT uz nechame model pripadne dat DONE, ale uz to nelameme
                    continue

                history.append({"role": "user", "content": "UNKNOWN_ACTION"})
            finally:
                trim_history(history)
                time.sleep(STEP_PAUSE_SECONDS)
        # Pokud uzivatel stiskl Stop, preskoc finalni dotaz (zamezi viseni na HTTP pri preruseni)
        if stop_event.is_set():
            log_hook("[STOP] ukonceno pred finalnim reportem")
            return

        # Po ukonceni smycky vyzadame finalni shrnuti od modelu s reportem a citacemi (vzdy, pokud nebylo stopnuto)
        try:
            status_tag = "COMPLETE" if finished_with_done else "PARTIAL"
            if decoded_agg:
                history.append({"role": "user", "content": f"DECODED_ALL: {json.dumps(decoded_agg)}"})
            history.append(
                {
                    "role": "user",
                    "content": (
                        f"Vytvor FINAL REPORT [{status_tag}] (jednim radkem, oddel sekce stredniky):"
                        " Souhrn faktu; Identita/aliasy; Organizace/role; Kontakty; Klicova data (datum/udalost);"
                        " Zdroje (URL + co obsahuje + datum, pokud je); Obrazky (URL + co ukazuji);"
                        " Dekodovane nalezy (DECODED_ALL, vcetne URL/textu z EXTRACT/OCR/SOCIAL)."
                    ),
                }
            )
            trim_history(history)
            final_reply = call_ollama(history)
            log_hook(f"FINAL_REPORT: {final_reply}")
            report_hook(f"FINAL_REPORT: {final_reply}")
        except Exception as exc:  # noqa: BLE001
            log_hook(f"[ERROR] Final report selhal: {exc}")
    finally:
        driver.quit()


def launch_gui():
    root = tk.Tk()
    root.title("Internetovy agent")

    report_win = tk.Toplevel(root)
    report_win.title("Finalni report")
    report_box = scrolledtext.ScrolledText(report_win, height=24, width=64, wrap="word", state="disabled")
    report_box.pack(fill="both", expand=True, padx=8, pady=8)

    image_win = tk.Toplevel(root)
    image_win.title("Obrazky (kolaz)")
    image_canvas = tk.Canvas(image_win, height=720, width=960)
    image_scroll = tk.Scrollbar(image_win, orient="vertical", command=image_canvas.yview)
    image_canvas.configure(yscrollcommand=image_scroll.set)
    image_canvas.pack(side="left", fill="both", expand=True)
    image_scroll.pack(side="right", fill="y")

    image_frame = tk.Frame(image_canvas)
    image_canvas.create_window((0, 0), window=image_frame, anchor="nw")

    image_refs: list[ImageTk.PhotoImage] = []
    image_frames: list[tk.Frame] = []

    social_win = tk.Toplevel(root)
    social_win.title("Social data")
    social_box = scrolledtext.ScrolledText(social_win, height=24, width=72, wrap="word", state="disabled")
    social_box.pack(fill="both", expand=True, padx=8, pady=8)

    def _update_scrollregion(event=None):  # noqa: ARG001
        image_canvas.configure(scrollregion=image_canvas.bbox("all"))

    image_frame.bind("<Configure>", _update_scrollregion)

    # Umisti prubezny report napravo od hlavniho okna, pokud to prostredi umozni.
    root.update_idletasks()
    try:
        rx = root.winfo_rootx() + root.winfo_width() + 12
        ry = root.winfo_rooty()
        report_win.geometry(f"+{rx}+{ry}")
        imgx = rx + report_win.winfo_width() + 12
        imgy = ry
        image_win.geometry(f"+{imgx}+{imgy}")
        socialx = imgx + image_win.winfo_width() + 12
        socialy = ry
        social_win.geometry(f"+{socialx}+{socialy}")
    except tk.TclError:
        pass

    prompt_label = tk.Label(root, text="Zadani (prompt):")
    prompt_label.pack(anchor="w", padx=8, pady=(8, 0))

    prompt_var = tk.StringVar(value="Najdi")
    prompt_entry = tk.Entry(root, textvariable=prompt_var, width=80)
    prompt_entry.pack(fill="x", padx=8, pady=4)

    headless_var = tk.BooleanVar(value=True)
    headless_check = tk.Checkbutton(root, text="Headless", variable=headless_var)
    headless_check.pack(anchor="w", padx=8)

    log_box = scrolledtext.ScrolledText(root, height=24, wrap="word", state="disabled")
    log_box.pack(fill="both", expand=True, padx=8, pady=8)

    tag_colors = {
        "step": "#0b61a4",
        "warn": "#c47f00",
        "error": "#c41010",
        "stop": "#7a3db8",
        "done": "#2d8a34",
        "model": "#444444",
        "info": "#333333",
    }
    for tag, color in tag_colors.items():
        log_box.tag_configure(tag, foreground=color)

    status_var = tk.StringVar(value="Ready")
    status_label = tk.Label(root, textvariable=status_var, anchor="w")
    status_label.pack(fill="x", padx=8, pady=(0, 8))

    q: queue.Queue[str] = queue.Queue()
    report_q: queue.Queue[str] = queue.Queue()
    social_q: queue.Queue[tuple[str, List[Dict[str, object]]]] = queue.Queue()
    image_q: queue.Queue[tuple[str, bytes]] = queue.Queue(maxsize=IMAGE_QUEUE_LIMIT)
    stop_event = threading.Event()
    worker_thread: list[threading.Thread] = []

    final_report_titles = [
        "Souhrn faktu",
        "Identita/aliasy",
        "Organizace/role",
        "Kontakty",
        "Klicova data",
        "Zdroje",
        "Obrazky",
    ]

    def append_log(msg: str) -> None:
        q.put(msg)

    def append_report(msg: str) -> None:
        report_q.put(msg)

    def append_social(platform: str, posts: List[Dict[str, object]]) -> None:
        social_q.put((platform, posts))

    def pick_tag(msg: str) -> str:
        if msg.startswith("[ERROR"):
            return "error"
        if msg.startswith("[WARN"):
            return "warn"
        if msg.startswith("[STOP"):
            return "stop"
        if msg.startswith("[DONE"):
            return "done"
        if re.match(r"^\[\d+\]", msg):
            return "step"
        if msg.startswith("MODEL:") or msg.startswith("FINAL_REPORT:"):
            return "model"
        return "info"

    def widget_exists(w: tk.Widget) -> bool:
        try:
            return bool(w) and w.winfo_exists()
        except tk.TclError:
            return False

    def render_social_batch(platform: str, posts: List[Dict[str, object]]) -> None:
        if not widget_exists(social_box):
            q.put("[WARN] Social okno zavreno, nelze zobrazit social data.")
            return
        social_box.configure(state="normal")
        for post in posts:
            text = (post.get("text") or "").strip()
            link = post.get("link") or ""
            ts_val = post.get("time") or ""
            decoded_list = post.get("decoded") or []
            struct = post.get("struct") or {}
            urls = ", ".join(struct.get("urls") or [])
            emails = ", ".join(struct.get("emails") or [])
            phones = ", ".join(struct.get("phones") or [])
            decoded_sample = "; ".join(decoded_list[:3]) if decoded_list else ""
            social_box.insert(
                "end",
                f"[{platform}] {ts_val}\n{text}\nURL: {link}\n" +
                (f"Decoded: {decoded_sample}\n" if decoded_sample else "") +
                (f"Contacts -> urls: {urls} emails: {emails} phones: {phones}\n" if (urls or emails or phones) else "") +
                "---\n",
            )

        # Omez velikost widgetu, aby nerostl do pameti.
        lines = int(social_box.index('end-1c').split('.')[0])
        if lines > SOCIAL_UI_MAX_LINES:
            trim_to = lines - SOCIAL_UI_MAX_LINES
            social_box.delete("1.0", f"{trim_to}.0")
        social_box.configure(state="disabled")
        social_box.see("end")

    def render_final_report(raw: str) -> None:
        """Pocisti finalni report a prehledne ho rozsekci do samostatnych bloku."""
        if not widget_exists(report_box):
            q.put("[WARN] Report okno zavreno, nelze vykreslit finalni report.")
            return
        content = raw.removeprefix("FINAL_REPORT:").strip()
        parts = [p.strip(" ;") for p in content.split(";") if p.strip()]

        report_box.configure(state="normal")
        report_box.delete("1.0", "end")

        for title, body in zip(final_report_titles, parts):
            report_box.insert("end", f"{title}:\n{body}\n\n")

        # Pokud model vrati mene casti, dopln prazdne sekce kvuli prehlednosti.
        for missing_title in final_report_titles[len(parts) :]:
            report_box.insert("end", f"{missing_title}:\n-\n\n")

        report_box.configure(state="disabled")
        report_box.see("1.0")

    def add_image_to_collage(url: str, data: bytes) -> None:
        try:
            img = Image.open(BytesIO(data))
            img.thumbnail((480, 360))
            photo = ImageTk.PhotoImage(img)
            image_refs.append(photo)
            frame = tk.Frame(image_frame, bd=1, relief="solid")
            image_frames.append(frame)
            idx = len(image_refs) - 1
            row, col = divmod(idx, 2)
            lbl = tk.Label(frame, image=photo)
            lbl.image = photo
            lbl.pack()
            cap = tk.Label(frame, text=url[:120], wraplength=460, justify="left")
            cap.pack(fill="x")
            frame.grid(row=row, column=col, padx=6, pady=6, sticky="n")
            _update_scrollregion()

            if len(image_refs) > IMAGE_REF_LIMIT:
                old_ref = image_refs.pop(0)
                old_frame = image_frames.pop(0)
                if widget_exists(old_frame):
                    old_frame.destroy()
                del old_ref
        except Exception as exc:  # noqa: BLE001
            q.put(f"[IMG_UI_ERR] {url}: {exc}")

    def enqueue_image(url: str) -> None:
        """Fetch image in worker thread and queue bytes for UI render."""
        if image_q.full():
            q.put("[WARN] Fronta obrazku plna, preskakuji dalsi stahovani.")
            return
        try:
            resp = requests.get(url, timeout=12)
            resp.raise_for_status()
            ctype = resp.headers.get("content-type", "")
            if "image" not in ctype:
                return
            image_q.put((url, resp.content))
        except Exception as exc:  # noqa: BLE001
            q.put(f"[IMG_ERR] {url}: {exc}")

    def process_queue() -> None:
        if not widget_exists(root):
            return
        while True:
            try:
                msg = q.get_nowait()
            except queue.Empty:
                break
            if widget_exists(log_box):
                log_box.configure(state="normal")
                log_box.insert("end", msg + "\n", (pick_tag(msg),))
                log_box.configure(state="disabled")
                log_box.see("end")
        while True:
            try:
                rmsg = report_q.get_nowait()
            except queue.Empty:
                break
            if rmsg.startswith("FINAL_REPORT:"):
                render_final_report(rmsg)
                continue
            if widget_exists(report_box):
                report_box.configure(state="normal")
                report_box.insert("end", rmsg + "\n")
                report_box.configure(state="disabled")
                report_box.see("end")
        while True:
            try:
                platform, posts = social_q.get_nowait()
            except queue.Empty:
                break
            render_social_batch(platform, posts)
        while True:
            try:
                url, data = image_q.get_nowait()
            except queue.Empty:
                break
            add_image_to_collage(url, data)
        root.after(200, process_queue)

    def run_in_thread():
        if worker_thread and worker_thread[0].is_alive():
            append_log("[WARN] Uz bezi; pockej nebo stiskni Stop.")
            return
        stop_event.clear()
        status_var.set("Bezi...")
        if widget_exists(log_box):
            log_box.configure(state="normal")
            log_box.delete("1.0", "end")
            log_box.configure(state="disabled")
        if widget_exists(report_box):
            report_box.configure(state="normal")
            report_box.delete("1.0", "end")
            report_box.configure(state="disabled")
        if widget_exists(social_box):
            social_box.configure(state="normal")
            social_box.delete("1.0", "end")
            social_box.configure(state="disabled")
        for child in list(image_frame.winfo_children()):
            child.destroy()
        image_refs.clear()
        image_frames.clear()

        def worker():
            try:
                run_agent(
                    prompt_var.get(),
                    headless=headless_var.get(),
                    log_hook=append_log,
                    image_hook=enqueue_image,
                    report_hook=append_report,
                    social_hook=append_social,
                    stop_event=stop_event,
                )
                append_log("[DONE]")
            except Exception as exc:  # noqa: BLE001
                append_log(f"[ERROR] {exc}")
            finally:
                status_var.set("Hotovo")

        t = threading.Thread(target=worker, daemon=True)
        worker_thread[:] = [t]
        t.start()

    def stop_run():
        stop_event.set()
        status_var.set("Zastavuji...")
        append_log("[STOP] pozadavek na zastaveni")
        if worker_thread and worker_thread[0].is_alive():
            worker_thread[0].join(timeout=5)
            if worker_thread[0].is_alive():
                append_log("[WARN] Vlákno stále bezi, pockej nebo zavri aplikaci.")

    btn_frame = tk.Frame(root)
    btn_frame.pack(fill="x", padx=8, pady=(0, 8))

    run_button = tk.Button(btn_frame, text="Spustit", command=run_in_thread)
    run_button.pack(side="left")

    stop_button = tk.Button(btn_frame, text="Stop", command=stop_run)
    stop_button.pack(side="left", padx=6)

    def save_final_report():
        text = report_box.get("1.0", "end-1c") if widget_exists(report_box) else ""
        if not text.strip():
            append_log("[INFO] Neni co ulozit (finalni report je prazdny).")
            return
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        default_name = f"final_report_{ts}.txt"
        path = filedialog.asksaveasfilename(
            defaultextension=".txt",
            initialfile=default_name,
            filetypes=[("Text", "*.txt"), ("All", "*.*")],
            title="Ulozit finalni report",
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            append_log(f"[INFO] Ulozeno do {path}")
        except OSError as exc:  # noqa: BLE001
            append_log(f"[ERROR] Ulozeni selhalo: {exc}")

    save_button = tk.Button(btn_frame, text="Ulozit finalni report", command=save_final_report)
    save_button.pack(side="left", padx=6)

    process_queue()
    root.mainloop()


if __name__ == "__main__":
    launch_gui()
