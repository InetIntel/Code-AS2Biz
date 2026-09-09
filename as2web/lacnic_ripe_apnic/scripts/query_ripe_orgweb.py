#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Query the RIPE Database REST API for the contact e-mails of the organisation
# objects referenced by aut-num objects, and derive an ASN -> e-mail mapping.
#
# Access is governed by the RIPE Database Terms and Conditions. The `unfiltered`
# responses used here contain personal data; do not redistribute it. RIPE may
# rate-limit or block bulk access -- confirm your usage is permitted and set
# --rate-sec accordingly. Identify your tool with --sourceapp-id.

import argparse
import json
import os
import time
import tempfile
import requests


# -------------------- Utils --------------------

def normalize_date(s: str) -> str:
    s = s.strip()
    if len(s) == 8 and s.isdigit():
        return s
    if len(s) == 6 and s.isdigit():
        yy = int(s[:2])
        return f"20{yy:02d}{s[2:]}"
    raise ValueError("date must be YYYYMMDD or YYMMDD (digits only)")


def atomic_write_json(obj, path: str, *, ensure_ascii=False, indent=None) -> None:
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=os.path.basename(path) + ".tmp.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=ensure_ascii, indent=indent)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def load_json_if_exists(path: str, default):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data


def append_ndjson(ndjson_path: str, org_id: str, data) -> None:
    os.makedirs(os.path.dirname(ndjson_path) or ".", exist_ok=True)
    line = json.dumps({"org_id": org_id, "data": data}, ensure_ascii=False)
    with open(ndjson_path, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def augment_orgid2email_from_ndjson(orgid2email: dict, ndjson_path: str) -> None:
    """
    Recover already-completed records from the ndjson log.
    Normalised form: org_id -> emails (list).
    """
    if not os.path.exists(ndjson_path):
        return

    with open(ndjson_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue

            org_id = rec.get("org_id")
            data = rec.get("data")

            if not org_id or org_id in orgid2email:
                continue

            if isinstance(data, dict):
                orgid2email[org_id] = data.get("emails", [])
            elif isinstance(data, list):
                orgid2email[org_id] = data
            else:
                orgid2email[org_id] = []


def load_ca2o_like_info(path: str):
    """
    Read the aut-num -> organisation list and extract:
      1. asn_to_orgid: {ASN -> ORG-ID}
      2. unique_orgids: sorted list of ORG-ID
    Only records with type == 'ASN' are used to build the ASN mapping.
    """
    with open(path, "r", encoding="utf-8") as f:
        entry_list = json.load(f)

    asn_to_orgid = {}
    orgid_set = set()

    for entry in entry_list:
        if not isinstance(entry, dict):
            continue
        if entry.get("type") != "ASN":
            continue

        asn = entry.get("asn")
        org_id = entry.get("organizationId")

        if not (isinstance(asn, str) and asn.strip()):
            continue
        if not (isinstance(org_id, str) and org_id.strip()):
            continue

        asn = asn.strip().upper()
        org_id = org_id.strip().upper()

        if "@AUT-AS" in org_id:
            continue

        asn_to_orgid[asn] = org_id
        orgid_set.add(org_id)

    return asn_to_orgid, sorted(orgid_set)


# -------------------- Query --------------------

def get_org_info(org_id: str, sourceapp_id: str | None, session: requests.Session | None = None):
    close_after = session is None
    if session is None:
        session = requests.Session()

    key = (org_id or "").strip().upper()
    url = f"https://rest.db.ripe.net/ripe/organisation/{key}.json?unfiltered"

    headers = {"Accept": "application/json"}
    params = {}
    if sourceapp_id:
        params["sourceapp"] = sourceapp_id

    try:
        r = session.get(url, headers=headers, params=params, timeout=20)

        if r.status_code == 404:
            print("Error:", r.status_code)
            return None
        if r.status_code >= 500:
            print("Error:", r.status_code)
            return None

        r.raise_for_status()

        data = r.json()
        objs = data.get("objects", {}).get("object", [])
        if not objs:
            return {"org_id": key, "found": False, "emails": []}

        attrs = objs[0].get("attributes", {}).get("attribute", [])
        flat = [{"name": a.get("name", ""), "value": a.get("value", "")} for a in attrs]

        emails = []
        for a in flat:
            n = a["name"].lower()
            if n in {"e-mail", "abuse-mailbox"}:
                v = a.get("value")
                if isinstance(v, str) and "@" in v:
                    emails.append(v.strip())

        return {
            "org_id": key,
            "found": True,
            "emails": sorted(set(emails)),
        }

    except requests.RequestException:
        print("RequestException")
        return None
    except ValueError:
        print("ValueError")
        return None
    finally:
        if close_after:
            try:
                session.close()
            except Exception:
                pass


# -------------------- Main --------------------

def main():
    ap = argparse.ArgumentParser(
        description="Query RIPE organisation info from an aut-num->org list and "
                    "produce orgid2email + asn2email. Review the RIPE Database "
                    "Terms and Conditions before running."
    )
    ap.add_argument("--date", required=True, help="YYYYMMDD or YYMMDD (e.g. 20250601 or 250601)")
    ap.add_argument("--rate-sec", type=float, default=5.0,
                    help="Seconds between requests (default 5). RIPE may require a "
                         "lower rate or block bulk access.")
    ap.add_argument("--flush-every", type=int, default=10,
                    help="Write a JSON snapshot every N successful queries (default 10).")

    ap.add_argument(
        "--ca2o-like-info",
        required=True,
        help="Path to the aut-num->organisation list "
             "[{'type':'ASN','asn':'AS3333','organizationId':'ORG-...'}].",
    )
    ap.add_argument("--sourceapp-id", required=True,
                    help="Your tool identifier, passed as the RIPE API 'sourceapp' "
                         "parameter. Use your own value.")

    ap.add_argument("--output-dir", required=True,
                    help="Output root; use a private, ignored directory because results contain personal data.")
    ap.add_argument("--org-snapshot", default=None, help="orgid->emails JSON output path")
    ap.add_argument("--asn-output", default=None, help="asn->emails JSON output path")
    ap.add_argument("--log", default=None, help="append-only NDJSON log path")

    args = ap.parse_args()

    date8 = normalize_date(args.date)
    ca2o_like_info_file = args.ca2o_like_info
    output_dir = os.path.join(args.output_dir, date8)
    os.makedirs(output_dir, exist_ok=True)
    org_snapshot_path = args.org_snapshot or os.path.join(output_dir, f"{date8}_ripe_orgid2email.json")
    asn_output_path = args.asn_output or os.path.join(output_dir, f"{date8}_ripe_asn2email.json")
    ndjson_path = args.log or os.path.join(output_dir, f"{date8}_ripe_orgid2email.ndjson")

    # 1) Read ASN -> ORG-ID from the aut-num->org list
    asn_to_orgid, org_ids = load_ca2o_like_info(ca2o_like_info_file)

    # 2) Load any existing orgid->emails snapshot and top it up from the ndjson log
    orgid2email = load_json_if_exists(org_snapshot_path, {})
    if not isinstance(orgid2email, dict):
        orgid2email = {}
    augment_orgid2email_from_ndjson(orgid2email, ndjson_path)

    # 3) Only process org_ids not yet completed
    todo = [oid for oid in org_ids if oid not in orgid2email]

    print("#ASNs:", len(asn_to_orgid))
    print("#Unique org IDs:", len(org_ids))
    print("#Queries to make:", len(todo))
    print("******************")

    session = requests.Session()
    pending = 0

    for org_id in todo:
        print(f"Querying {org_id}...")
        data = get_org_info(org_id, args.sourceapp_id, session=session)

        if data is not None:
            # Query succeeded: log it and mark complete, whether or not an email was found
            append_ndjson(ndjson_path, org_id, data)

            emails = data.get("emails", []) if isinstance(data, dict) else []
            orgid2email[org_id] = emails

            if emails:
                print(org_id, f"found {len(emails)} contact e-mail address(es)")
            else:
                print(org_id, "queried successfully but no email found")

            pending += 1
            if pending >= args.flush_every:
                atomic_write_json(orgid2email, org_snapshot_path, ensure_ascii=False)

                # refresh asn->emails on every flush
                asn2email = {
                    asn: orgid2email.get(org_id, [])
                    for asn, org_id in asn_to_orgid.items()
                    if org_id in orgid2email
                }
                atomic_write_json(asn2email, asn_output_path, ensure_ascii=False)

                pending = 0
                print("Flush")
        else:
            # genuine failure: do not snapshot, so it is retried next run
            print(org_id, "query failed")
            continue

        time.sleep(args.rate_sec)

    # Final write
    atomic_write_json(orgid2email, org_snapshot_path, ensure_ascii=False)

    asn2email = {
        asn: orgid2email.get(org_id, [])
        for asn, org_id in asn_to_orgid.items()
        if org_id in orgid2email
    }
    atomic_write_json(asn2email, asn_output_path, ensure_ascii=False)

    try:
        session.close()
    except Exception:
        pass

    print("Done.")
    print("orgid->emails saved to:", org_snapshot_path)
    print("asn->emails saved to:", asn_output_path)


if __name__ == "__main__":
    main()
