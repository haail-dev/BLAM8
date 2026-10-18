# Signed-in Help investigation

`POST /concierge/help` supplies a request-bound investigator to `answer_help`.
Anonymous landing-page help and the business-action/voice loop do not acquire
these tools. This extends typed internal Help; it does not extend every Agent mode.

## Sources and boundaries

- `read_help_document` reads a complete indexed article from the running release's
  versioned product bundle. The prompt includes the index, avoiding a keyword-only
  top-six retrieval bottleneck. Historical workspace documents are not searched.
- `read_help_code` reads selected top-level functions/constants in the reviewed
  `help_investigation.EVIDENCE` map, with path, line and file hash. It cannot accept
  filenames, URLs, commands or arbitrary repository searches. `RENDER_GIT_COMMIT`
  identifies the release when present and valid; otherwise the release is unknown.
- `check_help_account` checks only the authenticated request's tenant/member.
  Module and permission checks cover the capability catalogue. Detailed checks
  currently cover Knowledge reviews, money send permission and configured mailbox
  count. Other configuration, provider health and write readiness remain unchecked.

Member lookup errors and missing identities fail closed. A successful lookup with
no member row follows the application's legacy-owner convention within the
authenticated tenant. Settings diagnostics and the existing private setup snapshot
require settings-change access outside View as. Inactive/restricted members cannot
cause these privileged reads. Diagnostics contain states and counts, not customer
names, mailbox addresses, credentials, Knowledge prose or pending-draft text.

Knowledge checks read the current correction flag and review context. Up to five
pending reviews have their retained evidence validated using the correction
executor's own loader (identity, hashes and expiry); any remaining reviews are
explicitly counted as unchecked. A missing/expired/invalid snapshot is distinct
from a failed database read. Successful storage reads do not certify write RPCs,
the full migration, or an eventual correction/admission. No mutation is probed.

## Execution and answer contract

The model may make at most six tool calls over three rounds, followed by a final
answer with tool use disabled. Each tool check has a ten-second wait bound; a
timed-out read may finish in its worker thread, but cannot execute a write or
publish a late receipt. Results are capped at 24,000 serialized characters.
All model calls use the existing provider adapter and one request scope; token
usage and receipts are aggregated across calls. Tool results and source comments
are untrusted evidence, not instructions.

Failed or permission-denied account checks use a deterministic explanation so
the model cannot turn an unverified check into a readiness claim. A partial
Knowledge check retains a verified service flag while reporting storage as
unknown. Incomplete model JSON is not shown as a finished answer; if the provider
fails after tools ran, completed-call usage and evidence receipts are retained.
Known Knowledge blockers also use a deterministic diagnostic: service enablement,
review-batch counts, combined missing/expired/invalid evidence and unchecked
write readiness keep their exact meanings even if model prose overstates them.

After any investigation tool call, setup changes, recommendations, held facts and
the no-dependants completion flag are suppressed deterministically. Normal setup
turns retain their existing draft/review flow. The response includes an `evidence`
array with tool/source IDs, status, timestamp and release; it never contains code
or account records. The chat answer explains findings and unresolved checks.
There is no new evidence-panel UI in this change.

## Maintenance and validation

Add evidence only by reviewing a specific symbol and adding its regression check;
do not expose a module wholesale. Keep the product-help guide and evidence map in
step with actual diagnostics. Run `tests/test_help_investigation.py`, the affected
Help/setup tests, the full agnostic guard and the product-maintenance gate.
Offline tests validate scope, failed reads, source loading, bounded tool execution,
usage accounting and suppression of unintended setup writes. They do not prove
natural-language correctness or production service health.

No database migration or new credential is required. Release through the normal
PR/deployment process. A code revert removes investigation tools; there are no
investigation-owned records to migrate or undo.
