"""
Refresh creator cost data in index.html from a Google Sheet dump.

Usage:
    python refresh_costs.py sheet_dump.json [--write]

Without --write, runs in dry-run mode and prints what would change.
With --write, updates creator_costs.csv and rebakes the CREATORS array in
index.html. Metric fields (spend, purchases, revenue, daily, jobs, etc.)
are preserved untouched. Cost-related fields (agency, content_fee,
pct_of_spend, total_cost, renewal_fee, renewal_status, launch_date) get
refreshed, and creators present in the sheet but missing from CREATORS
are added as stubs with empty metrics.

The sheet_dump.json file is the raw output of the Google Sheets MCP
get_sheet_data call on the "New Master" tab. Format:
    {"values": [[header...], [row1...], [row2...], ...]}
or the full MCP response with nested valueRanges is also accepted.

Pulling a fresh dump (via Claude + Google Sheets MCP):
    Ask Claude: "Pull the New Master tab from spreadsheet
    14JFuRlI1E7cm3ADNIMbLXPo_BAsOneoqJviNY9c91lQ and save it to
    sheet_dump.json"
"""
import argparse
import csv
import json
import re
import sys
from pathlib import Path
from collections import defaultdict

REPO = Path(__file__).parent
INDEX_HTML = REPO / "index.html"
COSTS_CSV = REPO / "creator_costs.csv"


def parse_money(s):
    if not s:
        return 0
    s = str(s).strip().replace("$", "").replace(",", "")
    if s in ("", "N/A", "n/a"):
        return 0
    try:
        return int(float(s))
    except ValueError:
        return 0


def parse_pct(s):
    if not s:
        return None
    s = str(s).strip().rstrip("%")
    if s in ("", "N/A", "n/a"):
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


def normalize_handle(h):
    return (h or "").strip().lower().replace("\n", "")


def load_sheet_dump(path):
    """Accepts either a raw {"values": [...]} dump or the full MCP response."""
    data = json.loads(Path(path).read_text())
    # Unwrap MCP shape: {"result": {"valueRanges": [{"values": [...]}]}}
    if "result" in data:
        data = data["result"]
    if "valueRanges" in data:
        data = data["valueRanges"][0]
    if "values" not in data:
        raise ValueError(f"Unrecognized dump shape in {path}")
    return data["values"]


def aggregate_sheet(rows):
    """Group sheet rows by handle. Returns {handle: {...cost fields}}."""
    # Find header row (the one with "Handle" in it)
    header_idx = None
    for i, r in enumerate(rows[:5]):
        if "Handle" in r:
            header_idx = i
            break
    if header_idx is None:
        raise ValueError("Could not find header row with 'Handle' column")

    header = rows[header_idx]
    h = {name: idx for idx, name in enumerate(header)}

    def col(row, name, default=""):
        idx = h.get(name)
        if idx is None or idx >= len(row):
            return default
        return row[idx]

    by_handle = defaultdict(lambda: {
        "names": [],
        "agencies": [],
        "fees": [],
        "pcts": [],
        "renewal_fees": [],
        "renewal_statuses": [],
        "launch_dates": [],
    })

    for row in rows[header_idx + 1:]:
        if not row or len(row) < 3:
            continue
        handle = normalize_handle(col(row, "Handle"))
        if not handle:
            continue
        agg = by_handle[handle]
        agg["names"].append(col(row, "Creator's name").strip())
        agg["agencies"].append(col(row, "Agency").strip())
        agg["fees"].append(parse_money(col(row, "Cost of content")))
        pct = parse_pct(col(row, "Percent of Ad Spend \n(if applicable)") or
                        col(row, "Percent of Ad Spend (if applicable)") or
                        col(row, "Percent of Ad Spend"))
        if pct is not None:
            agg["pcts"].append(pct)
        agg["renewal_fees"].append(parse_money(col(row, "Cost to Renew")))
        rs = col(row, "Renewal Status").strip()
        if rs:
            agg["renewal_statuses"].append(rs)
        ld = col(row, "Ad Launch Date").strip()
        if ld:
            agg["launch_dates"].append(ld)

    # Reduce to single record per handle
    result = {}
    for handle, agg in by_handle.items():
        agencies = [a for a in agg["agencies"] if a]
        names = [n for n in agg["names"] if n]
        # Use most recent (last) non-empty value for agency/name
        agency = agencies[-1] if agencies else ""
        name = names[-1] if names else handle
        content_fee = sum(agg["fees"])  # sum across rows (each row = one piece of content)
        pct = max(agg["pcts"]) if agg["pcts"] else None
        renewal_fee = max(agg["renewal_fees"]) if agg["renewal_fees"] else 0
        renewal_status = agg["renewal_statuses"][-1] if agg["renewal_statuses"] else ""
        launch_date = agg["launch_dates"][0] if agg["launch_dates"] else ""
        result[handle] = {
            "name": name,
            "agency": agency,
            "content_fee": content_fee,
            "pct_of_spend": pct,
            "renewal_fee": renewal_fee,
            "renewal_status": renewal_status,
            "launch_date": launch_date,
        }
    return result


def extract_creators(html):
    m = re.search(r"const CREATORS = (\[.*?\]);", html, re.DOTALL)
    if not m:
        raise RuntimeError("Could not find CREATORS array in index.html")
    return json.loads(m.group(1)), m.span(1)


def stub_creator(handle, sheet_data):
    return {
        "handle": handle,
        "name": sheet_data["name"],
        "spend": 0, "purchases": 0, "revenue": 0,
        "roas": 0, "cpa": 0, "cpm": 0, "ctr": 0,
        "impressions": 0, "link_clicks": 0, "atc": 0, "checkouts": 0,
        "content_fee": sheet_data["content_fee"],
        "amortized": 0, "content_efficiency": 0,
        "days_live": 0, "status": "paused",
        "agency": sheet_data["agency"],
        "launch_date": sheet_data["launch_date"],
        "daily": [],
        "renewal_fee": sheet_data["renewal_fee"],
        "renewal_status": sheet_data["renewal_status"],
        "total_cost": sheet_data["content_fee"],
        "jobs": [],
        **({"pct_of_spend": sheet_data["pct_of_spend"]}
           if sheet_data["pct_of_spend"] is not None else {}),
    }


def refresh(sheet_path, write=False):
    sheet_records = aggregate_sheet(load_sheet_dump(sheet_path))
    html = INDEX_HTML.read_text()
    creators, span = extract_creators(html)
    by_handle = {c["handle"]: c for c in creators}

    updated, added, unchanged = 0, 0, 0
    update_log, add_log = [], []

    for handle, sd in sheet_records.items():
        if handle in by_handle:
            c = by_handle[handle]
            changes = []
            if c.get("agency", "") != sd["agency"]:
                changes.append(f"agency: {c.get('agency','')!r} -> {sd['agency']!r}")
                c["agency"] = sd["agency"]
            if c.get("content_fee", 0) != sd["content_fee"]:
                changes.append(f"content_fee: {c.get('content_fee',0)} -> {sd['content_fee']}")
                c["content_fee"] = sd["content_fee"]
            if sd["pct_of_spend"] is not None and c.get("pct_of_spend") != sd["pct_of_spend"]:
                changes.append(f"pct_of_spend: {c.get('pct_of_spend')!r} -> {sd['pct_of_spend']}")
                c["pct_of_spend"] = sd["pct_of_spend"]
            if sd["renewal_fee"] and c.get("renewal_fee", 0) != sd["renewal_fee"]:
                changes.append(f"renewal_fee: {c.get('renewal_fee',0)} -> {sd['renewal_fee']}")
                c["renewal_fee"] = sd["renewal_fee"]
            # Recompute total_cost (preserving the spend-based pct contribution)
            pct = c.get("pct_of_spend")
            new_total = c["content_fee"] + ((pct or 0) / 100.0) * (c.get("spend", 0) or 0)
            new_total = round(new_total, 2) if pct else c["content_fee"]
            if abs((c.get("total_cost", 0) or 0) - new_total) > 0.5:
                changes.append(f"total_cost: {c.get('total_cost',0)} -> {new_total}")
                c["total_cost"] = new_total
            if changes:
                updated += 1
                update_log.append(f"  {handle}: " + "; ".join(changes))
            else:
                unchanged += 1
        else:
            creators.append(stub_creator(handle, sd))
            added += 1
            add_log.append(f"  {handle} ({sd['name']}) | agency={sd['agency']} fee=${sd['content_fee']} pct={sd['pct_of_spend']}")

    in_creators_not_sheet = [c["handle"] for c in creators
                             if c["handle"] not in sheet_records]

    print(f"=== Refresh summary ({'DRY RUN' if not write else 'WRITING'}) ===")
    print(f"  Updated: {updated}")
    print(f"  Added:   {added}")
    print(f"  Unchanged: {unchanged}")
    print(f"  In CREATORS but not in sheet (left as-is): {len(in_creators_not_sheet)}")
    if update_log:
        print("\n--- Updates (first 30) ---")
        for line in update_log[:30]:
            print(line)
    if add_log:
        print("\n--- Added ---")
        for line in add_log:
            print(line)

    if not write:
        print("\n(Dry run — re-run with --write to apply.)")
        return

    new_json = json.dumps(creators, separators=(", ", ": "))
    new_html = html[:span[0]] + new_json + html[span[1]:]
    INDEX_HTML.write_text(new_html)
    print(f"\n✓ Wrote {INDEX_HTML}")


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("sheet_dump", help="Path to sheet_dump.json")
    p.add_argument("--write", action="store_true",
                   help="Apply changes (otherwise dry run).")
    args = p.parse_args()
    refresh(args.sheet_dump, write=args.write)


if __name__ == "__main__":
    main()
