"""
TempSMS Scraper — runs via GitHub Actions cron job.
Scrapes receive-smss.com and writes data to Firebase Firestore.

Requirements (auto-installed in workflow):
    pip install requests beautifulsoup4 firebase-admin
"""

import os
import re
import time
import json
import random
import hashlib
import logging
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup
import firebase_admin
from firebase_admin import credentials, firestore

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

BASE_URL = "https://receive-smss.com"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64; rv:124.0) Gecko/20100101 Firefox/124.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3 Safari/605.1.15",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.6367.82 Mobile Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36 Edg/122.0.0.0",
]

COUNTRY_FLAGS = {
    "US": "🇺🇸", "GB": "🇬🇧", "CA": "🇨🇦", "AU": "🇦🇺", "DE": "🇩🇪",
    "FR": "🇫🇷", "NL": "🇳🇱", "SE": "🇸🇪", "NO": "🇳🇴", "FI": "🇫🇮",
    "DK": "🇩🇰", "CH": "🇨🇭", "AT": "🇦🇹", "BE": "🇧🇪", "PL": "🇵🇱",
    "IT": "🇮🇹", "ES": "🇪🇸", "RU": "🇷🇺", "BR": "🇧🇷", "IN": "🇮🇳",
    "CN": "🇨🇳", "JP": "🇯🇵", "KR": "🇰🇷", "SG": "🇸🇬", "PH": "🇵🇭",
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
    "+63": ("PH", "Philippines"),
}

OTP_PATTERNS = [
    re.compile(r"\b(\d{6})\b"),
    re.compile(r"\b(\d{4})\b"),
    re.compile(r"\b(\d{3}[-\s]\d{3})\b"),
    re.compile(r"(?:code|OTP|verification|pin|passcode)[:\s]+([A-Z0-9]{4,8})", re.I),
]


def detect_country(number: str) -> tuple[str, str, str]:
    """Returns (countryCode, country, countryFlag)."""
    cleaned = number.replace(" ", "")
    for prefix in sorted(PHONE_PREFIXES, key=len, reverse=True):
        if cleaned.startswith(prefix):
            code, name = PHONE_PREFIXES[prefix]
            return code, name, COUNTRY_FLAGS.get(code, "🌐")
    return "US", "United States", "🇺🇸"


def extract_otp(body: str) -> str | None:
    for pattern in OTP_PATTERNS:
        match = pattern.search(body)
        if match:
            candidate = re.sub(r"[-\s]", "", match.group(1))
            if 4 <= len(candidate) <= 8:
                return match.group(1)
    return None


def make_headers() -> dict:
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Referer": BASE_URL + "/",
    }


def fetch(url: str, timeout: int = 15) -> str | None:
    try:
        resp = requests.get(url, headers=make_headers(), timeout=timeout)
        resp.raise_for_status()
        return resp.text
    except requests.RequestException as e:
        log.warning(f"Fetch failed for {url}: {e}")
        return None


def number_to_doc_id(number: str) -> str:
    """Create a safe Firestore document ID from a phone number."""
    return re.sub(r"[^\w]", "_", number)


def scrape_numbers() -> list[dict]:
    log.info("Scraping phone numbers list...")
    html = fetch(BASE_URL + "/")
    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    numbers = []
    seen = set()

    for a in soup.find_all("a", href=True):
        href = a["href"]
        match = re.search(r"/sms/(\+?[\d]+)/", href)
        if not match:
            continue
        raw = match.group(1)
        if raw in seen:
            continue
        seen.add(raw)

        code, name, flag = detect_country(raw)
        numbers.append({
            "number": raw,
            "countryCode": code,
            "country": name,
            "countryFlag": flag,
            "isActive": True,
            "updatedAt": datetime.now(timezone.utc).isoformat(),
        })

    log.info(f"Found {len(numbers)} phone numbers")
    return numbers


def scrape_messages(number: str) -> list[dict]:
    url = f"{BASE_URL}/sms/{number}/"
    log.info(f"Scraping messages for {number}")
    html = fetch(url)
    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    messages = []

    for row in soup.select("table.table tbody tr, .sms-list tr"):
        cells = row.find_all("td")
        if len(cells) < 2:
            continue

        sender = cells[0].get_text(strip=True) or "Unknown"
        body = cells[1].get_text(strip=True)
        time_text = cells[2].get_text(strip=True) if len(cells) > 2 else ""

        if not body:
            continue

        # Parse timestamp
        received_at = datetime.now(timezone.utc).isoformat()
        try:
            parsed = datetime.fromisoformat(time_text)
            received_at = parsed.replace(tzinfo=timezone.utc).isoformat()
        except Exception:
            pass

        msg_id = hashlib.md5(f"{number}-{sender}-{body[:40]}-{time_text}".encode()).hexdigest()
        otp = extract_otp(body)

        messages.append({
            "id": msg_id,
            "from": sender,
            "body": body,
            "receivedAt": received_at,
            "number": number,
            "extractedOtp": otp,
        })

    log.info(f"Found {len(messages)} messages for {number}")
    return messages


def init_firebase() -> firestore.Client:
    """Initialize Firebase from environment variable (JSON string)."""
    cred_json = os.environ.get("FIREBASE_SERVICE_ACCOUNT")
    if not cred_json:
        raise RuntimeError("FIREBASE_SERVICE_ACCOUNT environment variable not set")

    cred_dict = json.loads(cred_json)
    cred = credentials.Certificate(cred_dict)
    firebase_admin.initialize_app(cred)
    return firestore.client()


def run_scrape():
    log.info("=== TempSMS Scraper starting ===")
    db = init_firebase()

    numbers = scrape_numbers()
    if not numbers:
        log.warning("No numbers found, aborting")
        return

    total_messages = 0
    batch = db.batch()
    batch_count = 0

    for num_doc in numbers:
        doc_id = number_to_doc_id(num_doc["number"])
        num_ref = db.collection("numbers").document(doc_id)

        # Scrape messages for each number (with stealth delay)
        time.sleep(random.uniform(5, 10))
        msgs = scrape_messages(num_doc["number"])
        total_messages += len(msgs)

        # Write messages to subcollection
        for msg in msgs:
            msg_ref = num_ref.collection("messages").document(msg["id"])
            batch.set(msg_ref, msg, merge=True)
            batch_count += 1

            if batch_count >= 400:
                batch.commit()
                log.info(f"Committed batch of {batch_count} writes")
                batch = db.batch()
                batch_count = 0

        # Update number document with message count
        num_doc_with_count = {
            **num_doc,
            "messageCount": len(msgs),
            "lastActivity": msgs[0]["receivedAt"] if msgs else None,
        }
        batch.set(num_ref, num_doc_with_count, merge=True)
        batch_count += 1

    # Commit remaining
    if batch_count:
        batch.commit()
        log.info(f"Committed final batch of {batch_count} writes")

    # Update stats
    db.collection("meta").document("stats").set({
        "totalNumbers": len(numbers),
        "totalMessages": total_messages,
        "lastScrapeAt": datetime.now(timezone.utc).isoformat(),
        "isLive": True,
    })

    log.info(f"=== Scrape complete — {len(numbers)} numbers, {total_messages} messages ===")


if __name__ == "__main__":
    run_scrape()
