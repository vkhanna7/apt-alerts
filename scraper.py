"""
SF Apartment Alert Pipeline
Scrapes Craigslist + Apartments.com for 2-bed SF apartments near NVIDIA shuttle stops.
Sends de-duplicated alerts to vasudhak@nvidia.com at 7:30 AM and 5:30 PM.
"""

import os
import json
import math
import hashlib
import smtplib
import logging
import time
import random
from datetime import datetime, date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional
import requests
from bs4 import BeautifulSoup

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("alerts.log"), logging.StreamHandler()],
)
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
RECIPIENT_EMAIL  = "vasudhak@nvidia.com"
SENDER_EMAIL     = os.environ.get("SENDER_EMAIL", "")     # your Gmail or SMTP address
SENDER_PASSWORD  = os.environ.get("SENDER_PASSWORD", "")  # app password
SMTP_HOST        = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT        = int(os.environ.get("SMTP_PORT", "587"))
SEEN_DB          = Path("seen_listings.json")              # de-dupe store

MAX_RENT         = 6000
MIN_BEDS         = 2
MAX_WALK_MILES   = 0.5   # radius around each shuttle stop
AVAILABLE_FROM   = date(2025, 9, 1)   # only apartments available on or after this date

# ── Shuttle stops (lat, lng, name) ────────────────────────────────────────────
SHUTTLE_STOPS = [
    # Route 1
    (37.7682, -122.4534, "Stanyan & Waller St",         "Route 1"),
    (37.7713, -122.4371, "Divisadero & Haight St",      "Route 1"),
    (37.7609, -122.4350, "18th & Castro St",            "Route 1"),
    (37.7483, -122.4137, "Cesar Chavez & Folsom",       "Route 1"),
    # Route 2
    (37.7883, -122.4236, "Bush & Franklin St",          "Route 2"),
    (37.7787, -122.4148, "8th & Market St",             "Route 2"),
    (37.7761, -122.3984, "5th & Brannan St",            "Route 2"),
    (37.7653, -122.3949, "17th & Mississippi St",       "Route 2"),
]

# ── Haversine distance ─────────────────────────────────────────────────────────
def haversine_miles(lat1, lng1, lat2, lng2) -> float:
    R = 3958.8  # Earth radius in miles
    lat1, lng1, lat2, lng2 = map(math.radians, [lat1, lng1, lat2, lng2])
    dlat = lat2 - lat1
    dlng = lng2 - lng1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2
    return R * 2 * math.asin(math.sqrt(a))


def nearest_stops(lat: float, lng: float) -> list[dict]:
    """Return all shuttle stops within MAX_WALK_MILES, sorted by distance."""
    results = []
    for slat, slng, name, route in SHUTTLE_STOPS:
        d = haversine_miles(lat, lng, slat, slng)
        if d <= MAX_WALK_MILES:
            results.append({"name": name, "route": route, "miles": round(d, 2)})
    return sorted(results, key=lambda x: x["miles"])


# ── Availability date filter ──────────────────────────────────────────────────
import re

DATE_PATTERNS = [
    r'available\s+(\w+\.?\s+\d{1,2})',           # "available Sept 1"
    r'avail\.?\s+(\w+\.?\s+\d{1,2})',            # "avail. Sept 1"
    r'move.in\s+(\w+\.?\s+\d{1,2})',             # "move-in Sept 1"
    r'(\d{1,2})[\/\-](\d{1,2})(?:[\/\-]\d{2,4})?',  # "9/1" or "9-1"
]

MONTH_MAP = {
    'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'may': 5, 'jun': 6,
    'jul': 7, 'aug': 8, 'sep': 9, 'oct': 10, 'nov': 11, 'dec': 12
}

def parse_avail_date(text: str) -> Optional[date]:
    """Extract availability date from listing text. Returns None if not found."""
    text = text.lower()
    for pattern in DATE_PATTERNS[:3]:
        m = re.search(pattern, text)
        if m:
            try:
                parts = m.group(1).strip().split()
                month_str = parts[0].rstrip('.').lower()[:3]
                day = int(parts[1]) if len(parts) > 1 else 1
                month = MONTH_MAP.get(month_str)
                if month:
                    year = 2025 if month >= 8 else 2026
                    return date(year, month, day)
            except Exception:
                pass
    # numeric pattern like 9/1
    m = re.search(DATE_PATTERNS[3], text)
    if m:
        try:
            month, day = int(m.group(1)), int(m.group(2))
            if 1 <= month <= 12 and 1 <= day <= 31:
                year = 2025 if month >= 8 else 2026
                return date(year, month, day)
        except Exception:
            pass
    return None

def is_available_from_sept(listing: dict) -> bool:
    """
    Returns True if:
    - No availability date found (keep it — we can't rule it out)
    - Availability date is on or after AVAILABLE_FROM
    """
    text = " ".join([
        listing.get("title", ""),
        listing.get("meta", ""),
        listing.get("description", ""),
    ])
    avail = parse_avail_date(text)
    if avail is None:
        return True   # no date mentioned — include it
    if avail >= AVAILABLE_FROM:
        listing["avail_date"] = avail.strftime("%b %d")
        return True
    log.debug(f"Filtered out (available {avail}): {listing['title']}")
    return False


# ── De-duplication ────────────────────────────────────────────────────────────
def load_seen() -> set:
    if SEEN_DB.exists():
        return set(json.loads(SEEN_DB.read_text()))
    return set()


def save_seen(seen: set):
    SEEN_DB.write_text(json.dumps(list(seen)))


def listing_id(url: str) -> str:
    return hashlib.md5(url.encode()).hexdigest()


# ── Geocoding (free Nominatim) ────────────────────────────────────────────────
def geocode(address: str) -> Optional[tuple[float, float]]:
    try:
        resp = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": address + ", San Francisco, CA", "format": "json", "limit": 1},
            headers={"User-Agent": "NvidiaShuttleAlerts/1.0 vasudhak@nvidia.com"},
            timeout=10,
        )
        data = resp.json()
        if data:
            return float(data[0]["lat"]), float(data[0]["lon"])
    except Exception as e:
        log.warning(f"Geocode failed for '{address}': {e}")
    return None


# ── Craigslist scraper ────────────────────────────────────────────────────────
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}


def scrape_craigslist() -> list[dict]:
    listings = []
    url = (
        "https://sfbay.craigslist.org/search/sfc/apa"
        f"?min_bedrooms={MIN_BEDS}&max_bedrooms={MIN_BEDS}"
        f"&max_price={MAX_RENT}"
        "&availabilityMode=0&sale_date=all+dates"
        "&search_distance=5&postal=94103"  # centre of SF
    )
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(resp.text, "html.parser")
        items = soup.select("li.cl-search-result")
        log.info(f"Craigslist: found {len(items)} raw results")
        for item in items[:40]:
            try:
                title_el = item.select_one(".titlestring") or item.select_one("a.posting-title span")
                price_el  = item.select_one(".priceinfo")
                link_el   = item.select_one("a.posting-title") or item.select_one("a[href*='/apa/']")
                meta_el   = item.select_one(".housing")

                if not (title_el and price_el and link_el):
                    continue

                price_text = price_el.get_text(strip=True).replace("$", "").replace(",", "")
                price = int("".join(filter(str.isdigit, price_text)) or 0)
                if price == 0 or price > MAX_RENT:
                    continue

                link = link_el.get("href", "")
                if not link.startswith("http"):
                    link = "https://sfbay.craigslist.org" + link

                title = title_el.get_text(strip=True)
                meta  = meta_el.get_text(strip=True) if meta_el else ""

                listings.append({
                    "title":   title,
                    "price":   price,
                    "url":     link,
                    "source":  "Craigslist",
                    "address": title,  # refined below via detail fetch
                    "meta":    meta,
                    "lat":     None,
                    "lng":     None,
                })
            except Exception as e:
                log.debug(f"Craigslist item parse error: {e}")
    except Exception as e:
        log.error(f"Craigslist scrape failed: {e}")
    return listings


def enrich_craigslist(listing: dict) -> dict:
    """Fetch the detail page to get a real address and coordinates."""
    try:
        time.sleep(random.uniform(0.8, 1.6))  # be polite
        resp = requests.get(listing["url"], headers=HEADERS, timeout=15)
        soup = BeautifulSoup(resp.text, "html.parser")

        # Craigslist embeds lat/lng in the map link
        map_el = soup.select_one("#map")
        if map_el:
            lat = float(map_el.get("data-latitude", 0))
            lng = float(map_el.get("data-longitude", 0))
            if lat and lng:
                listing["lat"] = lat
                listing["lng"]  = lng

        # Try to pull an address string
        addr_el = soup.select_one(".mapaddress")
        if addr_el:
            listing["address"] = addr_el.get_text(strip=True)

        # Beds/baths from attrgroup
        for span in soup.select(".attrgroup span"):
            t = span.get_text(strip=True).lower()
            if "br" in t or "bed" in t:
                listing["meta"] = t + " " + listing.get("meta", "")
    except Exception as e:
        log.debug(f"Craigslist enrichment failed: {e}")
    return listing


# ── Apartments.com scraper ────────────────────────────────────────────────────
def scrape_apartments_com() -> list[dict]:
    """
    Apartments.com is JS-rendered; we use their JSON-LD data or
    their search API endpoint. Falls back gracefully if blocked.
    """
    listings = []
    # They expose a JSON feed via this pattern:
    url = "https://www.apartments.com/san-francisco-ca/2-bedrooms/?max-price=6000"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(resp.text, "html.parser")

        # Parse JSON-LD if present
        for tag in soup.select('script[type="application/ld+json"]'):
            try:
                data = json.loads(tag.string or "{}")
                items = data if isinstance(data, list) else [data]
                for item in items:
                    if item.get("@type") not in ("Apartment", "ApartmentComplex"):
                        continue
                    name    = item.get("name", "")
                    addr    = item.get("address", {})
                    address = f"{addr.get('streetAddress','')} {addr.get('addressLocality','')}"
                    url_l   = item.get("url", "")
                    geo     = item.get("geo", {})
                    lat     = float(geo.get("latitude", 0)) or None
                    lng     = float(geo.get("longitude", 0)) or None
                    offers  = item.get("offers", {})
                    price   = 0
                    if isinstance(offers, dict):
                        price = int(float(offers.get("price", 0) or 0))
                    if price > MAX_RENT or price == 0:
                        continue
                    listings.append({
                        "title":   name,
                        "price":   price,
                        "url":     url_l or url,
                        "source":  "Apartments.com",
                        "address": address.strip(),
                        "meta":    "2 bed",
                        "lat":     lat,
                        "lng":     lng,
                    })
            except Exception:
                pass

        # Fallback: parse listing cards
        if not listings:
            cards = soup.select("article.placard, li.mortar-wrapper")
            log.info(f"Apartments.com: {len(cards)} card elements found")
            for card in cards[:30]:
                try:
                    name_el  = card.select_one(".js-placardTitle, .property-title")
                    price_el = card.select_one(".price-range, .property-rents")
                    link_el  = card.select_one("a.property-link, a[data-url]")
                    addr_el  = card.select_one(".property-address")
                    if not (name_el and price_el and link_el):
                        continue
                    raw = price_el.get_text(strip=True)
                    digits = "".join(filter(str.isdigit, raw.split("–")[0]))
                    price  = int(digits) if digits else 0
                    if price == 0 or price > MAX_RENT:
                        continue
                    link = link_el.get("href") or link_el.get("data-url", "")
                    listings.append({
                        "title":   name_el.get_text(strip=True),
                        "price":   price,
                        "url":     link,
                        "source":  "Apartments.com",
                        "address": addr_el.get_text(strip=True) if addr_el else name_el.get_text(strip=True),
                        "meta":    "2 bed",
                        "lat":     None,
                        "lng":     None,
                    })
                except Exception as e:
                    log.debug(f"Apt card parse error: {e}")
    except Exception as e:
        log.error(f"Apartments.com scrape failed: {e}")
    return listings


# ── Filter by shuttle proximity ───────────────────────────────────────────────
def filter_by_proximity(listings: list[dict]) -> list[dict]:
    results = []
    for lst in listings:
        # Geocode if we don't have coords yet
        if not lst["lat"] and lst["address"]:
            coords = geocode(lst["address"])
            if coords:
                lst["lat"], lst["lng"] = coords
            time.sleep(1.1)  # Nominatim rate limit: 1 req/sec

        if not lst["lat"]:
            log.debug(f"Skipping (no coords): {lst['title']}")
            continue

        stops = nearest_stops(lst["lat"], lst["lng"])
        if stops:
            lst["nearby_stops"] = stops
            results.append(lst)
            log.info(f"MATCH: {lst['title']} — ${lst['price']} — nearest: {stops[0]['name']} ({stops[0]['miles']} mi)")
        else:
            log.debug(f"Too far from any stop: {lst['title']}")
    return results


# ── Email builder ─────────────────────────────────────────────────────────────
def build_email_html(listings: list[dict], send_time: str) -> str:
    now   = datetime.now().strftime("%A, %B %d · %I:%M %p")
    count = len(listings)

    rows = ""
    for lst in listings:
        stops_html = ""
        for s in lst.get("nearby_stops", [])[:3]:
            color = "#085041" if s["route"] == "Route 1" else "#0C447C"
            bg    = "#E1F5EE" if s["route"] == "Route 1" else "#E6F1FB"
            stops_html += (
                f'<span style="background:{bg};color:{color};font-size:11px;'
                f'padding:2px 8px;border-radius:20px;margin-right:4px;white-space:nowrap">'
                f'{s["name"]} — {s["miles"]} mi</span>'
            )

        parking_note = "Parking: check listing"
        if "park" in lst.get("meta", "").lower():
            parking_note = "Parking: mentioned in listing"

        rows += f"""
        <div style="background:#fff;border:1px solid #e8e8e4;border-radius:10px;
                    padding:16px 20px;margin-bottom:12px">
          <div style="display:flex;justify-content:space-between;align-items:flex-start;
                      margin-bottom:6px">
            <a href="{lst['url']}" style="font-size:15px;font-weight:500;color:#1a1a1a;
               text-decoration:none">{lst['title']}</a>
            <span style="font-size:16px;font-weight:500;color:#185FA5;white-space:nowrap;
                         margin-left:12px">${lst['price']:,}/mo</span>
          </div>
          <div style="font-size:12px;color:#888;margin-bottom:8px">
            {lst.get('address','') or 'San Francisco, CA'} &nbsp;·&nbsp;
            {lst.get('meta','2 bed')} &nbsp;·&nbsp; {parking_note} &nbsp;·&nbsp;
            {'Available ' + lst['avail_date'] + ' &nbsp;·&nbsp; ' if lst.get('avail_date') else ''}
            <span style="color:#5F5E5A">{lst['source']}</span>
          </div>
          <div style="margin-bottom:10px">{stops_html}</div>
          <a href="{lst['url']}"
             style="display:inline-block;font-size:12px;font-weight:500;color:#185FA5;
                    border:1px solid #B5D4F4;border-radius:6px;padding:4px 12px;
                    text-decoration:none">Apply now →</a>
        </div>"""

    html = f"""<!DOCTYPE html>
<html><body style="margin:0;padding:0;background:#f4f4f0;font-family:-apple-system,sans-serif">
<div style="max-width:600px;margin:24px auto;padding:0 16px">

  <div style="margin-bottom:20px">
    <p style="font-size:11px;color:#888;letter-spacing:.06em;text-transform:uppercase;margin:0 0 4px">
      NVIDIA Shuttle Apartment Alerts</p>
    <h1 style="font-size:22px;font-weight:500;color:#1a1a1a;margin:0">
      {count} new listing{'s' if count != 1 else ''} — {send_time} alert</h1>
    <p style="font-size:13px;color:#888;margin:4px 0 0">{now} · 2 bed · under $6,000/mo · ≤0.5 mi from shuttle</p>
  </div>

  {rows if rows else '<p style="color:#888;font-size:14px">No new listings matched your criteria this run. Check back next alert.</p>'}

  <div style="border-top:1px solid #e8e8e4;margin-top:20px;padding-top:16px;
              font-size:11px;color:#aaa;text-align:center">
    Stops: Stanyan/Waller · Divisadero/Haight · 18th/Castro · Cesar Chavez/Folsom
    (Route 1) &nbsp;|&nbsp;
    Bush/Franklin · 8th/Market · 5th/Brannan · 17th/Mississippi (Route 2)<br>
    <a href="mailto:{RECIPIENT_EMAIL}" style="color:#aaa">Unsubscribe</a>
  </div>
</div>
</body></html>"""
    return html


# ── Email sender ──────────────────────────────────────────────────────────────
def send_email(html: str, count: int, send_time: str):
    if not SENDER_EMAIL or not SENDER_PASSWORD:
        log.warning("SENDER_EMAIL / SENDER_PASSWORD not set — skipping email send.")
        # Save to file for testing
        Path("last_alert.html").write_text(html)
        log.info("Email HTML saved to last_alert.html for preview.")
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"[Apt Alert] {count} new listing{'s' if count != 1 else ''} — {send_time} · SF 2bd under $6k"
    msg["From"]    = SENDER_EMAIL
    msg["To"]      = RECIPIENT_EMAIL
    msg.attach(MIMEText(html, "html"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SENDER_EMAIL, SENDER_PASSWORD)
            server.sendmail(SENDER_EMAIL, RECIPIENT_EMAIL, msg.as_string())
        log.info(f"Email sent to {RECIPIENT_EMAIL} ({count} listings)")
    except Exception as e:
        log.error(f"Email send failed: {e}")


# ── Main pipeline ─────────────────────────────────────────────────────────────
def run(send_time: str = "morning"):
    log.info(f"=== Starting alert run: {send_time} ===")

    # 1. Scrape
    raw = []
    raw += scrape_craigslist()
    time.sleep(2)
    raw += scrape_apartments_com()
    log.info(f"Total raw listings: {len(raw)}")

    # 2. Filter by availability date (Sept 1 or later, or no date specified)
    raw = [l for l in raw if is_available_from_sept(l)]
    log.info(f"After availability filter: {len(raw)}")

    # 3. Filter by proximity to shuttle stops
    matched = filter_by_proximity(raw)
    log.info(f"Matched listings near shuttle stops: {len(matched)}")

    # 3. De-duplicate against seen listings
    seen    = load_seen()
    new     = [l for l in matched if listing_id(l["url"]) not in seen]
    log.info(f"New (unseen) listings: {len(new)}")

    # 4. Mark as seen
    for l in new:
        seen.add(listing_id(l["url"]))
    save_seen(seen)

    # 5. Build and send email
    label = "Morning" if send_time == "morning" else "Evening"
    html  = build_email_html(new, label)
    send_email(html, len(new), label)

    log.info(f"=== Run complete. {len(new)} new listings sent. ===")
    return new


if __name__ == "__main__":
    import sys
    slot = sys.argv[1] if len(sys.argv) > 1 else "morning"
    run(slot)
