"""
hr_sync.py — sync the canonical people roster from the HR Employee Database
(Notion) into Postgres, so the contribution app always knows the current team
(names, teams, employment type, status) without anyone editing code.

WHAT IT DOES (idempotent; safe to run daily):
  1. Ensures two tables exist:
       employee      — one row per HR person (canonical), keyed by Notion page id.
       person_alias  — every identity a person shows up as in a source system
                       (ShipHero name/id, Shopify name/id, engraving logger, etc.)
                       -> employee. This is how raw scanner strings like
                       "787802395" or a Shopify nickname resolve to one true person.
  2. Reads the Notion Employee Database via the Notion API.
  3. Upserts each person into `employee`, deriving dash_type for the dashboard's
     Type column:  Ops Intern team -> Intern ; Seasonal Fulfillment (team or
     position) -> Seasonal ; Warehouse Operations team -> FT ; else '' (untagged).
  4. Seeds an identity alias (alias = Employee Name -> that employee) so exact
     name matches resolve immediately. Existing / manually-added aliases are
     never clobbered (ON CONFLICT DO NOTHING).

read_api.py reads person_alias JOIN employee to fill the FT/Intern/Seasonal tag
live (cached 5 min), falling back to its built-in list if these tables are empty.

ENV:
  DATABASE_URL   — Postgres (same as every other cron; via db.connect()).
  NOTION_TOKEN   — a Notion internal-integration token (the Employee Database
                   must be shared with that integration). REQUIRED.
  HR_DB_ID       — Notion database id (defaults to the Employee Database).

Runs on Render as part of the nightly job. stdlib only (urllib) — no new deps.
"""
import os
import sys
import json
import urllib.request
import urllib.error

from db import connect

NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "").strip()
HR_DB_ID = os.environ.get("HR_DB_ID", "2295560a73f58047b56ef92a6ba0bf10").strip()
NOTION_VERSION = "2022-06-28"

# HR Status values that mean "not a current team member" -> is_active = False.
INACTIVE_STATUS = {"Archive", "Never Joined"}

DDL = """
CREATE TABLE IF NOT EXISTS employee (
    id                text PRIMARY KEY,          -- Notion page id (stable)
    name              text NOT NULL,             -- HR "Employee Name" (canonical)
    email             text,
    position          text,
    teams             jsonb,                     -- array of HR Team names
    employment_status text,                      -- HR Employment Status (often null)
    hr_status         text,                      -- HR Status (Active/Onboarding/...)
    location          text,
    manager           text,
    dash_type         text NOT NULL DEFAULT '',  -- 'FT' | 'Intern' | 'Seasonal' | ''
    is_active         boolean NOT NULL DEFAULT true,
    hire_date         date,
    raw               jsonb,                     -- full HR snapshot, for audit
    synced_at         timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS person_alias (
    alias        text PRIMARY KEY,               -- raw person string as seen in a source
    employee_id  text,                           -- -> employee.id (NULL = seen but unmapped)
    source       text,                           -- 'hr' | 'shiphero' | 'shopify' | 'logger' | 'manual'
    note         text,
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS person_alias_emp_idx ON person_alias (employee_id);
"""

UPSERT = """
INSERT INTO employee (id, name, email, position, teams, employment_status,
                      hr_status, location, manager, dash_type, is_active,
                      hire_date, raw, synced_at)
VALUES (%(id)s, %(name)s, %(email)s, %(position)s, %(teams)s, %(employment_status)s,
        %(hr_status)s, %(location)s, %(manager)s, %(dash_type)s, %(is_active)s,
        %(hire_date)s, %(raw)s, now())
ON CONFLICT (id) DO UPDATE SET
    name=EXCLUDED.name, email=EXCLUDED.email, position=EXCLUDED.position,
    teams=EXCLUDED.teams, employment_status=EXCLUDED.employment_status,
    hr_status=EXCLUDED.hr_status, location=EXCLUDED.location, manager=EXCLUDED.manager,
    dash_type=EXCLUDED.dash_type, is_active=EXCLUDED.is_active,
    hire_date=EXCLUDED.hire_date, raw=EXCLUDED.raw, synced_at=now();
"""

SEED_ALIAS = """
INSERT INTO person_alias (alias, employee_id, source, note)
VALUES (%s, %s, 'hr', 'auto: HR Employee Name')
ON CONFLICT (alias) DO NOTHING;
"""


# ---------- Notion property extraction ----------
def _title(prop):
    if not prop:
        return ""
    return "".join(t.get("plain_text", "") for t in prop.get("title", [])).strip()


def _rich(prop):
    if not prop:
        return ""
    return "".join(t.get("plain_text", "") for t in prop.get("rich_text", [])).strip()


def _select(prop):
    if not prop:
        return None
    s = prop.get("select")
    return s.get("name") if s else None


def _status(prop):
    if not prop:
        return None
    s = prop.get("status")
    return s.get("name") if s else None


def _multi(prop):
    if not prop:
        return []
    return [o.get("name") for o in prop.get("multi_select", []) if o.get("name")]


def _email(prop):
    return (prop or {}).get("email")


def _date_start(prop):
    if not prop:
        return None
    d = prop.get("date")
    if not d or not d.get("start"):
        return None
    return d["start"][:10]  # YYYY-MM-DD


def derive_type(teams, position):
    """FT / Intern / Seasonal / '' for the dashboard Type column."""
    tset = set(teams or [])
    pos = (position or "").lower()
    if "Ops Intern" in tset:
        return "Intern"
    if "Seasonal Fulfillment Associate" in tset or "seasonal" in pos:
        return "Seasonal"
    if "Warehouse Operations" in tset:
        return "FT"
    return ""


def is_active(hr_status, employment_status):
    if hr_status in INACTIVE_STATUS:
        return False
    if (employment_status or "") == "Former Employee":
        return False
    return True


def parse_page(page):
    props = page.get("properties", {})
    name = _title(props.get("Employee Name"))
    if not name:
        return None
    teams = _multi(props.get("Team"))
    position = _rich(props.get("Position"))
    hr_status = _status(props.get("Status"))
    emp_status = _select(props.get("Employment Status"))
    return {
        "id": page["id"],
        "name": name,
        "email": _email(props.get("Email")),
        "position": position or None,
        "teams": json.dumps(teams),
        "employment_status": emp_status,
        "hr_status": hr_status,
        "location": _select(props.get("Location")),
        "manager": _select(props.get("Manager")),
        "dash_type": derive_type(teams, position),
        "is_active": is_active(hr_status, emp_status),
        "hire_date": _date_start(props.get("Hire Date")),
        "raw": json.dumps({
            "teams": teams, "position": position, "hr_status": hr_status,
            "employment_status": emp_status,
        }),
    }


# ---------- Notion API ----------
def notion_query_all(db_id, token):
    url = "https://api.notion.com/v1/databases/%s/query" % db_id
    headers = {
        "Authorization": "Bearer %s" % token,
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }
    pages = []
    cursor = None
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        req = urllib.request.Request(
            url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")
            raise SystemExit("Notion API HTTP %s: %s" % (e.code, detail[:500]))
        pages.extend(data.get("results", []))
        if data.get("has_more") and data.get("next_cursor"):
            cursor = data["next_cursor"]
        else:
            break
    return pages


def main():
    if not NOTION_TOKEN:
        raise SystemExit("hr_sync: NOTION_TOKEN not set — skipping HR roster sync.")
    pages = notion_query_all(HR_DB_ID, NOTION_TOKEN)
    people = [p for p in (parse_page(pg) for pg in pages) if p]
    print("hr_sync: fetched %d HR pages, %d with names" % (len(pages), len(people)))

    with connect() as c:
        cur = c.cursor()
        cur.execute(DDL)
        for p in people:
            cur.execute(UPSERT, p)
            cur.execute(SEED_ALIAS, (p["name"], p["id"]))
        c.commit()

        cur.execute("SELECT count(*) FROM employee")
        emp_n = cur.fetchone()[0]
        cur.execute("SELECT dash_type, count(*) FROM employee "
                    "WHERE is_active AND dash_type<>'' GROUP BY 1 ORDER BY 1")
        by_type = cur.fetchall()
        cur.execute("SELECT count(*) FROM person_alias")
        alias_n = cur.fetchone()[0]
    print("hr_sync: employee rows=%d, aliases=%d, active-tagged=%s"
          % (emp_n, alias_n, dict(by_type)))


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        # Don't fail the whole nightly job if HR sync is misconfigured; log & move on.
        print(str(e), file=sys.stderr)
