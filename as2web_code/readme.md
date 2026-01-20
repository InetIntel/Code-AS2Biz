# AS2Web Dataset Generation

This directory contains Python scripts for generating the **AS2Web dataset**, which leverages three AS-centered source (Whois, PeeringDB, IPinfo) and a LLM-based online search API (Perplexity AI Sonar-pro).

---

## 📌 Steps Overview

### 1. 📥 Download, Query, and Process Raw WHOIS Data

Please follow the instructions below to download, query, and process raw WHOIS data. The final goal is to generate two output files for each RIR:

- `as2domains.json`: a mapping from AS number to the filtered domains
- `as_info.json`: a dictionary containing the AS name, organization name, and country for each AS

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

- Then, run the extraction script to generate `as2domains.json` and `as_info.json`:

```bash
python afrinic_extract.py --date 250101
```

#### (2) ARIN

- Request access and download three ARIN bulk WHOIS files by following the instructions at:
[`https://www.arin.net/reference/research/bulkwhois/`](https://www.arin.net/reference/research/bulkwhois/). 
  - asns.txt
  - orgs.txt
  - pocs.txt

- Then, run the extraction script to generate `as2domains.json` and `as_info.json`:

```bash
python arin_extract.py --date 250101
```

#### (3) APNIC

- First, manually download two bulk Whois files: [`https://ftp.apnic.net/apnic/whois/`](https://ftp.apnic.net/apnic/whois/)
  - apnic.db.aut-num.gz
  - apnic.db.organisation.gz 
   
- Then, run the extraction script to generate `as_info.json`:

```bash
python apnic_extract.py --date 250101
```

- Finally, query the live APNIC RDAP API to collect associated (filtered) domains for each AS and generate `as2domains.json`:

```bash
python apnic_query.py --date 250101
```

Note: The query is performed online against the current APNIC database.
The --date argument here controls the output directory (e.g., data/apnic/250101/)
and must match the date/folder used to store the downloaded bulk WHOIS files.

#### (4) RIPE NCC

- First, manually download the RIPE bulk Whois: [`https://ftp.ripe.net/ripe/dbase/`](https://ftp.ripe.net/ripe/dbase/)
  - ripe.db.gz

- Then, run the extraction script to generate `as_info.json`:

```bash
python ripe_extract.py --date 250101
```

   
- Finally, query the live RIPE Abuse Contact API to collect the abuse contact email for each AS and generate `as2domains.json`.

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


### 2. 📥 Download and Process IPinfo Data

Download and process IPinfo's public ASN data to extract domain mappings.

- Manually download the IPinfo Lite ASN data from:  
  [`https://ipinfo.io/lite`](https://ipinfo.io/lite)

- Save it in the following folder structure:
```text
data/
└── ipinfo/
    └── 250101/
        └── free-2025-01-01.asn.csv
```

- Then, run the extraction script to generate `as2domain.json`:

```bash
python ipinfo_extract.py --date 250101
```

### 3. 📥 Download and Process PeeringDB Data

Download and process PeeringDB to extract AS to website mappings.

- Manually download the PeeringDB data from:  
  [`https://www.caida.org/catalog/datasets/peeringdb/`](https://www.caida.org/catalog/datasets/peeringdb/)

- Save it in the following folder structure:
```text
data/
└── peeringdb/
    └── 250101/
        └── peeringdb_2_dump_2025_01_01.json
```

- Then, run the extraction script to generate `as2web.json`:

```bash
python peeringdb_extract.py --date 250101
```

### 4. 📥 Collect AS2Web from AS-centered Sources

This step aggregates domain information from **AS-centered sources** (Whois, PeeringDB, and IPinfo), applies fuzzy matching to identify the most relevant domain, and uses the `requests` library to check website accessibility.

- Run the script to generate two files:

  - `as_centered_as2web.json`: 
    A dictionary where each key is an ASN. For each ASN, the value contains:
    - `"Website"`: the URL of the most relevant domain (if accessible, with `https://` or `http://` prefix)
    - `"Sources"`: a list of sources (`Whois`, `PeeringDB`, and/or `IPinfo`) that contributed the domain
    - `"Accessible"`: a boolean (true/false) indicating whether the website is reachable via HTTP(S)

  - `as_centered_noweb_as.json`: 
    A list of ASNs for which no domain could be extracted from any of the three AS-centered sources.

```bash
python as_centered_extract.py --date 250101
```

- These files will be saved in the following directory:

```text
data/
└── as_centered_sources/
    └── 250101/
```


### 5. 📥 Query Perplexity AI

This step leverages Perplexity AI's **Sonar-Pro model** to identify websites for organizations whose ASes either:

- lack any domain from AS-centered sources, or  
- have inaccessible websites.

> 🔐 Please follow [Perplexity AI's official guide](https://docs.perplexity.ai/home) to:
> - Set up billing
> - Generate an API key

Then, replace the API key placeholder in `perplexity_ai_query.py` with your actual key to begin querying.

---

- Run the script to generate the following files:

  - `sonar_pro_responses_partial.json.gz`:  
    This file is updated every 30 seconds to store intermediate query results (useful for recovery in case of interruption).

  - `sonar_pro_responses_final.json.gz`:  
    The final output file containing all completed responses.

```bash
python perplexity_ai_query.py --date 250101
```

These files are saved in:

```text
data/
└── perplexity/
    └── 250101/
```

### 6. 📥 Generate Final AS2Web

This step combines results from **AS-centered sources** and **Perplexity AI** to produce the final AS2Web dataset.

- Run the following script to generate the final dataset:

```bash
python perplexity_ai_extract.py --date 250101
```

The outcome dataset is stored in

```text
data/
└── as2web/
    └── 250101/
```