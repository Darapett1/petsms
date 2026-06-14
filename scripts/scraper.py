"""
TempSMS Scraper — runs via GitHub Actions cron job.
Tries multiple public temporary-SMS aggregator sites in order.
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

# ── Sources (tried in order until one yields numbers) ──────────────────────
SOURCES = [
    "https://smstome.com",
    "https://receive-sms.cc",
    "https://receivesms.co",
    "https://sms-online.co",
    "https://receive-smss.com",
]

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 14; Pixel 8 Pro) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.6367.82 Mobile Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36 Edg/122.0.0.0",
]

COUNTRY_FLAGS = {
    "US":"🇺🇸","GB":"🇬🇧","CA":"🇨🇦","AU":"🇦🇺","DE":"🇩🇪",
    "FR":"🇫🇷","NL":"🇳🇱","SE":"🇸🇪","NO":"🇳🇴","FI":"🇫🇮",
    "DK":"🇩🇰","CH":"🇨🇭","AT":"🇦🇹","BE":"🇧🇪","PL":"🇵🇱",
    "IT":"🇮🇹","ES":"🇪🇸","RU":"🇷🇺","BR":"🇧🇷","IN":"🇮🇳",
    "CN":"🇨🇳","JP":"🇯🇵","KR":"🇰🇷","SG":"🇸🇬","PH":"🇵🇭",
    "MY":"🇲🇾","TH":"🇹🇭","VN":"🇻🇳","ID":"🇮🇩","HK":"🇭🇰",
}

PHONE_PREFIXES = {
    "+1":("US","United States"),"+44":("GB","United Kingdom"),
    "+61":("AU","Australia"),"+49":("DE","Germany"),
    "+33":("FR","France"),"+31":("NL","Netherlands"),
    "+46":("SE","Sweden"),"+47":("NO","Norway"),
    "+358":("FI","Finland"),"+45":("DK","Denmark"),
    "+41":("CH","Switzerland"),"+32":("BE","Belgium"),
    "+48":("PL","Poland"),"+39":("IT","Italy"),
    "+34":("ES","Spain"),"+7":("RU","Russia"),
    "+55":("BR","Brazil"),"+91":("IN","India"),
    "+86":("CN","China"),"+81":("JP","Japan"),
    "+82":("KR","South Korea"),"+65":("SG","Singapore"),
    "+63":("PH","Philippines"),"+60":("MY","Malaysia"),
    "+66":("TH","Thailand"),"+84":("VN","Vietnam"),
    "+62":("ID","Indonesia"),"+852":("HK","Hong Kong"),
}

OTP_PATTERNS = [
    re.compile(r"\b([0-9]{6})\b"),
    re.compile(r"\b([0-9]{4})\b"),
    re.compile(r"\b([0-9]{3}[-\s][0-9]{3})\b"),
    re.compile(r"(?:code|OTP|verification|pin|passcode)[:\s]+([A-Z0-9]{4,8})", re.I),
    re.compile(r"(?:is|:)\s+([0-9]{4,8})\b"),
]

SESSION = requests.Session()


def detect_country(number: str):
    cleaned = re.sub(r"\s+", "", number)
    for prefix in sorted(PHONE_PREFIXES, key=len, reverse=True):
        if cleaned.startswith(prefix):
            code, name = PHONE_PREFIXES[prefix]
            return code, name, COUNTRY_FLAGS.get(code, "🌐")
    return "US", "United States", "🇺🇸"


def extract_otp(body: str):
    for pat in OTP_PATTERNS:
        m = pat.search(body)
        if m:
            candidate = re.sub(r"[-\s]", "", m.group(1))
            if 4 <= len(candidate) <= 8:
                return m.group(1)
    return None


def headers():
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "DNT": "1",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }


def fetch(url: str, referer: str = None, timeout=18) -> str | None:
    h = headers()
    if referer:
        h["Referer"] = referer
    try:
        r = SESSION.get(url, headers=h, timeout=timeout, allow_redirects=True)
        if r.status_code == 200:
            return r.text
        log.warning(f"HTTP {r.status_code} — {url}")
        return None
    except Exception as e:
        log.warning(f"Fetch error [{url}]: {e}")
        return None


def doc_id(number: str) -> str:
    return re.sub(r"[^\w]", "_", number)


# ── Per-site parsers ──────────────────────────────────────────────────────

def parse_smstome(html: str, base: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    nums = []
    seen = set()
    for a in soup.find_all("a", href=True):
        m = re.search(r"/receive-sms/(\+?[\d]+)", a["href"])
        if not m: continue
        n = m.group(1)
        if n in seen: continue
        seen.add(n)
        code, name, flag = detect_country(n)
        nums.append({"number":n,"countryCode":code,"country":name,"countryFlag":flag,
                     "isActive":True,"_base":base,"_path":f"/receive-sms/{n}"})
    return nums


def parse_receive_sms_cc(html: str, base: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    nums = []
    seen = set()
    for a in soup.find_all("a", href=True):
        m = re.search(r"/sms/(\+?[\d]+)", a["href"])
        if not m: continue
        n = m.group(1)
        if n in seen: continue
        seen.add(n)
        code, name, flag = detect_country(n)
        nums.append({"number":n,"countryCode":code,"country":name,"countryFlag":flag,
                     "isActive":True,"_base":base,"_path":f"/sms/{n}"})
    return nums


def parse_receivesms_co(html: str, base: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    nums = []
    seen = set()
    for a in soup.find_all("a", href=True):
        m = re.search(r"/phone-number/(\+?[\d]+)", a["href"])
        if not m:
            m = re.search(r"/sms/(\+?[\d]+)", a["href"])
        if not m: continue
        n = m.group(1)
        if n in seen: continue
        seen.add(n)
        code, name, flag = detect_country(n)
        nums.append({"number":n,"countryCode":code,"country":name,"countryFlag":flag,
                     "isActive":True,"_base":base,"_path":f"/phone-number/{n}"})
    return nums


def parse_generic(html: str, base: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    nums = []
    seen = set()
    patterns = [
        r"/(?:sms|receive|inbox|number)/(\+?[\d]{7,15})",
        r"(\+[\d]{7,15})",
    ]
    for a in soup.find_all("a", href=True):
        for pat in patterns:
            m = re.search(pat, a["href"])
            if m:
                n = m.group(1)
                if n in seen: continue
                seen.add(n)
                code, name, flag = detect_country(n)
                nums.append({"number":n,"countryCode":code,"country":name,"countryFlag":flag,
                             "isActive":True,"_base":base,"_path":a["href"]})
                break
    return nums


SITE_PARSERS = {
    "https://smstome.com":    parse_smstome,
    "https://receive-sms.cc": parse_receive_sms_cc,
    "https://receivesms.co":  parse_receivesms_co,
}


def scrape_numbers() -> list[dict]:
    for base in SOURCES:
        log.info(f"Trying source: {base}")
        html = fetch(base + "/")
        if not html:
            continue
        parser = SITE_PARSERS.get(base, parse_generic)
        nums = parser(html, base)
        if nums:
            log.info(f"Got {len(nums)} numbers from {base}")
            return nums
        log.warning(f"No numbers parsed from {base}")
    return []


def scrape_messages_for(num_doc: dict) -> list[dict]:
    base = num_doc.get("_base", "")
    path = num_doc.get("_path", "")
    number = num_doc["number"]
    url = base + path if path else None
    if not url:
        return []

    time.sleep(random.uniform(5, 10))  # stealth delay
    html = fetch(url, referer=base + "/")
    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    msgs = []

    # Try table rows first
    for row in soup.select("table tbody tr, .sms-list tr, .message-list li"):
        cells = row.find_all(["td", "li"])
        if len(cells) < 2: continue
        sender   = cells[0].get_text(strip=True) or "Unknown"
        body_el  = cells[1] if len(cells) > 1 else cells[0]
        body     = body_el.get_text(strip=True)
        time_txt = cells[-1].get_text(strip=True) if len(cells) > 2 else ""
        if not body: continue
        _add_msg(msgs, number, sender, body, time_txt)

    # Fallback: look for common SMS card elements
    if not msgs:
        for card in soup.select(".sms-content, .message-content, [class*='sms'], [class*='message']"):
            body = card.get_text(strip=True)
            if len(body) > 3:
                sender_el = card.find_previous(class_=re.compile("from|sender|number", re.I))
                sender = sender_el.get_text(strip=True) if sender_el else "Unknown"
                _add_msg(msgs, number, sender, body, "")

    log.info(f"  {number}: {len(msgs)} messages")
    return msgs


def _add_msg(msgs, number, sender, body, time_txt):
    received_at = datetime.now(timezone.utc).isoformat()
    try:
        parsed = datetime.fromisoformat(time_txt)
        received_at = parsed.replace(tzinfo=timezone.utc).isoformat()
    except Exception:
        pass
    msg_id = hashlib.md5(f"{number}|{sender}|{body[:60]}".encode()).hexdigest()
    msgs.append({
        "id": msg_id,
        "from": sender,
        "body": body,
        "receivedAt": received_at,
        "number": number,
        "extractedOtp": extract_otp(body),
    })


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

        clean = {k:v for k,v in num_doc.items() if not k.startswith("_")}
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
        "totalNumbers":  len(numbers),
        "totalMessages": total_msgs,
        "lastScrapeAt":  datetime.now(timezone.utc).isoformat(),
        "isLive":        True,
    })
    log.info(f"Done — {len(numbers)} numbers, {total_msgs} messages")


if __name__ == "__main__":
    run_scrape()
