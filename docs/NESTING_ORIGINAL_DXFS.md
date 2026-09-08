# Original DXFs for saved nests

Open **Material nesting → Team drafts → Original DXFs** beside the saved revision you want to document. The panel always names that revision and its saved input fingerprint. Attaching evidence does not open or replace the working nest, change its current edits, or modify an earlier saved input. New visits to Material nesting still start empty.

## Attach originals explicitly

1. Select an existing saved revision. If the nest has not been saved, save it explicitly first.
2. Choose up to **100 original DXF files**, each nonempty and strictly smaller than **5,000,000 bytes**. Files are hashed locally at selection; nothing is uploaded yet. The byte fingerprint includes the original BOM, line endings and non-ASCII bytes.
3. Review the matching saved profiles. One original can produce several profiles across material groups. Choose the exact profiles to bind. Matching uses original-byte SHA-256, not filenames, dimensions or reported revision text. A repeated filename with different bytes is not a match. A duplicate selection of the same bytes is reported rather than uploaded twice in one batch.
4. Select **Attach selected originals**. Files upload sequentially. The browser paces write starts at least 800 milliseconds apart to leave headroom under the existing API rate limit; a large batch may take several minutes. Each row retains its own immutable request identity and receipt. Successful earlier files remain retained if a later file fails.
5. Review each result. **Original bytes retained** means the server read back the stored bytes and verified their SHA-256 and length before committing the receipt and all chosen bindings. It does not approve the contours, reported customer revision, manufacturing readiness or physical material.

Profiles with missing provenance or `utf8-text` hashes are ineligible for exact-original binding. Attaching bytes does not upgrade those declarations. Reimporting the original and explicitly saving another nest revision is a separate action. No import, save, history view or page entry automatically uploads original CAD.

## Recover an interrupted attachment

The server first records an immutable intent. Every storage write then gets its own audited, fresh attempt key before bytes are sent to storage. The application never overwrites a previous attempt. At most eight attempts are recorded for an intent; completion records one receipt with all selected bindings. Pending attempts are retained without a purge path in this increment.

If a response is lost, select **Check attachment** or **Check and finish attachment**. This asks the server to verify recorded attempts without resending the file. An existing completed receipt can be recovered even when the previous reply was lost. If verification cannot complete, the UI does not automatically resend bytes. Reselect the matching original if necessary, check the attachment, then explicitly choose **Resend original**. That action preserves the original intent and rechecks prior attempts before any new storage attempt is allocated.

A rate-limit response pauses the batch immediately, preserves its exact commands and remaining selections, and displays the server's Retry-After wait (60 seconds if unavailable). **Resume paused batch** is an explicit action after that wait. There is no automatic content retry loop.

**Stop after this file** stops before starting the next file. It does not roll back a request already submitted. Leaving the panel warns when local selections or uncertain operations remain. Local File handles are not persisted; committed intents can be found by explicitly reopening that saved revision. A company or user change aborts the local operation and discards late responses. The backend checks current authority at every write; only the original actor and credential with write permission can resume an intent.

## History and download

History is paged ten intents at a time and contains pending intents as well as completed receipts. A reported attempt count is the latest fetched count. Refresh reads the database; it neither verifies storage nor creates writes. Another newer nest revision does not invalidate attachment to this explicitly selected historical revision.

**Download original** uses an authenticated endpoint. The server checks the full bounded stored byte length and hash before sending an attachment; the browser also checks against the receipt before offering the file. A missing, corrupted or inaccessible object fails download. It does not erase the earlier receipt or imply that the original verification never happened. Public object URLs and storage keys are not exposed.

Viewing requires effective `purchasing:view`; attaching and resuming additionally require effective `purchasing:create`, normal company/read-only restrictions and current authentication. These existing permission names are retained for the saved-nest backend. Server capabilities determine which actions are available. No inventory balance, reservation, valuation, remnant credit or machine program changes through this workflow.

Private storage durability, restore tests, customer classification, retention obligations and legal holds need separate operational confirmation. “No purge implemented” is not a selected legal retention period. Required audit records and immutable database rows prove the application history; they do not establish provider Object Lock or perpetual object availability.

## Verification scope

The focused frontend suites cover exact raw-byte hashing, multiple profiles and reported-revision evidence, malformed or changed receipts, explicit selection/upload, lost intent/content replies, finalization before resend, company changes, read-only controls, 101-file refusal, pacing, Retry-After pause/resume and authenticated client transport. Backend failure/race tests cover the independent intent, attempt and completion transactions. Use synthetic CAD for verification; no production originals are needed to exercise these paths.
