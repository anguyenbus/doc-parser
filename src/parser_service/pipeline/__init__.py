"""
The page pipeline: split-first, per-unit parsing with resume and a fail-closed stitch.

Modules, in dependency order: ``config`` (states, config hash, limits), ``layout``
(store keys and record I/O), ``intake``, ``units``, ``local_runner``,
``remote_runner``, ``status`` (status and stitch), ``api`` (the entry points below).

``parse_file_in_store(source, location)`` (old name ``parse_pdf_pages_in_store``) is
the unit-level counterpart of ``parsing.document.parse_to_markdown``;
``parse_pdf_pages(source, work_root)`` is the same on a local directory:

  0. INTAKE  sniff the file's first bytes (``formats/sniff.py``) to route it to a kind.
             No kind, or a kind missing from ``PARSER_ENABLED_KINDS``, fails the
             file with ``unsupported_type``; a single-unit file over its raw cap
             fails with ``too_large``. Neither writes anything under ``work/``.
  1. SPLIT   a PDF: store ``source.pdf``, then its one-page files (pypdf), then
             ``manifest.json`` LAST (kind, unit, source hash, true page count,
             pinned pipeline config). A single-unit kind (txt, docx, html, eml,
             msg) is one unit, the file itself: store ``source.<ext>``, then the
             manifest (``unit`` ``document``, ``page_count`` 1). Routing after
             intake reads the manifest's ``kind``, never the file name.
  2. PARSE   each one-page file with the unchanged ``parse_to_markdown`` — Docling
             first, gate, escalation to VLM/Textract only when promoted — in a pool
             of worker processes. Each page writes ``results/NNNN.json`` (the page
             result and the commit marker; it holds the markdown) and then a
             readable copy, ``results/NNNN.md``, that the pipeline never reads.
  3. STATUS  ``status.json`` is rebuilt from the page records (never the source of
             truth). Every page is ``ok``, ``degraded``, ``failed`` or ``pending``.
  4. STITCH  only when EVERY page is ``ok``: ``output.md`` with a
             ``==== page {number} ===`` line before each page, plus ``output.json``.
             Anything else leaves the file ``failed`` with the page numbers and
             reasons in ``status.json`` and no ``output.md``. A partial document
             (``output.partial.md``, gaps marked) is written only on request.

Every read and write goes through a store (``store.py``): a local directory or an
S3 prefix. Restart is the normal path — there is no recovery mode. A page is
finished iff its result JSON exists, and a stored result is never overwritten
(first write wins). A re-run skips ``ok`` pages; a ``failed`` or ``degraded`` page
under ``max_attempts`` has its record moved to ``results/NNNN.attempt-K.json`` and
runs again. The attempt counter is written BEFORE each try, so a page that kills
its worker (no exception, no record) still becomes ``failed`` once the limit is
reached. A record also carries ``reason_code`` (the text of ``reason`` before the
first ``:``). A deterministic failure (``timeout`` and the other codes in
``FINAL_REASON_CODES``) stores the ``limits`` behind it and is final: it is never
re-run while those limits keep their values (``_is_final``).

Keys under the store location. ``work/`` is keyed by content and config (work id
= ``<source sha256>/<config hash>``), so the same bytes under any name are split
and parsed once per config, and a new config never mixes with an old one;
``out/`` is keyed by the named file (file id = ``<stem>-<sha256[:16]>``), so each
name gets its own status and output. ``work/`` is the part a lifecycle rule may
expire; ``out/`` is self-contained. ``published/`` (``publish.py``) is the
versioned hand-off to consumers:

    work/<work id>/source.<ext>                  copy of the original; written first. The
                                                 extension is fixed by the kind; a single-unit
                                                 kind's only unit is this file
    work/<work id>/pages/0001.pdf …              a PDF's one-page files (1-based)
    work/<work id>/manifest.json                 written last by the split
    work/<work id>/attempts/0001 …               attempt counter, written before each try
    work/<work id>/results/0001.json             the page's record; exists = page finished
    work/<work id>/results/0001.md               readable copy of the page's markdown
    work/<work id>/results/0001.attempt-1.json   an archived non-ok record
    out/<file id>/output.md                      only when every page is ok
    out/<file id>/output.json                    metadata for output.md
    out/<file id>/output.partial.md, .json       only on request, when not complete
    out/<file id>/status.json                    summary rebuilt from the records; written
                                                 LAST, after the outputs it reports
    published/v1/...                             on request: see ``publish.py``

A manifest with no ``kind`` (written before kinds existed) is a PDF of pages. Each
kind has its own pipeline config (``pipeline_config(kind)``, with the kind's
handler version), so each kind's work has its own config hash.
"""
