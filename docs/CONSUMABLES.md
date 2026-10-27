# Consumables checkout

Open **Inventory & Purchasing → Warehouse → Consumables** (`/consumables`). The
supply attendant records one item per checkout. The ERP deducts that quantity from
the selected stock location and saves who received it, what they received, how much,
when, and who issued it.

## Set up the supply room

1. Give the attendant a named **Admin, Manager or Supervisor** account. Use the
   attendant's own login so the issuer remains identifiable. Employees receiving
   supplies need active employee records in the same company; they do not need to
   log in at the supply desk.
2. Open **Manage consumables**, then create or verify each item in **Materials &
   Supplies** with type **Consumable**. Use a distinct item number and a clear name
   for each size/specification. Consumables reuse the existing material master;
   this screen does not maintain a second catalog.
3. Confirm the stocking unit before entering balances. `each` requires a whole
   number. Quantities in weight, length or volume can be fractional. If an item is
   stocked as a package, include its package size in the item name and use that
   convention consistently. Checkout does not convert boxes into individual pieces.
4. Physically count the opening balance by location/lot. Enter stock through
   **Warehouse → Inventory** receiving, or reconcile an existing balance through
   the existing count/adjustment workflow. Do not receive the opening count again
   if that stock is already recorded. Keep the paper records as the historical
   source; this release does not import or backdate paper checkouts.
5. Confirm the quantity available on the Consumables screen. The low-stock badge
   uses the item's existing `reorder_point`; an authorized user can configure it
   through the material API or materials import. The current Materials form has
   no reorder-point input. The badge compares total active on-hand stock across
   locations with the threshold, while checkout availability excludes restricted
   and reserved stock. It does not automatically create a purchase order.

## Record a checkout

1. Search **Supplies & stock** by item name, item number, location or lot, then
   select the stock location being used.
2. Search **Employee receiving supplies** by name or department. Only
   active employees can receive a new checkout.
3. Enter the quantity in the displayed unit. Review **Available after checkout**.
   Add optional notes such as a job number or purpose. Notes do not allocate the
   charge to a work order or change job costing.
4. Select **Record checkout** and wait for the receipt. The server supplies the
   issue time and issuer. The employee stays selected for the next item; select
   another stock item and enter its quantity.

The server rechecks available stock when saving, so another checkout can reduce
the quantity between selection and submission. Inactive, held, quarantined,
expired or otherwise unavailable stock cannot be issued. Configured locations
must be active and pickable. Existing allocated stock and outstanding work-order
reservations are protected. Serialized stock is unavailable in this initial
workflow; use the existing inventory/tool workflow for those items.

If the server has not confirmed a checkout, keep the page open and use **Retry
checkout** with the unchanged details. The same request ID returns the original
receipt without deducting stock twice. Do not create a new checkout to recover an
uncertain result. If the page was closed or reloaded, review history before
entering another issue; the pending retry key is held by the open screen.

## Review usage and replenish

**Checkout history** shows the checkout reference, employee and department, item, quantity/unit,
location/lot, issue time, issuer and notes. It is newest first, with pagination.
Search the history, filter by employee and date range, or use an item's **View
history** action. Previously issued records remain visible after an employee is
deactivated or an item is renamed; the saved identity labels are snapshots.

Dates and times display in **America/Chicago**. **From date** and **Through date**
include the whole selected Central calendar days, including daylight-saving
changes. The API stores and serves the timestamp in UTC.

Admins and Managers can select **Export CSV** to export the filtered history.
The export is audited and includes at most 10,000 matching checkouts; larger
requests are refused with instructions to narrow the filters, never silently
truncated. CSV text is protected against spreadsheet formula injection.

Use **Low stock only** to review replenishment needs. Receive new deliveries with
the existing warehouse/receiving workflow so purchasing, receipts and inventory
remain the stock source of truth.

## Corrections and access

Checkout records are immutable. This initial release has no return, void, edit,
delete or backdated checkout action. Do not post another checkout to cancel one.
If the physical stock balance needs correction, an authorized attendant can use
the existing inventory adjustment workflow with a reason referencing the checkout
ID, shown on the receipt, in history and in exports as `CC-000123`. That adjustment corrects stock only; it does
**not** reverse the original employee usage attribution. Retain the discrepancy
and its explanation with the operational records.

| Action | Access |
|---|---|
| View stock, employee picker and checkout history | Authenticated users in the active company; in-app route uses `inventory:view` |
| Record a checkout | Admin, Manager or Supervisor |
| Export filtered history | Admin or Manager |
| Edit or delete a posted checkout | Not available |

Platform Admin/superuser access follows the existing company-context rules;
read-only company contexts cannot record checkouts. All new stock, employee and
history queries are company-scoped. A checkout commits the stock decrease,
negative `ISSUE` ledger row, employee-attribution record and required audit
evidence together. Audit failure leaves no committed checkout.

## Release requirement

Apply migration **`112_consumable_checkouts`**, after
`111_continuous_improvement`, before serving the matching API/frontend. It creates
the empty `consumable_checkouts` table and its security controls; it does not load
items, employees, opening balances or paper history. No new environment variables
or background jobs are required. See [Deployment Runbook](DEPLOYMENT_RUNBOOK.md)
for release verification and rollback guidance. Implementing this feature does
not itself deploy it or initialize the production supply room.
