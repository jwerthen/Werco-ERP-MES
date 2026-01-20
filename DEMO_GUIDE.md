# Werco ERP/MES System - Complete Demo Guide

A comprehensive demonstration guide for the Werco Manufacturing ERP & MES System. This guide walks through all major features with step-by-step instructions for showcasing the complete manufacturing workflow.

---

## Table of Contents

1. [System Overview](#system-overview)
2. [Getting Started](#getting-started)
3. [Demo Walkthrough](#demo-walkthrough)
   - [Dashboard & Navigation](#1-dashboard--navigation)
   - [Parts Management](#2-parts-management)
   - [Bill of Materials (BOM)](#3-bill-of-materials-bom)
   - [Routings](#4-routings)
   - [Work Orders](#5-work-orders)
   - [Shop Floor Operations](#6-shop-floor-operations)
   - [Quote Calculator](#7-quote-calculator)
   - [Quality Management](#8-quality-management)
   - [Inventory Management](#9-inventory-management)
   - [Purchasing & Receiving](#10-purchasing--receiving)
   - [Shipping](#11-shipping)
   - [Scheduling](#12-scheduling)
   - [MRP (Material Requirements Planning)](#13-mrp-material-requirements-planning)
   - [Reports & Analytics](#14-reports--analytics)
   - [Administration](#15-administration)
4. [User Roles & Permissions](#user-roles--permissions)
5. [Compliance Features](#compliance-features)
6. [Tips for Effective Demos](#tips-for-effective-demos)

---

## System Overview

The Werco ERP/MES system is a fully integrated manufacturing management platform designed for:

- **Job Shop & Contract Manufacturing** - Handle diverse customer orders with varying specifications
- **AS9100D / ISO 9001 Compliance** - Built-in traceability, audit logging, and document control
- **CMMC Level 2 Security** - Role-based access, session management, and comprehensive audit trails
- **Real-time Shop Floor Visibility** - Track work in progress across all work centers
- **End-to-End Traceability** - From quote to shipment with full lot tracking

### Key Differentiators

| Feature | Description |
|---------|-------------|
| **Instant Quoting** | AI-powered quote calculator with DXF file analysis |
| **Multi-level BOM** | Exploded and single-level views with nested assemblies |
| **Real-time Dashboard** | Live shop floor status with 30-second auto-refresh |
| **Barcode Integration** | Scan-to-action for clock-in, receiving, and shipping |
| **Mobile-Responsive** | Full functionality on tablets for shop floor use |

---

## Getting Started

### Demo Environment Access

**Production Demo URL:** `https://werco-erp.up.railway.app`

**Demo Credentials:**

| Role | Email | Password | Use For |
|------|-------|----------|---------|
| Administrator | admin@werco.com | admin123 | Full system access, settings |
| Manager | manager@werco.com | password123 | Work orders, scheduling |
| Supervisor | supervisor@werco.com | password123 | Shop floor oversight |
| Operator | operator@werco.com | password123 | Shop floor clock-in/out |
| Quality | quality@werco.com | password123 | Inspections, NCRs |

### Recommended Demo Flow

For a complete 30-45 minute demo, follow this sequence:

1. **Dashboard Overview** (3 min) - Show real-time status
2. **Create a Part** (3 min) - Demonstrate part master
3. **Build a BOM** (5 min) - Show multi-level assembly
4. **Create Routing** (3 min) - Define manufacturing operations
5. **Generate Quote** (5 min) - Use quote calculator with DXF
6. **Create Work Order** (5 min) - Full work order creation
7. **Shop Floor Operations** (10 min) - Clock-in, production reporting
8. **Quality & Inspection** (5 min) - Inspection and NCR
9. **Shipping** (3 min) - Complete the order
10. **Reports & Traceability** (5 min) - Show audit trail

---

## Demo Walkthrough

### 1. Dashboard & Navigation

**Location:** Home page after login (`/`)

The dashboard provides a real-time overview of manufacturing operations.

#### Key Elements to Highlight:

**Summary Cards (Top Row)**
- **Active Work Orders** - Click to navigate to work orders list
- **Overdue Orders** - Red indicator for late jobs (click to filter)
- **Due Today** - Orders requiring immediate attention
- **Quality Alerts** - Open NCRs and inspection items

**Work Center Status Grid**
- Visual status indicators: Available (green), In Use (blue), Maintenance (amber), Offline (red)
- Shows queued and active operations per work center
- Click any work center to see its queue

**Recent Activity Feed**
- Live updates of system actions
- Shows who did what and when
- Demonstrates audit trail capability

**Demo Script:**
> "The dashboard gives managers instant visibility into shop floor operations. Notice how work centers show their current status - we can see CNC-001 is currently in use with 3 jobs queued. The system refreshes every 30 seconds automatically, or you can click refresh for immediate updates."

---

### 2. Parts Management

**Location:** Engineering → Parts (`/parts`)

Parts are the foundation of the system - every work order, BOM, and quote starts with a part.

#### Creating a New Part

1. Click **"New Part"** button
2. Fill in required fields:
   - **Part Number:** `DEMO-BRACKET-001` (auto-uppercases)
   - **Name:** `Demo Mounting Bracket`
   - **Part Type:** Assembly (for BOM demo) or Manufactured
   - **Unit of Measure:** Each
   - **Revision:** A (default)

3. Optional but recommended:
   - **Customer:** Select or add new
   - **Description:** `Aluminum mounting bracket with 4 holes and 2 bends`
   - **Critical Characteristics:** Check if applicable

4. Click **Save**

#### Part Types Explained

| Type | Description | Use Case |
|------|-------------|----------|
| **Manufactured** | Made in-house | CNC parts, weldments |
| **Assembly** | Has a BOM | Multi-part products |
| **Purchased** | Buy from vendor | Hardware, raw materials |
| **COTS** | Commercial Off-The-Shelf | Standard components |

**Demo Script:**
> "Parts are created with unique part numbers - the system enforces uppercase and validates the format. Notice we can flag critical characteristics for AS9100D compliance. Each part maintains full revision history, and we'll see how this connects to BOMs and routings."

---

### 3. Bill of Materials (BOM)

**Location:** Engineering → BOM (`/bom`)

BOMs define the components needed to build an assembly.

#### Creating a BOM

1. Click **"Create BOM"**
2. Select the parent part (must be Assembly or Manufactured type)
3. BOM is created in **Draft** status

#### Adding BOM Items

1. With a BOM selected, click **"Add Item"**
2. Select component part from dropdown
3. Set fields:
   - **Quantity:** Number required per parent
   - **Line Type:** Component, Hardware, Consumable, or Reference
   - **Unit of Measure:** Each, Feet, etc.
   - **Find Number:** Optional drawing reference

4. For hardware items, additional fields appear:
   - **Torque Spec:** e.g., "25 ft-lbs"
   - **Installation Notes:** Assembly instructions

#### BOM Views

- **Single Level** - Direct children only
- **Multi-Level (Exploded)** - Shows nested assemblies with extended quantities

#### BOM Lifecycle

1. **Draft** - Editable, add/remove items
2. **Released** - Locked for production use
3. Use **"Unrelease"** button to return to draft if changes needed

**Demo Script:**
> "BOMs support multi-level assemblies - watch as I switch to exploded view. See how the sub-assembly expands to show its components? The extended quantity automatically calculates how many of each part is needed. Notice the different categories - hardware items like bolts show torque specifications."

---

### 4. Routings

**Location:** Engineering → Routing (`/routing`)

Routings define the sequence of manufacturing operations.

#### Creating a Routing

1. Click **"New Routing"**
2. Select the part
3. Add operations in sequence:
   - **Operation Number:** 10, 20, 30... (auto-increments by 10)
   - **Operation Name:** e.g., "Laser Cut", "Bend", "Weld"
   - **Work Center:** Select from available work centers
   - **Setup Time:** Hours for first-piece setup
   - **Run Time:** Hours per piece

#### Operation Types

| Operation | Typical Work Center | Description |
|-----------|-------------------|-------------|
| Laser Cut | Laser | Sheet metal cutting |
| Bend | Press Brake | Sheet metal forming |
| Machine | CNC Mill/Lathe | Precision machining |
| Weld | Welding | Joining operations |
| Paint | Paint Booth | Coating application |
| Assemble | Assembly | Final assembly |
| Inspect | Inspection | Quality verification |

**Demo Script:**
> "Routings automatically pull into work orders. Each operation specifies the work center, which drives our shop floor queue system. Setup and run times are used for scheduling and costing. Notice operations are numbered by 10s - this leaves room for inserting steps later."

---

### 5. Work Orders

**Location:** Production → Work Orders (`/work-orders`)

Work orders drive all shop floor activity.

#### Creating a Work Order

1. Click **"New Work Order"**
2. Fill in header information:
   - **Part:** Select from parts list (filters to Manufactured/Assembly)
   - **Quantity Ordered:** Number to make
   - **Due Date:** Customer required date
   - **Priority:** 1 (Urgent) to 5 (Low)
   - **Customer PO:** Reference number
   - **Lot Number:** For traceability

3. **Operations Section:**
   - Click **"Load from Routing"** to auto-populate
   - Or manually add operations
   - Adjust times if needed for this specific order

4. Click **"Create Work Order"**
   - WO number auto-generates: `WO-YYYYMMDD-XXX`

#### Work Order Lifecycle

```
Created → Released → In Progress → Complete → Closed
                ↓
            On Hold (optional)
```

**Status Actions:**
- **Release** - Makes available for shop floor
- **Start** - Begins tracking, sets actual start date
- **Complete** - All quantity produced
- **Close** - Final accounting, locks record

#### Work Order Detail View

Click any work order to see:
- Header information with status
- Operations list with progress bars
- Time entries (clock in/out history)
- Print traveler option

**Demo Script:**
> "Watch as I create a work order - notice it pulls the routing automatically. The system generates a unique work order number with today's date. Once I release it, operators will see it in their work queue. Let me show you the detail view..."

---

### 6. Shop Floor Operations

**Location:** Shop Floor → Operations (`/shop-floor`) or Simple View (`/shop-floor/operations`)

The heart of the MES system - where operators interact with work orders.

#### Shop Floor Dashboard

**For Supervisors/Managers:**
- Real-time view of all work center activity
- Click work center to see its queue
- Expand work order rows for details

**Elements:**
- Work center filter dropdown
- Queue sorted by priority and due date
- Progress bars showing completion percentage
- Time remaining estimates

#### Clocking In to an Operation

1. Find the operation in the queue
2. Click **"Start"** or scan barcode
3. Select clock-in type:
   - **Production** - Making parts
   - **Setup** - First-piece setup
   - **Rework** - Fixing defects
4. Optionally add notes
5. Click **"Clock In"**

#### Reporting Production (Clock Out)

1. Click **"Stop"** on active operation
2. Enter production data:
   - **Quantity Complete:** Good parts made
   - **Quantity Scrapped:** Defective parts
   - **Notes:** Any issues or comments
3. Click **"Complete Clock Out"**

#### Simple Operations View

For operators who need a streamlined interface:
- Large touch-friendly buttons
- Focused on single work center
- Quick scan-to-action workflow

**Demo Script:**
> "This is what operators see on shop floor tablets. They select their work center and see jobs sorted by priority. Let me clock into this operation... notice it records who, what, and when. Now I'll report completing 10 pieces - the progress bar updates immediately, and this feeds into the dashboard."

---

### 7. Quote Calculator

**Location:** Sales → Quote Calculator (`/quote-calculator`)

AI-powered instant quoting for CNC and sheet metal work.

#### Sheet Metal Quote Demo

1. Select **"Sheet Metal"** tab
2. **Option A - Manual Entry:**
   - Enter flat pattern dimensions
   - Specify material and gauge
   - Enter cut length, holes, bends

3. **Option B - DXF Upload (Recommended for Demo):**
   - Click upload area
   - Select a DXF file
   - Watch auto-extraction of:
     - Flat pattern size
     - Total cut length
     - Number of holes
     - Number of bends

4. Select finishing options (powder coat, anodize, etc.)
5. Enter quantity
6. Toggle rush if applicable
7. Click **"Calculate Quote"**

#### Understanding the Results

The quote shows:
- **Material Cost** - Based on sheet pricing
- **Cutting Cost** - Laser time × rate (includes 30% buffer)
- **Bending Cost** - Time per bend × brake rate
- **Hardware Cost** - PEM inserts, weld nuts
- **Finish Cost** - Selected finishing operations
- **Setup Cost** - One-time setup charges
- **Markup** - Configurable percentage
- **Quantity Discount** - Volume pricing breaks
- **Rush Charge** - 50% premium if rushed

#### CNC Quote Demo

1. Select **"CNC Machining"** tab
2. Enter part dimensions (bounding box)
3. Select material
4. Specify complexity level
5. Add features (holes, pockets, slots)
6. Select tolerance requirements
7. Calculate

**Demo Script:**
> "Watch what happens when I upload this DXF file... the system analyzes the geometry and extracts cut length, hole count, and bend information automatically. It uses actual laser cutting speeds for the material and thickness. Notice the 30% buffer for head retractions - this matches real-world cutting times."

---

### 8. Quality Management

**Location:** Quality → Quality (`/quality`)

Full quality management with inspection and NCR tracking.

#### First Article Inspection (FAI)

1. Navigate to a work order at inspection
2. Click **"Create FAI"**
3. Fill in inspection criteria
4. Record measurements
5. Pass/Fail determination
6. Attach photos or documents

#### Non-Conformance Reports (NCR)

1. Click **"New NCR"**
2. Link to work order and operation
3. Describe the non-conformance
4. Assign disposition:
   - **Use As Is** - Accept with deviation
   - **Rework** - Fix and re-inspect
   - **Scrap** - Reject and dispose
   - **Return to Vendor** - Supplier issue

5. Root cause analysis
6. Corrective action assignment
7. Close when resolved

#### Calibration Tracking

**Location:** Quality → Calibration (`/calibration`)

- Track measurement equipment
- Due date alerts on dashboard
- Calibration history and certificates

**Demo Script:**
> "Quality tracking is fully integrated. When an operator reports scrap, supervisors are notified. NCRs track through disposition and corrective action. For AS9100D, we maintain full records with electronic signatures."

---

### 9. Inventory Management

**Location:** Inventory → Parts Inventory (`/inventory/parts`) or Materials (`/inventory/materials`)

Track on-hand quantities and movements.

#### Parts Inventory

- Current stock levels by part
- Location tracking (bins, shelves)
- Lot/serial number tracking
- Movement history

#### Materials Inventory

- Raw material stock (sheets, bars, etc.)
- Dimensions and specifications
- Cost tracking (FIFO/Average)
- Reorder point alerts

#### Inventory Transactions

- **Receive** - Add inventory from purchase
- **Issue** - Consume for work order
- **Adjust** - Cycle count corrections
- **Transfer** - Move between locations

**Demo Script:**
> "Inventory integrates with purchasing and production. When we receive material, it's available for MRP. When work orders consume material, stock is automatically decremented. Low stock alerts appear on the dashboard."

---

### 10. Purchasing & Receiving

**Location:** Purchasing → Purchasing (`/purchasing`) and Receiving (`/receiving`)

End-to-end procurement management.

#### Creating a Purchase Order

1. Click **"New PO"**
2. Select vendor
3. Add line items:
   - Material or part
   - Quantity and unit price
   - Required date
4. Submit for approval (if workflow enabled)
5. Send to vendor

#### Receiving Against PO

1. Navigate to Receiving
2. Find open PO
3. Click **"Receive"**
4. Enter received quantities
5. Specify storage location
6. Print receiving label

#### PO Upload Feature

**Location:** Purchasing → PO Upload (`/purchasing/upload`)

- Upload customer PO documents (PDF)
- AI extraction of line items
- Auto-create parts and work orders

**Demo Script:**
> "Purchasing integrates with inventory and quality. When we receive material, it's inspected and added to stock. Watch this PO upload feature - it reads the customer's purchase order and extracts all the line items automatically."

---

### 11. Shipping

**Location:** Shipping → Shipping (`/shipping`)

Final step in the order fulfillment process.

#### Creating a Shipment

1. Select completed work order(s)
2. Click **"Create Shipment"**
3. Enter shipping details:
   - Carrier and service
   - Tracking number
   - Package weight and dimensions
4. Print packing slip
5. Mark as shipped

#### Packing Slip

The packing slip includes:
- Ship-to address
- Line items with quantities
- Lot numbers for traceability
- Customer PO reference

**Demo Script:**
> "Shipping closes the loop on customer orders. The packing slip includes lot numbers for full traceability. Once shipped, the work order status updates and appears in the customer's shipped orders."

---

### 12. Scheduling

**Location:** Production → Scheduling (`/scheduling`)

Visual scheduling and capacity planning.

#### Gantt Chart View

- Drag-and-drop operation scheduling
- Work center capacity visualization
- Due date indicators
- Conflict highlighting

#### Capacity View

- Hours available vs. scheduled by work center
- Bottleneck identification
- What-if scenario planning

**Demo Script:**
> "The scheduling board shows operations across work centers over time. Red indicates overdue items. Managers can drag operations to reschedule, and the system warns of capacity conflicts."

---

### 13. MRP (Material Requirements Planning)

**Location:** Production → MRP (`/mrp`)

Plan material needs based on demand.

#### Running MRP

1. Set planning horizon (weeks)
2. Click **"Run MRP"**
3. System analyzes:
   - Open work orders
   - Current inventory
   - Lead times
   - BOM requirements

4. Results show:
   - **Shortages** - What to order
   - **Excess** - Overstocked items
   - **Planned Orders** - Suggested purchases

**Demo Script:**
> "MRP explodes all open work orders through their BOMs, compares against inventory, and tells us what to buy. This shortage report integrates directly with purchasing - one click to create a PO."

---

### 14. Reports & Analytics

**Location:** Reports → Reports (`/reports`) and Analytics (`/analytics`)

#### Standard Reports

| Report | Description |
|--------|-------------|
| Work Order Status | All orders with current status |
| Production Summary | Output by period |
| On-Time Delivery | OTD percentage tracking |
| Work Center Utilization | Efficiency metrics |
| Quality Summary | Defect rates, NCR trends |
| Inventory Valuation | Stock value report |

#### Traceability Report

**Location:** Quality → Traceability (`/traceability`)

Enter a lot number to see complete history:
- Original work order
- All operations performed
- Operators involved
- Materials consumed
- Quality records
- Shipping information

#### Audit Log

**Location:** Admin → Audit Log (`/admin/audit-log`)

Complete record of all system actions:
- User actions (create, update, delete)
- Login/logout events
- Data changes with before/after values

**Demo Script:**
> "For AS9100D compliance, we maintain full traceability. Enter any lot number and see its complete history - who touched it, when, and what was done. The audit log shows every system action for CMMC compliance."

---

### 15. Administration

**Location:** Admin → Settings (`/admin/settings`)

System configuration for administrators.

#### User Management

**Location:** Admin → Users (`/admin/users`)

- Create/edit user accounts
- Assign roles
- Enable/disable access
- View login history

#### Work Center Setup

**Location:** Work Centers (`/work-centers`)

- Define work centers
- Set hourly rates
- Configure capacity
- Assign operators

#### Customer Management

**Location:** Admin → Customers (`/customers`)

- Customer master data
- Contact information
- Shipping addresses
- Custom pricing

#### Quote Configuration

**Location:** Admin → Settings → Pricing

- Material costs and markup
- Machine hourly rates
- Finishing prices
- Quantity break pricing

---

## User Roles & Permissions

| Role | Dashboard | Work Orders | Shop Floor | Quality | Admin |
|------|-----------|-------------|------------|---------|-------|
| **Admin** | ✅ Full | ✅ Full | ✅ Full | ✅ Full | ✅ Full |
| **Manager** | ✅ Full | ✅ Create/Edit | ✅ Full | ✅ Full | ⚠️ Limited |
| **Supervisor** | ✅ Full | ✅ Edit/Release | ✅ Full | ✅ View | ❌ None |
| **Operator** | ✅ View | ✅ View | ✅ Clock In/Out | ✅ Report | ❌ None |
| **Quality** | ✅ View | ✅ View | ✅ View | ✅ Full | ❌ None |
| **Viewer** | ✅ View | ✅ View | ✅ View | ✅ View | ❌ None |

---

## Compliance Features

### AS9100D / ISO 9001

| Requirement | System Feature |
|-------------|----------------|
| Document Control | Revision tracking on parts, BOMs, routings |
| Traceability | Lot numbers linked through all transactions |
| Training Records | User certifications and expirations |
| Calibration | Equipment tracking with due date alerts |
| Inspection | First Article and in-process inspection records |
| Non-conformance | NCR workflow with disposition tracking |
| Corrective Action | Linked to NCRs with follow-up tracking |

### CMMC Level 2

| Requirement | System Feature |
|-------------|----------------|
| Access Control | Role-based permissions |
| Audit Logging | All actions logged with user, time, data |
| Session Management | Configurable timeout, secure cookies |
| Account Security | Lockout after failed attempts |
| Data Protection | HTTPS encryption, database encryption |

---

## Tips for Effective Demos

### Before the Demo

1. **Verify credentials work** - Login with each demo account
2. **Check data state** - Ensure demo parts/WOs exist
3. **Prepare DXF file** - Have a sample ready for quote calculator
4. **Test printer** - If showing travelers/packing slips
5. **Clear browser cache** - Fresh start recommended

### During the Demo

1. **Start with the problem** - "Managing a job shop is complex..."
2. **Follow a part through the system** - Creates a story
3. **Show real-time updates** - Open two browsers for dashboard
4. **Let them click** - Interactive engagement
5. **Highlight compliance** - Audit log and traceability

### Common Questions & Answers

**Q: Can we import existing data?**
> Yes, we have import tools for parts, customers, and BOMs. We can also migrate from other ERP systems.

**Q: How is pricing calculated in quotes?**
> The quote calculator uses actual machine speeds, material costs, and configured labor rates. The 30% laser buffer accounts for real-world cutting dynamics.

**Q: Can operators use tablets on the shop floor?**
> Absolutely - the interface is fully responsive. Many customers use mounted tablets at each work center.

**Q: How do you handle revisions?**
> Parts, BOMs, and routings all support revision control. The system maintains history and can show differences between revisions.

**Q: Is the data backed up?**
> Yes, automated daily backups with point-in-time recovery capability.

### Troubleshooting

| Issue | Solution |
|-------|----------|
| Page won't load | Check network, try hard refresh (Ctrl+Shift+R) |
| Can't login | Verify credentials, check caps lock |
| Quote won't calculate | Ensure materials are configured in admin settings |
| DXF upload fails | Verify file is valid DXF format (AutoCAD 2000+) |
| Work order stuck | Check all operations are complete |

---

## Demo Environment Reset

To reset demo data for a fresh demo:

1. Login as admin
2. Navigate to Admin → Settings
3. Click "Reset Demo Data" (if available)
4. Or contact system administrator

---

## Support

**Documentation:** https://docs.werco-erp.com
**Support Email:** support@werco.com
**Phone:** (555) 123-4567

---

*Werco ERP/MES System - Built for Manufacturing Excellence*
*AS9100D | ISO 9001 | CMMC Level 2 Compliant*
