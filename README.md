# Trade Group Membership Signal

## Problem statement

Sales teams targeting nonprofits need structured data about what organizations exist, what sector they operate in, and how professionally active they are. That intelligence is publicly available, but it tends to be scattered across trade association member directories behind pagination, JavaScript rendering, and hidden JSON APIs. Often, there is no clean, centralized database you can download.

This pipeline scrapes nonprofit trade association member directories, normalizes every member into a consistent schema, and verifies a sample against ProPublica's Nonprofit Explorer — turning fragmented public web pages into structured rows a sales or research team can act on immediately.

## Value

Trade group membership is a segmentation signal. An organization that pays dues to a trade association has declared its sector identity, demonstrated operational maturity, and opted into a professional community. For a sales or research team, that is more useful than a raw list of nonprofits — it narrows the universe to organizations that are active, self-identified, and reachable through a shared context.

A pipeline like this, run regularly across a set of associations, would let a team:
- Segment outreach by sector (land conservation, grantmaking, independent schools, etc.)
- Prioritize orgs that belong to multiple associations as higher-signal prospects
- Track membership changes over time as a signal of organizational growth or decline
- Cross-reference EINs against financial data (Form 990s, ProPublica) to add revenue and size context

## Why this approach

**Track choice.** Option 2 was chosen over Option 1 for two reasons.

First, trade group member directories are more structured and stable data sources than conference attendee lists, which are fragmented across sponsor pages, LinkedIn posts, press releases, and agendas. Each of these would require a different extraction strategy with limited coverage guarantees. 

Second, membership reflects an ongoing organizational commitment — dues are paid annually, membership is actively maintained — so the data ages more gracefully than a conference attendee list, which is a snapshot tied to a single event.

**Source selection.** Land Trust Alliance was chosen as the primary source because its member directory presented an interesting technical challenge: the page appears static but is actually powered by a hidden Algolia search API discovered through browser DevTools network inspection. Rather than brute-force scraping the rendered HTML, the pipeline calls the API directly — more reliable and closer to how the data actually moves. 

Council on Foundations was chosen as the second source deliberately, as a contrast: it is a straightforward static HTML directory. It looks alphabetical at first, but upon closer inspection, it actually shows all the members in a single page. 

Together, the two sources validate that the config-driven architecture handles fundamentally different scraping patterns without code changes.

**Config-driven design.** All source-specific details — URLs, selectors, pagination type, field mappings — live in `sources.yaml`. Adding a new association requires only a new config block, not new code. This was a deliberate choice to keep the scraper reusable and reviewable.

**Fuzzy matching for verification.** Trade association directories and ProPublica rarely use identical org names. Exact matching would produce false negatives. `rapidfuzz.token_sort_ratio` handles word-order variation and minor naming differences, with a score threshold of 80 and an ambiguity gap of 5 points to flag close calls rather than silently picking the wrong match.

## MVP

A command-line pipeline that scrapes a configured trade association member directory and outputs a structured CSV.

**What was shipped:**
- `run.py` — CLI entry point (`--source`, `--out`, `--verify-sample`)
- `scraper.py` — config-driven scraper supporting five pagination patterns: Algolia API, paginated, alphabetical, single-page static HTML, and JS-rendered (Playwright fallback)
- `verifier.py` — ProPublica Nonprofit Explorer lookup with fuzzy name matching and ambiguity logging
- `sources.yaml` — configuration for two associations
- Two output files in `output/`:
  - `land-trust-alliance.csv` — 137 records scraped via Algolia API
  - `council-on-foundations.csv` — 1,162 records scraped via static HTML

**Verification sample:** 15 orgs from each source (30 total) were verified against ProPublica. Results are written into the same CSV alongside the scraped records.

**What was not exercised:** The `paginated`, `alphabetical`, and `js_rendered` scraper paths are implemented but were not used by either of the two sources scraped. They are untested against real-world targets.

**What was not built:** async scraping, a database backend, deduplication across sources, or a scheduler. All were deliberate omissions for a V1.

## Methodology

**Scraping.** `run.py` reads the source key from the CLI, passes it to `scraper.py`, which loads the matching config block from `sources.yaml` and routes to the appropriate scraper implementation based on `pagination_type`.

- **`algolia_api`** (Land Trust Alliance): POSTs to the Algolia multi-index query endpoint with an empty search string to retrieve all records. Paginates using `nbPages` from the response until all pages are exhausted.
- **`single_page`** (Council on Foundations): GETs the directory URL, parses the HTML with BeautifulSoup + lxml, and extracts members using CSS selectors defined in config.
- **`paginated`**, **`alphabetical`**, **`js_rendered`**: Implemented for numeric pagination, letter-by-letter traversal, and Playwright headless rendering respectively — not exercised by the current two sources.

For every member element found, the name is extracted using a configurable `name_selector`. Missing or empty names are skipped with a warning log. Each valid name is normalized into the output schema via `_make_record`.

Rate limiting is handled with exponential backoff (max 3 retries) on 429 and 503 responses, plus a configurable delay between page requests.

**Verification.** A random sample of N records is passed to `verifier.py`. For each org name, the pipeline queries the ProPublica Nonprofit Explorer search API and scores every result using `rapidfuzz.token_sort_ratio` against a normalized version of the scraped name. Normalization strips punctuation, lowercases, and removes common suffixes (Inc, LLC, Corp, Assoc, etc.) to reduce false negatives from naming convention differences.

**Best-match selection.** The top-scoring result is accepted if its score is >= 80. If the second-best result is within 5 points of the best, the match is flagged as ambiguous in the logs — the top result is still picked, but the close call is recorded for transparency. If no result clears 80, the org is marked `verified_nonprofit = false` with empty EIN and name fields.

## Tools and tech

| Library / Service | Purpose | Why |
|---|---|---|
| `requests` | HTTP GET and POST | Lightweight, sufficient for all non-JS sources |
| `beautifulsoup4` + `lxml` | HTML parsing | Fast and reliable for static HTML; lxml is faster than Python's built-in parser |
| `playwright` | JS-rendered page fallback | Handles sources that require a real browser; lazy-imported so the pipeline doesn't crash if not installed |
| `pyyaml` | Config loading | Keeps source definitions in a human-readable, editable format |
| `pandas` | CSV output | Clean DataFrame-to-CSV export with one line |
| `rapidfuzz` | Fuzzy name matching | `token_sort_ratio` handles word-order variation and minor naming differences without requiring exact matches |
| ProPublica Nonprofit Explorer API | Org verification | Free, no API key required, returns EIN and canonical name |
| Algolia API | LTA member data | Discovered via DevTools — more reliable than scraping rendered HTML |

**AI tools.** Claude Code was used throughout the build. It was most useful for debugging — particularly fixing the retry logic, the `nbPages` pagination bug, and the verifier's normalization — followed by helping investigate and confirm the Algolia API structure on the LTA site, and generating the initial boilerplate for the three main modules.

## Cost, scale, feasibility

**Current runtime.** Both sources scrape in under a minute on a standard machine. Static HTML (Council on Foundations) is faster — a single GET request followed by local parsing. The Algolia API (Land Trust Alliance) requires multiple paginated POST requests with delays between each, making it slower despite returning structured data.

**Scaling to 50 trade groups.** The main cost is time, not money. A rough estimate across 50 associations — assuming a mix of static HTML, hidden APIs, and a handful of JS-rendered sources:

| Pagination type | Estimated time per source | Estimated share |
|---|---|---|
| Static HTML (single page or paginated) | ~1–3 minutes | ~65% |
| Algolia / hidden API | ~2–5 minutes | ~15% |
| JS-rendered (Playwright) | ~5–10 minutes | ~20% |

Note: estimates assume sources significantly larger than the two scraped here — LTA (137 records, ~7 API pages) and CoF (1,162 records, single HTML page) both complete in under a minute.

Rough total: **2–4 hours per weekly run** on a single machine. Parallelizing across sources (e.g. `concurrent.futures`) could possibly lower this.

**Infrastructure cost.** The pipeline has no external service dependencies beyond ProPublica (free, no key) and the source sites themselves. A cron job (scheduled task) on a small cloud VM (~$5–10/month) would be sufficient for weekly runs. Playwright requires a real browser binary, so serverless functions with tight memory limits are not a good fit for JS-heavy sources.

**What would need to change for production scale:**
- A database backend (SQLite for solo use, PostgreSQL for a team) to deduplicate across runs and track membership changes over time
- Alerting when a selector breaks or a source returns unexpectedly few records
- Parallelization for speed
- Selector maintenance as association sites update their layouts — the most likely ongoing cost

## Limitations

**LTA directory includes non-nonprofits.** The Land Trust Alliance affiliates directory lists Professional Affiliates (law firms, consultancies), Government Affiliations, and State Association Affiliates alongside actual nonprofit members. The scraper collects all of them — the `role` field distinguishes them, but downstream filtering would be needed if the goal is nonprofits only.

**ProPublica coverage gaps.** `verified_nonprofit = false` does not mean an org is not a nonprofit. ProPublica's Nonprofit Explorer only includes organizations legally required to file Form 990 with the IRS — meaning small nonprofits below the filing threshold and churches are excluded by design. A false result means no confident match was found, not that the org is not a legitimate nonprofit.

**Selector and credential fragility.** CSS selectors and Algolia API credentials are defined in `sources.yaml`. If a site redesigns its layout or rotates its API keys, the scraper will break silently or noisily depending on the failure mode. There is no alerting or automatic detection of schema changes.

**Scraper coverage.** The `paginated`, `alphabetical`, and `js_rendered` scraper implementations were written to cover common real-world patterns but were not exercised against actual sources during this build. More broadly, the five pagination types in the current implementation will not cover every association site — login-gated directories, infinite scroll, or custom search forms would require new scraper implementations.

**Fuzzy match threshold is a heuristic.** The score threshold of 80 and ambiguity gap of 5 were chosen based on observed results, not a systematic calibration. Edge cases — orgs with very short names, acronyms, or doing-business-as names — are more likely to produce false positives or false negatives.

## Example output

Ten rows across both sources, including verified and unverified results:

| scraped_org_name | source_type | source_name | role | source_url | verified_nonprofit | propublica_ein | propublica_org_name |
|---|---|---|---|---|---|---|---|
| Friends of Discovery Park | trade_group | Land Trust Alliance | Nonprofit Affiliate | https://landtrustalliance.org/resources/connect/affiliates | true | 911409342 | Friends Of Discovery Park |
| Heart of the Rockies Initiative | trade_group | Land Trust Alliance | State Association Affiliate | https://landtrustalliance.org/resources/connect/affiliates | true | 463635624 | Heart Of The Rockies Initiative |
| Kansas Alliance For Wetlands And Streams | trade_group | Land Trust Alliance | Nonprofit Affiliate | https://landtrustalliance.org/resources/connect/affiliates | true | 43783861 | Kansas Alliance For Wetlands And Streams Inc |
| BackOffice Thinking, LLC | trade_group | Land Trust Alliance | Professional Affiliate | https://landtrustalliance.org/resources/connect/affiliates | false | | |
| City of Missoula Open Space Program | trade_group | Land Trust Alliance | Government Affiliation | https://landtrustalliance.org/resources/connect/affiliates | false | | |
| Community Foundation of Brazoria County, Texas | trade_group | Council on Foundations | member | https://cof.org/member-directory/non-members | true | 760427068 | Community Foundation Of Brazoria County Texas |
| Community Foundation of North Florida, Inc. | trade_group | Council on Foundations | member | https://cof.org/member-directory/non-members | true | 593473384 | Community Foundation Of North Florida Inc |
| Central Minnesota Community Foundation | trade_group | Council on Foundations | member | https://cof.org/member-directory/non-members | false | | |
| Franklin County Foundation | trade_group | Council on Foundations | member | https://cof.org/member-directory/non-members | true | 471986298 | Franklin County Bar Foundation |
| Oklahoma City Community Foundation, Inc. | trade_group | Council on Foundations | member | https://cof.org/member-directory/non-members | true | 237024262 | Oklahoma City Community Foundation Inc |

The `BackOffice Thinking, LLC` and `City of Missoula Open Space Program` rows illustrate the LTA limitation noted above — not all directory members are nonprofits. The `Franklin County Foundation` → `Franklin County Bar Foundation` match is a fuzzy match edge case worth flagging: the score cleared the threshold but the canonical name suggests it may be a false positive.