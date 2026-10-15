# parser_service — module architecture

How the code in `src/parser_service/` is organised: the layers and what each module
owns, how one file and one page move through it, and the rules that keep the import
graph clean. Edges were traced from the actual imports; the package has no import
cycles.

## 1. Layers

Each layer imports only from the layers below it.

```mermaid
graph TD
    subgraph svc["service/ — the KServe page service"]
        PRED["predictor.py<br/><i>KServe model: one unit per request</i>"]
        WORK["worker.py<br/><i>long-lived, killable parse process</i>"]
    end
    subgraph pipe["pipeline/ — split-first page pipeline"]
        API["api.py<br/><i>parse_file_in_store, parse_pdf_pages</i>"]
        RUN["local_runner.py · remote_runner.py<br/><i>the two executors</i>"]
        STAT["status.py<br/><i>status.json, fail-closed stitch</i>"]
        UNITS["units.py<br/><i>process a unit, write its record</i>"]
        INTAKE["intake.py<br/><i>sniff, refuse, split, manifest</i>"]
        LAYOUT["layout.py<br/><i>store keys, record I/O</i>"]
        CONF["config.py<br/><i>config hash, limits, final rule</i>"]
    end
    subgraph fmt["formats/ — what a file is and how to parse it"]
        SNIFF["sniff.py · guards.py"]
        HAND["handlers.py · pdf.py · result.py"]
    end
    subgraph prs["parsing/ — parse one unit"]
        DOC["document.py<br/><i>parse_to_markdown: Docling, gate, escalation</i>"]
        GATE["quality_gate.py · render.py · markdown.py<br/>elements.py · confidence.py · route_stats.py"]
        ENG["engines/ vlm.py · textract.py · retry.py"]
    end
    subgraph low["storage/ · delivery/ · settings · observability · version"]
        STORE["storage/store.py · storage/aws.py"]
        DEL["delivery/publish.py · delivery/jobs.py"]
    end

    PRED --> WORK
    PRED --> RUN
    WORK --> HAND
    API --> RUN
    API --> STAT
    API --> INTAKE
    RUN --> UNITS
    UNITS --> HAND
    UNITS --> LAYOUT
    INTAKE --> SNIFF
    INTAKE --> LAYOUT
    STAT --> LAYOUT
    LAYOUT --> CONF
    HAND --> DOC
    DOC --> GATE
    DOC --> ENG
    API --> DEL
    LAYOUT --> STORE
    DEL --> STORE
```

`legacy/` sits beside these layers: the element-JSON `parse()` for the eval harness
(`legacy/element_json.py`, exposed as `parser_service.parse` and imported only when first
used) and the single-node batch script's I/O (`legacy/batch_io.py`).

## 2. Modules

| Package | Module | Owns |
|---|---|---|
| `service/` | `predictor.py` | The KServe custom predictor: one unit per request; 429 busy, 503 while the worker restarts, 409 config or kind refused |
| | `worker.py` | The long-lived parse process holding the warm models; SIGKILLed and restarted at a unit's deadline |
| `pipeline/` | `api.py` | `parse_file_in_store` (old name `parse_pdf_pages_in_store`) and `parse_pdf_pages` |
| | `local_runner.py` | The `local` executor: a process pool on one machine |
| | `remote_runner.py` | The `kserve` executor: the driver's client, and the service's side of a request |
| | `units.py` | Process one unit, write its record (first write wins), archive a failed attempt for a retry |
| | `status.py` | Rebuild `status.json` from the records; stitch `output.md` only when every unit is ok |
| | `intake.py` | Sniff a file's kind, refuse it (`unsupported_type`, `too_large`), split a PDF, write the manifest |
| | `layout.py` | Every key under `work/` and `out/`, and the record / attempt / JSON helpers |
| | `config.py` | Unit and file states, the per-kind pipeline config and its hash, enabled kinds, caps, deadlines, the final-failure rule |
| `formats/` | `sniff.py` | A file's kind from its bytes (and the extension for txt and html) |
| | `guards.py` | The DOCX cost guards: ZIP index and streamed element count |
| | `handlers.py` | One handler per kind (pdf, docx, html, txt, eml, msg) and their versions |
| | `pdf.py` | One PDF page: blank-page probe, `parse_to_markdown`, `classify_page_result` |
| | `result.py` | The unit result contract: states, `Reply`, `exception_reply`, `apply_output_cap` |
| `parsing/` | `document.py` | `parse_to_markdown`: Docling first, the quality gate, escalation, Markdown |
| | `quality_gate.py` | Keep Docling's page, or promote it to the escalation engine |
| | `render.py` | Rasterise pages, count text-layer tokens, the scan fast-path probe |
| | `markdown.py` | Elements to Markdown |
| | `elements.py` | Docling items to eval-harness elements, the output skeleton, input size checks |
| | `confidence.py`, `route_stats.py` | Advisory confidence scores; the route vocabulary and CSV fields |
| | `engines/vlm.py`, `engines/textract.py`, `engines/retry.py` | The escalation engines (Bedrock Claude, Textract) and their bounded retry |
| `storage/` | `store.py`, `aws.py` | The store interface (local folder or S3 prefix); the S3 URI parser and client factory |
| `delivery/` | `publish.py`, `jobs.py` | Versioned `published/` documents and events; inbox jobs (`job.json`) |
| top level | `settings.py`, `observability.py`, `version.py` | Start-up validation of the environment; metrics and structured events; the package version |

## 3. Runtime flow: one file

```mermaid
flowchart LR
    F["file + job.json"] --> D["driver: pipeline/api.py"]
    D --> I["intake.py<br/>sniff kind, caps, split"]
    I -- "unsupported / too large" --> X["status failed<br/>failed event"]
    I --> M["manifest + units in work/"]
    M --> R["remote_runner.py<br/>one request per unit"]
    R --> P["service/predictor.py"]
    P --> W["service/worker.py<br/>(deadline)"]
    W --> H["formats/handlers.py<br/>pdf / docx / html / txt / email"]
    H --> REC["units.py writes the record"]
    REC --> S["status.py<br/>status.json; stitch if all ok"]
    S --> PUB["delivery/publish.py<br/>document.md, document.json, event"]
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

- **Imports point down the layers**, and the package has no import cycles.
- **Function-level imports are deliberate and few.** `parser_service.parse` loads the
  legacy parser on first use. The worker and the handlers import the Docling path
  only when they parse, so the page service's own process stays light (a test checks
  that it never loads Docling or torch). The legacy parser keeps its own.
- **Shared code has one public home.** A name used across packages is public (no
  leading underscore) and lives in the module that owns it: `formats/result.py`,
  `parsing/elements.py`, `storage/aws.py`, `version.py`. Inside `pipeline/`, the
  modules also share package-private helpers (`_run_pages`, `_stitch_markdown`, ...),
  which nothing outside the package should import.
- **Tests patch a name where it is looked up.** Patch the module that defines it. The
  one name patched across modules, `pipeline.layout._load_record_checked`, is called as
  `layout._load_record_checked` from the other pipeline modules, so the patch reaches
  every caller.
