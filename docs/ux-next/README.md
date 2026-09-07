# ERP workflow improvements

This release carries the next eight UX/UI packages into the existing ERP. Packages 1–6 follow the proposed improvement list. Packages 7–8 add consistent page/detail layouts and reviewed quote/PO email delivery, as stated during implementation.

| Package | What users can do | Implementation and evidence |
| --- | --- | --- |
| 1. Saved workspaces | Save named filters, visible columns, order, sorting and density in Work Orders, Purchasing and Quality; recover personal layouts across sessions. | [Workspaces and drafts](workspaces-and-drafts.md) |
| 2. Operational inbox | Assign an owner and next action to live late jobs, blockers, stock, quality, purchasing and MRP issues; acknowledge or snooze without claiming the source problem is resolved. | [Operational inbox](inbox.md) |
| 3. Resumable drafts | Resume an unfinished work order, purchase order or quote; see conflict/recovery choices when another tab or an interrupted write changes the draft. | [Draft implementation](workspaces-and-drafts.md), [independent race review](workspace-correctness-review.md) |
| 4. MRP supply drafts | Review a recommendation, quantity, supplier and due date, then create a linked draft PO or WO with duplicate protection. | [MRP supply review](mrp.md) |
| 5. Scheduling review | Preview changed dates, affected jobs and capacity before applying the exact reviewed plan; stale plans require fresh review. | [Scheduling impact](scheduling.md) |
| 6. Measured performance | Work with large work-order lists without mounting the hidden mobile rows; load the nesting importer when it is opened. | [Measurements and tradeoffs](performance.md) |
| 7. Consistent layouts | Use shared page/detail headings and responsive action rows across operational pages, documents and shipment details. | [Layouts](layouts.md) |
| 8. Reviewed email | Prepare the exact quote/PO PDF, inspect it in the app, edit the message and explicitly send; inspect recorded outcomes and reconcile an uncertain attempt. | [Email delivery](email-delivery.md) |

## Representative screens

All captures and PDF samples use fictional local data.

- [Saved work-order workspace](screenshots/saved-workspace-desktop.png)
- [Purchase-order draft recovery on mobile](screenshots/purchase-draft-mobile.png)
- [Operational inbox assignment on mobile](screenshots/inbox-mobile-assignment-viewport.jpg)
- [MRP supply review](screenshots/mrp-supply-review.png)
- [Scheduling impact review](screenshots/scheduling-impact-review.png)
- [Quote email review](screenshots/quote-email-review.png)

## Release notes

Database migrations 091–094 extend the existing Alembic chain with private user workspaces, operational triage state, MRP supply links and reviewed document deliveries. PostgreSQL RLS and revoked standard Data API grants keep the new tables behind the ERP's existing authenticated API. Applying migrations does not rewrite existing operational records.

The performance results are controlled synthetic browser measurements, not production field metrics. The scheduling preview retains the existing daily-hours capacity model. Email acceptance records what the SMTP server reported; it does not establish recipient inbox delivery. No real email was sent during validation. Sending requires the deployment's configured SMTP service, and the composer explains when that service is unavailable.

SQLite tests and generated PostgreSQL DDL cover the new migration and authorization contracts. Actual concurrent PostgreSQL contention and delivery to a real recipient are outside the local acceptance evidence. See the package notes for precise limits, [frontend/browser validation](validation.md) and [backend/integration validation](backend-validation.md) for full-suite results.

## Separate follow-up found during review

The older background notification transport in `backend/app/services/email_service.py` uses automatic STARTTLS together with an explicit STARTTLS call. The new reviewed-document transport corrects this pattern and tests both STARTTLS and implicit TLS. Updating and validating the older notification transport is a separate follow-up; it is not called by the new quote/PO composer.
