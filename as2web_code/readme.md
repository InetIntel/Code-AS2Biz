# AS2Web Dataset Generation

This directory contains Python scripts for generating the **AS2Web dataset**, which leverages three AS-centered source (Whois, PeeringDB, IPinfo) and a LLM-based online search API (Perplexity AI Sonar-pro).

---

## 📌 Steps Overview

### 1. 📥 Download, Query, and Process Raw WHOIS Data

Please follow the instructions below to download, query, and process raw WHOIS data. The final goal is to generate two output files for each RIR:

- `as2domain.json`: a mapping from AS number to the most relevant domain  
- `as_info.json`: a dictionary containing the AS name and organization name for each AS

In each step, you may need to **manually download** bulk WHOIS data.  
Please ensure the files are saved using the appropriate naming and folder structure.

Data Directory Structure After Manually Downloading the Required Raw Whois Files (also in the [`/data/README.md`](./data/README.md)):

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
└── lacnic/
    └── 250101/
```

#### (1) AFRINIC

- Manually download the AFRINIC bulk Whois file: [`https://ftp.afrinic.net/pub/dbase/afrinic.db.gz`](https://ftp.afrinic.net/pub/dbase/afrinic.db.gz)

- Then, run the extraction script to generate `as2domain.json` and `as_info.json`:

```bash
python afrinic_extract.py --date 250101
```

#### (2) ARIN

- Request access and download three ARIN bulk WHOIS files by following the instructions at:
[`https://www.arin.net/reference/research/bulkwhois/`](https://www.arin.net/reference/research/bulkwhois/). 
  - asns.txt
  - orgs.txt
  - pocs.txt

- Then, run the extraction script to generate `as2domain.json` and `as_info.json`:

```bash
python arin_extract.py --date 250101
```

#### (3) APNIC

- First, manually download two bulk Whois files, which are later used to extract `as_info.json`: [`https://ftp.apnic.net/apnic/whois/`](https://ftp.apnic.net/apnic/whois/)
  - apnic.db.aut-num.gz
  - apnic.db.organisation.gz 
   
- Next, query the live APNIC RDAP API to collect associated emails for each AS:

```bash
python apnic_query.py --date 250101
```

Note: The query is performed online against the current APNIC database.
The --date argument here controls the output directory (e.g., data/apnic/250101/)
and must match the date/folder used to store the downloaded bulk WHOIS files.

- Finally, run the extraction script to generate `as2domain.json` and `as_info.json`:

```bash
python apnic_extract.py --date 250101
```

#### (4) RIPE NCC

- First, manually download the RIPE bulk Whois, which are later used to extract `as_info.json`: [`https://ftp.ripe.net/ripe/dbase/`](https://ftp.ripe.net/ripe/dbase/)
  - ripe.db.gz
   
- Next, query the live RIPE Abuse Contact API to collect the abuse contact email for each AS.

  - To use the RIPEstat Data API, please follow the official [RIPE Data API usage guidelines](https://stat.ripe.net/docs/02.data-api/), which include:

    - Sending a short email to RIPE NCC describing your use case
    - Registering a **`sourceapp`** identifier (to identify your application in queries)

  - Once registered, include your `sourceapp` value in the argument and run:

```bash
python ripe_query.py --date 250101 --sourceapp your_id
```

Note: The query is performed online against the current RIPE database.
The --date argument here controls the output directory (e.g., data/ripe/250101/)
and must match the date/folder used to store the downloaded bulk WHOIS files.

- Finally, run the extraction script to generate `as2domain.json` and `as_info.json`:

```bash
python ripe_extract.py --date 250101
```

#### (5) LACNIC

LACNIC does **not** publish bulk WHOIS data or provide an email-related API.
Instead, we use a script to query WHOIS records for each ASN individually via the command-line `whois` client. The collected responses are stored locally for further processing.

> ⚠️ Make sure your system has the `whois` command-line tool installed (e.g., `apt install whois` or `brew install whois`).

- Run the query script:

```bash
python lacnic_query.py --date 250101
```

- Run the extraction script to generate `as2domain.json` and `as_info.json`:

```bash
python lacnic_extract.py --date 250101
```


