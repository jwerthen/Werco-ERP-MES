# Werco Glossary

Plain-language definitions for the terms you'll run into while using the Werco system. Terms are listed A to Z. The user roles are explained at the end.

## Terms

| Term | What it means |
|------|---------------|
| **BOM (bill of materials)** | The recipe for a part — the full list of materials, components, and quantities needed to build it. |
| **CAR (corrective action report)** | A record opened to fix the root cause of a recurring or serious problem so it doesn't happen again, not just to patch the one bad part. |
| **CoC (certificate of conformance)** | A supplier's signed document stating that the material or parts they shipped meet the requirements. Captured at receiving and often sent out with a shipment. |
| **ECO (engineering change order)** | A controlled, written change to a part, drawing, or how something is made. Tracked so everyone works from the latest approved version. |
| **FAI (first article inspection)** | A full, documented inspection of the first piece off a new or changed job to prove it meets the drawing before you run the rest. |
| **Heat number** | The supplier's batch identifier for a melt of metal, shown on the material cert. Captured at receiving so the exact material batch can be traced to the parts made from it. |
| **Kiosk mode** | The simplified, big-button shop-floor screen built for shared touchscreens at the machine. Operators are sent here automatically after signing in. |
| **KPI (key performance indicator)** | A headline number that tells you how something is doing at a glance — for example active work orders, due today, overdue, or open NCRs. The Dashboard and Analytics screens show KPIs as cards. |
| **Lot / Serial** | Two ways to track material. A **lot** is a batch of identical parts tracked as a group; a **serial** number tracks one single unique part. Both let you trace where material came from and where it went. |
| **Make vs. buy part** | Whether a part is built here in-house ("make") or purchased from a supplier ("buy"). This affects whether it gets a work order or a purchase order. |
| **MRP (material requirements planning)** | A calculation that looks at what you need to build versus what's on hand, finds shortages, and suggests purchase orders to cover them. |
| **NCR (non-conformance report)** | A record raised when a part or material doesn't meet the requirement (wrong dimension, damage, defect). It documents the issue and how it was resolved. |
| **OEE (overall equipment effectiveness)** | A score for how productive a machine or work center is, combining how much it's running, how fast, and how much good output it makes. |
| **Operation** | A single step in making a part — for example cut, bend, weld, or inspect. A job is a sequence of operations done in order. |
| **Part revision** | The version of a part or drawing (Rev A, Rev B, and so on). When a part changes, a new revision is created so older records stay accurate. |
| **Priority (P1–P10)** | The urgency ranking on a work order. **P1** is the most urgent; **P10** is the least. Higher-priority jobs rise to the top of the queue. |
| **PO (purchase order)** | An official order sent to a supplier to buy materials, parts, or services. |
| **QMS (quality management system)** | The set of standards, procedures, and records that keep quality consistent and auditable (AS9100D / ISO 9001). The **QMS Standards** screen is your in-system reference for it. |
| **Quote / RFQ** | An **RFQ** (request for quote) is a customer asking what a job would cost; a **quote** is the priced answer you send back. |
| **Routing** | The ordered list of operations and work centers that defines how a part is made from start to finish. |
| **Run order (`RUN 1`, `RUN 2`, …)** | The running order a manager sets per machine on the Dispatch Board. Jobs with a RUN number sit at the top of that machine's queue in that order; unranked jobs follow, sorted by priority and due date. It's a recommendation — it never blocks starting a job. |
| **SPC (statistical process control)** | Tracking measurements over time on charts to spot when a process is drifting out of spec before it makes bad parts. |
| **Traceability** | The ability to follow a part's full history — what material (lot/serial) went into it, which jobs and operations it passed through, and where it shipped. |
| **Traveler** | The printed routing packet that travels with a job through the shop, listing the operations, instructions, and sign-offs for that work order. |
| **Work center** | A machine, station, or area where operations happen (for example a press brake, a CNC mill, the paint booth, or inspection). |
| **Work order (WO)** | The instruction to build a specific part in a specific quantity by a due date. It carries the BOM, routing, and operations for that job. Often shortened to **WO** (and **WO #** for its number). |

## User roles

Your role decides what you can see and do. All roles only see data for your own company.

| Role | What it's for |
|------|---------------|
| **platform_admin** | Werco oversight. Can switch between companies and view across them (read-only). Not a day-to-day shop role. |
| **admin** | Full access for one company, including Admin Settings and user management. Usually IT or a system administrator. |
| **manager** | Broad control over operations plus approvals. Cannot change admin-only settings. |
| **supervisor** | Runs shop execution and planning, with limited user and admin controls. Shift supervisors and team leads. |
| **operator** | Executes work on the floor — start, hold, resume, and complete operations and record production. |
| **quality** | Handles inspections and quality approvals (NCRs, CARs, FAIs, and related sign-offs). |
| **shipping** | Handles shipping operations — preparing shipments, marking them shipped, and packing slips. |
| **viewer** | Read-only access. For auditors, executives, and guests who need to look but not change anything. |
