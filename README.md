# NVIDIA Shuttle Apartment Alerts

Scrapes Craigslist and Apartments.com for **2-bed SF apartments under $6,000/mo** within
**0.5 miles** of any NVIDIA shuttle stop (Routes 1 and 2). Sends de-duplicated email
digests to `vasudhak@nvidia.com` at **7:30 AM** and **5:30 PM** daily.

---

## Shuttle stops covered

| Route | Stop |
|-------|------|
| Route 1 | Stanyan & Waller St |
| Route 1 | Divisadero & Haight St |
| Route 1 | 18th & Castro St |
| Route 1 | Cesar Chavez & Folsom |
| Route 2 | Bush & Franklin St |
| Route 2 | 8th & Market St |
| Route 2 | 5th & Brannan St |
| Route 2 | 17th & Mississippi St |

---

## Quick start (local)

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Set credentials (Gmail recommended — use an App Password)
export SENDER_EMAIL="your-gmail@gmail.com"
export SENDER_PASSWORD="xxxx xxxx xxxx xxxx"   # 16-char Gmail App Password

# 3. Test a single run (saves email HTML to last_alert.html if no creds)
python scraper.py morning

# 4. Start the scheduler (runs indefinitely)
python scheduler.py
```

### Getting a Gmail App Password
1. Enable 2FA on your Google account
2. Go to **Google Account → Security → App Passwords**
3. Create a new app password — use the 16-character code as `SENDER_PASSWORD`

---

## Deploy to Render (free, always-on)

1. Push this folder to a GitHub repo
2. Go to [render.com](https://render.com) → **New → Blueprint**
3. Connect your repo — Render reads `render.yaml` automatically
4. In the Render dashboard, set the two environment variables:
   - `SENDER_EMAIL`
   - `SENDER_PASSWORD`
5. Deploy — the worker starts and runs 24/7

---

## How de-duplication works

Every listing URL is hashed (MD5) and stored in `seen_listings.json`.
Each alert run only emails listings whose hash is **not already in that file**.
This means:
- Morning alert: all new listings since yesterday evening
- Evening alert: all new listings since this morning
- No apartment ever appears twice across any alert

To reset (see all listings again): `rm seen_listings.json`

---

## File structure

```
apt_alerts/
├── scraper.py          # scraping, geocoding, proximity filter, email
├── scheduler.py        # daily loop: 7:30 AM + 5:30 PM triggers
├── requirements.txt
├── render.yaml         # one-click Render deployment
├── seen_listings.json  # auto-created, tracks sent listings
└── alerts.log          # auto-created, run history
```

---

## Adding more sources

To add Zillow or Realtor.com, add a new `scrape_X()` function in `scraper.py`
that returns a list of dicts with keys: `title`, `price`, `url`, `source`,
`address`, `meta`, `lat`, `lng`. Then call it inside `run()`.
