# Thermal Receiving-Label Printing Runbook

This runbook covers the 4×6 thermal **receiving label** that the system renders
when inventory is received and sends to a **ProxyBox Zero** (pbxz.io) bridge that
drives a **Westinghouse WHTP203e** direct-thermal printer. It is the operational
source of truth for configuring the printer bridge, the per-company egress kill
switch, manual reprint vs. auto-print-on-receipt, where labels are stored, and
troubleshooting.

> **Scope.** This is the goods-receiving label printed off a PO receipt (part /
> rev / qty / lot / Code128, with a CRITICAL banner for critical-characteristic
> parts). It is separate from the multi-carrier shipping label
> ([docs/SHIPPING_CARRIER_INTEGRATION.md](SHIPPING_CARRIER_INTEGRATION.md)),
> though both follow the same encrypted-credential + egress-kill-switch pattern.

## Architecture

The application never talks to the printer directly. A rendered PDF is tunneled to
the locally-attached printer through ProxyBox:

```
receive_material (receiving.py) ──enqueue (best-effort)──► ARQ print_receiving_label_job
manual reprint  (receiving.py /print-label)                        │
        │  thin: validate → call service → map errors              │
        ▼                                                          ▼
PrintService  (app/services/print_service.py)
        │  egress kill switch, tenant scoping, Document storage, audit, event
        ├──► label_service.build_receiving_label_pdf  → 4x6 PDF bytes (reportlab)
        └──► ProxyBoxClient  (app/services/proxybox_client.py)
                   │  POST /print/{target}  (base64 PDF) → poll GET /jobs/{id}
                   ▼
             ProxyBox Zero device  ──USB──►  Westinghouse WHTP203e
```

### What ProxyBox is

ProxyBox Zero (pbxz.io) is a small bridge device that exposes an HTTPS print API
and relays jobs to a USB-attached printer on the same local network. There are two
addressing modes:

- **Cloud tunnel** — a `https://pbx-xxxx.pbxz.cloud/...` base URL that reaches the
  device over ProxyBox's relay. **This is the mode to use here:** the backend runs
  on Railway (a hosted cloud) and has no route to the printer's LAN, so it must
  reach the device through the cloud tunnel rather than a local address.
- **Local address** — a `*.pbxz.io` / LAN address reachable only from the same
  network as the device. Not usable from the Railway-hosted backend.

`CompanyPrintProfile.proxybox_base_url` holds the **full** base including the API
version path (e.g. `https://pbx-xxxx.pbxz.cloud/api/v1`).

### Hardware — PDF, not ZPL

The WHTP203e is a **raster / driver-based** direct-thermal printer, **not** a
ZPL/EPL label-language printer. So the renderer produces a **PDF** sized to the
exact 4×6 media and ProxyBox rasterizes it to the printer — we do **not** emit ZPL.

- `label_service.build_receiving_label_pdf` renders with reportlab onto a fixed
  `4in × 6in` page via `canvas.Canvas` (absolute top→bottom layout, not a flowing
  document).
- **Monochrome only** — direct-thermal has no color. The CRITICAL banner is a
  filled black rectangle with reversed (white) text, never a colored fill.
- A `fmt` argument is reserved for a future ZPL fast-path; only `"pdf"` is
  implemented today (any other value raises `ValueError`).

## Data model

| Object | Where | Purpose |
|--------|-------|---------|
| `CompanyPrintProfile` | `app/models/print_profile.py` (table `company_print_profiles`) | One row per company (TenantMixin + OptimisticLockMixin, unique `company_id`): `proxybox_base_url`, `proxybox_target`, Fernet-encrypted `encrypted_api_key` + display-only `api_key_last4`, `default_paper_size` (`"4x6"`), `default_copies` (`1`), `auto_print_on_receipt`, **`allow_print_egress`** (kill switch), `is_active`. |
| `POReceipt.label_document_id` | `app/models/purchasing.py` | Nullable FK → `documents.id` linking a receipt to the label it produced. |
| `DocumentType.RECEIVING_LABEL` | `app/models/document.py` (`"receiving_label"`) | Document type for stored label PDFs. |

Migrations: **`051_receiving_label_printing.py`** (creates `company_print_profiles`,
adds `po_receipts.label_document_id` + FK) and **`052_document_type_receiving_label.py`**
(adds the `RECEIVING_LABEL` value to the Postgres `documenttype` enum, isolated in
its own revision because `ALTER TYPE … ADD VALUE` must run outside the migration
transaction — same pattern as 047 for the shipping-label enum values). 052 must be
applied together with (immediately after) 051 before the auto-print path is enabled.

The API key is **Fernet-encrypted at rest** via the shared carriers crypto helper
(`app/services/carriers/crypto.py`), which resolves its key from
`INTEGRATION_ENCRYPTION_KEY`, falling back to `WEBHOOK_ENCRYPTION_KEY` — the **same**
key used for carrier secrets. No new encryption key is introduced.

## Configuration

### 1. Encryption key

The ProxyBox API key reuses the carrier-secret encryption key
(`INTEGRATION_ENCRYPTION_KEY`, falling back to `WEBHOOK_ENCRYPTION_KEY`). See
[docs/ENVIRONMENT_VARIABLES.md](ENVIRONMENT_VARIABLES.md#external-services). The
app and migrations boot without it, but in `production`/`staging` **storing or using
a ProxyBox key fails loudly** until at least one of those keys is set (CMMC SC-28).
If carrier integration is already configured, no new key is needed.

### 2. ProxyBox timing knobs (optional)

Three optional settings tune the HTTP timing only (per-company connection details
live on the profile, not in env):

| Variable | Default | Meaning |
|----------|---------|---------|
| `PROXYBOX_TIMEOUT_SECONDS` | `30.0` | Per-request httpx timeout |
| `PROXYBOX_POLL_INTERVAL_SECONDS` | `1.0` | Job-status poll cadence |
| `PROXYBOX_MAX_WAIT_SECONDS` | `30.0` | Max wait for a terminal job state before returning a non-failed `timeout` result |

### 3. Set up the device / target and enter the API key (admin)

Admin Settings → **Label Printing** tab (`frontend/src/pages/AdminSettings.tsx`),
backed by `GET` / `PUT /api/v1/receiving/print-profile` (admin-only). Provide:

- **`proxybox_base_url`** — the full cloud-tunnel base incl. the API version path,
  e.g. `https://pbx-xxxx.pbxz.cloud/api/v1`.
- **`proxybox_target`** — the target printer identifier registered on the ProxyBox
  device.
- **`api_key`** — **write-only**, Fernet-encrypted at rest, never returned
  (responses expose only `api_key_last4` and `has_api_key`). Sending it rotates the
  stored key; omitting it leaves the existing one.
- **`default_copies`** (1–20) and **`default_paper_size`** (`"4x6"`).
- **`auto_print_on_receipt`** and **`allow_print_egress`** toggles (see below).
- **`is_active`** — deactivate the profile rather than deleting it (there is no
  delete route; the egress gate treats an inactive profile as "off").

To find the target, register the WHTP203e on the ProxyBox device per the pbxz.io
device setup, copy the device's API base URL + API key from the ProxyBox dashboard,
and use the printer's registered target name as `proxybox_target`.

## The `allow_print_egress` kill switch (CUI / CMMC sign-off)

**`allow_print_egress` defaults FALSE.** Until an admin explicitly enables it,
`PrintService._require_egress` raises `PrintEgressDisabledError` (→ **HTTP 409** on
the manual route; a no-op for the auto-print job) and **no outbound call to the
ProxyBox tunnel is made**.

This is the data-egress control: a label PDF is transmitted to a third-party cloud
relay (`*.pbxz.cloud`) on its way to the printer, and the label carries part
numbers, lot/heat/serial traceability, and (for critical parts) a CRITICAL marker.
Under CMMC / a DoD contract that outbound transmission may need explicit human
sign-off before it is permitted; this switch is the gate. **Treat enabling it as a
compliance decision**, not a routine setting.

The gate also requires a **complete** profile: an active profile with
`allow_print_egress = true` **and** a `proxybox_base_url`, `proxybox_target`, and a
stored API key. A missing piece raises `PrintEgressDisabledError` too.

Flipping the switch is audited as a **status change** on the tamper-evident trail
(`Label-print egress ENABLED/DISABLED for company …`) via `AuditService`, so
enabling and disabling print egress is on the audit log. Profile create/update is
also audited (an update flags `api_key_rotated` rather than recording the value);
the plaintext key never appears in audit or operational-event payloads.

## Label layout & fields

`build_receiving_label_pdf` lays out, top → bottom:

1. **CRITICAL CHARACTERISTIC** banner — reversed white-on-black, **only** when the
   received part has `Part.is_critical`.
2. **Part number + Rev** (largest, bold).
3. **Description** (measured and ellipsis-truncated to the print width — reportlab
   does not wrap).
4. **QTY + UOM** (bold, prominent).
5. **Traceability block** — `LOT` (always), `HEAT` (if present), `SERIAL` (if
   present).
6. **Source block** — `PO`, `VENDOR`, `RECEIPT`, `RECEIVED` (date).
7. **Destination** — `BIN: {location_code}` (bold), the receipt's location.
8. **Code128 barcode of the lot number**, anchored at the bottom margin, with the
   human-readable lot centered beneath the bars.

The fields are pulled from the `POReceipt` and its joined part / PO / vendor /
location, tenant-scoped to the active company.

## Manual reprint vs. auto-print

### Manual (re)print

`POST /api/v1/receiving/receipt/{receipt_id}/print-label` (roles
**Admin / Manager / Supervisor** — the same gate as `POST /receiving/receive`).
Optional body `{ "copies": <1–20> }` overrides the profile default. The route is
`async`: it awaits the print and returns a real success/failure.

In the UI, **Receiving** (`frontend/src/pages/Receiving.tsx`) surfaces this as a
**"Print label"** action on the receive-success toast and a **"Label"** button per
row in the receiving views (visible to Admin / Manager / Supervisor). A 409 surfaces
as "Label printing isn't enabled — an admin can configure it in print settings."

### Auto-print on receipt

When material is received, `receive_material` enqueues the ARQ job
`print_receiving_label_job` (registered in `app/worker.py` → delegates to
`app/jobs/label_jobs.py::print_receiving_label_task`) **best-effort, after the
receipt commits** — a printer/tunnel/Redis problem can never fail or block an
already-committed receipt.

**The job is the sole decider of whether to print.** It is a no-op unless the
company's profile is **active** with **both** `auto_print_on_receipt` **and**
`allow_print_egress` ON (and a usable target configured). The two toggles are
independent: auto-print is gated on `auto_print_on_receipt`, and *any* egress is
additionally gated on `allow_print_egress`. The job never raises out of the worker;
a print failure is logged (never leaking the key) and recorded as a
`receiving_label_print_failed` operational event.

## Where labels are stored

The rendered PDF is persisted as a standard `Document`
(`DocumentType.RECEIVING_LABEL`), linked from the receipt via
`POReceipt.label_document_id`. Bytes go through the shared storage service
(`app/services/storage_service.py`) — the **same store as every other document**:
`{company_id}/receiving/{uuid}.pdf` with an S3 backend, or the resolved local upload
dir in dev.

**Record retention first:** `PrintService.print_receipt_label` renders, stores the
Document, links it, audits, emits, and **commits before the network call** — so the
label is retained for reprint even if the printer is unreachable. A ProxyBox failure
then propagates to the caller (→ 502 on the manual route) *after* the Document is
safely committed.

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| Manual reprint returns **409** ("egress is disabled" / "profile is incomplete") | `allow_print_egress` is OFF, the profile is inactive, or base URL / target / API key is missing | Admin → **Label Printing**: complete the profile and enable `allow_print_egress`. |
| Manual reprint returns **502** | ProxyBox/printer error — device offline, printer out of media, bad target, auth rejected | Check the device is online and the printer is powered/loaded; verify `proxybox_target` and that the API key is current. The label Document is still saved — reprint once fixed. |
| Manual reprint returns **404** | Receipt not found or belongs to another tenant | Confirm the receipt id and the active company. |
| Auto-print silently does nothing | Job no-op gate not met | Profile must be **active** with **both** `auto_print_on_receipt` and `allow_print_egress` ON. Check for a `receiving_label_print_failed` operational event for an actual failure vs. a deliberate no-op. |
| Job returns a non-terminal `timeout` (no failure raised) | The bridge accepted the job but no terminal status arrived within `PROXYBOX_MAX_WAIT_SECONDS` | The job may still print on the device. Raise `PROXYBOX_MAX_WAIT_SECONDS` if your device reports slowly. A genuine terminal *failure* status raises (→ 502 manual) instead. |
| Wrong paper size / clipped label | `default_paper_size` not `"4x6"`, or the printer media/driver isn't 4×6 | Set `default_paper_size` to `4x6` and confirm the WHTP203e is loaded with 4×6 direct-thermal media and registered at that size on the device. |
| "No ProxyBox API key configured" | Egress is on but no key stored | Re-enter the `api_key` in the Label Printing tab (write-only; rotates the stored key). |

## RBAC summary

| Action | Roles | Enforcement |
|--------|-------|-------------|
| Print / reprint a receiving label (`POST /receiving/receipt/{id}/print-label`) | **Admin / Manager / Supervisor** | `require_role([ADMIN, MANAGER, SUPERVISOR])` (same gate as `POST /receiving/receive`) |
| Read / configure the print profile (`GET` / `PUT /receiving/print-profile`) | **Admin** | `get_admin_user` |

See [docs/RBAC_PERMISSIONS.md](RBAC_PERMISSIONS.md#receiving).

## Related files

- Model: `backend/app/models/print_profile.py` (`CompanyPrintProfile`),
  `backend/app/models/purchasing.py` (`POReceipt.label_document_id`),
  `backend/app/models/document.py` (`DocumentType.RECEIVING_LABEL`)
- Schemas: `backend/app/schemas/print_profile.py`
- Services: `backend/app/services/label_service.py` (PDF renderer),
  `backend/app/services/proxybox_client.py` (ProxyBox HTTP client),
  `backend/app/services/print_service.py` (orchestration + `PrintEgressDisabledError`)
- Endpoints: `backend/app/api/endpoints/receiving.py` (print-label + print-profile)
- Job: `backend/app/jobs/label_jobs.py` (+ `app/worker.py` registration of
  `print_receiving_label_job`)
- Config: `backend/app/core/config.py` (`PROXYBOX_*`, reuses
  `INTEGRATION_ENCRYPTION_KEY`)
- Migrations: `backend/alembic/versions/051_receiving_label_printing.py`,
  `052_document_type_receiving_label.py`
- Frontend: `frontend/src/pages/AdminSettings.tsx` (Label Printing tab),
  `frontend/src/pages/Receiving.tsx` (Print label / Label buttons)
- API reference: [docs/API.md](API.md#receiving--inspection) → Receiving & Inspection
