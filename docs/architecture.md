# parser_service — module architecture

How the modules in `src/parser_service/` relate. Two views: a **dependency graph**
(who imports whom) and a **runtime flow** (how one page moves through the system).
Edges below were traced from the actual intra-package imports.

## 1. Module dependency graph

```mermaid
graph TD
    subgraph entry["Entry points"]
        MP["markdown_pipeline.py<br/><i>parse_to_markdown()<br/>→ clean Markdown for RAG</i>"]
        PS["parser_service.py<br/><i>parse_document()<br/>→ element JSON</i>"]
        INIT["__init__.py<br/><i>package exports</i>"]
    end

    subgraph decide["Decision & rendering"]
        QG["quality_gate.py<br/><i>evaluate_page() → keep / promote</i>"]
        RENDER["render.py<br/><i>rasterize, text_layer_tokens,<br/>scan-fastpath probe</i>"]
        MD["markdown.py<br/><i>elements → markdown string</i>"]
    end

    subgraph engines["Escalation engines"]
        VLM["vlm_client.py<br/><i>call_vlm() — Bedrock Claude</i>"]
        TX["textract_client.py<br/><i>analyze_page() — Textract OCR</i>"]
        RETRY["retry.py<br/><i>bounded backoff +<br/>transient detection</i>"]
    end

    subgraph telem["Telemetry & scoring"]
        CONF["confidence.py<br/><i>page/document_confidence()</i>"]
        RS["route_stats.py<br/><i>route vocabulary + CSV fields</i>"]
    end

    subgraph io["I/O helpers"]
        IOL["io_layer.py<br/><i>input load / size caps</i>"]
    end

    INIT --> PS
    MP --> QG
    MP --> RENDER
    MP --> MD
    MP --> VLM
    MP --> TX
    MP --> CONF
    MP -. "_empty_output / source" .-> PS
    PS --> QG
    PS --> RENDER
    PS --> VLM
    CONF --> RS
    VLM --> RETRY
    TX --> RETRY

    subgraph ext["External services / libs"]
        DOCLING(["Docling<br/>DocumentConverter"])
        BEDROCK(["AWS Bedrock<br/>Claude Sonnet 4.6"])
        TEXTRACT(["AWS Textract"])
        PYPDF(["pypdf / pypdfium2"])
    end

    MP --> DOCLING
    RENDER --> PYPDF
    VLM --> BEDROCK
    TX --> TEXTRACT

    classDef entryC fill:#1e3a5f,stroke:#4a90d9,color:#fff
    classDef engineC fill:#5f1e3a,stroke:#d94a90,color:#fff
    classDef extC fill:#333,stroke:#888,color:#ddd
    class MP,PS,INIT entryC
    class VLM,TX,RETRY engineC
    class DOCLING,BEDROCK,TEXTRACT,PYPDF extC
```

### Module roles

| Module | Role | Depends on (intra-package) |
|---|---|---|
| **markdown_pipeline.py** | The orchestrator — Docling-first → escalation → Markdown. The hub. | `quality_gate`, `render`, `markdown`, `vlm_client`, `textract_client`, `confidence`, + `parser_service` (source / empty-output) |
| **parser_service.py** | The older JSON entry point (element JSON output) | `quality_gate`, `render`, `vlm_client` |
| **quality_gate.py** | Decides `keep` vs `promote_to_vlm` per page | *(leaf)* |
| **render.py** | Rasterizes pages, counts text tokens, scan-fastpath image probe | *(leaf; uses pypdf / pypdfium2)* |
| **vlm_client.py** | Bedrock Claude escalation engine | `retry` |
| **textract_client.py** | Textract escalation engine | `retry` |
| **retry.py** | Shared exponential-backoff + transient-error helper | *(leaf)* |
| **confidence.py** | Advisory page/doc confidence scoring | `route_stats` (route vocabulary) |
| **route_stats.py** | Route-name constants + CSV telemetry schema | *(leaf; also consumed by scripts)* |
| **markdown.py** | Renders elements → Markdown text | *(leaf)* |
| **io_layer.py** | Input loading / byte-size handling | *(leaf)* |

> Note: `markdown_pipeline` only *mentions* `route_stats` in a comment — the CSV is
> actually written by `scripts/parse_batch.py`, which imports `route_stats.FIELDNAMES`.

## 2. Runtime flow (one page)

```mermaid
flowchart TD
    A["PDF / DOCX / image"] --> B["markdown_pipeline.parse_to_markdown()"]
    B --> FP{"PARSER_SCAN_FASTPATH<br/>all pages scanned?<br/><i>render.classify_for_fastpath</i>"}
    FP -- yes --> ESC
    FP -- no --> C["Docling convert()"]
    C --> D["quality_gate.evaluate_page()"]
    D -- keep --> KEEP["use Docling markdown<br/>route = docling-kept"]
    D -- promote --> ESC{"PARSER_ESCALATION_ENGINE"}
    ESC -- vlm --> V["render page → image<br/>vlm_client.call_vlm()<br/>→ Bedrock Claude"]
    ESC -- textract --> T["textract_client.analyze_page()<br/>→ Textract"]
    V --> ARB["arbitration:<br/>keep the better of<br/>engine vs Docling"]
    T --> ARB
    KEEP --> AGG["assemble Markdown<br/>+ page_routes telemetry"]
    ARB --> AGG
    AGG --> SC["confidence.document_confidence()<br/>→ advisory score + warnings"]
    SC --> OUT["{ markdown, page_routes,<br/>warnings, confidence }"]

    style B fill:#1e3a5f,stroke:#4a90d9,color:#fff
    style ESC fill:#5f1e3a,stroke:#d94a90,color:#fff
    style OUT fill:#1e5f3a,stroke:#4ad990,color:#fff
```

## Mental model

`markdown_pipeline` is the conductor. It leans on `render` + `quality_gate` to decide
*whether* a page needs help, calls one of two engines (`vlm_client` / `textract_client`,
both hardened by `retry`) when it does, then scores the whole result with `confidence`
(which speaks the route vocabulary defined in `route_stats`). `parser_service` is the
parallel JSON-output path sharing the same gate / render / VLM building blocks.
