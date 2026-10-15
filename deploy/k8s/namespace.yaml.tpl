apiVersion: v1
kind: Namespace
metadata:
  name: ${NAMESPACE}
  labels:
    test-run: ${TEST_RUN_ID}
---
# Shared by the page service and the driver. The pipeline config pinned in each
# file's manifest at intake is built from these values, and the page service
# refuses a page whose file was split under another config (HTTP 409), so both
# sides read the same ConfigMap.
apiVersion: v1
kind: ConfigMap
metadata:
  name: parser-config
  namespace: ${NAMESPACE}
data:
  AWS_REGION: ${AWS_REGION}
  AWS_DEFAULT_REGION: ${AWS_REGION}
  PARSER_STORE_LOCATION: ${STORE_LOCATION}
  BEDROCK_VLM_MODEL: ${BEDROCK_VLM_MODEL}
  PARSER_ESCALATION_ENGINE: vlm
  PARSER_PAGE_MAX_ATTEMPTS: "3"
  PARSER_LOG_LEVEL: INFO
  # The kinds intake routes and the page service parses (comma list, from test.env):
  # every kind with a tested handler. Remove a kind to switch it off: restart the
  # page service (it then
  # refuses that kind with 409) and start new drivers (they fail its files with
  # unsupported_type at intake, writing no work).
  PARSER_ENABLED_KINDS: "${PARSER_ENABLED_KINDS}"
  # The page service kills a page's parse worker after this many seconds and
  # writes one final `timeout` record (HTTP 200). Not in the pipeline config:
  # changing it never re-parses finished pages, it only releases `timeout`
  # records stored under another value.
  PARSER_DEADLINE_PDF_PAGE_S: "600"
  # The same for one whole TXT document. Provisional until the corpus inventory
  # (sub-step 1) replaces it; read only once txt is in PARSER_ENABLED_KINDS.
  PARSER_DEADLINE_TXT_S: "60"
  # Raw size cap of a TXT file, in MB (1,000,000 bytes), checked by the driver at
  # intake: a larger file fails alone with too_large and writes no work.
  # Provisional until the corpus inventory.
  PARSER_MAX_TXT_MB: "50"
  # The same deadline and raw cap for one whole DOCX and one whole HTML document.
  # Provisional until the corpus inventory; read only once the kind is in
  # PARSER_ENABLED_KINDS (code complete, not enabled).
  PARSER_DEADLINE_DOCX_S: "300"
  PARSER_DEADLINE_HTML_S: "300"
  PARSER_MAX_DOCX_MB: "100"
  PARSER_MAX_HTML_MB: "50"
  # The same for one whole XLSX workbook. Provisional.
  PARSER_DEADLINE_XLSX_S: "300"
  PARSER_MAX_XLSX_MB: "50"
  # The same for one whole email: one deadline and one raw cap shared by eml and
  # msg (the email handler). Provisional until the corpus inventory; read only once
  # the kind is in PARSER_ENABLED_KINDS (code complete, not enabled).
  PARSER_DEADLINE_EMAIL_S: "120"
  PARSER_MAX_EMAIL_MB: "50"
  # The same for one image: one deadline and one raw cap shared by png and jpeg
  # (the image handler). An image is parsed like one scanned PDF page (OCR, then
  # escalation), so it starts with the PDF page's deadline. Provisional.
  PARSER_DEADLINE_IMAGE_S: "600"
  PARSER_MAX_IMAGE_MB: "50"
  # Image cost guard, checked by the page service before an image reaches the parse
  # worker (header only, nothing decoded): an image declaring more megapixels
  # (width x height x frames) is a final `too_complex` failure. Decoding costs about
  # 27 MB per megapixel in the worker. Provisional until the corpus inventory.
  PARSER_IMAGE_MAX_MEGAPIXELS: "50"
  # A DOCX, HTML or email document with an embedded image of at least this many
  # pixels (width x height) is flagged `images_not_ocrd` (images are not OCRed).
  # The flag never changes the unit's state; read by the parse worker.
  PARSER_IMAGES_MIN_PIXELS: "40000"
  # Cap on one single-unit document's markdown, in MB, enforced in the parse
  # worker: over it the unit is a final `output_too_large` failure and the text
  # never leaves the worker. PDF pages have no output cap. Provisional.
  PARSER_MAX_OUTPUT_MB: "20"
  # DOCX cost guard, checked by the page service before a DOCX reaches the parse
  # worker (no Docling). Over a ZIP limit (expanded total, largest part, the
  # expanded:compressed ratio overall and per part of 1 MiB or more, entry count),
  # or a stream that disagrees with the ZIP index, the unit is a final `zip_bomb`
  # failure; over a count in word/document.xml, a final `too_complex` one. Raising
  # a value releases the records stored under the old one. MB = 1,000,000 bytes.
  # Provisional until the corpus inventory; read only once docx is enabled.
  PARSER_DOCX_MAX_EXPANDED_MB: "300"
  PARSER_DOCX_MAX_PART_MB: "200"
  PARSER_DOCX_MAX_RATIO: "100"
  PARSER_DOCX_MAX_ENTRIES: "10000"
  PARSER_DOCX_MAX_PARAGRAPHS: "50000"
  PARSER_DOCX_MAX_TABLE_CELLS: "100000"
  # XLSX cost guard, checked by the page service before an XLSX reaches the parse
  # worker (no Docling): the same four ZIP limits as a DOCX (`zip_bomb`), then one
  # streamed pass over the worksheets. More cells, or a summed used-range area
  # (rows x columns of each sheet) over the grid limit, is a final `too_complex`.
  # Docling loads the whole workbook: ~2.7 KB per cell and ~0.2 KB per used-range
  # cell; two cells at A1 and XFD1048576 never finish. Provisional.
  PARSER_XLSX_MAX_EXPANDED_MB: "300"
  PARSER_XLSX_MAX_PART_MB: "200"
  PARSER_XLSX_MAX_RATIO: "100"
  PARSER_XLSX_MAX_ENTRIES: "10000"
  PARSER_XLSX_MAX_CELLS: "500000"
  PARSER_XLSX_MAX_GRID_CELLS: "5000000"
  # The driver's timeout for one page request. Both sides refuse to start unless
  # it is at least the largest parse deadline + 120 s (download, kill, record).
  PARSER_REMOTE_REQUEST_TIMEOUT_S: "900"
  # How long the page service waits for a new parse worker to load the models
  # and parse the warm-up page. Three failed starts in a row: the service exits.
  PARSER_WORKER_START_TIMEOUT_S: "300"
