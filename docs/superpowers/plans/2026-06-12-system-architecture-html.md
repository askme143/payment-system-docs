# System Architecture HTML Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a generated `system-architecture-doc.html` page that documents the email notification outbox architecture before backend implementation.

**Architecture:** Keep `docs-data/documentation.json` as the source of truth and extend `scripts/generate_docs.py` to render a new first-class architecture page. Generate ordinary D2 node-link diagrams for architecture views while preserving the existing sequence diagram pipeline.

**Tech Stack:** Python documentation generator, JSON Schema, static HTML, D2, `unittest`.

---

### Task 1: Documentation Contract Tests

**Files:**
- Modify: `tests/test_generate_docs.py`

- [ ] **Step 1: Add tests for the new page contract**

Add tests that assert `systemArchitecture` exists, `system-architecture-doc.html` is generated, architecture D2 files are generated, top links include the new page, and the page contains the expected notification terms.

- [ ] **Step 2: Run focused tests and verify failure**

Run: `python3 -m unittest tests.test_generate_docs.GenerateDocsTest.test_system_architecture_page_is_generated tests.test_generate_docs.GenerateDocsTest.test_system_architecture_d2_files_are_generated -v`

Expected before implementation: failures mentioning missing `systemArchitecture` or missing generated files.

### Task 2: JSON Data And Schema

**Files:**
- Modify: `docs-data/documentation.json`
- Modify: `docs-data/schema/documentation.schema.json`

- [ ] **Step 1: Add `site.pages.systemArchitecture`**

Add:

```json
"systemArchitecture": {
  "title": "결제 시스템 아키텍처 문서",
  "file": "system-architecture-doc.html"
}
```

- [ ] **Step 2: Add `systemArchitecture` content data**

Add an architecture section with two diagrams:

- `email-notification-outbox`
- `email-delivery-lifecycle`

Include component, data store, and operations sections with the terms `notification_outbox`, `notification_templates`, `worker`, `retry_scheduled`, and `dead_letter`.

- [ ] **Step 3: Extend JSON Schema**

Add root `systemArchitecture`, add `systemArchitecture` to allowed `site.pages` properties, and define architecture section shapes for diagrams, nodes, edges, components, data stores, and operations.

### Task 3: Generator Support

**Files:**
- Modify: `scripts/generate_docs.py`

- [ ] **Step 1: Add architecture validation**

Validate architecture diagram ids, node references, and required section fields.

- [ ] **Step 2: Add architecture D2 rendering**

Generate D2 source for architecture node-link diagrams under:

```text
diagrams/system-architecture-email-notification-outbox.d2
diagrams/system-architecture-email-delivery-lifecycle.d2
```

- [ ] **Step 3: Add architecture HTML rendering**

Render `system-architecture-doc.html` with page-local navigation, D2 blocks, component responsibility table, data ownership table, and operations section.

- [ ] **Step 4: Add global navigation link**

Include the architecture page in top links across generated pages.

### Task 4: Generate Outputs And Verify

**Files:**
- Generate: `system-architecture-doc.html`
- Generate: `diagrams/system-architecture-email-notification-outbox.d2`
- Generate: `diagrams/system-architecture-email-delivery-lifecycle.d2`
- Possibly generate SVG files if D2 CLI is available.
- Modify generated existing HTML files through the generator.

- [ ] **Step 1: Regenerate docs**

Run: `python3 scripts/generate_docs.py --data docs-data/documentation.json --out .`

- [ ] **Step 2: Run tests**

Run: `python3 -m unittest tests/test_generate_docs.py`

- [ ] **Step 3: Render SVGs if available**

Run: `command -v d2 && python3 scripts/generate_docs.py --data docs-data/documentation.json --out . --render-d2 || true`

- [ ] **Step 4: Inspect generated page**

Open or inspect `system-architecture-doc.html` and verify the architecture diagrams, tables, and top links render as expected.

### Task 5: Cleanup And Commit

**Files:**
- Modify: `.gitignore`

- [ ] **Step 1: Ignore visual companion artifacts**

Add `.superpowers/` to `.gitignore`.

- [ ] **Step 2: Check git status**

Run: `git status --short`

- [ ] **Step 3: Commit**

Commit the plan and generated documentation changes with:

```bash
git add .
git commit -m "docs: add system architecture page"
```
