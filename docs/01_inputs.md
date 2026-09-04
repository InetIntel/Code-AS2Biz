# AS2Web / AS2Biz inputs

This repository is **code, not a turnkey pipeline.** Several stages depend on
data that we are not allowed to redistribute, or on artifacts that only exist
inside our lab. For those, this document tells you exactly what the file is,
where to get it (or how to build it), and what schema each script expects. Some
steps ship only as a script or a snippet you adapt to inputs you obtain
yourself; that is expected.

Every input falls into one of three kinds:

| Kind | Meaning | What you do |
| --- | --- | --- |
| **A — third-party data** | A dataset you download from its provider, usually under a licence or an access request | Follow the provider's process, then point the script at the file |
| **B — live API queries** | Data you collect yourself by querying an RIR | Confirm the query is permitted, set a polite rate, run the provided script |
| **C — derived artifact** | Something produced by another pipeline (ours or a third party's) | Build it with the referenced tool, or hand the script a file in the documented schema; several are optional |

Paths like `inputs/` and `outputs/` below are only examples. Every script takes
explicit `--*-dir` / `--*-json` arguments; put files wherever you want.

---

## Licensing and terms — at a glance

| Source | How to obtain | Terms that apply |
| --- | --- | --- |
| ARIN Bulk Whois | Access request to ARIN | ARIN Bulk Whois AUP; not redistributable |
| AFRINIC bulk whois | AFRINIC FTP (`ftp.afrinic.net/dbase/`) | AFRINIC Database Terms & Conditions |
| APNIC / LACNIC RDAP, RIPE REST | Live queries you run | Each RIR's whois/RDAP AUP and (RIPE) Database T&C; responses contain personal data |
| IPinfo IP-to-ASN | Purchase or IPinfo research access | Your IPinfo data agreement |
| PeeringDB dump | CAIDA access request (`caida.org/catalog/datasets/peeringdb/`) | CAIDA's dataset terms (or PeeringDB's, if taken directly) |
| RIR delegated-extended stats | Public FTP (fetched by `build_asn_registry.py`) | Public; no personal data |
| ParallelLives (operational lifetimes) | `github.com/SystemsLab-Sapienza/ParallelLives` | That repository's licence |
| IIL-AS2Org | `github.com/InetIntel/Dataset-AS-to-Organization-Mapping` | That repository's licence |

**Do not redistribute personal data** (contact e-mail addresses) obtained from
RIR whois/RDAP/REST, or unfiltered dumps derived from it. The AS2Web outputs you
publish should be domains and URLs only.

---

## A — Third-party datasets

### ARIN Bulk Whois — `arin.bulkwhois.<date>.asns.txt`, `.orgs.txt`, `.pocs.txt`

* **Used by:** `as2web/arin_afrinic/main_pipeline.py`
* **Get it:** request *Bulk Whois Data* from ARIN
  (<https://www.arin.net/reference/research/bulkwhois/>) and agree to ARIN's
  Bulk Whois Acceptable Use Policy. ARIN sends you the files. **Not
  redistributable.**
* **Layout the script expects:** `--raw-whois-dir <dir>` containing
  `arin/<YYYY>/arin.bulkwhois.<YYYY-MM-DD>.asns.txt` (and `.orgs.txt`,
  `.pocs.txt`).
* **What we read:** records are blocks separated by blank lines.
  * `asns.txt`: `ASHandle`, `OrgID`, `ASName`
  * `orgs.txt`: `OrgID`, `OrgName`, `OrgAbuseHandle` / `OrgAdminHandle` /
    `OrgTechHandle` / `OrgNOCHandle`
  * `pocs.txt`: `POCHandle`, `Mailbox`
  The script walks ASN → OrgID → contact handles → mailbox domains and keeps
  the mailbox domain most similar (fuzzy match) to the AS/org name.

### AFRINIC Bulk Whois — `afrinic.bulkwhois.<date>.db.txt.gz`

* **Used by:** `as2web/arin_afrinic/main_pipeline.py`
* **Get it:** AFRINIC publishes the database dump on its FTP
  (<https://ftp.afrinic.net/dbase/>); use is governed by the AFRINIC Database
  Terms and Conditions.
* **Layout:** `--raw-whois-dir <dir>` containing
  `afrinic/<YYYY>/afrinic.bulkwhois.<YYYY-MM-DD>.db.txt.gz`.
* **What we read:** RPSL blocks. From `aut-num`: `as-name`, `org`. From
  `organisation`: `org-name`, `e-mail`. ASN → org → e-mail domain, same fuzzy
  pick as ARIN.

**Hand corrections (optional).** `main_pipeline.py --manual-overrides <json>`
applies a correction list after the automatic extraction:

```json
{
  "domain_by_asn": {"arin": {"<asn>": "<domain>"}, "afrinic": {"<asn>": "<domain>"}},
  "domain_rewrite": {"<from-domain>": "<to-domain>"}
}
```

The repository ships no values — keep your own file if you maintain a list.

### IPinfo IP-to-ASN database — `<YYYY-MM-DD>.asn.csv.gz`

* **Used by:** `as2web/ipinfo/main_pipeline.py`
* **Get it:** obtain IPinfo's ASN dataset either by purchasing it from IPinfo
  (<https://ipinfo.io>) or by applying for IPinfo research/academic access. Use
  is governed by your IPinfo data agreement — check what it allows before
  redistributing anything derived from it.
* **Layout:** `--ipinfo-dir <dir>` containing files named
  `<YYYY-MM-DD>.asn.csv.gz`.
* **Schema:** CSV with header `start_ip,end_ip,asn,name,domain`. We use only
  `asn` and `domain`, taking the most frequent `domain` per ASN.

### PeeringDB snapshot — `peeringdb_2_dump_<YYYY>_<MM>_<DD>.json`

* **Used by:** `as2web/peeringdb/data_collector.py`
* **Get it:** we used the CAIDA PeeringDB dataset. Request access at
  <https://www.caida.org/catalog/datasets/peeringdb/>; CAIDA provides the
  download location after approving your request. (You may also use
  PeeringDB's own API / dump under PeeringDB's terms; the JSON layout is the
  same.)
* **Layout:** `--peeringdb-dir <dir>` containing
  `<YYYY>/<MM>/peeringdb_2_dump_<YYYY>_<MM>_<DD>.json`.
* **What we read:** the top-level `org` and `net` arrays (`id`, `org_id`,
  `asn`, `name`, `website`, `aka`, `country`).

---

## B — Live RIR queries

These scripts read the **ASN registry** (kind C, below) to learn which ASNs
belong to each RIR, then query that RIR for contact data.

Before running any of them:

* Read the RIR's acceptable-use / database terms. RDAP and whois responses,
  and the REST API responses, contain **personal data** (contact e-mail
  addresses). Do not redistribute that data or unfiltered dumps derived from
  it; the AS2Web outputs you publish should be domains/URLs only.
* Confirm that automated bulk querying at your intended volume is allowed, and
  keep the request rate polite. Each script exposes a delay flag — start high.

### APNIC — `as2web/lacnic_ripe_apnic/scripts/query_apnic.py`

* Queries `https://rdap.apnic.net/autnum/<asn>`.
* `--delegation-json <asn_registry.json>` (see kind C).
* `--output-dir <dir>` writes
  `<dir>/<date>/<date>_apnic_as2email.json`
  (`{ "<asn>": ["<email>", ...] }`).
* Rate: `--sleep` seconds between requests (default conservative).

### LACNIC — `as2web/lacnic_ripe_apnic/scripts/lacnic_rdap.py`

* Queries `https://rdap.lacnic.net/rdap/autnum/AS<asn>`; honours `429` /
  `Retry-After`.
* `--delegation-json <asn_registry.json>`.
* `--output-dir <dir>` writes
  `<dir>/<date>/<date>_lacnic_as2email.json`.
* Rate: `--rate-sec` (default conservative); LACNIC throttles aggressively.

### RIPE — `as2web/lacnic_ripe_apnic/scripts/query_ripe_orgweb.py`

* Queries `https://rest.db.ripe.net/ripe/organisation/<ORG-ID>.json?unfiltered`,
  governed by the RIPE Database Terms and Conditions.
* **Input:** a list of the RIPE `organisation` handles referenced by
  `aut-num` objects, as JSON:

  ```json
  [
    {"type": "ASN", "asn": "AS3333", "organizationId": "ORG-RIEN1-RIPE"},
    ...
  ]
  ```

  Build it from RIPE's published database split file
  `ripe.db.aut-num.gz` (<https://ftp.ripe.net/ripe/dbase/split/>) by reading
  the `org:` attribute of each `aut-num`. Pass with `--ca2o-like-info <file>`.
  (This is the same information carried by `ripe_ca2o_like_info.json` in our
  internal pipeline.)
* **`--sourceapp-id`:** the RIPE API asks callers to identify their tool. Use
  a value that is meaningful for *you*, not a placeholder.
* Rate: `--rate-sec` (default 5 s). RIPE may require a lower rate or block
  bulk access — confirm first.
* `--output-dir <dir>` writes
  `<dir>/<date>/<date>_ripe_asn2email.json` plus the resumable organisation
  snapshot and NDJSON log.

### Turning the e-mail files into domain candidates

`as2web/lacnic_ripe_apnic/main_pipeline.py --input-dir <dir> --output-dir <dir>`
reads the three `<dir>/<date>/<date>_<rir>_as2email.json` files above and writes
`<rir>/<date>/as2domain.json` (`{ "<asn>": "<domain>" }`).

---

## C — Derived artifacts

### ASN registry — `asn_registry.json`  *(older code/paths: `administrative_alive.json`)*

* **Used by:** the RIR query scripts, and
  `as2web/as_centered_as2web/main_pipeline.py`
  (`--delegation-dir <dir>/<YYYY>/<MM>/<DD>/administrative_alive.json`).
* **Schema:** `{ "<asn>": ["<rir>", "<cc>"] }` where `<rir>` ∈
  `arin ripe apnic lacnic afrinic` and `<cc>` is the delegation country code.
* **Build it:** `python as2web/tools/build_asn_registry.py --out
  inputs/delegation/YYYY/MM/DD/administrative_alive.json`.
  It parses the five public RIR *delegated-extended* statistics files; no
  lab data involved. For a specific historical day, download that day's dated
  files and pass `--from-dir`.
  This writes it directly to the layout expected by `as_centered`; pass the
  same file to the RIR query scripts as `--delegation-json`.

### Per-RIR AS/org names — `<rir>_info.json`

* **Used by:**
  * `as2web/as_centered_as2web/main_pipeline.py` — resolve which candidate
    domain to keep when sources disagree, and populate `as2orgname.json`.
  * `as2web/peeringdb/data_collector.py` — same purpose, for PeeringDB's
    multi-URL organisations. (It looks for the file under the name
    `<rir>_ca2o_like_info.json`; the schema is identical — symlink or copy.)
  * `as2web/web_search/main.py` — the previous-snapshot *reuse* check
    (`--whois_dir`). **Optional there:** omit `--whois_dir` and reuse is
    skipped, every target ASN is queried fresh.
* **Optional everywhere.** Without it, `as_centered` still runs: ASNs whose
  sources give a single registrable domain are unaffected; only ASNs with
  *conflicting* candidate domains are dropped, and `as2orgname.json` (hence the
  web-search stage's coverage for domain-less ASNs) is smaller.
* **Schema** — a JSON object per RIR:

  ```json
  // arin_info.json
  { "AS13335": {"ASName": "CLOUDFLARENET", "org": "Cloudflare, Inc."} }

  // ripe_info.json / apnic_info.json / afrinic_info.json
  { "AS3333": {"as-name": "RIPE-NCC-AS", "org": "ORG-RIEN1-RIPE", "descr": "RIPE NCC"} }

  // lacnic_info.json   (value is a plain org-name string)
  { "28000": "LACNIC" }
  ```

  Keys may be `"AS<n>"` or `"as<n>"`; `org` may be an org handle or a name
  (it is only fuzzy-matched against domains). For the reuse check the only
  thing that matters is that the value is *stable* between snapshots for an
  unchanged AS.
* **Build it** from the same bulk-whois / split files you already downloaded
  for section A/B. Minimal ARIN example:

  ```python
  import json
  def blocks(p):
      return open(p, encoding="latin-1").read().strip().split("\n\n")
  asn_org, asn_name = {}, {}
  for b in blocks("arin.bulkwhois.<date>.asns.txt"):
      d = dict(l.split(":", 1) for l in b.splitlines() if ":" in l)
      h = d.get("ASHandle", "").strip().removeprefix("AS")
      if h:
          asn_name[h] = d.get("ASName", "").strip()
          asn_org[h] = d.get("OrgID", "").strip()
  org_name = {}
  for b in blocks("arin.bulkwhois.<date>.orgs.txt"):
      d = dict(l.split(":", 1) for l in b.splitlines() if ":" in l)
      if d.get("OrgID"):
          org_name[d["OrgID"].strip()] = d.get("OrgName", "").strip()
  out = {f"AS{h}": {"ASName": asn_name.get(h, ""),
                    "org": org_name.get(asn_org.get(h, ""), "")}
         for h in asn_name}
  json.dump(out, open("arin_info.json", "w"))
  ```

  For RIPE/APNIC/AFRINIC, read the `split/` files: from each `aut-num` take
  `as-name`, `org`, `descr`. For LACNIC, map ASN → the `owner`/`org-name`
  string.

### Operational lifetimes — `operational_lifetimes.csv`  *(optional)*

* **Used by:** `as2web/as_centered_as2web/main_pipeline.py`
  (`--operational-lifetime-dir <dir>/<YYYYMMDD>/operational_lifetimes.csv`).
* **Purpose:** restrict the AS scope to ASNs seen originating routes in BGP
  within the last year. **If you do not supply it, the scope is simply every
  ASN in `asn_registry.json`** — the pipeline runs fine.
* **Schema:** CSV with columns `ASN,startdate,enddate` (dates parseable by
  pandas; multiple rows per ASN allowed, the latest `enddate` is used).
* **Produce it:** if you want this filter, generate the data with the authors'
  code at <https://github.com/SystemsLab-Sapienza/ParallelLives> and emit a
  CSV in the schema above. We do not ship a BGP pipeline.

### Previous snapshot — `combined/<YYYYMMDD>/as2web.json`  *(optional)*

* **Used by:** `as2web/web_search/main.py` reuse heuristic and
  `combine_as2web_results.py`. Only relevant from the second snapshot onward.
  Absent on a first run; nothing to do.

### IIL AS-to-Organization mapping — `IIL-AS2Org.<YYYY-MM>.json`  *(optional)*

* **Used by:** `as2biz/post_process.py sibling` for sibling-organisation
  augmentation (ASNs under the same `OrgID` inherit categories from classified
  siblings).
* **Get it:** download the matching snapshot from
  <https://github.com/InetIntel/Dataset-AS-to-Organization-Mapping> and use it
  per that repository's licence.
* **Schema used:** `{ "as2org": { "<asn>": {"OrgID": "<id>", ...}, ... } }`.
* **Optional:** skip the sibling-augmentation cells and only the `Inherit
  from AS…` labels are lost.

### Wikipedia inputs — `wiki_info.json`, `classifiable_as2brand.json`  *(optional)*

* **Used by:** `as2biz/prepare_openai_batch_wiki.py` (the optional Wikipedia
  classification path). The website-classification path
  (`prepare_openai_batch_process.py`) needs none of this.
* **Produced by** `as2biz/wiki_fetch.py`, which takes the fallback ASN list plus
  `as2orgname.json`, strips corporate suffixes to a "brand", searches English
  Wikipedia, and keeps each article whose text actually contains the brand
  string. Pass `--contact` (Wikimedia asks API clients to identify themselves).
* **Schemas** (if you build the files yourself instead):
  * `wiki_info.json`: `{ "<brand>": {"title": "...", "url": "...", "full_text": "<plaintext>"} }`
  * `classifiable_as2brand.json`: `{ "<asn>": "<brand>" }`, where `<brand>`
    is a key of `wiki_info.json`.
