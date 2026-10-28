# Customer PO dashboard

Open **Work Orders → Customer POs**, search by customer or PO number, and click a
purchase order to track its work orders. You can also click a **Customer PO** in
the work-order table, mobile card, or work-order detail page.

When entering a **Customer PO** on a new work order or quote, begin typing the
number to see existing PO suggestions. Suggestions are limited to the selected
customer. With no customer selected, each match shows its customer; choosing a
match fills both fields. Use the arrow keys and Enter, or click a suggestion.
You can also type a new PO number without selecting a match.

The dashboard shows job completion, active work, in-progress and held jobs,
overdue work, and the next due date. Each work order links to its detail page and
shows its PO line, part, unit number, quantities, operation progress, current
operations/work centers, status, and dates when available. It refreshes every
30 seconds and when the window regains focus; **Refresh** updates it immediately.

## What belongs to a PO

Membership uses the existing **customer name + customer PO number** on each work
order, matched exactly within the current company. Identical PO numbers for two
customers remain separate. Jobs with a PO number and no customer are grouped
together under an unspecified customer. Blank PO numbers do not create a group.
Customer and PO spelling are not normalized, so correct inconsistent references
on the work orders if they need to belong to the same group.

All non-deleted jobs are included, including drafts, complete, closed, cancelled,
and laser child work orders carrying the same customer/PO reference. The page is
read-only and does not create a separate customer-order record or change job
status. Supplier POs in Purchasing are separate records.

## Reading the figures

- **Jobs complete** counts complete and closed jobs, divided by all jobs except
  cancelled ones. This is a count of finished jobs, not an estimate of labor or
  units shipped. Cancelled jobs remain visible.
- **Operation progress** is the existing per-work-order routing progress,
  including partial operation quantities. It is separate from finished quantity.
- **On hold** includes active jobs with a work-order hold or an operation hold;
  cancelled laser nests do not count as holds. A job can be in progress and have
  a held operation at the same time, so these counts can overlap.
- **Overdue** and **due today** use the must-ship-by date when present, otherwise
  the due date, relative to America/Chicago. Complete, closed, and cancelled
  jobs are excluded.
- Summary figures cover **all** work orders on the PO, even when the list has
  multiple pages. Quantities stay on individual jobs, so sheet runs on laser
  jobs are not added to finished-part quantities.

## API

Both endpoints require the same workspace access as Work Orders and scope all
queries to the active company. They perform no reconciliation or writes.

- `GET /api/v1/work-orders/customer-pos?skip=0&limit=50&search=...`
  returns paginated customer/PO groups with full-group statistics.
- `GET /api/v1/work-orders/customer-po-suggestions?search=...&customer_name=...&limit=10`
  returns up to 20 distinct existing customer/PO pairs matching a literal,
  case-insensitive PO prefix. Omit `customer_name` to search all customers;
  an empty value selects only unnamed customers. Blank searches return no items.
- `GET /api/v1/work-orders/customer-po-dashboard?customer_name=...&customer_po=...&skip=0&limit=50`
  returns the exact group's statistics and a page of work orders. An empty
  `customer_name` selects only jobs with no customer; it is never a wildcard.

URLs encode the customer and PO using query parameters so slashes, ampersands,
hashes, and other characters in customer references remain intact.
