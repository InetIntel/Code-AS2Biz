# AS2Web / AS2Biz

Code and prompts used to construct the AS2Web (AS-number → website) and AS2Biz
(AS-number → business category) datasets.

## Scope of this release

This repository contains **code only**, and it is **not a turnkey end-to-end
pipeline**. Some stages depend on datasets we cannot redistribute (RIR bulk
whois, IPinfo, PeeringDB) or on artifacts produced by other pipelines; for a
few steps we can only provide a script or a snippet that you adapt to inputs
you obtain yourself.

Every external and derived input — where to request it, its licence/terms, the
exact schema each script expects, and which inputs are optional — is documented
in **[`docs/01_inputs.md`](docs/01_inputs.md)**. Read that first.

The raw WARC archives of the crawled websites are not included here; contact
the lab if you need them.

## Environment

Run all commands from the repository root:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

## Conventions

The commands below use `DATE=20260301` and example `inputs/` / `outputs/`
directories. All input and output paths are passed explicitly on the command
line; place files wherever you like. Datasets should be versioned by snapshot
date.

## Responsible crawling

Several stages contact third-party sites: `as2web/as_centered_as2web/check_domain.py`
(AS2Web stage 5), `as2biz/scraper.py` (AS2Biz stage 1), `as2biz/wiki_fetch.py`
(Wikipedia), and the RIR query scripts. If you run them:

- **Identify yourself — do not reuse ours.** `scraper.py`, `check_domain.py`, and
  `web_search/main.py` send a `Mozilla/5.0 (compatible; …; +<URL>)` User-Agent
  whose `+<URL>` points at our dataset repository. Change it to a page that
  describes *your* project and gives *your* contact address before you crawl.
  The same applies to `wiki_fetch.py --contact` (Wikimedia User-Agent) and
  `query_ripe_orgweb.py --sourceapp-id` (RIPE asks callers to name their tool) —
  pass values that are meaningful for you, not placeholders.
- **`scraper.py` does not consult `robots.txt` by default** (`RESPECT_ROBOTS =
  False` near the top of the file). The released datasets were built this way;
  set it to `True` to honour `robots.txt` strictly.
- **`scraper.py` applies `playwright-stealth`** when the package is installed, to
  mask headless-browser fingerprints. Uninstall the package to disable it.
- Chromium's process sandbox remains enabled by default. The
  `--disable-browser-sandbox` escape hatch is intended only for an appropriately
  isolated container or worker where Chromium cannot start otherwise.
- **TLS is verified by default.** `--allow-invalid-tls` (on `scraper.py`,
  `check_domain.py`, and `web_search/main.py`) makes the crawl/probe accept
  expired, self-signed, or hostname-mismatched certificates. It lowers the
  authenticity of what you fetch and the integrity of reachability results, so
  use it only when you deliberately want "the server answered but its
  certificate is invalid" to count as reachable, and understand the risk. It
  does not affect the OpenAI API request in `web_search/main.py`, which always
  verifies TLS.
- **Feed these scripts trusted input only.** `scraper.py`, `check_domain.py`, and
  `web_search/main.py` fetch URLs from your input files and follow redirects.
  `check_domain.py` and `web_search/main.py` reject a first URL whose host
  resolves to a loopback / private / link-local / reserved address; that check
  is best-effort — it does not re-validate redirect hops and cannot stop DNS
  rebinding. `scraper.py` (Chromium) does **not** provide this check at all.
  Run all three only in an environment that has no route to sensitive internal
  networks or metadata services.
- Use a polite `--concurrency` / rate, and run from a host that can absorb the
  traffic and be held accountable for it.

---

# Reproducing AS2Web

The AS2Web stages fuse several per-source AS→domain signals, verify them, and
fall back to LLM web search for ASNs still lacking a reachable site.

### 0. ASN registry

The pipeline starts from a list of ASNs with their RIR and country
(`{ "<asn>": ["<rir>", "<cc>"] }`). Build it from the public RIR
delegated-extended statistics files:

```bash
python as2web/tools/build_asn_registry.py \
  --out inputs/delegation/2026/03/01/administrative_alive.json
```

This writes the registry directly to the dated path consumed by stage 4. The
same file is passed to the RIR query scripts below.

### 1. Obtain the source inputs

Per `docs/01_inputs.md`, obtain what you need:

- **ARIN + AFRINIC bulk whois** under `inputs/raw_whois/{arin,afrinic}/<YYYY>/`
  (kind A — access request / FTP, not redistributable);
- **IPinfo** `<YYYY-MM-DD>.asn.csv.gz` under `inputs/ipinfo/` (kind A — purchased
  from IPinfo or via IPinfo research access);
- **PeeringDB** dump under `inputs/peeringdb/<YYYY>/<MM>/` (kind A — CAIDA access
  request);
- optionally **`<rir>_info.json`** (AS/org names) under `outputs/whois/<rir>/DATE/`
  and **`operational_lifetimes.csv`** under
  `inputs/operational_lifetime/DATE/` (kind C — both optional; see the doc for
  schemas and effects if absent).

### 2. Generate RIR-derived domains

ARIN + AFRINIC, from bulk whois contact e-mails:

```bash
python as2web/arin_afrinic/main_pipeline.py --date 20260301 \
  --raw-whois-dir inputs/raw_whois --output-dir outputs/whois
```

APNIC / LACNIC / RIPE, by querying the RIRs (**review each RIR's terms and set a
polite rate first** — see `docs/01_inputs.md` → kind B):

```bash
python as2web/lacnic_ripe_apnic/scripts/query_apnic.py --date 20260301 \
  --delegation-json inputs/delegation/2026/03/01/administrative_alive.json \
  --output-dir inputs/rir_email
python as2web/lacnic_ripe_apnic/scripts/lacnic_rdap.py --date 20260301 \
  --delegation-json inputs/delegation/2026/03/01/administrative_alive.json \
  --output-dir inputs/rir_email
python as2web/lacnic_ripe_apnic/scripts/query_ripe_orgweb.py --date 20260301 \
  --ca2o-like-info inputs/ripe_autnum_orgids.json --sourceapp-id <your-id> \
  --output-dir inputs/rir_email
```

Then convert the three e-mail files to domain mappings:

```bash
python as2web/lacnic_ripe_apnic/main_pipeline.py --date 20260301 \
  --input-dir inputs/rir_email --output-dir outputs/whois
```

This stage produces `outputs/whois/<rir>/DATE/as2domain.json`.

### 3. IPinfo and PeeringDB domain candidates

```bash
python as2web/ipinfo/main_pipeline.py --date 20260301 \
  --ipinfo-dir inputs/ipinfo --output-dir outputs/ipinfo

python as2web/peeringdb/data_collector.py --date 20260301 \
  --peeringdb-dir inputs/peeringdb --whois-dir outputs/whois \
  --output-dir outputs/peeringdb_collector

python as2web/peeringdb/data_processing.py --date 20260301 \
  --input-dir outputs/peeringdb_collector --output-dir outputs/peeringdb
```

The last command produces `outputs/peeringdb/DATE/DATE_pdb_as2url.json`.
(`data_collector.py` uses `<rir>_info.json` only to disambiguate organisations
with several candidate URLs; it warns and continues if the file is absent.)

### 4. Fuse sources into AS-centered candidates

```bash
python as2web/as_centered_as2web/main_pipeline.py --date 20260301 \
  --delegation-dir inputs/delegation \
  --operational-lifetime-dir inputs/operational_lifetime \
  --whois-dir outputs/whois --ipinfo-dir outputs/ipinfo \
  --peeringdb-dir outputs/peeringdb --output-dir outputs/as2web
```

Creates `outputs/as2web/DATE/`: `final_as_scope.json`, `asn2cc.json`,
`as2orgname.json`, `as_centered_as2domain_unchecked.json`,
`as_centered_domain2source.json`. Without `operational_lifetimes.csv` the scope
is every ASN in the registry; without `<rir>_info.json` only source-conflicting
ASNs are dropped.

### 5. Check candidate URL availability

```bash
python as2web/as_centered_as2web/check_domain.py --date 20260301 \
  --base-dir outputs/as2web
```

Reads `as_centered_as2domain_unchecked.json`, writes `as_centered_as2domain.json`
in the same date directory. Start with `--limit 50` when testing. This probes
tens of thousands of third-party domains over HTTP — run it from a host that
can absorb the traffic and identify itself.

### 6. Supplement unavailable URLs with web search

Set `OPENAI_API_KEY`, then preview without calling the API:

```bash
export OPENAI_API_KEY='...'
python as2web/web_search/main.py --date 20260301 \
  --as2domain_dir outputs/as2web/20260301 \
  --input_dir outputs/as2web/20260301 \
  --base_dir outputs/web_search --dry_run
```

Drop `--dry_run` to submit paid requests. The prompt is the `PROMPT_TEMPLATE`
constant in `as2web/web_search/main.py`. `--whois_dir outputs/whois` is optional and
only enables the previous-snapshot reuse heuristic; omit it and every target
ASN is queried fresh. `--model` selects the model (names drift; use a current
web-search-capable one).

### 7. Produce the final AS2Web mapping

```bash
python as2web/web_search/combine_as2web_results.py --date 20260301 \
  --as2domain_dir outputs/as2web --web_search_dir outputs/web_search \
  --scope_dir outputs/as2web --out_dir outputs/combined
```

`--scope_dir` locates both `final_as_scope.json` and
`as_centered_domain2source.json` (both written by stage 4). Final outputs:
`outputs/combined/DATE/as2web.json` and `as2web_detail.json` — the input to
AS2Biz.

---

# AS2Biz

Starts from the final AS2Web URL mapping. Code is in `as2biz/`; the
business-category prompt and taxonomy are in `as2biz/prompt.py`.

The final per-ASN category mapping merges five sources, in this order of
preference: website classification (`Direct - Website`), sibling-organisation
inheritance (`Inherit from AS…`), reuse of the previous snapshot when the RIR
registry identity is unchanged (`Reuse from {YYYY-MM}`), Wikipedia
(`Direct - Wikipedia`), and a last-resort web-search classifier
(`Direct - Fallback Web Search AI`).
`as2biz/post_process.py` (sub-commands `sibling` / `reuse` / `wiki` / `merge`)
orchestrates the merge, with the crawl and the batch/search steps run in between.

### 1. Crawl websites and preserve WARC files

```bash
python as2biz/scraper.py \
  --input outputs/combined/20260901/as2web.json \
  --archives-dir archives --version-tag 2026-09-01 \
  --concurrency 16 --durability-level balanced \
  --health-check-on-start true --post-run-health-check true
```

Writes the WARC store, per-version slices, and indexes below `archives/`. Use
`as2biz/check_scraper.py` to report progress.

### 2. Validate and repair the WARC archive

`auto_doctor.py` can delete and rebuild corrupt WARC files — run it only on a
copied or versioned archive.

```bash
python as2biz/warc_health_monitor.py \
  --archives-dir archives --version-tag 2026-09-01 \
  --state-json archives/warc_health_2026-09-01.json

python as2biz/auto_doctor.py \
  --archives-dir archives --version-tag 2026-09-01 \
  --input outputs/combined/20260901/as2web.json \
  --state-json archives/warc_health_2026-09-01.json
```

### 3. Website-classification batches

Set `OPENAI_API_KEY`. Build a preview first:

```bash
export OPENAI_API_KEY='...'
python as2biz/prepare_openai_batch_process.py \
  --as2web-json outputs/combined/20260901/as2web.json \
  --archives-dir archives --version-tag 2026-09-01 \
  --mode preview --preview-size 1000 --submit-batch false
```

`--prompt-contract auto` is the default: snapshots before 2026-09 use the
legacy full-category-name contract, while 2026-09 and later use the category-ID
contract. `--routing-mode` defaults to `off`: every prompt goes to `--model`,
which defaults to `gpt-5.6-terra` for the 2026-09 contract. Passing
`--routing-mode versioned` instead routes each unique prompt between GPT-5.6
Luna and Terra by a prior-snapshot policy; that path needs the previous
snapshot's `--prev-mapping` / `--prev-web-class`, or `--allow-no-prior true` to
send everything to Terra.

The built-in Batch prices, in USD per one million input/output tokens, are
Terra `2.00/12.00` (and Luna `0.20/1.20` when routing is enabled). The preview
prints a conservative cost ceiling. Check current pricing before submission and
override changed rates with `--model-price MODEL:INPUT:OUTPUT`.

Re-run with `--submit-batch true` to submit. Then use
`--download-results-only true --promote-routed true` to retrieve, validate, and
materialize `<routed-dir>/as2biz_main.json`, a per-ASN category map
`{ "<asn>": ["<category>", ...] }`. Preserve every batch artifact with the
dataset version.

### 4. Website + sibling-organisation merge, then previous-snapshot reuse

```bash
python as2biz/post_process.py sibling \
  --web-class <routed-dir>/as2biz_main.json \
  --scope outputs/as2web/20260901/final_as_scope.json \
  --iil-as2org inputs/IIL-AS2Org.2026-09.json \
  --out-dir result/2026-09

# optional: carry a previous snapshot's categories for ASNs whose RIR-whois
# identity (as-name, org, descr) is byte-identical between the two snapshots
python as2biz/post_process.py reuse \
  --out-dir result/2026-09 \
  --prev-as2biz inputs/as2biz.2026-03.json --prev-label 2026-03 \
  --whois-dir inputs/whois --prev-date 20260301 --curr-date 20260826
```

`sibling` applies sibling-org inheritance (`--iil-as2org` optional, see
`docs/01_inputs.md`) and writes `result/2026-09/fallback_as_list.json` — the
ASNs still unclassified. `reuse` then shrinks that list (writing
`reuse_based_as2biz.json` for the merge and `reuse_manifest.json` recording each
reused ASN's previous provenance) so the paid Wikipedia and web-search stages do
less work.

### 5. Wikipedia classification (optional)

```bash
python as2biz/wiki_fetch.py \
  --fallback-list result/2026-09/fallback_as_list.json \
  --as2orgname outputs/as2web/20260901/as2orgname.json \
  --out-dir result/2026-09/wikipedia --contact "you@example.org"

python as2biz/prepare_openai_batch_wiki.py \
  --wiki-json result/2026-09/wikipedia/wiki_info.json \
  --asn2brand-json result/2026-09/wikipedia/classifiable_as2brand.json \
  --output-batch-jsonl result/2026-09/wiki_batch_input.jsonl \
  --output-dir result/2026-09 --submit-batch true --wait

python as2biz/post_process.py wiki \
  --out-dir result/2026-09 \
  --wiki-results result/2026-09/wiki_results_by_asn.json \
  --as2orgname outputs/as2web/20260901/as2orgname.json \
  --asn2cc outputs/as2web/20260901/asn2cc.json
```

The `post_process.py wiki` step parses the batch results and writes the fallback
web-search input. Skip `--wiki-results` to run it without the Wikipedia pass.

### 6. Fallback web-search classification

For organisations still unclassified. Uses an LLM with a web-search tool and the
prompt in `as2biz/prompt.py` (`descr` + `fallback_web_search_prompt`).

```bash
export OPENAI_API_KEY='...'
python as2biz/fallback_openai_search.py --date 2026-09 --result_root result
```

### 7. Final merge

```bash
python as2biz/post_process.py merge --out-dir result/2026-09 --date 2026-09 \
  --web-classification-llm gpt-5.6-terra \
  --web-search-llm "gpt-5.2+web_search tool" \
  --wiki-classification-llm gpt-5.2
```

Writes `result/2026-09/as2biz.2026-09.json` — `{"metadata": {...}, "data":
{"<asn>": {"<category>": "<source>"}}}`. `metadata` records the three model
strings above plus per-source ASN counts (`total_ases`, `ases_from_*`). The
`--*-llm` values are copied verbatim into `metadata`; keep their wording stable
across snapshots.

### Utilities

`extract_text.py` (dump per-site text from WARC), `store_warc_health_check.py`
/ `slice_warc_health_check.py` (one-shot health checks), `fix_store_warcs.py` /
`rebuild_slice_tool.py` (repair primitives used by `auto_doctor.py`).

---

# Datasets

The datasets built with this code are released separately:

- AS2Web: <https://github.com/InetIntel/Dataset-AS2Web>
- AS2Biz: <https://github.com/InetIntel/Dataset-AS2Biz>

## License

Access to and use of the source code, documentation, and prompts in this
repository are subject to the [Acceptable Use Agreement and Software License](LICENSE).
That agreement does not cover the AS2Web or AS2Biz datasets, third-party input
data, or WARC archives; those materials are governed by their respective
licenses and acceptable-use terms.
