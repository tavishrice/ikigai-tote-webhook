"""
Nightly rollup. Rebuilds contribution_daily for the last N ET days (default 3,
so late-arriving Tote Completes and corrections are always reflected).

Run as a Render Cron Job:  python rollup_cron.py
"""
import os, datetime as dt
from db import connect

DAYS = int(os.environ.get("ROLLUP_DAYS", "3"))

def main():
    today_et = (dt.datetime.now(dt.timezone.utc)
                .astimezone(dt.timezone(dt.timedelta(hours=-4))).date())
    with connect() as c, c.cursor() as cur:
        for i in range(DAYS):
            d = today_et - dt.timedelta(days=i)
            cur.execute("SELECT refresh_contribution_day(%s)", (d,))
            print(f"[rollup] refreshed {d}", flush=True)
        c.commit()

if __name__ == "__main__":
    main()
