#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_asn_registry.py
─────────────────────
Build the ASN registry file that the AS2Web stages take as their starting
point (referred to in older code/paths as ``administrative_alive.json``).

Output schema (JSON):

    {
        "<asn>": ["<rir>", "<cc>"],
        ...
    }

  * ``<asn>``  decimal AS number as a string, no "AS" prefix
  * ``<rir>``  one of: arin, ripe, apnic, lacnic, afrinic
  * ``<cc>``   ISO 3166-1 alpha-2 country code from the delegation record
               (may be "ZZ"/"" when the RIR does not record one)

The data comes entirely from the five public RIR *delegated-extended*
statistics files (RIR statistics exchange format). Nothing here is
lab-specific; you can re-run it yourself for any day the RIRs publish.

Examples
--------
    # download the current files and build the registry
    python as2web/tools/build_asn_registry.py --out inputs/asn_registry.json

    # use files you already downloaded (named delegated-*-extended*)
    python as2web/tools/build_asn_registry.py --from-dir inputs/delegated \\
        --out inputs/asn_registry.json

Notes
-----
  * The delegated files are published under each RIR's terms of use; they
    contain no personal data.
  * "latest" is a moving target. If you need a specific historical day,
    download the dated file from the RIR's ``pub/stats`` archive and pass
    ``--from-dir``.
"""

import argparse
import json
import sys
from pathlib import Path

import requests

# registry field in the delegated file  ->  name used throughout AS2Web
RIR_SOURCES = {
    "arin":    "https://ftp.arin.net/pub/stats/arin/delegated-arin-extended-latest",
    "ripe":    "https://ftp.ripe.net/pub/stats/ripencc/delegated-ripencc-extended-latest",
    "apnic":   "https://ftp.apnic.net/pub/stats/apnic/delegated-apnic-extended-latest",
    "lacnic":  "https://ftp.lacnic.net/pub/stats/lacnic/delegated-lacnic-extended-latest",
    "afrinic": "https://ftp.afrinic.net/pub/stats/afrinic/delegated-afrinic-extended-latest",
}

# "registry" token as it appears in the file  ->  canonical short name
REGISTRY_ALIASES = {
    "arin": "arin",
    "ripencc": "ripe",
    "ripe": "ripe",
    "apnic": "apnic",
    "lacnic": "lacnic",
    "afrinic": "afrinic",
}


def parse_delegated_text(text, statuses, default_rir):
    """
    Yield (asn_str, rir, cc) for every asn record whose status is in `statuses`.

    RIR statistics exchange format, pipe-separated:
        registry|cc|type|start|value|date|status[|opaque-id][|...]
    An `asn` record covers `value` consecutive AS numbers starting at `start`.
    """
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("|")
        if len(parts) < 7:
            continue
        registry, cc, rectype, start, value, _date, status = parts[:7]
        if rectype != "asn":
            continue
        if status not in statuses:
            continue
        rir = REGISTRY_ALIASES.get(registry.lower(), default_rir)
        try:
            first = int(start)
            count = int(value)
        except ValueError:
            continue
        cc = (cc or "").strip().upper()
        for asn in range(first, first + count):
            yield str(asn), rir, cc


def load_source_text(rir, args):
    if args.from_dir:
        d = Path(args.from_dir)
        # accept a few common naming conventions
        candidates = [
            d / f"delegated-{rir}-extended-latest",
            d / f"delegated-{'ripencc' if rir == 'ripe' else rir}-extended-latest",
        ]
        candidates += sorted(d.glob(f"delegated-{'ripencc' if rir == 'ripe' else rir}-extended*"))
        for c in candidates:
            if c.is_file():
                return c.read_text(encoding="utf-8", errors="replace")
        raise FileNotFoundError(
            f"no delegated-extended file for {rir} in {d} "
            f"(looked for delegated-{rir}-extended*)"
        )
    url = RIR_SOURCES[rir]
    print(f"  fetching {url}", file=sys.stderr)
    resp = requests.get(url, timeout=120)
    resp.raise_for_status()
    return resp.text


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True, help="Path to write the registry JSON.")
    ap.add_argument("--from-dir", default=None,
                    help="Read pre-downloaded delegated-*-extended* files from this "
                         "directory instead of fetching them over HTTP.")
    ap.add_argument("--status", default="allocated,assigned",
                    help="Comma-separated delegation statuses to keep "
                         "(default: allocated,assigned).")
    args = ap.parse_args()

    statuses = {s.strip() for s in args.status.split(",") if s.strip()}
    registry = {}
    conflicts = 0

    for rir in RIR_SOURCES:
        try:
            text = load_source_text(rir, args)
        except Exception as e:  # noqa: BLE001 - report and continue
            print(f"[WARN] {rir}: {e}", file=sys.stderr)
            continue
        added = 0
        for asn, rec_rir, cc in parse_delegated_text(text, statuses, rir):
            if asn in registry and registry[asn][0] != rec_rir:
                conflicts += 1
            registry[asn] = [rec_rir, cc]
            added += 1
        print(f"  {rir}: {added:,} asn entries", file=sys.stderr)

    if not registry:
        print("[ERROR] no records parsed; nothing written.", file=sys.stderr)
        raise SystemExit(1)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as f:
        json.dump(registry, f)

    print(f"Wrote {len(registry):,} ASNs to {out}"
          + (f"  ({conflicts} cross-RIR conflicts, last wins)" if conflicts else ""),
          file=sys.stderr)


if __name__ == "__main__":
    main()
