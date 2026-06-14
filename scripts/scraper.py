"""
TempSMS Scraper — runs via GitHub Actions cron job.
Sources: sms-online.co, quackr.io (both work reliably from GitHub Actions IPs).
Writes numbers + messages to Firebase Firestore.

pip install requests beautifulsoup4 firebase-admin
"""

import os, re, time, json, random, hashlib, logging
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup
import firebase_admin
from firebase_admin import credentials, firestore

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

SOURCES = [
    "https://sms-online.co",
    "https://quackr.io",
]

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
]

COUNTRY_FLAGS = {
    "US": "🇺🇸", "GB": "🇬🇧", "CA": "🇨🇦", "AU": "🇦🇺", "DE": "🇩🇪",
    "FR": "🇫🇷", "NL": "🇳🇱", "SE": "🇸🇪", "NO": "🇳🇴", "FI": "🇫🇮",
    "DK": "🇩🇰", "CH": "🇨🇭", "AT": "🇦🇹", "BE": "🇧🇪", "PL": "🇵🇱",
    "IT": "🇮🇹", "ES": "🇪🇸", "RU": "🇷🇺", "BR": "🇧🇷", "IN": "🇮🇳",
    "CN": "🇨🇳", "JP": "🇯🇵", "KR": "🇰🇷", "SG": "🇸🇬", "PH": "🇵🇭",
    "MY": "🇲🇾", "TH": "🇹🇭", "VN": "🇻🇳", "ID": "🇮🇩", "HK": "🇭🇰",
}

PHONE_PREFIXES = {
    "+1": ("US", "United States"), "+44": ("GB", "United Kingdom"),
    "+61": ("AU", "Australia"), "+49": ("DE", "Germany"),
    "+33": ("FR", "France"), "+31": ("NL", "Netherlands"),
    "+46": ("SE", "Sweden"), "+47": ("NO", "Norway"),
    "+358": ("FI", "Finland"), "+45": ("DK", "Denmark"),
    "+41": ("CH", "Switzerland"), "+32": ("BE", "Belgium"),
    "+48": ("PL", "Poland"), "+39": ("IT", "Italy"),
    "+34": ("ES", "Spain"), "+7": ("RU", "Russia"),
    "+55": ("BR", "Brazil"), "+91": ("IN", "India"),
    "+86": ("CN", "China"), "+81": ("JP", "Japan"),
    "+82": ("KR", "South Korea"), "+65": ("SG", "Singapore"),
    "+63": ("PH", "Philippines"), "+60": ("MY", "Malaysia"),
    "+66": ("TH", "Thailand"), "+84": ("VN", "Vietnam"),
    "+62": ("ID", "Indonesia"), "+852": ("HK", "Hong Kong"),
}

OTP_PATTERNS = [
    re.compile(r"\b([0-9]{6})\b"),
    re.compile(r"\b([0-9]{4})\b"),
    re.compile(r"(?:code|OTP|verification|pin|passcode)[:\s]+([A-Z0-9]{4,8})", re.I),
    re.compile(r"(?:is|:)\s+([0-9]{4,8})\b"),
]

SESSION = requests.Session()


def detect_country(number: str):
    cleaned = re.sub(r"\s+", "", number)
    if not cleaned.startswith("+"):
        cleaned = "+" + cleaned
    for prefix in sorted(PHONE_PREFIXES, key=len, reverse=True):
        if cleaned.startswith(prefix):
            code, name = PHONE_PREFIXES[prefix]
            return code, name, COUNTRY_FLAGS.get(code, "🌐")
    return "US", "United States", COUNTRY_FLAGS.get("US", "🌐")


def extract_otp(body: str):
    for pat in OTP_PATTERNS:
        m = pat.search(body)
        if m:
            candidate = re.sub(r"[-\s]", "", m.group(1))
            if 4 <= len(candidate) <= 8:
                return m.group(1)
    return None


def make_headers(referer: str = None):
    h = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }
    if referer:
        h["Referer"] = referer
    return h


def fetch_html(url: str, referer: str = None, timeout=20):
    try:
        r = SESSION.get(url, headers=make_headers(referer), timeout=timeout, allow_redirects=True)
        if r.status_code == 200:
            return r.text
        log.warning(f"HTTP {r.status_code} — {url}")
        return None
    except Exception as e:
        log.warning(f"Fetch error [{url}]: {e}")
        return None


def doc_id(number: str):
    return re.sub(r"[^\w]", "_", number)


def normalize_number(raw: str):
    cleaned = re.sub(r"\s+", "", raw)
    if cleaned and not cleaned.startswith("+"):
        cleaned = "+" + cleaned
    return cleaned


# ── sms-online.co parsers ─────────────────────────────────────────────────

def parse_smsonline_numbers(html: str):
    soup = BeautifulSoup(html, "html.parser")
    nums = []
    seen = set()
    for a in soup.find_all("a", href=True):
        m = re.search(r"/receive-free-sms/(\d+)", a["href"])
        if not m:
            continue
        raw = m.group(1)
        if raw in seen:
            continue
        seen.add(raw)
        number = normalize_number(raw)
        code, name, flag = detect_country(number)
        nums.append({
            "number": number,
            "countryCode": code,
            "country": name,
            "countryFlag": flag,
            "isActive": True,
            "_base": "https://sms-online.co",
            "_path": f"/receive-free-sms/{raw}",
        })
    return nums


def parse_smsonline_messages(html: str, number: str):
    soup = BeautifulSoup(html, "html.parser")
    msgs = []
    for item in soup.select("div.list-item"):
        sender_el = item.select_one(".list-item-title")
        body_el = item.select_one(".list-item-content")
        if not body_el:
            continue
        sender = sender_el.get_text(strip=True) if sender_el else "Unknown"
        body = body_el.get_text(strip=True)
        if not body:
            continue
        _add_msg(msgs, number, sender, body, "")
    return msgs


# ── quackr.io parsers ─────────────────────────────────────────────────────

def parse_quackr_numbers(html: str):
    soup = BeautifulSoup(html, "html.parser")
    nums = []
    seen = set()
    for a in soup.find_all("a", href=True):
        m = re.search(r"/temporary-numbers/[\w-]+/(\d+)", a["href"])
        if not m:
            continue
        raw = m.group(1)
        if raw in seen:
            continue
        seen.add(raw)
        number = normalize_number(raw)
        code, name, flag = detect_country(number)
        path_m = re.search(r"(/temporary-numbers/[^\s\"']+)", a["href"])
        nums.append({
            "number": number,
            "countryCode": code,
            "country": name,
            "countryFlag": flag,
            "isActive": True,
            "_base": "https://quackr.io",
            "_path": path_m.group(1) if path_m else f"/temporary-numbers/{raw}",
        })
    return nums


def parse_quackr_messages(html: str, number: str):
    soup = BeautifulSoup(html, "html.parser")
    msgs = []
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            items = data if isinstance(data, list) else [data]
            for item in items:
                body = item.get("text") or item.get("description") or ""
                author = item.get("author", {})
                sender = author.get("name", "Unknown") if isinstance(author, dict) else "Unknown"
                if body:
                    _add_msg(msgs, number, sender, body, "")
        except Exception:
            pass
    return msgs


# ── Shared helpers ────────────────────────────────────────────────────────

def _add_msg(msgs, number, sender, body, time_txt):
    received_at = datetime.now(timezone.utc).isoformat()
    msg_id = hashlib.md5(f"{number}|{sender}|{body[:60]}".encode()).hexdigest()
    msgs.append({
        "id": msg_id,
        "from": sender,
        "body": body,
        "receivedAt": received_at,
        "number": number,
        "extractedOtp": extract_otp(body),
    })


PARSERS = {
    "https://sms-online.co": {
        "list_url": "https://sms-online.co/receive-free-sms",
        "parse_nums": parse_smsonline_numbers,
        "parse_msgs": parse_smsonline_messages,
    },
    "https://quackr.io": {
        "list_url": "https://quackr.io/temporary-numbers",
        "parse_nums": parse_quackr_numbers,
        "parse_msgs": parse_quackr_messages,
    },
}


def scrape_numbers():
    for base in SOURCES:
        p = PARSERS[base]
        log.info(f"Trying source: {base}")
        html = fetch_html(p["list_url"])
        if not html:
            continue
        nums = p["parse_nums"](html)
        if nums:
            log.info(f"Got {len(nums)} numbers from {base}")
            return nums
        log.warning(f"No numbers parsed from {base}")
    return []


def scrape_messages_for(num_doc: dict):
    base = num_doc.get("_base", "")
    path = num_doc.get("_path", "")
    number = num_doc["number"]
    if not base or not path:
        return []
    url = base + path
    time.sleep(random.uniform(2, 5))
    html = fetch_html(url, referer=PARSERS.get(base, {}).get("list_url", base))
    if not html:
        return []
    parse_msgs = PARSERS.get(base, {}).get("parse_msgs")
    if parse_msgs:
        return parse_msgs(html, number)
    return []


# ── Firebase ──────────────────────────────────────────────────────────────

def init_firebase():
    cred_json = os.environ.get("FIREBASE_SERVICE_ACCOUNT")
    if not cred_json:
        raise RuntimeError("FIREBASE_SERVICE_ACCOUNT env var not set")
    cred = credentials.Certificate(json.loads(cred_json))
    firebase_admin.initialize_app(cred)
    return firestore.client()


def run_scrape():
    log.info("=== TempSMS Scraper ===")
    db = init_firebase()

    numbers = scrape_numbers()
    if not numbers:
        log.error("No numbers found from any source. Aborting.")
        return

    batch = db.batch()
    batch_ops = 0
    total_msgs = 0

    for num_doc in numbers:
        msgs = scrape_messages_for(num_doc)
        total_msgs += len(msgs)
        did = doc_id(num_doc["number"])
        num_ref = db.collection("numbers").document(did)

        clean = {k: v for k, v in num_doc.items() if not k.startswith("_")}
        clean.update({
            "messageCount": len(msgs),
            "lastActivity": msgs[0]["receivedAt"] if msgs else None,
            "updatedAt": datetime.now(timezone.utc).isoformat(),
        })
        batch.set(num_ref, clean, merge=True)
        batch_ops += 1

        for msg in msgs:
            msg_ref = num_ref.collection("messages").document(msg["id"])
            batch.set(msg_ref, msg, merge=True)
            batch_ops += 1
            if batch_ops >= 400:
                batch.commit()
                log.info("Batch committed (400 ops)")
                batch = db.batch()
                batch_ops = 0

    if batch_ops:
        batch.commit()

    db.collection("meta").document("stats").set({
        "totalNumbers": len(numbers),
        "totalMessages": total_msgs,
        "lastScrapeAt": datetime.now(timezone.utc).isoformat(),
        "isLive": True,
    })
    log.info(f"Done — {len(numbers)} numbers, {total_msgs} messages")


if __name__ == "__main__":
    run_scrape()
