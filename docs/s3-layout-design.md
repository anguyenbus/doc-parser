# S3 layout for the document parser

**Who should read this:** engineers who send documents to the parser, engineers who build
on its markdown (RAG ingestion), and anyone reviewing the design.

## In one paragraph

A producer drops a bundle of PDFs into `inbox/` with a `job.json` that names each
document. The parser splits every PDF into pages and parses them in `work/`. That area is a
private cache keyed by **what the bytes are**, not what the file is called. Each finished
document is published to `published/v1/` as an **immutable version**: the page markdown, the
whole document, and a metadata file. A `_manifest.json` is written **last**, and an event is
added to a log. Consumers read only `published/`, and they react only to `_manifest.json` or
to an event. Progress and failures for people are in `ops/`.

```
s3://<bucket>/parser/
├── inbox/<source_system>/<job_id>/        PRODUCERS write      the request
│   ├── *.pdf
│   └── job.json            ← written last
├── work/<sha256>/<config_hash>/           PARSER only          cache: pages and page results
├── out/<file_id>/                         PARSER only          per-file working output and status
├── published/v1/                          PARSER writes, CONSUMERS read: the contract
│   ├── docs/<doc_id>/<version>/
│   │   ├── pages/0001.md …
│   │   ├── document.md
│   │   ├── document.json
│   │   └── _manifest.json  ← written last
│   └── events/dt=YYYY-MM-DD/<doc_id>--<version>--<type>.json
└── ops/jobs/<job_id>/                     PARSER writes, PEOPLE read   job progress
    ├── docs/<doc_id>.json
    └── status.json
```

## The five areas at a glance

| Area | Written by | Read by | Can an object change? | Kept for |
|---|---|---|---|---|
| `inbox/` | producers | parser | no; a new request is a new job | until processed (retention rule) |
| `work/` | parser | parser | records: never (first write wins). Counters: yes | expire after ~30 days |
| `out/` | parser | parser, people debugging | yes, rebuilt on each run | while useful |
| `published/` | parser | **consumers** | **never** | as long as consumers need it |
| `ops/` | parser | people, dashboards | yes, latest state | while useful |

The split exists because the areas have different owners, readers and lifetimes. With
separate prefixes, one IAM policy (consumers: read `published/` only) or one lifecycle rule
(expire `work/`) applies to exactly one of them.

## A document's journey

Here is the bundle from the real run in the appendix: one document to parse and one to
delete.

1. **The producer uploads** `inbox/finance/audit-2026-10-08/Annual Report.pdf`, then writes
   `job.json` **last**. A half-uploaded bundle has no `job.json`, so it never starts.
2. **A driver pod starts per document.** It's a Kubernetes Indexed Job, and index *i*
   handles `docs[i]`.
3. **Split.** The driver computes the PDF's sha256 and the pipeline-config hash, then writes
   `work/1c7c8b7e…/70cd0b8e13c4/`: `source.pdf`, `pages/0001.pdf …`, and then `manifest.json`
   last.
4. **Parse.** Each page goes to the page service (KServe), which writes
   `results/NNNN.json` once. A page that is already there is skipped.
5. **Publish.** The driver writes `published/v1/docs/annual-report-2025/e3f75aa44487ff0f/`:
   the page files, `document.md`, `document.json`, and then `_manifest.json` last. It then
   writes the event `events/dt=2026-10-08/annual-report-2025--e3f75aa44487ff0f--published.json`.
6. **Record.** The driver writes `out/<file_id>/status.json` last for that file, then
   `ops/jobs/audit-2026-10-08/docs/annual-report-2025.json`, and rebuilds the job's
   `status.json`.
7. **The delete.** The second document has `"action": "delete"`. Its driver parses nothing.
   It writes the event `old-policy--audit-2026-10-08--deleted.json` and records `deleted`
   in `ops/`.
8. **The consumer** is notified about the `_manifest.json` (S3 → EventBridge → SQS) or reads
   the event log. It fetches the version and replaces every chunk of `annual-report-2025` in
   its index. For `old-policy` it purges every chunk.

Running any step again is safe. In the real run, running the document a second time parsed
nothing ("5 already done") and wrote **nothing** under `published/`. Only the internal
`out/` and `ops/` files were refreshed.

---

## `inbox/`: the request

```json
{
  "job_id": "audit-2026-10-08",
  "docs": [
    {"doc_id": "annual-report-2025", "file": "Annual Report.pdf",
     "metadata": {"title": "Annual report 2025"}},
    {"doc_id": "old-policy", "action": "delete"}
  ]
}
```

| Field | Rule |
|---|---|
| `job_id`, `doc_id` | lowercase `a-z 0-9 . _ -`, up to 128 characters, starting with a letter or digit, no `..`. They become S3 key segments, so there's no `/`, no spaces (S3 events URL-encode them) and no upper case (that would give one document two spellings). |
| `doc_id` | **The document's identity, chosen by the producer.** Send the same `doc_id` again with new bytes and it becomes a new version of the *same* document. |
| `file` | A PDF next to `job.json`, or a full path or `s3://` URI. `..` is refused. |
| `action` | `upsert` (the default) parses and publishes. `delete` publishes a `deleted` event. |
| `metadata` | Any JSON object, copied unchanged into `document.json` and the events. |

`job.json` is validated before any work starts. A bad job fails with exit code 1 and parses
nothing.

## `work/<sha256>/<config_hash>/`: the private cache

```
work/<sha256>/<config_hash>/
├── source.pdf                 copy of the original (written 1st)
├── pages/0001.pdf …           one-page PDFs (written 2nd)
├── manifest.json              written LAST by the split: page count, pinned config
├── attempts/0001 …            attempt counter, written BEFORE each try
└── results/
    ├── 0001.json              the page record: exists = page finished (first write wins)
    ├── 0001.md                readable copy, never read by the pipeline
    └── 0001.attempt-1.json    an earlier non-ok result, kept when the page was retried
```

**Why it's keyed by content and config.** Parsing is a function of two things: the bytes and
the pipeline config (parser version, escalation engine, VLM model, and so on). That pair is
the key.
- The same PDF under any name, or any `doc_id`, is parsed **once**.
- A **new model** gets a new `config_hash` and parses into its own directory. It never mixes
  with old results and is never refused. The old results stay untouched.
- The page service checks it: a page request names `work_id = <sha256>/<config_hash>`, and a
  pod running a different config answers **409**.

**Why it's safe to crash at any point.** Each step ends with a marker written last:

| Marker | Rule | What it buys |
|---|---|---|
| `manifest.json` | written after every page PDF | A crash mid-split leaves no manifest, so the split is simply redone. |
| `attempts/NNNN` | written *before* a page is parsed | A page that kills its pod (OOM, SIGKILL) is still counted, and ends `failed` at the limit instead of looping forever. |
| `results/NNNN.json` | write-if-absent (S3 `If-None-Match: *`) | Two pods racing on one page can't overwrite each other. Duplicate requests are harmless. Resume = "skip pages with an `ok` record". |

`source.pdf` is kept so pages that the lifecycle rule has expired can be rebuilt from it.

## `out/<file_id>/`: per-file working output

`file_id` = `<file name without .pdf>-<sha256[:16]>`, for example `Annual Report-1c7c8b7e9d9a6394`.

| File | When |
|---|---|
| `output.md` and `output.json` | only when every page is `ok` |
| `output.partial.md` / `.json` | only on request, when not complete |
| `status.json` | always. It's written **last**, after the outputs it reports, so it never says `complete` before `output.md` exists. |

`out/` is internal. It is rebuilt on every run, so a re-run rewrites it. Consumers should
not read it: they read `published/`.

## `published/v1/`: the contract

### Two ids: identity and content

| | `doc_id` | `version` |
|---|---|---|
| Means | *which* document | *which content of it* |
| Comes from | the producer (`job.json`), or the file name slugified for single-file runs | `sha256(source_sha256 + "/" + config_hash)[:16]` |
| Stays the same when | the document is corrected, re-uploaded or re-parsed | the same bytes are parsed with the same config |
| Changes when | never (it's the identity) | the bytes change, or the model or config changes |

A content hash can tell you that two files are the same. It can't tell you that a corrected
file *replaces* an older one. That's why identity comes from the producer and content comes
from a hash. With both, the consumer can replace old chunks when a new version arrives and
purge them on a delete.

### Version names

| Document state | Version | Example |
|---|---|---|
| every page ok | `<hash>` | `e3f75aa44487ff0f` |
| incomplete, published as partial (opt-in) | `<hash>.partial-NNNN` (NNNN = ok pages) | `e3f75aa44487ff0f.partial-0004` |
| incomplete, not published | `<hash>.failed-NNNN`: an event only, no files | `3ac615bc7b937695.failed-0002` |

Ok pages are never replaced. So when a repair adds pages, the result gets a new partial name,
and in the end the plain `<hash>`, the complete version. Versions are names, not a sequence.
To order them, use `published_at`.

### The files in a version

| File | Content |
|---|---|
| `pages/NNNN.md` | One ok page's markdown, without the page marker. Missing pages have no file. |
| `document.md` | The whole document. Before each page is a line `==== page N ===`; a missing page appears as `==== page N (missing: failed) ===` with no text. For a complete version it is byte-identical to `out/.../output.md`. |
| `document.json` | Ids, `job_id`, `source_name`, `source_sha256`, `work_id`, `page_count`, `coverage`, `missing_pages`, `config`, `config_hash`, `metadata`, `published_at`, and a per-page map (below). |
| `_manifest.json` | **Written last.** It lists every file above with its `sha256` and `bytes`. **If it exists, the version is complete. If it doesn't, ignore the version.** |

The per-page map in `document.json`:

```json
{"page": 2, "state": "ok", "route": "docling-kept", "confidence": 0.95,
 "char_span": [2780, 5827], "file": "pages/0002.md"}
```

- `char_span` is `[start, end)` into `document.md`, measured in **Unicode code points**
  (Python string indices), with `end` exclusive. The span starts at the page's marker line.
  Note that JavaScript and Java count UTF-16 code units, so convert before slicing text that
  contains characters outside the BMP.
- `route` tells you how the page was produced: `docling-kept`, `vlm`, `textract`, `blank`,
  and so on. A consumer can weight model-transcribed pages differently.

### Never changes once written

Every file under `docs/` is written with write-if-absent, so a version is written once and
never changed.
- A crash halfway through leaves a version **without** `_manifest.json`. The next run fills
  in the missing files and writes the manifest.
- Files that already exist win. The manifest's checksums are computed from the **stored**
  bytes, so they always match what a reader downloads.

### Incomplete documents

Fail closed is the default: a document with any page that isn't `ok` publishes **no** files,
only a `failed` event listing the missing pages and why. Nobody waits for a document that
isn't coming, and a missing page can never silently become "the document doesn't say that".

With `--publish-partial`, an incomplete document is published as a `partial` version with
`coverage` (ok pages ÷ real page count) and `missing_pages`. **The consumer decides** whether
to index partial versions. The parser only reports coverage honestly.

### The event log

`published/v1/events/dt=<YYYY-MM-DD>/<doc_id>--<version>--<type>.json`

| `type` | When | Points to |
|---|---|---|
| `published` | a complete version | its `_manifest.json` and `document.json` |
| `partial` | a partial version (opt-in) | the same |
| `failed` | an incomplete document, not published | the missing pages and reasons |
| `deleted` | `action: delete` in a job | (for a delete, the key's `<version>` slot holds the `job_id`) |

- Keys are **deterministic**. `dt` is the version's `published_at` date (UTC), so a re-run
  rewrites nothing and never duplicates an event. Two exceptions, harmless because the event
  means the same thing each time: a `failed` or `deleted` event re-run on a *later day* is
  logged again under that day.
- **Delivery** is S3 → EventBridge → one SQS queue per consumer, filtered on keys ending in
  `_manifest.json`. That's infrastructure, configured outside this repo.
- **The log exists for replay.** SQS keeps messages for 14 days at most. Backfilling a new
  index, or checking that nothing was missed, means reading the log. The `dt=` partitions
  make it queryable with Athena.

## `ops/jobs/<job_id>/`: progress for people

- `docs/<doc_id>.json`: one per document, written only by the driver that ran it, so drivers
  never overwrite each other. It holds `state` (`complete`, `failed`, `deleted` or `error`),
  page counts, the not-ok pages with reasons, the published version and a link to `status.json`.
- `status.json`: the job summary, rebuilt from those files after every document. Its states:
  `complete` (every document complete or deleted), `in_progress` (any document not yet run),
  or `failed`.
- **Coverage is counted against the real total.** `pages_total` and `coverage` stay `null`
  until **every** document has been split and its page count is known. A job is never
  "95% done" just because its biggest PDF hasn't been opened yet.

---

## The consumer contract

What the RAG team builds against:

1. **Trigger only on `_manifest.json`**, through the queue or the event log. Never list
   `docs/` to discover documents.
2. **Ignore any version without `_manifest.json`.** Check each file's sha256 against the
   manifest.
3. **Everything under `docs/<doc_id>/<version>/` is immutable.** Cache freely.
4. **Key the index on `(doc_id, version)`.** When a version arrives, replace every chunk of
   that `doc_id`. When a `deleted` event arrives, purge every chunk of that `doc_id`.
5. **Index `partial` versions deliberately**, after checking `coverage` and `missing_pages`.
6. **Cite pages** with `pages/NNNN.md` or `char_span`. Convert `char_span` from code points
   if your language counts UTF-16.
7. **Ignore unknown fields.** A breaking change gets a new `published/v2/` running in
   parallel, never an in-place change to `v1`.

## Guarantees and what enforces them

| Guarantee | Mechanism | Tested in |
|---|---|---|
| A consumer never sees a half-written version | `_manifest.json` written last; consumers trigger only on it | `test_publish.py::test_a_complete_file_is_published_with_the_manifest_written_last` |
| A published version never changes | write-if-absent on every file; manifest computed from stored bytes | `test_a_rerun_writes_nothing_new_under_published` |
| `status.json` never says complete too early | written after the outputs and the publish | same test (last write) |
| The same bytes are parsed once | `work/` keyed by sha256 | `test_page_pipeline_store.py::test_the_same_bytes_under_another_name_share_the_work_but_not_the_output` |
| A new model never mixes with old results | `config_hash` in the `work/` path; 409 from pods with another config | `test_page_pipeline.py::test_changed_config_parses_into_its_own_work_dir`, `test_kserve_predictor.py` |
| A new model gives a new version | `version` hashes the config | `test_publish.py::test_a_new_pipeline_config_is_a_new_version` |
| Lost pages can't hide | fail closed by default; partial versions carry `coverage` and `missing_pages`; job coverage stays `null` until every page count is known | `test_partial_versions_then_the_complete_version_after_repair`, `test_jobs.py::test_job_coverage_waits_for_every_page_count` |
| A page that kills its pod ends | attempts counted before the parse | `test_remote_executor.py::test_a_page_that_kills_every_pod_is_finalized_failed` |
| Deletes reach consumers | `deleted` event | `test_jobs.py::test_cli_runs_a_job_document_by_document` |

The full suite (469 tests, including the live S3 test) passes on this layout.

## Design decisions, and the alternatives we rejected

**"Why not use the content hash as the document id?"**
A hash identifies bytes, not documents. A corrected upload gets a new hash and looks like a
new document, so the old chunks stay searchable forever. We use the hash where it's right
(the `work/` cache key and the `version`) and the producer's `doc_id` for identity.

**"Why not have consumers watch `out/…/output.md`?"**
`out/` is rewritten on every run. A re-run would fire again and look like new content, and a
consumer could read `output.md` before `output.json` exists. `published/` exists precisely so
that consumers get an immutable version with a single marker written last.

**"Why not ship at a coverage threshold, say 95%?"**
In RAG, a missing page doesn't look like a gap. It produces a confident "the document doesn't
say that". So the parser never decides "good enough" on the consumer's behalf. It publishes
complete versions by default, and partial versions on request with `coverage` attached, so
the consumer applies the threshold it can defend. A repair re-runs only the pages not yet ok
and publishes a new version.

**"Why an event log in S3 *and* SQS? Isn't that two sources of truth?"**
There's one source of truth: the `_manifest.json` files. SQS is delivery, kept 14 days at
most. The log is for replay and reconciliation. Both are derived from the manifests. A
consumer that will never re-index could skip the log, but the first change of embedding model
forces a re-index.

**"Why per-page files *and* a whole document?"**
Chunkers that respect page boundaries, and citations, want pages. Readers and
long-context use want the whole document. Each is cheap, and `char_span` maps between them.

**"Why is the config in the `work/` path and not just in the manifest?"**
With the config only in the manifest, a model upgrade either mixes results or forces a
restart that throws the old ones away. In the path, old and new run side by side, and
switching back is free.

**"Why no readable date in the version?"**
A date would make re-runs on different days produce different versions, which breaks
idempotency. Order versions by `published_at`, which is in `document.json`, the manifest
and every event.

## Known limits

- **Pages are cached per file, not by page content.** A 4,000-page PDF with one changed page
  is parsed again in full. A page-content cache can be added under `work/`, but hashing a
  page correctly is subtle. A split page's bytes change when the PDF is re-saved, and a
  scanned page's content stream is the same on every page. So the cache key must include the
  page's images and fonts. Measure the hit rate before building it.
- **One writer per `work_id`.** Two jobs carrying identical bytes and parsed at the same time
  are two writers on one cache entry. Page records are safe (first write wins), but the
  retry archive step assumes a single writer.
- **Not built yet:** the EventBridge-to-SQS wiring, the lifecycle and retention rules
  (`work/` around 30 days, `inbox/` after processing), and the S3 trigger that starts a job
  when `job.json` lands. The scale test started jobs by hand.
- **The job summary is rebuilt by each driver from per-document files.** It's always correct
  after the last document's driver finishes. Before that it can briefly lag a document that
  finished at the same moment.
- **Data from the October scale test uses the old `work/<name>-<hash>/` layout** and won't
  resume under this one.

## Appendix: a real run

A 5-page excerpt run as a two-document job (one upsert, one delete), Docling only. Hashes
are shortened.

```
inbox/finance/audit-2026-10-08/{Annual Report.pdf, job.json}

work/1c7c8b7e…/70cd0b8e13c4/{source.pdf, manifest.json, pages/0001-0005.pdf,
                              attempts/0001-0005, results/0001-0005.{json,md}}
out/Annual Report-1c7c8b7e9d9a6394/{output.md, output.json, status.json}
published/v1/docs/annual-report-2025/e3f75aa44487ff0f/
    pages/0001.md … 0005.md, document.md, document.json, _manifest.json
published/v1/events/dt=2026-10-08/annual-report-2025--e3f75aa44487ff0f--published.json
published/v1/events/dt=2026-10-08/old-policy--audit-2026-10-08--deleted.json
ops/jobs/audit-2026-10-08/{docs/annual-report-2025.json, docs/old-policy.json, status.json}
```

`_manifest.json` (two files shown):

```json
{
  "schema_version": 1,
  "doc_id": "annual-report-2025",
  "version": "e3f75aa44487ff0f",
  "state": "complete",
  "published_at": "2026-10-08T11:14:53.863855+00:00",
  "files": [
    {"path": "pages/0001.md", "sha256": "bd57076a…", "bytes": 2764},
    {"path": "document.json", "sha256": "b8d2860d…", "bytes": 1789}
  ]
}
```

The `published` event:

```json
{
  "type": "published",
  "schema_version": 1,
  "doc_id": "annual-report-2025",
  "version": "e3f75aa44487ff0f",
  "job_id": "audit-2026-10-08",
  "source_name": "Annual Report.pdf",
  "page_count": 5,
  "coverage": 1.0,
  "missing_pages": [],
  "manifest": "s3://…/published/v1/docs/annual-report-2025/e3f75aa44487ff0f/_manifest.json",
  "metadata": {"title": "Annual report 2025"},
  "at": "2026-10-08T11:14:53.863855+00:00"
}
```

`ops/jobs/audit-2026-10-08/status.json` (without the per-document list):

```json
{
  "job_id": "audit-2026-10-08",
  "state": "complete",
  "docs_total": 2,
  "docs_by_state": {"complete": 1, "deleted": 1},
  "pages_total": 5,
  "pages_ok": 5,
  "coverage": 1.0
}
```

## Where it lives in the code

| Piece | File |
|---|---|
| `work/` and `out/` layout, split, page records, status, stitch | `src/parser_service/page_pipeline.py` |
| `published/` versions, manifest, events, ids | `src/parser_service/delivery/publish.py` |
| `job.json` and `ops/jobs/` | `src/parser_service/delivery/jobs.py` |
| The driver CLI (`--job`, `--publish`, `--publish-partial`, `--doc-id`) | `scripts/parse_pages.py` |
| The page service (checks `work_id`, answers 409 for another config) | `src/parser_service/service/predictor.py` |
| The Kubernetes driver Job (`--job=…`) | `deploy/k8s/driver-job.yaml.tpl` |
