# AS2Web Data Directory

This directory should contain raw WHOIS data, PeeringDB, and IPinfo data used for generating the AS2Web dataset. Please download and place the raw data in the following structure:

## 📦 Folder Structure

Place your data in the following structure:

```text
data/
├── afrinic/
│   └── 250101/
│       └── afrinic.db.gz
├── arin/
│   └── 250101/
│       ├── asns.txt
│       ├── orgs.txt
│       └── pocs.txt
├── apnic/
│   └── 250101/
│       ├── apnic.db.aut-num.gz
│       └── apnic.db.organisation.gz
├── ripe/
│   └── 250101/
│       └── ripe.db.gz
├── lacnic/
│   └── 250101/
├── ipinfo/
│   └── 250101/
│       └── free-2025-01-01.asn.csv
├── peeringdb/
│   └── 250101/
│       └── peeringdb_2_dump_2025_01_01.json
```

- `250101/` represents the snapshot date in `yymmdd` format (e.g., January 1, 2025).
- You can create multiple subfolders for different dates if needed.