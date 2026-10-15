# Test fixtures

Most fixtures are synthetic, and the tests that use them build them at test time. The few
committed binaries are listed here when where they came from is not obvious.

## `email_plain_and_html.msg`

This is a small synthetic Outlook message (9,216 bytes). `tests/test_email_handler.py` uses it
(spec sub-step 6, task 6.1). Nothing in this project's dependency set can write `.msg`, so the
file was made once, by hand, on 2026-10-14.

- **Writer.** The minimal MS-CFB/MS-OXMSG writer `ato_cases.renderers.outlook.render_msg` from
  the author's separate synthetic-data project, "ato-cases". That writer emits a plain-text body
  only. A one-off script therefore wrapped its property stream to add three
  properties:
  - `PidTagHtml` (0x1013, binary): the HTML body;
  - `PidTagInternetCodepage` (0x3FDE) = 65001: the HTML body is UTF-8;
  - `PidTagAttachMimeTag` (0x370E) = `text/plain`: on the attachment.
- **Content.** All of it is invented. No real person or mailbox appears in it.
  - Subject: `Synthetic fixture: plain and HTML bodies`.
  - From: `Fixture Sender <sender@example.invalid>`.
  - To: `Fixture To <to@example.invalid>`.
  - Cc: `Fixture Cc <cc@example.invalid>`.
  - Sent: 2026-10-14 09:30 UTC.
  - Plain body: holds `PLAIN-BODY-MARKER`.
  - HTML body: holds `HTML-ONLY-MARKER` and a 2x2 table.
  - One by-value attachment, `notes.txt` (`text/plain`, 47 bytes). Its content holds
    `ATTACHMENT-CONTENT-MARKER`.
- **Check.** python-oxmsg 0.0.2 reads the subject, sender, both recipients (one To, one Cc), the
  sent date, both bodies and the attachment.

The test asserts these values. If you replace the file, keep the markers and update the test.

## `slides.pptx`

A one-slide PowerPoint deck (about 28 KB) made with python-pptx on 2026-10-15: a title
("Quarterly review") and one line of text, all invented. The page pipeline has no kind for
PPTX, so the tests use it as the file that intake refuses with `unsupported_type`.
