# Role-Based Access Control (RBAC) Documentation

## Overview

Werco ERP implements a comprehensive RBAC system with 7 predefined roles. Permissions are enforced both on the backend (API endpoints) and frontend (UI elements).

## Roles

| Role | Description | Use Case |
|------|-------------|----------|
| **Admin** | Full system access | System administrators, IT staff |
| **Manager** | Department-wide access with approval capabilities | Department managers, production managers |
| **Supervisor** | Team-level access with create/edit permissions | Shift supervisors, team leads |
| **Operator** | View and update assigned work | Machine operators, production workers |
| **Quality** | Quality-specific actions | Quality inspectors, QC staff |
| **Shipping** | Shipping operations | Shipping clerks, warehouse staff |
| **Viewer** | Read-only access | Auditors, executives, guests |

## Access enforcement model

Permissions are enforced at two layers, and the two layers **intentionally differ for reads**:

- **Writes / state changes** (Create, Edit, Delete, Approve, Release, Send, Adjust, Transfer, Complete, Inspect, …) are enforced **server-side** via the `require_role` dependency on the endpoint. These are the authoritative access controls and match the matrix below.
- **Operational/domain reads** — the **View** rows for the operational modules below (e.g. Work Orders, Parts, BOMs, Routings, Inventory, Purchasing, Receiving, Customers, Quotes) — are **tenant-scoped** (every query is filtered to the caller's active company via `get_current_company_id`) and are available to **any authenticated user within that tenant**. The list/detail GET endpoints depend on `get_current_user` only and do **not** restrict reads by role. The **View** columns therefore describe the *intended in-app navigation* (which the frontend gates for usability), not a server-enforced read restriction. This is the current intended design: **read-broad / write-restricted**.
- **Administrative / governance reads are the exception and _are_ enforced server-side:** **Users** (`require_role([ADMIN, MANAGER])`), **Admin Settings** (`ADMIN`), and **Audit Logs** (`require_role([ADMIN, MANAGER])`).

> If the business requires least-privilege on domain reads (e.g. hiding vendor pricing / PO financials from Operator/Quality/Shipping at the API), enforce it **uniformly** by adding `require_role` to the read endpoints across modules, with authorization tests — not per-router. Until then, treat the **View** columns for operational modules as UI-visibility, not as a server-enforced control.

## Permission Matrix

### Work Orders

| Permission | Admin | Manager | Supervisor | Operator | Quality | Shipping | Viewer |
|------------|:-----:|:-------:|:----------:|:--------:|:-------:|:--------:|:------:|
| View | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Create | ✓ | ✓ | ✓ | | | | |
| Edit | ✓ | ✓ | ✓ | | | | |
| Delete | ✓ | ✓ | | | | | |
| Release | ✓ | ✓ | ✓ | | | | |
| Complete | ✓ | ✓ | ✓ | ✓ | ✓ | | |
| Approve labor (TimeEntry) | ✓ | ✓ | ✓ | | ✓ | | |

> **Approve labor — endpoint mapping (Batch 11B / G5-A).** The shop-floor labor sign-off
> `POST /api/v1/shop-floor/time-entries/{id}/approve` and `…/unapprove` (which set / clear
> `TimeEntry.approved` + `approved_by`, the field the opt-in `REQUIRE_APPROVED_LABOR_FOR_COST` flag
> keys labor-cost rollups on) are enforced **in code** to this row:
> `require_role([ADMIN, MANAGER, SUPERVISOR, QUALITY])` (`app/api/endpoints/shop_floor.py`). In
> addition to the role gate, **self-approval is
> forbidden**: a user cannot approve or unapprove their **own** TimeEntry (segregation of duties for
> the labor-cost gate) — that returns **403** even for an approver-role user. A cross-tenant id returns
> **404**. Both actions are audited (`time_entry_approve` / `time_entry_unapprove`).

> **Operator-qualification gate is record-only (Batch 11C / G5-B).** `POST /api/v1/shop-floor/clock-in`
> and `POST /api/v1/shop-floor/operations/{id}/start` stay **operator-facing** — open to **any
> authenticated user** (`get_current_user`), no new role gate. The G5-B qualification gate (no active
> `SkillMatrix` entry at level ≥ 2 for the work center, or a missing/expired required
> `OperatorCertification`) **only records** a tamper-evident `audit_log` row
> (`OPERATOR_QUALIFICATION_EXCEPTION`) + a warning event and surfaces a `qualification_exceptions`
> array on the response; it does **not** gate the operator's role or block the clock-in / start. The
> gate's lookups are tenant-scoped (every skill/cert/work-center query filters the active company).

> **Laser-nest manual entry + reference PDF — endpoint mapping.** Manually keying a laser nest and
> all per-nest mutations follow the Work Orders **Create / Edit / Delete** rows above —
> `require_role([ADMIN, MANAGER, SUPERVISOR])`: `POST /api/v1/work-orders/{id}/laser-nests/manual`
> (create), `PATCH /api/v1/laser-nests/{id}` (edit), `POST /api/v1/laser-nests/{id}/attach-document`
> and `DELETE /api/v1/laser-nests/{id}/document` (attach/detach the reference PDF), and
> `DELETE /api/v1/laser-nests/{id}` (soft-delete; the operation goes `ON_HOLD`). This matches the
> existing laser-nest **package import** trio (`…/laser-nest-packages/preview` and `…/import`) and the
> stateless PDF field-extraction endpoint `POST /api/v1/laser-nests/extract` (same ADMIN/MANAGER/
> SUPERVISOR gate; no DB write, no audit). The
> **exception** is the operator-readable inline PDF preview `GET /api/v1/laser-nests/{id}/document`,
> which is open to **any authenticated user** (`get_current_user`) so operators can view the shop
> drawing — read-only and still tenant-scoped (a cross-tenant or soft-deleted nest → **404**). All
> writes are audited; nests are soft-deleted, never hard-deleted. See `docs/API.md` → Laser Nests.

> **Scanner resolve-action is read-only and open to any authenticated user (A0.4).**
> `POST /api/v1/scanner/resolve-action` (the QR traveler / badge scan resolver,
> `app/api/endpoints/scanner.py`) carries no role gate (`get_current_user` only) — it mirrors the
> read-broad shop-floor reads it sits in front of. It is **read-only** (no audit rows, no
> operational events, no auth side effects; a badge scan is a lookup only — badge **login** stays
> exclusively on `POST /auth/employee-login`) and **tenant-scoped** (a cross-tenant code, or a
> soft-deleted work order, resolves to `kind: "unknown"`). URL-shaped traveler codes resolve too;
> the URL's host is deliberately **not** validated — a scanned URL carries no tenant authority, and
> tenancy always derives from the authenticated caller. The per-action gating it reports
> (`legal_actions` / `blockers`) reflects operation / time-entry **state**, not role — the
> shop-floor write verbs it mirrors (clock-in, production, complete, hold, resume) are themselves
> operator-facing (any authenticated user), so the resolver bypasses no role check. See
> `docs/API.md` → Scanner.

### Parts

| Permission | Admin | Manager | Supervisor | Operator | Quality | Shipping | Viewer |
|------------|:-----:|:-------:|:----------:|:--------:|:-------:|:--------:|:------:|
| View | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Create | ✓ | ✓ | ✓ | | | | |
| Edit | ✓ | ✓ | ✓ | | | | |
| Delete | ✓ | | | | | | |

### BOMs

| Permission | Admin | Manager | Supervisor | Operator | Quality | Shipping | Viewer |
|------------|:-----:|:-------:|:----------:|:--------:|:-------:|:--------:|:------:|
| View | ✓ | ✓ | ✓ | ✓ | ✓ | | ✓ |
| Create | ✓ | ✓ | ✓ | | | | |
| Edit | ✓ | ✓ | ✓ | | | | |
| Delete | ✓ | ✓ | | | | | |
| Release | ✓ | ✓ | | | | | |

### Routings

| Permission | Admin | Manager | Supervisor | Operator | Quality | Shipping | Viewer |
|------------|:-----:|:-------:|:----------:|:--------:|:-------:|:--------:|:------:|
| View | ✓ | ✓ | ✓ | ✓ | ✓ | | ✓ |
| Create | ✓ | ✓ | ✓ | | | | |
| Copy | ✓ | ✓ | | | | | |
| Generate from drawing (AI) | ✓ | ✓ | ✓ | | | | |
| Edit (draft routing) | ✓ | ✓ | ✓ | | | | |
| Edit time standards (released routing) | ✓ | ✓ | | | | | |
| Delete | ✓ | ✓ | | | | | |
| Release | ✓ | ✓ | | | | | |

> **Edit row splits by routing status — endpoint mapping (`feat/routing-editable-time-standards`).**
> `PUT /api/v1/routing/{routing_id}/operations/{operation_id}` (`update_operation`,
> `app/api/endpoints/routing.py`) carries the decorator-level
> `require_role([ADMIN, MANAGER, SUPERVISOR])` — the **Edit (draft routing)** row, where every
> operation field is editable. On a **released** routing the same endpoint allows in-place edits to
> **time standards only** (`setup_hours`, `run_hours_per_unit`, `move_hours`, `queue_hours`,
> `cycle_time_seconds`, `pieces_per_cycle`) and gates that path **in code** to **Admin / Manager**
> only — a **Supervisor** hitting the released-edit path receives **403**
> (*"Editing a released routing's time standards requires the Admin or Manager role."*). This mirrors
> **Release** (also Admin/Manager-only): editing live released content is release-adjacent authority,
> so it is held to the release role set rather than the broader draft-edit set. Changing any
> non-time-standard (process) field on a released routing returns **400** (create a new revision
> instead); an **obsolete** routing is fully locked (**400**). Adding/deleting/reordering operations
> on a released routing also returns **400** (process is frozen on release). Superuser / Platform
> Admin bypass role checks, as elsewhere. Every applied change is tamper-evidently audit-logged; see
> [docs/CMMC_LEVEL_2_COMPLIANCE.md](CMMC_LEVEL_2_COMPLIANCE.md) → CONFIGURATION MANAGEMENT (CM).

> **Copy & AI generation — endpoint mapping (`feat/process-sheets-library`).**
> `POST /api/v1/routing/{routing_id}/copy` (`copy_routing`, `app/api/endpoints/routing.py`) carries
> `require_role([ADMIN, MANAGER])` — deliberately **narrower than Create** (no Supervisor). The
> two-step AI generation flow — `POST /routing/generate-from-drawing` then
> `POST /routing/create-from-generation` — carries `require_role([ADMIN, MANAGER, SUPERVISOR])`, the
> Create role set. Both paths produce **draft** routings (Release stays Admin/Manager), and the copy
> endpoint writes a tamper-evident `audit_log` CREATE with `extra_data.copied_from` (the source
> routing id) — see `docs/API.md` → Routing and the
> [CMMC change log](CMMC_LEVEL_2_COMPLIANCE.md) entry dated 2026-07-06.

### Process Sheets

| Permission | Admin | Manager | Supervisor | Operator | Quality | Shipping | Viewer |
|------------|:-----:|:-------:|:----------:|:--------:|:-------:|:--------:|:------:|
| View | ✓ | ✓ | ✓ | ✓ | ✓ | | ✓ |
| Create | ✓ | ✓ | ✓ | | ✓ | | |
| Edit (draft sheet + steps) | ✓ | ✓ | ✓ | | ✓ | | |
| Delete (draft only) | ✓ | ✓ | ✓ | | ✓ | | |
| New revision | ✓ | ✓ | ✓ | | ✓ | | |
| Release | ✓ | ✓ | | | ✓ | | |
| Obsolete | ✓ | ✓ | | | ✓ | | |

> **Role split — endpoint mapping (`feat/process-sheets-library`).** All `/api/v1/process-sheets`
> writes are gated by decorator-level `require_role` in `app/api/endpoints/process_sheets.py`:
> **authoring** (create, header edit, step CRUD, soft-delete, new-revision) carries
> `AUTHOR_ROLES = [ADMIN, MANAGER, SUPERVISOR, QUALITY]`; **release** and **obsolete** carry
> `RELEASE_ROLES = [ADMIN, MANAGER, QUALITY]`. Unlike Routings, **Quality** participates in both
> sets — process sheets are inspection documents, and quality owns released inspection content
> (release-adjacent authority), while release stays narrower than authoring, mirroring the
> Routings draft-edit vs release split. Mutability is status-gated in the service: only **draft**
> sheets are editable — header edits, step CRUD, and delete on a released/obsolete sheet return
> **409** (create a new revision instead). GET endpoints depend on `get_current_user` only
> (tenant-scoped, read-broad — see the access enforcement model above). Superuser / Platform Admin
> bypass role checks, as elsewhere.

### Inventory

| Permission | Admin | Manager | Supervisor | Operator | Quality | Shipping | Viewer |
|------------|:-----:|:-------:|:----------:|:--------:|:-------:|:--------:|:------:|
| View | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Adjust | ✓ | ✓ | ✓ | | | | |
| Transfer | ✓ | ✓ | ✓ | | | | |

### Purchasing

| Permission | Admin | Manager | Supervisor | Operator | Quality | Shipping | Viewer |
|------------|:-----:|:-------:|:----------:|:--------:|:-------:|:--------:|:------:|
| View | ✓ | ✓ | ✓ | | | | ✓ |
| Create | ✓ | ✓ | ✓ | | | | |
| Approve | ✓ | ✓ | | | | | |

> **Read enforcement:** Per the [Access enforcement model](#access-enforcement-model),
> Purchasing list/detail reads (`list_vendors`, `list_purchase_orders`, and the
> single-record GETs in `app/api/endpoints/purchasing.py`) are tenant-scoped but **not**
> role-restricted — any authenticated user in the tenant can read vendor and PO data, so
> the **View** row above reflects intended UI visibility rather than a server-enforced
> restriction. Only the write/approve actions (Create, Approve, send, line edits) are
> role-gated. Receiving (below) follows the same read-broad / write-restricted pattern.

### Receiving

| Permission | Admin | Manager | Supervisor | Operator | Quality | Shipping | Viewer |
|------------|:-----:|:-------:|:----------:|:--------:|:-------:|:--------:|:------:|
| View | ✓ | ✓ | ✓ | | ✓ | | ✓ |
| Create | ✓ | ✓ | ✓ | | | | |
| Inspect | ✓ | ✓ | | | ✓ | | |
| Print / reprint receiving label | ✓ | ✓ | ✓ | | | | |
| Configure print profile | ✓ | | | | | | |

> **Write enforcement:** The Create and Inspect rows above are now enforced **in code** on
> the canonical `/api/v1/receiving` endpoints (`app/api/endpoints/receiving.py`):
> `POST /receiving/receive` → `require_role([ADMIN, MANAGER, SUPERVISOR])` and
> `POST /receiving/inspect/{receipt_id}` → `require_role([ADMIN, MANAGER, QUALITY])`
> (superuser / Platform Admin bypass role checks, as elsewhere). This replaces a prior state
> where the receive endpoint was not role-restricted and a duplicate receiving/inspection
> path existed under `/api/v1/purchasing`; that duplicate has been removed, so `/api/v1/receiving`
> is the single source of truth. Receiving reads follow the same read-broad / write-restricted
> pattern noted for Purchasing above.

> **Thermal receiving-label printing (ProxyBox / WHTP203e).** Manually (re)printing the
> 4×6 receiving label — `POST /receiving/receipt/{receipt_id}/print-label` — is enforced
> to **Admin / Manager / Supervisor** via `require_role([ADMIN, MANAGER, SUPERVISOR])`,
> the same gate as `POST /receiving/receive`. Configuring the per-company print profile —
> `GET` / `PUT /receiving/print-profile` (ProxyBox base URL / target / API key, copies,
> paper size, and the `auto_print_on_receipt` + `allow_print_egress` toggles) — is
> **admin-only** via `get_admin_user`, so only an admin can enter the printer credential
> or flip the outbound-egress kill switch (default OFF, audited as a status change). See
> [docs/THERMAL_LABEL_PRINTING.md](THERMAL_LABEL_PRINTING.md).

### Shipping

| Permission | Admin | Manager | Supervisor | Operator | Quality | Shipping | Viewer |
|------------|:-----:|:-------:|:----------:|:--------:|:-------:|:--------:|:------:|
| View | ✓ | ✓ | ✓ | | | ✓ | ✓ |
| Create | ✓ | ✓ | ✓ | | | ✓ | |
| Complete | ✓ | ✓ | ✓ | | | ✓ | |
| Rate-shop / validate address (`shipping:rate`) | ✓ | ✓ | ✓ | | | ✓ | |
| Buy label / BOL / schedule pickup (`shipping:label`) | ✓ | ✓ | ✓ | | | ✓ | |
| Void / refund label (`shipping:void`) | ✓ | ✓ | ✓ | | | ✓ | |
| Issue Certificate of Conformance | ✓ | ✓ | | | ✓ | | |

> **Carrier-integration write actions — endpoint mapping (multi-carrier shipping integration).**
> The carrier actions on `app/api/endpoints/shipping.py` —
> `POST /shipping/validate-address`, `POST /shipping/{id}/rate-shop`,
> `POST /shipping/{id}/buy-label`, `POST /shipping/{id}/buy-bol`,
> `POST /shipping/{id}/schedule-pickup`, `POST /shipping/{id}/void-label`, and
> `POST /shipping/{id}/refund` — are enforced **in code** to
> `require_role([ADMIN, MANAGER, SUPERVISOR, SHIPPING])` (`CARRIER_WRITE_ROLES`). They transmit
> customer data to a carrier (gated by the per-company `allow_carrier_egress` kill switch in the
> service) and move money (label/BOL/void/refund are audited), so they carry the same role set that
> may complete a shipment. The new permission strings `shipping:rate`, `shipping:label`, and
> `shipping:void` (in `app/models/role_permission.py`, granted to Admin / Manager / Supervisor /
> Shipping) drive the **frontend** `PermissionGate` / `usePermissions` visibility; the
> `require_role` lists above are the authoritative server-side control. The read endpoints
> (`GET /shipping/{id}/rates`, `GET /shipping/{id}/tracking`) stay open to any authenticated tenant
> user (read-broad / write-restricted). See
> [docs/SHIPPING_CARRIER_INTEGRATION.md](SHIPPING_CARRIER_INTEGRATION.md) and `docs/API.md` →
> Shipping. The inbound carrier tracking webhook (`POST /webhooks/carriers/{provider}`) is
> **unauthenticated by design** — see the Admin → Integrations note below.

> **Complete (mark shipped) — endpoint mapping (2026-06-09).** The Shipping **Complete** action
> `POST /api/v1/shipping/{shipment_id}/ship` (`mark_shipped`) is now enforced **in code** to the
> Complete row via `require_role([ADMIN, MANAGER, SUPERVISOR, SHIPPING])`
> (`app/api/endpoints/shipping.py`). **This is a permission change:** the endpoint was previously open
> to **any authenticated user**, who could close a work order by shipping it; a non-privileged user now
> receives **403**. Marking shipped is the terminal shipping action that transitions the work order to
> `CLOSED`, so it carries the **Complete** permission (not the broader View/Create reads).
>
> **Certificate of Conformance — endpoint mapping (Batch 11C / G6-B).** Issuing a CoC
> `POST /api/v1/shipping/{shipment_id}/coc` (mint or return the existing frozen-snapshot CoC) is
> enforced **in code** to `require_role([ADMIN, MANAGER, QUALITY])`
> (`app/api/endpoints/shipping.py`) — a quality artifact, so the write is restricted (this is why the
> matrix row above does **not** include the **Shipping** role, which otherwise holds Shipping
> Create/Complete). Reading the CoC — `GET /shipping/{shipment_id}/coc` (metadata) and
> `GET /shipping/{shipment_id}/coc/pdf` (rendered PDF) — is open to **any authenticated user** in the
> active company (read-broad / write-restricted, like the other shipping reads). All three are
> tenant-scoped (cross-tenant `shipment_id` → **404**). A CoC is also **auto-issued on ship** when
> required; the auto-issue runs in the ship handler's context and is not separately role-gated beyond
> the existing ship permission.

### Quality

| Permission | Admin | Manager | Supervisor | Operator | Quality | Shipping | Viewer |
|------------|:-----:|:-------:|:----------:|:--------:|:-------:|:--------:|:------:|
| View | ✓ | ✓ | ✓ | ✓ | ✓ | | ✓ |
| Inspect | ✓ | ✓ | ✓ | | ✓ | | |
| Approve | ✓ | ✓ | | | ✓ | | |
| Calibration | ✓ | ✓ | | | ✓ | | |
| Manage scrap reason codes | ✓ | ✓ | | | ✓ | | |

> **Inspect — endpoint mapping.** The shop-floor inspection sign-off
> `POST /api/v1/shop-floor/operations/{operation_id}/inspection` (which records
> `WorkOrderOperation.inspection_complete = True` and clears the completion inspection quality gate)
> is enforced **in code** to this Inspect row:
> `require_role([ADMIN, MANAGER, SUPERVISOR, QUALITY])` (`app/api/endpoints/shop_floor.py`,
> `mark_operation_inspected`). The role set matches the matrix exactly — this repo has no separate
> `INSPECTOR` role, so operation inspection is performed by Admin / Manager / Supervisor / Quality.
>
> **Scrap reason codes (Lean Phase 1) — read-broad / write-restricted.** Managing the tenant's
> structured scrap vocabulary (`POST /api/v1/quality/scrap-reason-codes`,
> `PUT /quality/scrap-reason-codes/{id}`) is a quality-system configuration task, enforced **in
> code** via `require_role([ADMIN, MANAGER, QUALITY])` (`SCRAP_REASON_WRITE_ROLES` in
> `app/api/endpoints/scrap_reasons.py`) — the same write set that owns the NCR/CAR vocabulary. The
> **read** (`GET /quality/scrap-reason-codes`) depends on `get_current_user` only — any
> authenticated user in the tenant, including Operators via the kiosk/desktop scrap pickers — so
> the matrix row above reflects the server-enforced **write** control. There is no delete endpoint:
> retirement is `is_active: false` (historical scrap rows reference these ids — traceability).

### Operator Certifications & Training

| Permission | Admin | Manager | Supervisor | Operator | Quality | Shipping | Viewer |
|------------|:-----:|:-------:|:----------:|:--------:|:-------:|:--------:|:------:|
| View | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Create | ✓ | ✓ | | | ✓ | | |
| Edit | ✓ | ✓ | | | ✓ | | |
| Delete | ✓ | ✓ | | | ✓ | | |

### Skill Matrix

| Permission | Admin | Manager | Supervisor | Operator | Quality | Shipping | Viewer |
|------------|:-----:|:-------:|:----------:|:--------:|:-------:|:--------:|:------:|
| View | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Create | ✓ | ✓ | ✓ | | | | |
| Edit | ✓ | ✓ | ✓ | | | | |

> **Write enforcement — these role sets are new (defaults chosen for this fix, 2026-06-09).** The
> seven write endpoints on the operator-certifications router
> (`app/api/endpoints/operator_certifications.py`, mounted at `/api/v1/operator-certifications`) are now
> enforced **in code**; the RBAC matrix previously had **no rows** for these record types and the writes
> were open to any authenticated user.
> - **Certifications + training writes** —
>   `POST/PUT/DELETE …/certifications/{…}` (`create_certification` / `update_certification` /
>   `delete_certification`) and `POST/PUT …/training/{…}` (`create_training` / `update_training`) —
>   require `require_role([ADMIN, MANAGER, QUALITY])` (`CERT_TRAINING_WRITE_ROLES`). These are
>   operator-qualification / conformance records that Quality owns alongside Admin/Manager.
> - **Skill-matrix writes** — `POST …/skill-matrix/` (`create_skill_entry`, which upserts) and
>   `PUT …/skill-matrix/{entry_id}` (`update_skill_entry`) — require
>   `require_role([ADMIN, MANAGER, SUPERVISOR])` (`SKILL_MATRIX_WRITE_ROLES`), because skill-matrix
>   entries are competency assessments performed by Supervisors (and above).
>
> Any other authenticated role now receives **403**. **Read** endpoints (the certifications dashboard /
> list / by-user / by-id, training list / by-user, and the skill-matrix check / by-user /
> by-work-center / list) stay open to **any authenticated user**, tenant-scoped — the read-broad /
> write-restricted model. Superuser / Platform Admin bypass role checks, as elsewhere.
>
> **Writes are audited + FK-validated.** Each write now records a tamper-evident `audit_log` row
> (resource types `operator_certification` / `training_record` / `skill_matrix`; create / update /
> delete). The create endpoints (and `update_training`'s re-pointed `work_center_id`) reject a
> `user_id` / `work_center_id` that does not belong to the active company with **422** before insert
> (cross-tenant FK-injection guard). See `docs/API.md` and `docs/WORK_ORDER_COMPLETION_REMEDIATION.md`.

### Engineering Change Orders (ECO)

| Permission | Admin | Manager | Supervisor | Operator | Quality | Shipping | Viewer |
|------------|:-----:|:-------:|:----------:|:--------:|:-------:|:--------:|:------:|
| View | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Create / Edit | ✓ | ✓ | | | | | |
| Submit / Approve / Reject | ✓ | ✓ | | | | | |
| Implement / Complete | ✓ | ✓ | | | | | |
| Add / Edit task, Add approval | ✓ | ✓ | | | | | |

> **ECO mutations are Admin / Manager (enforced in code).** Every state-changing ECO endpoint
> (`POST /eco/eco/`, `PUT /eco/eco/{id}`, and the `submit` / `approve` / `reject` / `implement` /
> `complete` transitions, plus `tasks` create/update and `approvals` create) is gated with
> `require_role([ADMIN, MANAGER])` (`app/api/endpoints/engineering_changes.py`). Any other authenticated
> role receives **403**. The read endpoints (list, get, dashboard, list approvals, affected items) remain
> open to all authenticated users. Previously these mutations were available to **any** authenticated
> user — this row records the tightened authorization landed in WO-completion remediation Batch 11A
> (G4-Fix1), alongside the ECO router's tenant scoping and audit logging.

### Users

| Permission | Admin | Manager | Supervisor | Operator | Quality | Shipping | Viewer |
|------------|:-----:|:-------:|:----------:|:--------:|:-------:|:--------:|:------:|
| View | ✓ | ✓ | | | | | |
| Create | ✓ | | | | | | |
| Edit | ✓ | | | | | | |
| Delete | ✓ | | | | | | |
| Roles | ✓ | | | | | | |

> **User writes are Admin-only, and both `require_role([ADMIN])`** — `POST /users/` (create),
> `PUT /users/{id}` (edit, incl. role assignment), and `DELETE /users/{id}` (deactivate) all gate to
> **Admin** (`app/api/endpoints/users.py`). The **View** rows are the governance-read exception noted
> above: `GET /users/` (list) and `GET /users/{id}` are `require_role([ADMIN, MANAGER])`, so a
> **Supervisor** gets a **failed load** (403), not a read — user records are *not* on the read-broad
> domain default. Superuser / Platform Admin bypass role checks, as elsewhere.
>
> **`platform_admin` is never assignable from a tenant path, and admins cannot self-elevate.** Both
> user-write endpoints now enforce the same guards as user import (below, under Bulk Imports):
> - **`POST /users/` and `PUT /users/{id}` reject `role = platform_admin` with 400**
>   (`"Platform admin role cannot be assigned"`). `platform_admin` is the cross-company Werco
>   oversight role and can never be minted from a tenant-scoped path — not by create, update,
>   approval (`POST /users/{id}/approve`, `"…cannot be assigned through approval"`), or import — even
>   by a company Admin.
> - **Self role-escalation guard:** on `PUT /users/{id}`, an Admin editing **their own** record cannot
>   change **their own** role (**400**, `"You cannot change your own role"`); editing their own
>   name/email/other fields stays allowed. This mirrors the delete endpoint's "cannot deactivate
>   yourself" self-guard, so an Admin cannot self-elevate and a role change to one's own account must
>   be made by a different Admin.
>
> Every user mutation — create, update (including any role change), approve, password-reset,
> deactivate, and activate — is recorded in the tamper-evident audit log.
>
> **Password-strength policy.** A password set on any of these paths — `POST /users/` (create),
> `POST /users/{id}/reset-password`, and self-service `POST /users/change-password` — must satisfy
> the server-side strength policy (≥ 12 chars; uppercase, lowercase, number, and special char; no
> common weak substring), the **same policy** as `POST /auth/register`. The user CSV import applies
> it per row to user-supplied passwords; operator auto-generated (badge) passwords are exempt. See
> `docs/API.md` → Users.
>
> **Badge printing (A0.4).** The badge print sheet `/print/badges` (opened from the Users page via
> multi-select → "Print Badges") is **frontend-gated by `canManageUsers`** (=
> `users:create` OR `users:edit`) — both the Users-page button
> (`frontend/src/utils/permissions.ts`) and the `/print/badges` route (route map in
> `frontend/src/App.tsx`) require it. After user management was aligned to Admin-only, only **Admin**
> holds those permission strings (plus Platform Admin / superuser), so badge printing is now
> **effectively Admin-only**: a **Manager** (who holds `users:view` — the read-only list) and a
> **Supervisor** (who holds no `users:*` permission) never see the Print Badges control or reach the
> route. No new endpoint or permission string was added: badges are client-rendered QR codes of
> `users.employee_id`, and the page loads its data from the existing `GET /api/v1/users/`, which is
> server-enforced to `require_role([ADMIN, MANAGER])` (see the access-enforcement note above). The
> badge gate is therefore **narrower than** that read split — a Manager can open the Users page and
> read the list but cannot print badges — consistent with the Admin-only user-write posture in the
> note directly above.

### Bulk Imports (Import Center / Excel Migration Kit)

| Permission | Admin | Manager | Supervisor | Operator | Quality | Shipping | Viewer |
|------------|:-----:|:-------:|:----------:|:--------:|:-------:|:--------:|:------:|
| Download templates (`GET /import/templates*`) | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Import users (`POST /users/import-csv`) | ✓ | | | | | | |
| Import parts / materials | ✓ | ✓ | ✓ | | | | |
| Import customers / vendors / work centers | ✓ | ✓ | | | | | |
| Import routings (`POST /routing/import/preview`, `/import/commit`) | ✓ | ✓ | ✓ | | | | |
| Import open work orders (`POST /work-orders/import`) | ✓ | ✓ | ✓ | | | | |
| Import open purchase orders (`POST /purchasing/purchase-orders/import`) | ✓ | ✓ | | | | | |

> **Endpoint mapping (A0.2 Excel migration kit, enforced in code).** All rows above apply
> identically to dry-run (`?dry_run=true`) and commit calls.
> - **Templates are open to any authenticated user** (`get_current_user`): the XLSX templates are
>   static workbooks containing **no tenant data**, so listing/downloading them carries no read risk.
> - **Open-WO import mirrors Work Orders → Create**:
>   `require_role([ADMIN, MANAGER, SUPERVISOR])` (`app/api/endpoints/work_orders.py`) — importing an
>   open work order creates+releases a WO through the same generation path as `POST /work-orders/`,
>   so it carries exactly the WO Create/Release role set.
> - **Open-PO import is Admin / Manager only — deliberately narrower than WO import**:
>   `require_role([ADMIN, MANAGER])` (`app/api/endpoints/purchasing.py`). Imported POs land directly
>   in **`sent` (issued)** status, and the interactive PO `/send` transition is Admin/Manager-only —
>   allowing Supervisor here would let a spreadsheet issue POs its holder cannot issue in the UI
>   (privilege escalation via import).
> - **User import is Admin-only and cannot mint `platform_admin`**: a row with
>   `role = platform_admin` is rejected per-row (`"role 'platform_admin' cannot be assigned via
>   import"`), and `platform_admin` is excluded from the advertised valid-roles list. The
>   platform-admin role is the cross-company Werco oversight role and must never be assignable from
>   a tenant spreadsheet, even by a company Admin.
> - **Routing import mirrors Routings → Create**:
>   `require_role([ADMIN, MANAGER, SUPERVISOR])` (`app/api/endpoints/routing.py`) on both the
>   `/routing/import/preview` (dry-run) and `/routing/import/commit` endpoints — it creates **draft**
>   routings through the same path as `POST /routing/`, so it carries exactly the Routings Create role
>   set (Release stays Admin/Manager — imported routings land as draft and must be released
>   separately). The **frontend** gates the Routing page **Import Routings** button (which opens the
>   `RoutingImportWizard` dry-run/commit modal) on the `routings:create` permission via
>   `hasPermission` (`frontend/src/pages/Routing.tsx`), matching this server-side role set —
>   operator / quality / shipping / viewer never see the button. The Import Center's **Routings** tab
>   (`mode: 'linked'`) only surfaces the template download + column hints and links to the Routing
>   page; the upload/preview/commit lives in the wizard, not in the Import Center.
> - The entity-import role sets (parts/materials → A/M/S; customers/vendors/work centers → A/M) are
>   unchanged from the pre-existing CSV imports and match each module's Create row above.
> - **Audit:** every committed import row writes a tamper-evident `audit_log` entry tagged
>   `source = "import"`; dry runs write nothing (savepoint rollback). See `docs/API.md` →
>   Bulk Imports & Templates and `docs/EXCEL_MIGRATION_RUNBOOK.md`.

### Analytics

| Permission | Admin | Manager | Supervisor | Operator | Quality | Shipping | Viewer |
|------------|:-----:|:-------:|:----------:|:--------:|:-------:|:--------:|:------:|
| View | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Flow / WIP-aging / adoption (Lean Phase 1) | ✓ | ✓ | ✓ | | | | |
| FPY / scrap Pareto (Lean Phase 1) | ✓ | ✓ | ✓ | | ✓ | | |
| Export | ✓ | ✓ | | | | | |

> **Lean Phase 1 analytics reads are role-gated in code.** `GET /api/v1/analytics/flow`,
> `GET /analytics/wip-aging`, and `GET /analytics/adoption` require
> `require_role([ADMIN, MANAGER, SUPERVISOR])`; `GET /analytics/fpy` and
> `GET /analytics/scrap-pareto` additionally admit **Quality**
> (`require_role([ADMIN, MANAGER, SUPERVISOR, QUALITY])`) — yield and scrap categorization are
> quality-system reads. All five are read-only and tenant-scoped (`app/api/endpoints/analytics.py`).
> The pre-existing View row (overview / KPIs / trends / quality metrics) remains any-authenticated.
>
> **`GET /reports/ship-otd` is any-authenticated (pre-existing reports posture).** The Lean Phase 1
> ship-based OTD/OTIF detail report follows `reports.py`'s convention — `get_current_user` only, no
> role gate, tenant-scoped. **Observation (compliance review, 2026-07-10):** this report exposes
> customer-name delivery rollups (per-customer OTD %, late counts) to every role in the tenant,
> including Operator/Viewer, under that pre-existing posture. If reports are later role-tiered,
> this endpoint should be revisited with the rest of `/reports/*`.

### OEE

| Permission | Admin | Manager | Supervisor | Operator | Quality | Shipping | Viewer |
|------------|:-----:|:-------:|:----------:|:--------:|:-------:|:--------:|:------:|
| View (dashboard / trends / six-big-losses / list records & targets) | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Write (auto-calculate / create-edit-delete records & targets) | ✓ | ✓ | ✓ | | | | |

> **Write enforcement (read-broad / write-restricted).** The OEE **write/mutation** endpoints —
> `POST /api/v1/oee/calculate/{work_center_id}`, `POST`/`PUT`/`DELETE /oee/records`, and
> `POST`/`PUT`/`DELETE /oee/targets` — are now enforced **in code** to the Write row via
> `require_role([ADMIN, MANAGER, SUPERVISOR])` (`OEE_WRITE_ROLES` in `app/api/endpoints/oee.py`),
> matching the sibling Analytics-write posture. **This is a permission change:** these endpoints were
> previously open to any authenticated user. OEE **read** endpoints (`/oee/dashboard`, `/oee/trends`,
> `/oee/six-big-losses/{wc}`, and the list/get GETs for records and targets) depend on
> `get_current_user` only — they are tenant-scoped but not role-restricted, so operators/viewers can
> still load OEE dashboards. The **View** row therefore reflects intended UI visibility; the **Write**
> row is a server-enforced control. Superuser / Platform Admin bypass role checks, as elsewhere.
>
> **Audit coverage (2026-06-09).** The OEE write endpoints now also write a tamper-evident `audit_log`
> row on every record/target create/update/delete (and the auto-calc upsert), so OEE mutations are on
> the hash chain alongside the role gate. No role change — audit-trail coverage only.

### Werco Copilot (read-only AI chat)

| Permission | Admin | Manager | Supervisor | Operator | Quality | Shipping | Viewer |
|------------|:-----:|:-------:|:----------:|:--------:|:-------:|:--------:|:------:|
| Chat (`POST /copilot/chat`) | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |

> **Endpoint vs. tool-level access.** The endpoint (`app/api/endpoints/copilot.py`) requires only
> an authenticated user (`get_current_user`) — it is **strictly read-only** (every copilot tool
> wraps an existing read path; nothing can be created, updated, or deleted), so the chat itself
> carries no role gate. **Tool-level access mirrors each tool's source endpoint**: all eight v1
> tools wrap any-authenticated reads. The `search_erp` tool **excludes employee (`user`-type)
> results entirely** — data minimization, so employee names/emails never enter model prompts
> regardless of the caller's role; the **Admin/Manager-only** gate on user results inside global
> search now applies to `GET /search` only. The tool registry
> (`CopilotToolSpec.allowed_roles` in `app/services/copilot_service.py`) supports fully
> role-restricted tools for the future: such tools are omitted from other roles' tool lists and
> refuse politely if invoked anyway.
>
> **Tenant scope is never model-controlled.** `company_id` is injected server-side from the
> active company (`get_current_company_id`) into every tool call; tool input schemas carry no
> tenant identifier, and undeclared input keys supplied by the model (including a `company_id`)
> are dropped before dispatch. Per-user rate limit: 20 requests/minute default
> (`COPILOT_RATE_LIMIT_PER_MINUTE`). See [docs/API.md](API.md) → Werco Copilot.

### Visitor Logs

| Permission | Admin | Manager | Supervisor | Operator | Quality | Shipping | Viewer |
|------------|:-----:|:-------:|:----------:|:--------:|:-------:|:--------:|:------:|
| View / search log (`visitor_logs:view`) | ✓ | ✓ | ✓ | | | | |
| Sign in / sign out a visitor | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Export log (CSV) | ✓ | ✓ | | | | | |
| Delete (soft) a visitor record | ✓ | ✓ | | | | | |
| Manage sign-in stations (create / reset-PIN / revoke) | ✓ | ✓ | | | | | |

> **Per-endpoint mapping (`/api/v1/visitor-logs`, `app/api/endpoints/visitor_logs.py`).** The two
> **visitor write** endpoints — `POST /sign-in` and `POST /sign-out` — are gated by the dedicated
> `get_signin_principal` dependency, which accepts **either** a PIN-minted station signin token
> (`type="signin"`, the lobby tablet) **or any authenticated staff user**. So the "Sign in / sign
> out" row is open to every authenticated user (and to an unattended station tablet), not a role —
> it is **not** the `require_role` model the rows below use. The **list** endpoint `GET /` is
> `require_role([ADMIN, MANAGER, SUPERVISOR])` (this is the server-enforced read gate the
> `visitor_logs:view` permission and the `/visitor-log` route mirror — visitor PII is *not* on the
> read-broad domain default). **Export** (`GET /export.csv`, audits an `EXPORT` action), **soft-delete**
> (`DELETE /{id}`), and **all station administration** (`POST /stations`, `GET /stations`,
> `POST /stations/{id}/revoke`, `POST /stations/{id}/reset-pin`) are `require_role([ADMIN, MANAGER])`.
> Every query is tenant-scoped (staff via `get_current_company_id`; the tablet via the authoritative
> `signin_stations` row, never the client `cid`); visitor records are soft-deleted, never
> hard-deleted; and every state change is tamper-evidently audited (station writes record the station
> label as the actor). See [docs/API.md](API.md) → Visitor Logs and
> [docs/VISITOR_SIGNIN.md](VISITOR_SIGNIN.md).
>
> **`station-login` is the only new public write surface.** `POST /visitor-logs/station-login` is
> unauthenticated by design — a tablet cannot present a JWT — but it is **PIN-gated**: it verifies the
> shared station PIN against the bcrypt `pin_hash` and a bad/revoked station or wrong PIN returns
> **401** (indistinguishable; the failed attempt is audited). It is therefore not on this permission
> matrix. Like the inbound carrier webhook, trust is established without a user role.

### Crew-Station Kiosk

| Permission | Admin | Manager | Supervisor | Operator | Quality | Shipping | Viewer |
|------------|:-----:|:-------:|:----------:|:--------:|:-------:|:--------:|:------:|
| Manage kiosk stations (create / list / reset-PIN / revoke) | ✓ | ✓ | | | | | |

> **Per-endpoint mapping (`/api/v1/shop-floor/kiosk-stations` + `POST /auth/kiosk-badge-token`).**
> All four station-administration endpoints (`POST /kiosk-stations`, `GET /kiosk-stations`,
> `POST /kiosk-stations/{id}/revoke`, `POST /kiosk-stations/{id}/reset-pin`, in
> `app/api/endpoints/shop_floor.py`) are `require_role([ADMIN, MANAGER])` — the same set as
> visitor sign-in stations. Everything the crew terminal itself does is **not** on the role
> matrix, because neither of its credentials is a role-bearing user session:
>
> - The **station token** (`type="kiosk"`, PIN-minted via the public rate-limited
>   `POST /shop-floor/kiosk-stations/station-login`) carries no user identity and is honored by
>   exactly two things — the roster-enriched work-center-queue read (its own bound work center
>   only, via the dedicated `get_kiosk_or_user` dependency) and the badge-token mint. Every other
>   endpoint rejects it with **401**; tenant scope and revocation come from the authoritative
>   `kiosk_stations` row, never the client `cid`.
> - The **badge-minted operator token** (`POST /auth/kiosk-badge-token`, station-token-gated) is
>   a 5-minute `scope="kiosk"` access token for the badge-identified user — on the allowed paths
>   the operator IS `current_user`, so the shop-floor endpoints' existing role/tenant/audit rules
>   apply unchanged and every labor mutation is attributed to the operator, never the station.
>   Outside `/api/v1/shop-floor/*` (+ `POST /auth/employee-logout`) the token is **403**
>   (path-fenced in `get_current_user`). No refresh token is ever minted.
>
> Station lifecycle (create / reset-PIN / revoke), station-login failures, and badge-token
> issuance/failures all write tamper-evident audit rows. See [docs/API.md](API.md) →
> Authentication → Kiosk station tokens, and [docs/KIOSK.md](KIOSK.md) → Crew station mode.

### Admin

| Permission | Admin | Manager | Supervisor | Operator | Quality | Shipping | Viewer |
|------------|:-----:|:-------:|:----------:|:--------:|:-------:|:--------:|:------:|
| Settings | ✓ | | | | | | |
| Integrations (`admin:integrations`) | ✓ | | | | | | |
| Audit Logs | ✓ | ✓ | | | | | |
| AI usage & cost summary (`/ai-usage/summary`) | ✓ | ✓ | | | | | |
| AI egress kill switch (`PUT /companies/me/ai-egress`) | ✓ | | | | | | |
| Wallboard display tokens (`/auth/display-token` issue/list/revoke) | ✓ | ✓ | | | | | |
| Visitor sign-in stations (`/visitor-logs/stations` create/list/revoke/reset-pin) | ✓ | ✓ | | | | | |
| Crew kiosk stations (`/shop-floor/kiosk-stations` create/list/revoke/reset-pin) | ✓ | ✓ | | | | | |
| System | ✓ | | | | | | |

> **Integrations (carrier-account credentials + shipping profile) — endpoint mapping.** The
> carrier-integration admin console — `app/api/endpoints/integrations.py`, mounted under
> `/api/v1/admin/settings` — is enforced **in code** to `require_role([ADMIN])` on every route:
> the carrier-account CRUD (`GET`/`POST`/`PUT`/`DELETE …/carrier-accounts`), the credential-only
> `POST …/carrier-accounts/{id}/test-connection`, and the company shipping-profile
> `GET`/`PUT …/shipping-profile` (which holds the `allow_carrier_egress` kill switch). Carrier
> secrets are write-only (Fernet-encrypted, never returned — only `api_key_last4` /
> `has_webhook_secret`); deletes are soft deletes; create/update/delete and the egress toggle are
> audited. The new `admin:integrations` permission string (granted to **Admin** in
> `app/models/role_permission.py`) drives the frontend Carrier Integrations tab's visibility. See
> [docs/SHIPPING_CARRIER_INTEGRATION.md](SHIPPING_CARRIER_INTEGRATION.md).
>
> **Inbound carrier webhook is unauthenticated (by design).** `POST /api/v1/webhooks/carriers/{provider}`
> (`app/api/endpoints/carrier_webhooks.py`) has **no auth dependency** — a carrier cannot present a
> JWT. Trust is established by **HMAC signature** verification against the stored per-tenant webhook
> secret, and the owning tenant is resolved **only from stored shipment data**
> (`Shipment.aggregator_shipment_id`), never from caller input. A request that matches no secret or no
> shipment is dropped with **204** (no existence oracle). It is therefore not on this permission matrix.
>
> **Intentionally-unauthenticated endpoints (the full set).** Four write/verify surfaces establish trust
> *without* a user role — and so none appears on the role matrix: the **carrier webhook** above (HMAC
> signature), the visitor **`station-login`** (a shared station PIN mints a scoped `signin` token — see
> Visitor Logs above), the crew-kiosk **`station-login`** (`POST /shop-floor/kiosk-stations/station-login`,
> a shared station PIN mints a scoped `kiosk` token — see Crew-Station Kiosk above), and the wallboard
> **display-token** verification (a scoped `display` JWT — see below). Each binds the request to a
> tenant through stored server-side state, never caller-supplied identity.

> **Audit-log access (tenant-scoped).** The **Audit Logs** row above covers audit *retrieval*:
> `GET /api/v1/audit/`, `/audit/summary`, `/audit/actions`, `/audit/resource-types`
> (`require_role([ADMIN, MANAGER])`). These are **tenant-scoped** — each filters by the caller's
> active company (`get_current_company_id`), so Admin/Manager see only their own company's audit
> data.
>
> **Audit-integrity endpoints (`/api/v1/audit/integrity/*`).** These verify the tamper-evident
> hash chain and are authorized separately from retrieval:
>
> | Endpoint | Role | Scope |
> |----------|------|-------|
> | `GET /audit/integrity/status` | **Platform Admin only** (`require_platform_admin`) | Global chain |
> | `GET /audit/integrity/verify` | **Platform Admin only** | Global chain |
> | `GET /audit/integrity/verify-recent` | **Platform Admin only** | Global chain |
> | `GET /audit/integrity/record/{sequence_number}` | **Admin** (`require_role([ADMIN])`) | **Own active company only** |
>
> The three aggregate endpoints are Platform-Admin-only because the hash chain is a single global
> sequence interleaved across all tenants — its stats/issues (record counts, sequence ranges,
> record ids) can't be scoped to one company without leaking other tenants' data. The per-record
> endpoint serves a company Admin's "are *my* records intact?" need: a company-scoped Admin may
> verify only a record belonging to their active company, and a cross-tenant record returns
> **404** (not 403) so it can't be used to probe for another company's records. Platform Admins /
> superusers may verify any record (superuser bypasses role checks, as elsewhere).

> **AI usage & cost summary.** `GET /api/v1/ai-usage/summary` (`app/api/endpoints/ai_usage.py`)
> is enforced **in code** via `require_role([ADMIN, MANAGER])` and is **tenant-scoped** to the
> caller's active company. It returns read-only per-task / per-model aggregates over the
> `ai_usage_events` LLM telemetry ledger (operational telemetry, not audit data — see
> [docs/API.md](API.md) → AI Usage Telemetry). Note the **Manager allowance is currently dormant
> in the UI**: the only consuming surface is the Admin Settings → AI Usage & Cost tab, and
> `/admin/settings` is AdminRoute-gated (admin role / superuser), so Managers can exercise this
> permission only via direct API calls today.

> **AI egress kill switch (`PUT /api/v1/companies/me/ai-egress`).** Enforced **in code** via
> `require_role([ADMIN])` — **Admin-only**, matching the sibling CUI egress kill switches
> (`allow_carrier_egress` / `allow_print_egress`, also Admin-only): flipping the CUI boundary is a
> decision reserved to Admins. It only ever mutates the caller's **own active company**
> (`get_current_company_id`; the company is never taken from the request body). Flipping the
> `Company.allow_ai_egress` CUI control writes tamper-evident `audit_log` rows (a field update **and**
> an `ai_egress_enabled` / `ai_egress_disabled` status change). The toggle is surfaced in the UI at
> **Admin Settings → AI Privacy** (`/admin/settings?tab=aiprivacy`); within that tab the control is
> interactive for ADMIN (enabling egress requires explicit confirmation) and read-only for
> other roles. See [docs/API.md](API.md) →
> Company (self-service) and [docs/AI_QUOTING_AGENT_RUNBOOK.md](AI_QUOTING_AGENT_RUNBOOK.md).

> **Wallboard display tokens (`/auth/display-token`, A0.5).** Issue / list / revoke are enforced
> **in code** via `require_role([ADMIN, MANAGER])` and tenant-scoped to the active company;
> issuance and revocation write tamper-evident `audit_log` rows. **A display token is not a role
> and carries no user identity** — it is a single-endpoint credential for an unattended TV. What it
> **can** do: authenticate the read-only `GET /shop-floor/wallboard` (via the dedicated
> `get_display_or_user` dependency), scoped to the issuing company (taken from the `display_tokens`
> DB row, never from the client). What it **cannot** do: reach any other endpoint (`verify_token`
> accepts only `type == "access"` JWTs, so a display token gets **401** everywhere else), write
> anything (the wallboard endpoint performs zero writes), or outlive revocation/expiry (the DB row
> is re-checked on every request; a revoked token dies on the TV's next ~30s poll). As with AI
> usage above, the **Manager allowance is currently UI-dormant**: the managing surface is Admin
> Settings → Wallboard Displays and `/admin/settings` is AdminRoute-gated, so Managers can exercise
> it only via direct API calls today. See [docs/API.md](API.md) → Authentication → Display tokens
> and [docs/WALLBOARD.md](WALLBOARD.md).

## Backend Implementation

### Using `require_role` Dependency

```python
from app.api.deps import require_role
from app.models.user import UserRole

@router.post("/work-orders")
def create_work_order(
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR]))
):
    # Only admin, manager, and supervisor can create work orders
    ...
```

### Available Roles

```python
class UserRole(str, enum.Enum):
    ADMIN = "admin"
    MANAGER = "manager"
    SUPERVISOR = "supervisor"
    OPERATOR = "operator"
    QUALITY = "quality"
    SHIPPING = "shipping"
    VIEWER = "viewer"
```

## Frontend Implementation

### Using Permission Components

```tsx
import { PermissionGate, CanCreate, CanEdit, CanDelete, AdminOnly } from './components/PermissionGate';

// Single permission check
<PermissionGate permission="work_orders:create">
  <CreateButton />
</PermissionGate>

// Any of multiple permissions
<PermissionGate anyOf={['work_orders:edit', 'work_orders:delete']}>
  <ActionMenu />
</PermissionGate>

// Convenience components
<CanCreate resource="work_orders">
  <CreateButton />
</CanCreate>

<AdminOnly>
  <AdminPanel />
</AdminOnly>
```

### Using Permission Hook

```tsx
import { usePermissions } from './hooks/usePermissions';

function MyComponent() {
  const { can, canAny, isAdmin, role } = usePermissions();
  
  if (can('work_orders:create')) {
    // Show create button
  }
  
  if (isAdmin) {
    // Show admin features
  }
}
```

### Protected Routes

```tsx
import { ProtectedRoute, AdminRoute } from './components/ProtectedRoute';

<Route path="/admin" element={
  <ProtectedRoute requireAdmin>
    <AdminPage />
  </ProtectedRoute>
} />

<Route path="/users" element={
  <ProtectedRoute permission="users:view">
    <UsersPage />
  </ProtectedRoute>
} />
```

## Superuser Override

Users with `is_superuser=true` bypass all permission checks. This is reserved for system administrators who need full access regardless of role assignment.

## Adding New Permissions

1. **Backend**: Add new endpoint with `require_role()` dependency
2. **Frontend**: 
   - Add permission to `Permission` type in `utils/permissions.ts`
   - Add to appropriate role arrays in `ROLE_PERMISSIONS`
   - Use `PermissionGate` or `usePermissions` in components

## Security Notes

- Permissions are checked on BOTH frontend (UI) and backend (API)
- Frontend checks are for UX only - they can be bypassed
- Backend checks are the authoritative security layer
- Always verify permissions server-side before performing actions
