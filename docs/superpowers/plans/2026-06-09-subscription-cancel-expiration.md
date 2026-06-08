# Subscription Cancel Expiration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a documented subscription expiration flow for `cancel_scheduled` subscriptions that pass `currentPeriodEnd/cancelAt` and become `canceled`.

**Architecture:** Keep `docs-data/documentation.json` as the source of truth. Expand the existing `subscription-cancel` sequence page with a second D2 diagram instead of adding a new page. Use the existing generator to refresh HTML and diagram artifacts.

**Tech Stack:** Python `unittest`, JSON documentation data, `scripts/generate_docs.py`, D2 diagram source and SVG artifacts.

---

## File Structure

- Modify `tests/test_generate_docs.py`: extend the existing real documentation test for subscription cancel flow so it fails until the new expiration diagram exists.
- Modify `docs-data/documentation.json`: update the `subscription-cancel` sequence page, keep the existing reservation diagram focused on `active -> cancel_scheduled`, and add a new `subscription-cancel-expiration` diagram for `cancel_scheduled -> canceled`.
- Regenerate `subscription-cancel-sequence.html`: generated HTML for the existing cancel sequence page.
- Regenerate `diagrams/subscription-cancel-subscription-cancel-end-of-period.d2`: generated D2 for the existing reservation diagram after removing the final one-line expiration branch.
- Create `diagrams/subscription-cancel-subscription-cancel-expiration.d2`: generated D2 for the new expiration diagram.
- Create `diagrams/subscription-cancel-subscription-cancel-expiration.svg`: rendered SVG for the new expiration diagram when `d2` is available.

## Task 1: Add Failing Coverage

**Files:**
- Modify: `tests/test_generate_docs.py`

- [ ] **Step 1: Extend the existing cancel-flow test**

Replace the body of `test_real_documentation_includes_subscription_cancel_flow` assertions after `sequence = ...` with:

```python
            diagram = (out_dir / "diagrams" / "subscription-cancel-subscription-cancel-expiration.d2").read_text(encoding="utf-8")

            self.assertIn("POST /subscriptions/{subscriptionId}/cancel", detail)
            self.assertIn("해지 예약", detail)
            self.assertIn("기간 종료 시 해지 예약", sequence)
            self.assertIn("해지 예약 만료 처리", sequence)
            self.assertIn("cancel_scheduled", sequence)
            self.assertIn("cancel_scheduled -&gt; canceled", sequence)
            self.assertIn("currentPeriodEnd &lt;= now", sequence)
            self.assertIn("재구독", sequence)
            self.assertIn("subscription-cancel-subscription-cancel-expiration.d2", sequence)
            self.assertIn("만료 대상 구독 조회", diagram)
            self.assertIn("currentPeriodEnd <= now", diagram)
            self.assertIn("최종 종료 상태 저장", diagram)
            self.assertIn("구독 최신 상태 조회", diagram)
```

- [ ] **Step 2: Run the focused test to verify it fails**

Run:

```bash
python -m unittest tests.test_generate_docs.GenerateDocsTest.test_real_documentation_includes_subscription_cancel_flow
```

Expected: `ERROR` or `FAIL` because `diagrams/subscription-cancel-subscription-cancel-expiration.d2` does not exist yet, or because the new phrases are missing.

- [ ] **Step 3: Commit the failing test**

Run:

```bash
git add tests/test_generate_docs.py
git commit -m "test: cover subscription cancel expiration docs"
```

Expected: commit succeeds with only `tests/test_generate_docs.py` staged.

## Task 2: Add Expiration Diagram Data

**Files:**
- Modify: `docs-data/documentation.json`

- [ ] **Step 1: Update the `subscription-cancel` summary and API IDs**

In the `subscription-cancel` sequence object, change:

```json
"summary": "회원이 기간 종료 시 해지 예약을 요청하면 현재 이용 기간은 유지하고 다음 정기 결제 대상에서 제외하는 흐름입니다.",
"apiIds": [
  "subscriptions-cancel",
  "subscriptions-resume"
],
```

to:

```json
"summary": "회원이 기간 종료 시 해지 예약을 요청하면 현재 이용 기간은 유지하고, 기간 종료 이후 만료 배치가 구독을 canceled로 최종 마감하는 흐름입니다.",
"apiIds": [
  "subscriptions-cancel",
  "subscriptions-resume",
  "subscriptions-me",
  "internal-billing-run"
],
```

- [ ] **Step 2: Keep the first diagram focused on reservation**

In the `subscription-cancel-end-of-period` diagram, remove this step from `steps`:

```json
{
  "type": "branch",
  "label": "기간 종료 시 구독 종료 처리",
  "code": "cancel_scheduled -> canceled",
  "note": "currentPeriodEnd 이후 접근 권한 비활성화",
  "theme": "neutral"
}
```

Also remove this row from that diagram's `stateSummary`:

```json
{
  "event": "기간 종료 후",
  "subscriptionState": "canceled",
  "paymentState": "paid",
  "description": "구독이 종료되고 서비스 접근 권한이 비활성화됩니다."
}
```

- [ ] **Step 3: Add the new expiration diagram after `subscription-cancel-end-of-period`**

Insert this object as the second item in the `subscription-cancel` sequence's `diagrams` array:

```json
{
  "id": "subscription-cancel-expiration",
  "title": "해지 예약 만료 처리",
  "description": "cancel_scheduled 상태의 구독이 currentPeriodEnd 또는 cancelAt을 지난 뒤 최종 canceled 상태로 마감되고, 이후 회원 화면에서는 재개가 아니라 재구독을 안내하는 흐름입니다.",
  "actorIds": [
    "scheduler",
    "server",
    "client"
  ],
  "relatedApiIds": [
    "internal-billing-run",
    "subscriptions-me"
  ],
  "steps": [
    {
      "type": "message",
      "from": "scheduler",
      "to": "server",
      "label": "만료 대상 구독 조회",
      "apiId": "internal-billing-run",
      "note": "status=cancel_scheduled, currentPeriodEnd <= now 또는 cancelAt <= now"
    },
    {
      "type": "self",
      "from": "server",
      "label": "대상 구독 잠금 및 상태 재검증",
      "code": "status == cancel_scheduled && currentPeriodEnd <= now",
      "note": "기간 종료 전이거나 이미 active/canceled로 바뀐 구독은 제외"
    },
    {
      "type": "branch",
      "label": "최종 종료 상태 저장",
      "code": "cancel_scheduled -> canceled",
      "note": "canceledAt=now, accessUntil=currentPeriodEnd, nextBillingDate=null 유지",
      "theme": "neutral"
    },
    {
      "type": "self",
      "from": "server",
      "label": "권한 회수 및 감사 로그 저장",
      "code": "entitlement disabled, auditLog.cancel_expired",
      "note": "상태 전이와 권한 회수는 같은 트랜잭션 경계에서 처리"
    },
    {
      "type": "branch",
      "label": "알림 작업 예약",
      "code": "template=subscription_canceled_after_period",
      "note": "알림 실패는 구독 상태를 되돌리지 않고 재시도 큐 또는 운영 로그에 남김",
      "theme": "neutral"
    },
    {
      "type": "message",
      "from": "client",
      "to": "server",
      "label": "구독 최신 상태 조회",
      "apiId": "subscriptions-me",
      "note": "만료 후에는 resumeAvailable=false, 재구독 CTA 표시"
    },
    {
      "type": "message",
      "from": "server",
      "to": "client",
      "label": "종료 상태와 재구독 안내 반환",
      "code": "status=canceled, resumeAvailable=false, resubscribeUrl",
      "note": "subscriptions-resume으로 되돌릴 수 없고 신규 구독 또는 재구독으로 안내"
    }
  ],
  "stateSummary": [
    {
      "event": "기간 종료 전",
      "subscriptionState": "cancel_scheduled",
      "paymentState": "paid",
      "description": "회원은 currentPeriodEnd까지 접근할 수 있고 해지 예약 철회가 가능합니다."
    },
    {
      "event": "만료 배치 처리 후",
      "subscriptionState": "canceled",
      "paymentState": "paid",
      "description": "구독이 최종 종료되고 접근 권한이 회수되며 정기 과금 대상에 다시 포함되지 않습니다."
    },
    {
      "event": "이후 구독 조회",
      "subscriptionState": "canceled",
      "paymentState": "paid",
      "description": "회원 화면은 재개 버튼 대신 신규 구독 또는 재구독 안내를 표시합니다."
    }
  ]
}
```

- [ ] **Step 4: Validate JSON formatting**

Run:

```bash
python -m json.tool docs-data/documentation.json > /tmp/documentation-json-check.json
```

Expected: command exits `0` and prints no error.

## Task 3: Verify Data Makes the Test Pass

**Files:**
- Modified by previous tasks: `docs-data/documentation.json`
- Test: `tests/test_generate_docs.py`

- [ ] **Step 1: Run the focused test**

Run:

```bash
python -m unittest tests.test_generate_docs.GenerateDocsTest.test_real_documentation_includes_subscription_cancel_flow
```

Expected: `OK`.

- [ ] **Step 2: Run all generator tests**

Run:

```bash
python -m unittest tests.test_generate_docs
```

Expected: `OK`.

- [ ] **Step 3: Commit the documentation data change**

Run:

```bash
git add docs-data/documentation.json
git commit -m "docs: add subscription cancel expiration flow"
```

Expected: commit succeeds with only `docs-data/documentation.json` staged.

## Task 4: Regenerate HTML and Diagram Artifacts

**Files:**
- Modify: `subscription-cancel-sequence.html`
- Modify: `diagrams/subscription-cancel-subscription-cancel-end-of-period.d2`
- Create: `diagrams/subscription-cancel-subscription-cancel-expiration.d2`
- Create: `diagrams/subscription-cancel-subscription-cancel-expiration.svg`

- [ ] **Step 1: Regenerate documentation with D2 rendering**

Run:

```bash
python scripts/generate_docs.py --data docs-data/documentation.json --out . --render-d2
```

Expected: output includes `subscription-cancel-sequence.html`. If `d2` is unavailable, install or enable the same D2 toolchain used by this repository before continuing; the final artifact set must include the new SVG.

- [ ] **Step 2: Inspect generated cancel page**

Run:

```bash
python -m unittest tests.test_generate_docs.GenerateDocsTest.test_real_documentation_includes_subscription_cancel_flow
```

Expected: `OK`.

- [ ] **Step 3: Verify generated D2 contains the new flow**

Run:

```bash
rg -n "해지 예약 만료 처리|currentPeriodEnd <= now|재구독" subscription-cancel-sequence.html diagrams/subscription-cancel-subscription-cancel-expiration.d2
```

Expected: matches appear in both `subscription-cancel-sequence.html` and `diagrams/subscription-cancel-subscription-cancel-expiration.d2`.

- [ ] **Step 4: Commit generated artifacts**

Run:

```bash
git add subscription-cancel-sequence.html diagrams/subscription-cancel-subscription-cancel-end-of-period.d2 diagrams/subscription-cancel-subscription-cancel-end-of-period.svg diagrams/subscription-cancel-subscription-cancel-expiration.d2 diagrams/subscription-cancel-subscription-cancel-expiration.svg
git commit -m "docs: regenerate subscription cancel expiration artifacts"
```

Expected: commit succeeds with generated HTML and diagram artifacts only.

## Task 5: Final Verification

**Files:**
- Test: `tests/test_generate_docs.py`
- Generated artifact: `subscription-cancel-sequence.html`
- Generated artifact: `diagrams/subscription-cancel-subscription-cancel-expiration.d2`

- [ ] **Step 1: Run the full test suite**

Run:

```bash
python -m unittest
```

Expected: all tests pass with `OK`.

- [ ] **Step 2: Check git status**

Run:

```bash
git status --short
```

Expected: only unrelated pre-existing workspace changes remain. There should be no unstaged changes for `tests/test_generate_docs.py`, `docs-data/documentation.json`, `subscription-cancel-sequence.html`, or the new subscription cancel expiration diagram files.

- [ ] **Step 3: Summarize implementation**

Report these points:

```text
Implemented subscription cancel expiration as a second diagram inside the existing subscription cancel page.
Final subscription state remains canceled.
Verified with python -m unittest.
```

---

## Self-Review

- Spec coverage: The plan covers the existing-page approach, `cancel_scheduled -> canceled`, scheduler target selection, entitlement removal, idempotent exclusion of non-target states, `subscriptions-me` post-expiration behavior, and tests.
- Placeholder scan: No unfinished-marker text or undefined later work remains.
- Type consistency: Diagram IDs, file names, API IDs, and status values match the generator naming convention and existing documentation data.
