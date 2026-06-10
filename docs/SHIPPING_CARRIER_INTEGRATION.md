# Multi-Carrier Shipping Integration Runbook

This runbook covers the multi-carrier shipping integration: rate-shopping, label
purchase, freight Bills of Lading, pickups, and tracking through a swappable
carrier-aggregator abstraction (EasyPost today). It is the operational source of
truth for configuring a carrier account, the customer-data egress kill switch,
inbound webhooks, the tracking poll cron, and label/BOL storage.

> **Honest status — read this first.**
> - **PARCEL (rate-shop / buy-label / track / pickup) is implemented** end-to-end
>   against EasyPost's documented v2 REST API.
> - **LTL FREIGHT (`buy-bol`) is scaffolded behind the same interface but NOT
>   functional on EasyPost.** EasyPost LTL is an Enterprise-gated feature with no
>   public REST wire format we can implement/verify, so the EasyPost adapter
>   raises `NotSupportedError` for freight and the API returns **HTTP 501**. The
>   freight path is real at the service/model/schema/UI layers and waits on a
>   future Zenkraft (or EasyPost Enterprise) adapter. See
>   [Adding a freight-capable provider](#adding-a-freight-capable-provider).

## Architecture

### The swappable `CarrierProvider` abstraction

The rest of the application never talks to a carrier SDK directly. Everything
flows through three layers:

```
endpoints (shipping.py / integrations.py / carrier_webhooks.py)
        │  thin: validate → call service → map errors
        ▼
ShippingService  (app/services/shipping_service.py)
        │  egress kill switch, tenant scoping, audit, idempotency,
        │  Document storage, persistence of quotes/packages/tracking
        ▼
carriers.registry.get_provider(account) ──► CarrierProvider (ABC)
        │                                         ▲
        │                                         │ implements
        └────────────────────────────────► EasyPostProvider (only adapter today)
```

- **`app/services/carriers/types.py`** — the normalized, provider-agnostic shapes
  the whole app sees: `CarrierAddress`, `ParcelDimensions`, `PalletDimensions`,
  `RateQuote`, `Label`, `BillOfLading`, `Pickup`, `TrackingEvent`,
  `AddressValidationResult`, `ParsedTrackingWebhook`, and the normalized
  `TrackingStatus` enum. **Money and physical dimensions are `Decimal`**, never
  float.
- **`app/services/carriers/base.py`** — `CarrierProvider`, the abstract interface
  every adapter implements (`validate_address`, `get_rates`, `buy_label`,
  `create_freight_shipment`, `buy_bol`, `schedule_pickup`, `get_tracking`,
  `parse_tracking_webhook`, `verify_webhook_signature`). Class flags
  `supports_freight` / `supports_pickup` advertise capability.
- **`app/services/carriers/easypost_adapter.py`** — `EasyPostProvider`, the only
  concrete adapter today. Talks raw `httpx` to `https://api.easypost.com/v2` (no
  EasyPost SDK dependency). Auth is HTTP Basic with the API key as username and an
  empty password. Maps EasyPost JSON onto the normalized types.
  `supports_freight = False`, `supports_pickup = True`.
- **`app/services/carriers/registry.py`** — `get_provider(carrier_account)`, the
  single swap point. Reads `carrier_account.provider`, decrypts the API key
  in-memory, and returns the matching adapter. `"easypost"` → `EasyPostProvider`;
  `"zenkraft"` → `NotSupportedError` (placeholder until that adapter lands); any
  other value → `NotSupportedError`.
- **`app/services/carriers/crypto.py`** — Fernet encrypt/decrypt for carrier
  secrets. Key resolution: `INTEGRATION_ENCRYPTION_KEY`, falling back to
  `WEBHOOK_ENCRYPTION_KEY`, finally an ephemeral generated key (dev/test only).
- **`app/services/carriers/exceptions.py`** — typed errors the service maps onto
  clean HTTP responses: `CarrierError` (base, → 502 / 404), `RateUnavailableError`,
  `LabelPurchaseError`, `AddressInvalidError` (→ 422), `NotSupportedError` (→ 501),
  `WebhookVerificationError`, and `EgressDisabledError` (→ 409).

### Adding a freight-capable provider

Adding a new aggregator (e.g. Zenkraft for native FedEx Freight / LTL) is a
**registry change plus one new adapter** — the service layer, endpoints, models,
schemas, and UI are unchanged:

1. Implement `ZenkraftProvider(CarrierProvider)` in
   `app/services/carriers/` (a new `zenkraft_adapter.py`), mapping the Zenkraft
   wire format onto the normalized `types`. Implement the freight path
   (`create_freight_shipment` → returns a provider shipment id; `buy_bol` →
   returns a normalized `BillOfLading`) and set `supports_freight = True`.
2. Wire it into `registry.get_provider` under the `"zenkraft"` branch (replace the
   current `NotSupportedError`).
3. Money stays `Decimal`; emit `provider_rate_id` as the composite
   `"<provider_shipment_id>:<rate_id>"` so the existing `buy_*` flow recovers the
   owning shipment, exactly as the EasyPost adapter does.

No DB migration is required for a new provider — `carrier_accounts.provider` is a
free-text column. The freight columns on `shipments` / `shipment_packages`
(`freight_class`, `nmfc_code`, `pallet_count`, `pro_number`, `bol_number`,
`bol_document_id`, `accessorials`) already exist (migration 046).

## Data model (migration 046 / 047)

New / changed tables (all tenant-scoped via `TenantMixin`, `company_id` non-null +
indexed):

| Table | Mixins | Purpose |
|-------|--------|---------|
| `carrier_accounts` | Tenant + SoftDelete + OptimisticLock | Per-company aggregator credentials (Fernet-encrypted `encrypted_api_key` / `webhook_secret_encrypted`), opaque `carrier_refs`, `is_active` / `is_default`. Unique `(company_id, name)`. |
| `company_shipping_profiles` | Tenant + OptimisticLock | One row per company: ship-from origin (decomposed, label-grade), package defaults, and **`allow_carrier_egress`** (default FALSE). Unique `(company_id)`. |
| `shipment_packages` | Tenant + SoftDelete | Boxes / pallets per shipment (`Numeric` dims), freight class / NMFC. |
| `shipment_rate_quotes` | Tenant | Persisted rate-shop results (compliance: "why this carrier / price"). `Numeric(12,2)` amounts. |
| `shipment_tracking_events` | Tenant | Append-only tracking events (webhook / poll), de-duped by `provider_event_id`. Not soft-deletable. |

`shipments` gained `SoftDeleteMixin` and ~24 carrier columns (all nullable,
backward-compatible with the legacy manual flow): `carrier_account_id`,
`ship_mode` (`parcel` / `freight` / `manual`), `aggregator_shipment_id` (indexed —
the webhook tenant-resolution key), `selected_rate_id`, `service_code`,
`label_document_id` / `bol_document_id`, `estimated_cost` / `actual_cost`
(`Numeric(12,2)`) / `cost_currency`, `label_purchased_at` / `voided_at` /
`refund_status`, `tracking_status` / `tracking_status_detail` /
`last_tracking_sync_at`, the freight fields above, and `idempotency_key` (a
partial-unique index `uq_shipment_idempotency` on `(company_id, idempotency_key)
WHERE idempotency_key IS NOT NULL`).

`DocumentType` gained `SHIPPING_LABEL` and `BILL_OF_LADING` (migration 047 adds
those values to the Postgres `documenttype` enum, idempotently and Postgres-only).

## Configuration

### 1. Encryption key

Set `INTEGRATION_ENCRYPTION_KEY` to a Fernet key (it falls back to
`WEBHOOK_ENCRYPTION_KEY` if unset). See
[docs/ENVIRONMENT_VARIABLES.md](ENVIRONMENT_VARIABLES.md#external-services).
The app and migrations boot without it, but in `production`/`staging`
**creating or using a carrier account (or verifying an inbound webhook) fails
loudly** until at least one of these keys is set (CMMC SC-28) — a loud startup
warning is logged meanwhile. It is never silently replaced by an ephemeral key in
prod/staging (that would leave stored secrets undecryptable after a restart); the
ephemeral generated-key fallback exists only in dev/test.

Generate one:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

### 2. Carrier account (admin)

Admin Settings → **Carrier Integrations** tab
(`frontend/src/components/admin/CarrierIntegrationsTab.tsx`), backed by
`POST /api/v1/admin/settings/carrier-accounts`. Provide:

- `name` (unique per company), `provider` (`easypost`), `environment`
  (`production` / `test`).
- `api_key` — **write-only**, Fernet-encrypted at rest, never returned (responses
  expose only `api_key_last4`).
- `webhook_secret` — optional, write-only, used to verify inbound webhooks (see
  below). Exposed only as `has_webhook_secret`.
- `carrier_refs` — optional opaque bring-your-own-carrier account handles, e.g.
  `{"fedex": "...", "ups": "..."}` (NOT secrets).
- `is_default` — at most one default account per company (the service uses it when
  no `carrier_account_id` is passed).

Run **Test connection**
(`POST …/carrier-accounts/{id}/test-connection`) to validate the credential. This
is the **only** carrier round-trip exempt from the egress kill switch — it sends
no customer data, only authenticates the stored key (a benign `GET /users`).

Deleting a carrier account is a **soft delete** (it may be referenced by purchased
labels/BOLs); it is never physically removed.

### 3. Ship-from profile + egress (admin)

`Company.address` is free text and unusable for labels, so the ship-from origin is
decomposed into discrete fields on `company_shipping_profiles`. Configure it via
`PUT /api/v1/admin/settings/shipping-profile` (ship-from name/company/phone/email/
street/city/state/zip/country + optional package defaults). A profile is created
on first PUT (404 until then).

## The `allow_carrier_egress` CUI kill switch

**`allow_carrier_egress` defaults FALSE.** Until an admin explicitly enables it on
the company shipping profile, `ShippingService._require_egress` raises
`EgressDisabledError` (→ **HTTP 409**) and **no external carrier call is made** for
any operation that transmits customer data:

- address validation
- rate-shop
- buy-label / buy-bol
- schedule-pickup
- void / refund

This is the data-egress / CUI control: validating, rate-shopping, or labeling a
shipment sends the **customer ship-to address** (and ship-from identity) to a
third-party aggregator. Under CMMC / a DoD contract that egress may need explicit
human sign-off before it is permitted. The switch is the gate.

Exempt from the switch:

- **`test-connection`** — sends only the stored credential, no customer data.
- **Inbound tracking webhooks + the poll cron's *apply* step** — these *receive*
  data; the apply path makes no outbound call. (The poll cron's *fetch* step is
  outbound and **does** honor the switch — it only polls tenants with egress ON.)

Flipping the switch is audited as a **status change** on the tamper-evident trail
(`Carrier customer-data egress ENABLED/DISABLED for company …`), so enabling and
disabling egress is on the audit log.

## Webhooks (inbound tracking)

Carriers POST tracking updates to `POST /api/v1/webhooks/carriers/{provider}`
(e.g. `/webhooks/carriers/easypost`). This is the **only unauthenticated route in
the app** — a carrier cannot present a JWT. Trust and tenancy are established
without any caller-supplied identity:

1. The raw body is read **before** parsing (HMAC is computed over the exact bytes).
2. Candidate `carrier_accounts` for `{provider}` that have a webhook secret are
   gathered **across all tenants** (the caller didn't say which).
3. The signature is verified against each candidate's **decrypted** webhook secret
   using the provider's constant-time `verify_webhook_signature`. EasyPost signs
   with HMAC-SHA256 over the raw body, hex-encoded, as
   `hmac-sha256-hex=<hex>` in the `X-Hmac-Signature` header (the secret is
   NFKD-normalized first). No match → **204** with no body (no existence oracle).
4. The owning `Shipment` (and thus `company_id`) is resolved **exclusively from
   STORED data** — the shipment's `aggregator_shipment_id` (or `tracking_number`)
   — never from the path/body. No match → **204**.
5. The normalized events are enqueued to the ARQ `process_tracking_webhook_job`
   with the **resolved** `company_id` + `shipment_id`; the handler returns **200**
   fast. The DB write (de-dup + status flow-back) happens in the job.

### Webhook setup

- In the carrier dashboard (EasyPost), register the webhook URL
  `https://<your-api-host>/api/v1/webhooks/carriers/easypost` and copy the signing
  secret.
- Store that secret as the carrier account's `webhook_secret` (admin update). It
  is Fernet-encrypted at rest; only `has_webhook_secret` is ever surfaced.
- Tracking events update `tracking_status` / `tracking_status_detail` /
  `last_tracking_sync_at`; a `DELIVERED` event sets `actual_delivery`. Tracking is
  **informational only** — it never auto-closes the work order (the manual
  `mark_shipped` path remains the only WO-closing action).

## Tracking poll cron (fallback)

If a webhook is missed, the ARQ cron `poll_tracking_job`
(`app/jobs/shipping_jobs.py`, registered in `app/worker.py`, runs every 30 min)
refreshes tracking for in-flight shipments. It:

- fans out **per active company** (one isolated pass each);
- **only polls tenants whose `allow_carrier_egress` is ON** (the
  `provider.get_tracking` call is outbound carrier traffic);
- selects shipments with a tracking number, a non-terminal `tracking_status`, and
  not voided;
- applies returned events via `record_tracking_events` (`source="poll"`,
  de-duped against the webhook events);
- is strictly **best-effort** — every per-shipment / per-company failure is
  logged and swallowed so the cron never raises out of the worker.

## Label / BOL document storage

Purchased labels and BOLs are persisted as standard `Document` rows
(`DocumentType.SHIPPING_LABEL` / `BILL_OF_LADING`), linked from the shipment via
`label_document_id` / `bol_document_id`. PDF/PNG/ZPL bytes (when the provider
returns them) are written to the **same local-disk store as every other
document** — the directory resolved from `UPLOAD_DIR` (default `/app/uploads`,
falling back to `UPLOAD_DIR_FALLBACK` / `./uploads`). When the provider returns a
hosted label URL instead of bytes, the URL is stored on the document and the file
stays retrievable from the carrier. (S3 is out of scope for carrier artifacts.)

The frontend prints a purchased label at `/print/shipping-label/:id`
(`frontend/src/pages/PrintShippingLabel.tsx`, gated `shipping:view`).

## Money & idempotency

- All money is `Decimal` / `Numeric(12,2)` end to end.
- `buy_label` / `buy_freight_bol` are **idempotent**: a pre-check returns the
  existing purchase (`already_purchased=true`, no provider call) if a label/BOL was
  already bought, and a deterministic idempotency key
  (`sha256(company_id:shipment_id:rate_id)`) is both persisted (the partial-unique
  index) and sent to the provider as an `Idempotency-Key` header (defense in depth).
- Label/BOL purchase, void, and refund are **money-moving** actions and write
  tamper-evident `AuditService` entries; **no secrets** appear in audit or
  operational-event payloads (`api_key` / `encrypted_api_key` are scrubbed by
  `SENSITIVE_EVENT_KEYS`).

## Testing with EasyPost (sandbox)

EasyPost ships separate **TEST** and **PRODUCTION** API keys.

1. Create a carrier account with the **TEST** key and `environment = "test"`.
2. Run **Test connection** to confirm the credential authenticates.
3. Enable `allow_carrier_egress` on the company shipping profile (TEST keys still
   require egress ON for rate/label/validate — the kill switch is provider-agnostic).
4. Configure the ship-from profile and rate-shop a shipment; EasyPost TEST returns
   sandbox rates and labels (test labels are watermarked / non-postage).
5. Buy a label, then exercise void/refund. Webhooks fire from the EasyPost
   sandbox; register the webhook URL + signing secret as above to test the inbound
   flow, or rely on the poll cron.

The backend test suites cover the adapter, service, and API layers:
`backend/tests/services/test_easypost_adapter.py`,
`backend/tests/services/test_shipping_service.py`,
`backend/tests/api/test_shipping_carrier_integration.py`.

## RBAC summary

| Action | Roles | Enforcement |
|--------|-------|-------------|
| Carrier account CRUD + test-connection + shipping-profile | **Admin** | `require_role([ADMIN])` (perm `admin:integrations`) |
| Rate-shop / validate-address | Admin / Manager / Supervisor / Shipping | `require_role(...)` (perm `shipping:rate`) |
| Buy-label / buy-bol / schedule-pickup | Admin / Manager / Supervisor / Shipping | `require_role(...)` (perm `shipping:label`) |
| Void / refund | Admin / Manager / Supervisor / Shipping | `require_role(...)` (perm `shipping:void`) |
| List rates / tracking (read) | Any authenticated tenant user | `get_current_user` (read-broad) |

The backend enforces these via `require_role([...])` (the authoritative control);
the `shipping:rate` / `shipping:label` / `shipping:void` / `admin:integrations`
permission strings drive the **frontend** `PermissionGate` / `usePermissions`
visibility. See [docs/RBAC_PERMISSIONS.md](RBAC_PERMISSIONS.md#shipping).

## Related files

- Models: `backend/app/models/carrier_account.py`, `backend/app/models/shipping.py`
- Service: `backend/app/services/shipping_service.py`
- Carriers: `backend/app/services/carriers/`
- Endpoints: `backend/app/api/endpoints/shipping.py`,
  `integrations.py`, `carrier_webhooks.py`
- Jobs: `backend/app/jobs/shipping_jobs.py` (+ `app/worker.py` registration)
- Migrations: `backend/alembic/versions/046_carrier_shipping.py`,
  `047_document_type_shipping_labels.py`
- API reference: [docs/API.md](API.md#shipping) → Shipping / Carrier Integrations /
  Carrier Webhooks
