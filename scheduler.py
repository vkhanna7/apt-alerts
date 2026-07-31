"""
Scheduler — runs the apartment alert pipeline at 7:30 AM and 5:30 PM daily.
Run this script once; it loops forever (deploy on any always-on server or Render free tier).
"""

import time
import logging
from datetime import datetime
import scraper

log = logging.getLogger(__name__)

ALERT_TIMES = [
    ("07:30", "morning"),
    ("17:30", "evening"),
]

def should_run(target_hhmm: str, last_ran: dict) -> bool:
    now = datetime.now()
    today_key = f"{now.date()}_{target_hhmm}"
    if last_ran.get(target_hhmm) == today_key:
        return False  # already ran today
    hh, mm = map(int, target_hhmm.split(":"))
    return now.hour == hh and now.minute == mm

def main():
    log.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    last_ran = {}
    log.info("Scheduler started. Alert times: 7:30 AM and 5:30 PM daily.")
    while True:
        now = datetime.now()
        for target_hhmm, slot in ALERT_TIMES:
            if should_run(target_hhmm, last_ran):
                log.info(f"Triggering {slot} alert ({target_hhmm})")
                try:
                    scraper.run(slot)
                    last_ran[target_hhmm] = f"{now.date()}_{target_hhmm}"
                except Exception as e:
                    log.error(f"Alert run failed: {e}")
        time.sleep(30)  # check every 30 seconds

if __name__ == "__main__":
    main()
