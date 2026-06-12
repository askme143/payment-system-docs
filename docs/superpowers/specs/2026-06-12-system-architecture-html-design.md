# System Architecture HTML Documentation Design

## Context

The repository generates its published HTML documentation from `docs-data/documentation.json` through `scripts/generate_docs.py`. Sequence pages already produce D2 source files and optional SVG render targets under `diagrams/`.

Before implementing the email notification outbox, the documentation should gain an architecture-level page that explains how the email queue, worker, templates, payment jobs, MongoDB, and SMTP delivery relate to each other.

## Decision

Add a new standalone generated HTML page:

- Page key: `systemArchitecture`
- File: `system-architecture-doc.html`
- Title: `결제 시스템 아키텍처 문서`

This page becomes a first-class documentation page alongside the sequence index, API catalog, API details, MongoDB document, and risk register.

## Scope

The first version focuses on the email notification outbox architecture. It should still be named broadly enough to host more system diagrams later.

Included in the first version:

- A top-level architecture diagram showing:
  - Payment API and subscription jobs
  - MongoDB state collections
  - `notification_outbox`
  - `notification_templates`
  - notification worker
  - SMTP email adapter
  - recipient mailbox
- A delivery lifecycle diagram or section showing:
  - enqueue
  - claim
  - render
  - send
  - sent, retry, and dead-letter outcomes
- A component responsibility table.
- A data ownership table for outbox and template collections.
- A short operations section describing worker summaries, retry behavior, and dead-letter visibility.

Excluded from the first version:

- Admin UI for retrying dead-letter items.
- Full C4 coverage of the entire payment backend.
- Runtime deployment manifests.

## Documentation Model

Extend `docs-data/documentation.json` with a new `site.pages.systemArchitecture` entry.

Add a new top-level data section such as `systemArchitecture` with:

- `title`
- `summary`
- `diagrams`
- `components`
- `dataStores`
- `operations`

The generator should validate and render this section when present. The schema should be extended so tests can catch missing required fields.

## Diagram Format

Use D2 for the generated architecture diagrams. D2 is already used by the current documentation pipeline, and its official documentation describes it as a declarative language for diagrams that fits generated architecture documentation well.

Unlike existing sequence diagrams, architecture diagrams should use ordinary D2 node-link diagrams rather than `shape: sequence_diagram`.

Generated assets:

- `diagrams/system-architecture-email-notification-outbox.d2`
- `diagrams/system-architecture-email-delivery-lifecycle.d2`
- SVG files for those diagrams when `--render-d2` is used and the D2 CLI is available.

## Page Layout

Render the page using the existing `page()`, `hero()`, `nav()`, `top_links()`, and D2 block styles where possible.

Navigation:

- Add `시스템 아키텍처 문서` to the global top links.
- Include the page in related top links from sequence, API, database, and risk pages.
- Add page-local navigation for architecture sections.

The page should stay technical and scannable, matching the existing generated docs.

## Architecture Content

The first architecture diagram should communicate this path:

1. Payment and subscription application flows enqueue notification intent.
2. Mongo persists business state, `notification_outbox`, and `notification_templates`.
3. Notification worker claims due outbox items with a processing lock.
4. Worker resolves and renders the product-specific template.
5. SMTP adapter sends the email.
6. Worker records `sent`, `retry_scheduled`, or `dead_letter`.

The lifecycle section should make clear that email failure does not roll back payment, invoice, subscription, or audit state.

## Error Handling In Documentation

The generated page should explicitly document these failure paths:

- Duplicate enqueue is suppressed by deterministic `idempotency_key`.
- Transient send failures schedule retries with backoff.
- Permanent failures move to `dead_letter`.
- Expired worker locks allow another worker to claim stale `processing` items.
- Template resolution failure is permanent until template data is fixed.

## Testing

Update documentation tests to verify:

- The new page key exists in `docs-data/documentation.json`.
- `system-architecture-doc.html` is generated.
- D2 files for architecture diagrams are generated.
- Global top links include the architecture page.
- Architecture page sections contain the expected outbox, template, worker, retry, and dead-letter terms.

Run:

- `python3 scripts/generate_docs.py --data docs-data/documentation.json --out .`
- `python3 -m unittest tests/test_generate_docs.py`

If the D2 CLI is available, also run:

- `python3 scripts/generate_docs.py --data docs-data/documentation.json --out . --render-d2`

## Review Notes

This design keeps generated HTML as an output artifact. The source of truth remains the JSON data model and the Python generator.
