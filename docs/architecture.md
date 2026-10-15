# parser_service — module architecture

How the code in `src/parser_service/` is organised: the layers and what each module
owns, how one file and one page move through it, and the rules that keep the import
graph clean. The dependencies below were computed from the actual imports: there are
no import cycles between modules or between packages.

## 1. Layers

A package imports only from the layers below it (not only the next one down).

```mermaid
%%{init: {"flowchart": {"wrappingWidth": 560}}}%%
flowchart TB
    L1["<b>1 · service/</b> — the KServe page service<br/>predictor.py · worker.py"]
    L2["<b>2 · pipeline/</b> — split-first page pipeline<br/>api.py · local_runner.py · remote_runner.py · status.py<br/>units.py · intake.py · runner.py · layout.py · config.py"]
    L3["<b>3 · settings.py</b> — start-up validation"]
    L4["<b>4 · formats/</b> — what a file is, how to parse it<br/>sniff.py · guards.py · handlers.py · pdf.py · image.py · result.py"]
    L5["<b>5 · parsing/</b> — parse one unit<br/>document.py · quality_gate.py · render.py · markdown.py<br/>elements.py · confidence.py · route_stats.py<br/>engines/ vlm.py · textract.py · retry.py"]
    L6["<b>6 · foundation</b><br/>storage/ store.py · aws.py — delivery/ publish.py · jobs.py<br/>observability.py · version.py"]

    L1 --> L2 --> L3 --> L4 --> L5 --> L6
```

Inside `pipeline/` the modules follow the same rule: `config` ← `layout`, `runner` ← `intake`,
`units` ← the runners and `status` ← `api`.

## 2. Modules

| Package | Module | Owns |
|---|---|---|
| `service/` | `predictor.py` | The KServe custom predictor: one unit per request; 429 busy, 503 while the worker restarts, 409 config or kind refused |
| | `worker.py` | The long-lived parse process holding the warm models; SIGKILLed and restarted at a unit's deadline |
| `pipeline/` | `api.py` | `parse_file_in_store` (old name `parse_pdf_pages_in_store`) and `parse_pdf_pages` |
| | `local_runner.py` | The `local` executor: a process pool on one machine |
| | `remote_runner.py` | The `kserve` executor: the driver's client, and the service's side of a request |
| | `status.py` | Rebuild `status.json` from the records; stitch `output.md` only when every unit is ok |
| | `units.py` | Process one unit, write its record (first write wins), archive a failed attempt for a retry |
| | `intake.py` | Sniff a file's kind, refuse it (`unsupported_type`, `too_large`), split a PDF, write the manifest |
| | `runner.py` | The contract between the pipeline and a parse runner: `Request`, `ParseRunner`, the worker errors |
| | `layout.py` | Every key under `work/` and `out/`, and the record / attempt / JSON helpers |
| | `config.py` | Unit and file states, the per-kind pipeline config and its hash, enabled kinds, caps, deadlines, the final-failure rule |
| top level | `settings.py` | Start-up validation of the environment for the scripts and the service |
| `formats/` | `sniff.py` | A file's kind from its bytes (and the extension for txt and html) |
| | `guards.py` | The cost guards: DOCX (ZIP index, streamed element count), XLSX (ZIP index, streamed cell and used-range count) and images (declared megapixels, header only) |
| | `handlers.py` | One handler per kind (pdf, docx, xlsx, html, txt, eml, msg, png, jpeg) and their versions |
| | `pdf.py` | One PDF page: blank-page probe, `parse_to_markdown`, `classify_page_result` |
| | `image.py` | One PNG or JPEG, parsed as one page: blank probe, `parse_to_markdown`, the PDF page's classification |
| | `result.py` | The unit result contract: states, `Reply`, `exception_reply`, `apply_output_cap` |
| `parsing/` | `document.py` | `parse_to_markdown`: Docling first, the quality gate, escalation, Markdown |
| | `quality_gate.py` | Keep Docling's page, or promote it to the escalation engine |
| | `render.py` | Rasterise pages, count text-layer tokens, the scan fast-path probe, fit an input image to the engines' limits |
| | `markdown.py` | Elements to Markdown |
| | `elements.py` | Docling items to eval-harness elements, the output skeleton, input size checks |
| | `confidence.py`, `route_stats.py` | Advisory confidence scores; the route vocabulary and CSV fields |
| | `engines/vlm.py`, `engines/textract.py`, `engines/retry.py` | The escalation engines (Bedrock Claude, Textract) and their bounded retry |
| `storage/` | `store.py`, `aws.py` | The store interface (local folder or S3 prefix); the S3 URI parser and client factory |
| `delivery/` | `publish.py`, `jobs.py` | Versioned `published/` documents and events; inbox jobs (`job.json`) |
| top level | `observability.py`, `version.py` | Metrics and structured events; the package version |

## 3. Runtime flow: one file

The driver pod (`scripts/parse_pages.py`, one per file) does intake and bookkeeping;
the page-service pods do all parsing and write each unit's record.

```mermaid
flowchart TB
    subgraph DRV["driver pod · scripts/parse_pages.py"]
        direction TB
        A["pipeline/api.py<br/>parse_file_in_store"] --> I["pipeline/intake.py<br/>sniff kind, caps, split"]
        I -- "unsupported or too large" --> X["status.json failed<br/>failed event"]
        I --> M["manifest and units<br/>in work/"]
        M --> R["pipeline/remote_runner.py<br/>send each unit not yet ok"]
        S["pipeline/status.py<br/>status.json; stitch if all ok"] --> P["delivery/publish.py<br/>document.md, document.json, event"]
    end
    subgraph SVC["page-service pod"]
        direction TB
        PR["service/predictor.py"] --> PPR["remote_runner.process_page_request<br/>config and kind checks"]
        PPR --> U["pipeline/units.py<br/>attempt counter, cost guards"]
        U --> W["service/worker.py<br/>parse under the deadline"]
        W --> H["formats/handlers.py<br/>pdf, docx, xlsx, html, txt, eml, msg, png, jpeg"]
        U --> REC["record in work/results/"]
    end
    R -- "POST /v1/models/page-parser:predict" --> PR
    REC -. "read back" .-> S
```

## 4. Runtime flow: one PDF page

```mermaid
flowchart TD
    A["one-page PDF"] --> BL{"blank page?<br/><i>formats/pdf.py</i>"}
    BL -- yes --> OK["ok, route blank"]
    BL -- no --> B["parsing/document.py<br/>parse_to_markdown()"]
    B --> FP{"PARSER_SCAN_FASTPATH and<br/>all pages scanned?"}
    FP -- yes --> ESC
    FP -- no --> C["Docling convert()"]
    C --> G["quality_gate.evaluate_page()"]
    G -- keep --> KEEP["Docling markdown<br/>route docling-kept"]
    G -- promote --> ESC{"PARSER_ESCALATION_ENGINE"}
    ESC -- vlm --> V["engines/vlm.py<br/>Bedrock Claude"]
    ESC -- textract --> T["engines/textract.py<br/>Textract"]
    V --> ARB["optional arbitration:<br/>engine vs Docling"]
    T --> ARB
    KEEP --> CL["classify_page_result()<br/>ok / degraded / failed"]
    ARB --> CL
```

## 5. Rules that keep the structure

- **Imports point down the layers**, between packages and between modules. There are
  no import cycles at either level.
- **Contracts live with the layer that uses them.** The pipeline defines what it
  needs from a parse runner (`pipeline/runner.py`); the service implements it. The
  unit result contract (`formats/result.py`) sits below both.
- **Function-level imports are deliberate and few.** The worker and the handlers
  import the Docling path only when they parse, so the page service's own process
  stays light (a test checks that it never loads Docling or torch).
- **Shared code has one public home.** A name used across packages is public (no
  leading underscore) and lives in the module that owns it: `formats/result.py`,
  `pipeline/runner.py`, `parsing/elements.py`, `storage/aws.py`, `version.py`. Inside
  `pipeline/`, the modules also share package-private helpers (`_run_pages`,
  `_stitch_markdown`, ...), which nothing outside the package imports.
- **Tests patch a name where it is looked up.** Patch the module that defines it. The
  one name patched across modules, `pipeline.layout._load_record_checked`, is called as
  `layout._load_record_checked` from the other pipeline modules, so the patch reaches
  every caller.
