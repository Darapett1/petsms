"""
TempSMS Scraper — 5-server architecture, 20 numbers per server.
Source names are never exposed; only "Server 1–5" labels are shown to users.
"""

import os, re, time, json, random, hashlib, logging
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup
import firebase_admin
from firebase_admin import credentials, firestore

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

MAX_PER_SOURCE = 20

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
    "SI": "🇸🇮", "RO": "🇷🇴", "UA": "🇺🇦", "CZ": "🇨🇿", "SK": "🇸🇰",
    "HU": "🇭🇺", "PT": "🇵🇹", "GR": "🇬🇷", "TR": "🇹🇷", "IL": "🇮🇱",
    "MX": "🇲🇽", "AR": "🇦🇷", "CO": "🇨🇴", "ZA": "🇿🇦", "NG": "🇳🇬",
}

PHONE_PREFIXES = {
    "+1":   ("US", "United States"),
    "+44":  ("GB", "United Kingdom"),
    "+61":  ("AU", "Australia"),
    "+49":  ("DE", "Germany"),
    "+33":  ("FR", "France"),
    "+31":  ("NL", "Netherlands"),
    "+46":  ("SE", "Sweden"),
    "+47":  ("NO", "Norway"),
    "+358": ("FI", "Finland"),
    "+45":  ("DK", "Denmark"),
    "+41":  ("CH", "Switzerland"),
    "+32":  ("BE", "Belgium"),
    "+48":  ("PL", "Poland"),
    "+39":  ("IT", "Italy"),
    "+34":  ("ES", "Spain"),
    "+7":   ("RU", "Russia"),
    "+55":  ("BR", "Brazil"),
    "+91":  ("IN", "India"),
    "+86":  ("CN", "China"),
    "+81":  ("JP", "Japan"),
    "+82":  ("KR", "South Korea"),
    "+65":  ("SG", "Singapore"),
    "+63":  ("PH", "Philippines"),
    "+60":  ("MY", "Malaysia"),
    "+66":  ("TH", "Thailand"),
    "+84":  ("VN", "Vietnam"),
    "+62":  ("ID", "Indonesia"),
    "+852": ("HK", "Hong Kong"),
    "+386": ("SI", "Slovenia"),
    "+40":  ("RO", "Romania"),
    "+380": ("UA", "Ukraine"),
    "+420": ("CZ", "Czech Republic"),
    "+421": ("SK", "Slovakia"),
    "+36":  ("HU", "Hungary"),
    "+351": ("PT", "Portugal"),
    "+30":  ("GR", "Greece"),
    "+90":  ("TR", "Turkey"),
    "+972": ("IL", "Israel"),
    "+52":  ("MX", "Mexico"),
    "+54":  ("AR", "Argentina"),
    "+57":  ("CO", "Colombia"),
    "+27":  ("ZA", "South Africa"),
    "+234": ("NG", "Nigeria"),
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
        # NOTE: no "br" — requests cannot decode Brotli without the brotli package
        "Accept-Encoding": "gzip, deflate",
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }
    if referer:
        h["Referer"] = referer
    return h


def fetch_html(url: str, referer: str = None, timeout: int = 30, retries: int = 2):
    for attempt in range(retries + 1):
        try:
            r = SESSION.get(url, headers=make_headers(referer), timeout=timeout, allow_redirects=True)
            if r.status_code == 200:
                return r.text
            log.warning(f"HTTP {r.status_code} — {url}")
            return None
        except Exception as e:
            log.warning(f"Fetch error attempt {attempt+1} [{url}]: {e}")
            if attempt < retries:
                time.sleep(3)
    return None


def doc_id(number: str, server: int) -> str:
    return f"s{server}_{re.sub(r'[^\w]', '_', number)}"


def normalize_number(raw: str) -> str:
    cleaned = re.sub(r"\s+", "", raw)
    if cleaned and not cleaned.startswith("+"):
        cleaned = "+" + cleaned
    return cleaned


def _add_msg(msgs, number, sender, body):
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


# ── Server 1: sms-online.co ───────────────────────────────────────────────

def parse_s1_numbers(html: str):
    soup = BeautifulSoup(html, "html.parser")
    nums, seen = [], set()
    for a in soup.find_all("a", href=True):
        m = re.search(r"/receive-free-sms/(\d+)", a["href"])
        if not m or m.group(1) in seen:
            continue
        seen.add(m.group(1))
        raw = m.group(1)
        number = normalize_number(raw)
        code, name, flag = detect_country(number)
        nums.append({
            "number": number, "countryCode": code, "country": name, "countryFlag": flag,
            "isActive": True,
            "_base": "https://sms-online.co",
            "_path": f"/receive-free-sms/{raw}",
        })
        if len(nums) >= MAX_PER_SOURCE:
            break
    return nums


def parse_s1_messages(html: str, number: str):
    soup = BeautifulSoup(html, "html.parser")
    msgs = []
    for item in soup.select("div.list-item"):
        sender_el = item.select_one(".list-item-title")
        body_el   = item.select_one(".list-item-content")
        if not body_el:
            continue
        sender = sender_el.get_text(strip=True) if sender_el else "Unknown"
        body   = body_el.get_text(strip=True)
        if body:
            _add_msg(msgs, number, sender, body)
    return msgs


# ── Server 2: receive-sms.cc ─────────────────────────────────────────────
# Numbers: /Country-Phone-Number/NUMBER
# Messages: div.item > div.form (sender) + div.con (body)

def parse_s2_numbers(html: str):
    soup = BeautifulSoup(html, "html.parser")
    nums, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        m = re.search(r"/[A-Z][a-z]+-Phone-Number/(\d{7,15})", href)
        if not m or m.group(1) in seen:
            continue
        seen.add(m.group(1))
        raw = m.group(1)
        number = normalize_number(raw)
        code, name, flag = detect_country(number)
        path = re.search(r"(/[A-Z][^/]+-Phone-Number/\d+)", href)
        nums.append({
            "number": number, "countryCode": code, "country": name, "countryFlag": flag,
            "isActive": True,
            "_base": "https://receive-sms.cc",
            "_path": path.group(1) if path else f"/US-Phone-Number/{raw}",
        })
        if len(nums) >= MAX_PER_SOURCE:
            break
    return nums


def parse_s2_messages(html: str, number: str):
    soup = BeautifulSoup(html, "html.parser")
    msgs = []
    for item in soup.select("div.item"):
        form_el = item.select_one("div.form")
        con_el  = item.select_one("div.con")
        if not con_el:
            continue
        raw_sender = form_el.get_text(strip=True) if form_el else "Unknown"
        # Strip "From " prefix if present
        sender = re.sub(r"^[Ff]rom\s*", "", raw_sender).strip() or "Unknown"
        body   = con_el.get_text(strip=True)
        if body and len(body) > 2:
            _add_msg(msgs, number, sender, body)
    return msgs


# ── Server 3: temporary-phone-number.com ─────────────────────────────────
# Numbers: /Country-Phone-Number/NUMBER
# Messages: div.direct-chat-msg.left — sender in span.direct-chat-name, body in div.direct-chat-text

def parse_s3_numbers(html: str):
    soup = BeautifulSoup(html, "html.parser")
    nums, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        m = re.search(r"/[A-Z][a-z]+-Phone-Number/(\d{7,15})", href)
        if not m or m.group(1) in seen:
            continue
        seen.add(m.group(1))
        raw = m.group(1)
        number = normalize_number(raw)
        code, name, flag = detect_country(number)
        path = re.search(r"(/[A-Z][^/]+-Phone-Number/\d+)", href)
        nums.append({
            "number": number, "countryCode": code, "country": name, "countryFlag": flag,
            "isActive": True,
            "_base": "https://temporary-phone-number.com",
            "_path": path.group(1) if path else f"/UK-Phone-Number/{raw}",
        })
        if len(nums) >= MAX_PER_SOURCE:
            break
    return nums


def parse_s3_messages(html: str, number: str):
    soup = BeautifulSoup(html, "html.parser")
    msgs = []
    SKIP_KEYWORDS = ["register", "login", "pagead", "adsbygoogle"]
    for msg_div in soup.select("div.direct-chat-msg.left"):
        name_el = msg_div.select_one("span.direct-chat-name")
        body_el = msg_div.select_one("div.direct-chat-text")
        if not body_el:
            continue
        body = body_el.get_text(strip=True)
        # Skip login-gate messages and ad blocks
        if any(kw in body.lower() for kw in SKIP_KEYWORDS):
            continue
        if not body or len(body) < 3:
            continue
        sender = name_el.get_text(strip=True) if name_el else "Unknown"
        _add_msg(msgs, number, sender, body)
    return msgs


# ── Server 4: receive-sms-free.cc ─────────────────────────────────────────
# Confirmed structure — /Free-Country-Phone-Number/number/
# Messages: div.sms-item > span.sender-badge + p.sms-content

def parse_s4_numbers(html: str):
    soup = BeautifulSoup(html, "html.parser")
    nums, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        m = re.search(r"/Free-[^/]+-Phone-Number/(\d{7,15})/?", href)
        if not m:
            continue
        raw = m.group(1)
        if raw in seen:
            continue
        seen.add(raw)
        number = normalize_number(raw)
        code, name, flag = detect_country(number)
        path_m = re.search(r"(/Free-[^/]+-Phone-Number/\d+/?)", href)
        nums.append({
            "number": number, "countryCode": code, "country": name, "countryFlag": flag,
            "isActive": True,
            "_base": "https://receive-sms-free.cc",
            "_path": path_m.group(1) if path_m else f"/Free-USA-Phone-Number/{raw}/",
        })
        if len(nums) >= MAX_PER_SOURCE:
            break
    return nums


def parse_s4_messages(html: str, number: str):
    soup = BeautifulSoup(html, "html.parser")
    msgs = []
    for item in soup.select("div.sms-item"):
        sender_el = item.select_one("span.sender-badge")
        body_el   = item.select_one("p.sms-content")
        if not body_el:
            continue
        sender = sender_el.get_text(strip=True) if sender_el else "Unknown"
        body   = body_el.get_text(strip=True)
        if body and len(body) > 2:
            _add_msg(msgs, number, sender, body)
    return msgs


# ── Server 5: hs3x.com ────────────────────────────────────────────────────
# Numbers: read-sms-NUMBER.html
# Messages: table.plist rows (From / Message / Time)

def parse_s5_numbers(html: str):
    soup = BeautifulSoup(html, "html.parser")
    nums, seen = [], set()
    for a in soup.find_all("a", href=True):
        m = re.search(r"read-sms-(\d{7,15})\.html", a["href"])
        if not m or m.group(1) in seen:
            continue
        seen.add(m.group(1))
        raw = m.group(1)
        number = normalize_number(raw)
        code, name, flag = detect_country(number)
        nums.append({
            "number": number, "countryCode": code, "country": name, "countryFlag": flag,
            "isActive": True,
            "_base": "https://hs3x.com",
            "_path": f"/read-sms-{raw}.html",
        })
        if len(nums) >= MAX_PER_SOURCE:
            break
    return nums


def parse_s5_messages(html: str, number: str):
    soup = BeautifulSoup(html, "html.parser")
    msgs = []
    for row in soup.select("table.plist tr"):
        cells = row.find_all("td")
        if len(cells) < 2:
            continue
        if cells[0].find("strong"):
            continue
        sender = cells[0].get_text(strip=True) or "Unknown"
        body   = cells[1].get_text(strip=True)
        if body and len(body) > 2:
            _add_msg(msgs, number, sender, body)
    return msgs


# ── Source registry ───────────────────────────────────────────────────────

SOURCES = [
    {
        "server": 1,
        "base": "https://sms-online.co",
        "list_url": "https://sms-online.co/receive-free-sms",
        "parse_nums": parse_s1_numbers,
        "parse_msgs": parse_s1_messages,
    },
    {
        "server": 2,
        "base": "https://receive-sms.cc",
        "list_url": "https://receive-sms.cc/",
        "parse_nums": parse_s2_numbers,
        "parse_msgs": parse_s2_messages,
    },
    {
        "server": 3,
        "base": "https://temporary-phone-number.com",
        "list_url": "https://temporary-phone-number.com/",
        "parse_nums": parse_s3_numbers,
        "parse_msgs": parse_s3_messages,
    },
    {
        "server": 4,
        "base": "https://receive-sms-free.cc",
        "list_url": "https://receive-sms-free.cc/",
        "parse_nums": parse_s4_numbers,
        "parse_msgs": parse_s4_messages,
    },
    {
        "server": 5,
        "base": "https://hs3x.com",
        "list_url": "https://hs3x.com/",
        "parse_nums": parse_s5_numbers,
        "parse_msgs": parse_s5_messages,
    },
]

BASE_TO_SOURCE = {s["base"]: s for s in SOURCES}


def scrape_messages_for(num_doc: dict):
    base = num_doc.get("_base", "")
    path = num_doc.get("_path", "")
    number = num_doc["number"]
    if not base or not path:
        return []
    url = base + path
    time.sleep(random.uniform(1.5, 3.5))
    src = BASE_TO_SOURCE.get(base, {})
    html = fetch_html(url, referer=src.get("list_url", base))
    if not html:
        return []
    try:
        return src["parse_msgs"](html, number)
    except Exception as e:
        log.warning(f"Message parse error [{url}]: {e}")
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
    log.info("=== TempSMS Scraper (5-server, %d numbers/server) ===", MAX_PER_SOURCE)
    db = init_firebase()

    all_numbers = []
    server_stats = {i: {"numbers": 0, "messages": 0} for i in range(1, 6)}

    for src in SOURCES:
        snum = src["server"]
        log.info(f"[Server {snum}] Fetching listing: {src['list_url']}")
        html = fetch_html(src["list_url"])
        if not html:
            log.warning(f"[Server {snum}] Failed to fetch listing page")
            continue
        try:
            nums = src["parse_nums"](html)
        except Exception as e:
            log.warning(f"[Server {snum}] Parse error: {e}")
            nums = []

        if not nums:
            log.warning(f"[Server {snum}] No numbers parsed")
            continue

        log.info(f"[Server {snum}] Found {len(nums)} numbers")
        for n in nums:
            n["server"] = snum
        all_numbers.extend(nums)
        server_stats[snum]["numbers"] = len(nums)

    if not all_numbers:
        log.error("No numbers from any server. Aborting.")
        return

    batch = db.batch()
    batch_ops = 0
    total_msgs = 0

    for num_doc in all_numbers:
        snum = num_doc["server"]
        msgs = scrape_messages_for(num_doc)
        # newest-first
        msgs_sorted = sorted(msgs, key=lambda x: x["receivedAt"], reverse=True)
        total_msgs += len(msgs_sorted)
        server_stats[snum]["messages"] += len(msgs_sorted)

        did = doc_id(num_doc["number"], snum)
        num_ref = db.collection("numbers").document(did)

        clean = {k: v for k, v in num_doc.items() if not k.startswith("_")}
        clean.update({
            "messageCount": len(msgs_sorted),
            "lastActivity": msgs_sorted[0]["receivedAt"] if msgs_sorted else None,
            "updatedAt": datetime.now(timezone.utc).isoformat(),
        })
        batch.set(num_ref, clean, merge=True)
        batch_ops += 1

        for msg in msgs_sorted:
            msg_ref = num_ref.collection("messages").document(msg["id"])
            batch.set(msg_ref, msg, merge=True)
            batch_ops += 1
            if batch_ops >= 400:
                batch.commit()
                log.info("Batch committed (400 ops limit)")
                batch = db.batch()
                batch_ops = 0

    if batch_ops:
        batch.commit()

    db.collection("meta").document("stats").set({
        "totalNumbers": len(all_numbers),
        "totalMessages": total_msgs,
        "lastScrapeAt": datetime.now(timezone.utc).isoformat(),
        "isLive": True,
        "serverStats": {str(k): v for k, v in server_stats.items()},
    })

    for snum, stats in server_stats.items():
        log.info(f"[Server {snum}] {stats['numbers']} numbers, {stats['messages']} messages")
    log.info(f"=== Done: {len(all_numbers)} numbers, {total_msgs} messages ===")


if __name__ == "__main__":
    run_scrape()
