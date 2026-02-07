# Werco ERP Admin Training Guide

Version: 1.0  
Audience: Admins, managers, and supervisors running production planning and system configuration  
Scope: End-to-end use of all admin-facing functions in the current app

## 1. Training Objectives

By the end of training, an admin should be able to:

1. Configure the system (users, permissions, work centers, rates, quote defaults).
2. Build and release production data (parts, BOMs, routings, work orders).
3. Schedule and dispatch work with priority control and reason logging.
4. Run purchasing, receiving, quality, shipping, and reporting workflows.
5. Audit activity and troubleshoot common production execution issues.

## 2. Access and Role Model

Roles in system:

1. `admin` - full access, including `Admin Settings`.
2. `manager` - broad operational control, no admin-only settings.
3. `supervisor` - shop execution and planning, limited user/admin controls.
4. `operator` - execute work only.
5. `quality`, `shipping`, `viewer` - domain-specific or read-only.

Reference docs:

1. `docs/RBAC_PERMISSIONS.md`
2. `frontend/src/utils/permissions.ts`

## 3. Navigation and Productivity Basics

Main navigation groups:

1. Dashboard
2. Shop Floor
3. Scheduling
4. Work Orders
5. Engineering
6. Inventory and Purchasing
7. Sales and Shipping
8. Quality
9. Documents, Analytics, Reports
10. Administration

Global productivity features:

1. Global search palette: `Ctrl+K`
2. New item shortcut pattern: `Ctrl+N` (where implemented)
3. Save shortcut pattern: `Ctrl+S` (where implemented)
4. Refresh shortcut pattern: `Ctrl+Shift+R`
5. Escape to close modals

## 4. Core Admin Workflows

## 4.1 Daily Opening Checklist

1. Open `Dashboard` and verify overdue, due today, and work center status.
2. Open `Scheduling` and review unscheduled jobs count, capacity heatmap overload cells, and Dispatch Queue top priorities.
3. Review `Shop Floor` for active jobs and blocked/on-hold work.
4. Review `Receiving` inspection queue and `Quality` open NCR/CAR/FAI items.

## 4.2 Work Order Planning and Release

From `Work Orders`:

1. Filter by status/customer/part.
2. Open draft work orders and verify routing and due date.
3. Release drafts to push to production.

From `Work Orders > New Work Order`:

1. Select part.
2. Set quantity, due date, and priority.
3. Use customer selector to type/search existing customers, pick from dropdown, or create new customer inline if no match.
4. Review auto-populated operations from released routing or BOM preview.
5. Edit operations when needed.
6. Create work order and review on detail page.

From `Work Order Detail`:

1. Release, start, and complete work orders.
2. Complete operations manually when needed.
3. Review material requirements and live operator activity.
4. Print traveler from detail page.

## 4.3 Scheduling and Dispatch

From `Scheduling`:

1. Use weekly Gantt to view scheduled operations by work center.
2. Drag work order cards between work centers to reassign current operation.
3. Use `Schedule` action for explicit start date assignment.
4. Use `Earliest` for one-click earliest slot.
5. Use Dispatch Queue (sorted by dispatch score) to manage unscheduled and scheduled rows.
6. Update priorities per row (`P1` to `P10`) and use optional `Priority Reason` for single or bulk updates.
7. Use bulk actions: set priority, move work center, shift dates, and schedule earliest.
8. Review Capacity Heatmap overload percentages by work center/day and rebalance when cells exceed 100 percent.

## 4.4 Shop Floor Oversight

From `Shop Floor`:

1. Monitor active jobs and elapsed run time.
2. Clock operators in/out when needed.
3. Review `Priority Focus Queue` (top ranked jobs to run next).
4. Expand rows for work order operation detail.
5. Update priorities with optional reason if role allows editing.

From `Shop Floor > Operations`:

1. Mobile/kiosk execution board.
2. Filter by work center/status/search/due date/actionable-only.
3. Use `Most Important Next` queue for fast operator focus.
4. Start, hold, resume, and complete operations.
5. Record partial completions and notes.
6. Open operation details modal and drill into full work order.

## 4.5 Engineering Data Management

### Parts

1. Create and update parts.
2. Track customer part mapping and part status.
3. Access related BOM and routing from part context.

### BOM

1. Create BOMs for assembly/manufactured parts.
2. Add/edit/delete line items.
3. Import BOM from file with preview and commit.
4. Optionally auto-create missing parts during import.
5. Release/unrelease BOM revisions.
6. Explode BOM for structure verification.

### Routing

1. Create routings and operation sequences.
2. Assign work centers and operation times.
3. Add, update, and delete operations.
4. Release routings for production use.
5. Verify routable items from BOM context.

### Custom Fields

1. Define custom fields by resource/group.
2. Set field type and required/active behavior.
3. Edit or retire obsolete fields.

## 4.6 Inventory, MRP, and Purchasing

### Inventory

1. Catalog, summary, and detail tabs.
2. Receive inventory into location/lot/PO context.
3. Transfer inventory between locations.
4. Track low stock alerts and valuation context.

### Materials and Parts Inventory Views

1. Material-specific catalog and summary.
2. Create material parts and storage locations inline.
3. Auto-suggest part numbers where configured.

### MRP

1. Run MRP.
2. Review shortages.
3. Review recommended actions.
4. Process actions and track MRP run history.

### Purchasing

1. Tabs: orders, vendors, receiving queue, pending inspection.
2. Create vendor and purchase order.
3. Send PO to vendor.
4. Receive material with lot/cert/packing slip metadata.
5. Complete receiving inspection (accept/reject, notes).
6. Upload/download vendor documents.

### Receiving (Dedicated)

1. Tabs: receive, inspection queue, history.
2. Receive against open PO lines.
3. Complete inspections with method, accepted/rejected qty, notes.
4. Review receiving stats and 30-day history.

### PO Upload

1. Upload PO or quote PDF.
2. Review extracted fields.
3. Match parts/vendors and create purchase order from upload.

## 4.7 Quality and Compliance

### Quality

1. Tabs: NCR, CAR, FAI.
2. Create, review, and track quality records.
3. Use quality summary metrics for trend visibility.

### Calibration

1. Register equipment.
2. Track due dates and status.
3. Record calibration events and update equipment records.

### Traceability

1. Search by lot/serial.
2. Trace lot genealogy and where-used history.

### Audit Log

1. Filter by resource/action.
2. Review change history and actor/time context.
3. Use for internal audits, customer audits, and incident response.

## 4.8 Sales, Shipping, and Customer Functions

### Customers

1. Create/update customer records.
2. Review customer stats and associated work order activity.

### Quotes

1. Create quote.
2. Send quote.
3. Convert quote to work order when approved.

### Quote Calculator

1. Run CNC or sheet metal quote calculations.
2. Upload DXF for geometry analysis.
3. Use materials/finishes defaults from admin settings.
4. Create quote from calculator output.

### Shipping

1. Review ready-to-ship queue.
2. Create shipment records.
3. Mark shipped and print packing slips.

## 4.9 System Administration

### Users

1. Create users and assign role.
2. Activate/deactivate accounts.
3. Reset passwords.

### Work Centers

1. Create/update work centers.
2. Update status (`available`, `in_use`, `maintenance`, `offline`).
3. Maintain type/rate/capacity.

### Admin Settings (Admin-only route)

Tabs and usage:

1. Materials - quote and cost model material library
2. Machines - machine rates/setup assumptions
3. Finishes - outside finishing cost and lead assumptions
4. Labor Rates - default labor cost model
5. Work Center Rates - editable rate table
6. Work Center Types - manage canonical work center type list
7. Outside Services - vendor process services and lead times
8. Overhead/Markup - margin and quantity break settings
9. Employees - 4-digit operator account provisioning
10. Roles and Permissions - override/reset role permissions
11. Audit - settings audit trail

## 5. Reporting and Analytics

### Reports

Tabs:

1. Dashboard reports
2. Costing
3. Timesheets

Standard report set:

1. Daily output
2. Work center utilization
3. Work order costing
4. Production summary
5. Quality metrics
6. Vendor performance
7. Inventory value
8. Employee time report

### Analytics

Views:

1. KPI dashboard
2. Production trends
3. Quality metrics
4. Inventory turnover and demand prediction
5. Capacity forecast
6. Cost analysis
7. Report templates and data source health

## 6. End-to-End Training Exercise

Use this as a live class script:

1. Create a customer.
2. Create a manufactured part.
3. Build and release BOM.
4. Build and release routing.
5. Create work order with that customer and due date.
6. Release work order.
7. In `Scheduling`, schedule earliest.
8. Raise priority to `P2` with a reason.
9. Start operation in shop floor view.
10. Complete operation and work order.
11. Create shipment and print packing slip.
12. Verify audit entries for all key transactions.

## 7. Troubleshooting Playbook

Common issues:

1. Work order will not release: verify operations exist and confirm routing/BOM status is released where required.
2. Cannot schedule unscheduled work: verify current operation has valid work center and confirm work order is active (`released` or `in_progress`).
3. Priority update fails: validate permission (`work_orders:edit`) and priority range (`1` to `10`).
4. Receiving inspection blocked: confirm accepted + rejected quantities are valid and required reject notes are entered.
5. Operator cannot start operation: check that prior sequence is complete and status is valid (`pending`, `ready`, or `on_hold` as applicable).

## 8. Function Coverage Appendix (Admin)

This map is for training completeness and references app function names as implemented.

1. `AdminSettings`: activateUser, createAdminFinish, createAdminLaborRate, createAdminMachine, createAdminMaterial, createAdminOutsideService, createUser, deactivateUser, deleteAdminFinish, deleteAdminLaborRate, deleteAdminMachine, deleteAdminMaterial, deleteAdminOutsideService, getAdminFinishes, getAdminLaborRates, getAdminMachines, getAdminMaterials, getAdminOutsideServices, getAdminOverhead, getAdminWorkCenterRates, getAdminWorkCenterTypes, getRolePermissions, getSettingsAuditLog, getUsers, resetRolePermissions, seedAdminLaborRates, seedAdminOutsideServices, seedQuoteDefaults, updateAdminFinish, updateAdminLaborRate, updateAdminMachine, updateAdminMaterial, updateAdminOutsideService, updateAdminOverhead, updateAdminWorkCenterRate, updateAdminWorkCenterTypes, updateRolePermissions, updateUser
2. `Analytics`: getAnalyticsQualityMetrics, getCapacityForecast, getCostAnalysis, getDataSources, getInventoryDemandPrediction, getInventoryTurnover, getKPIDashboard, getOEEDetails, getProductionTrends, getReportTemplates
3. `AuditLog`: getAuditActions, getAuditLogs, getAuditResourceTypes, getAuditSummary
4. `BOM`: addBOMItem, commitBOMImport, createBOM, createPart, deleteBOM, deleteBOMItem, explodeBOM, getBOM, getBOMs, getParts, previewBOMImport, releaseBOM, unreleaseBOM
5. `Calibration`: createEquipment, getEquipment, recordCalibration, updateEquipment
6. `Customers`: createCustomer, getCustomers, getCustomerStats, updateCustomer
7. `CustomFields`: createCustomFieldDefinition, deleteCustomFieldDefinition, getCustomFieldDefinitions, updateCustomFieldDefinition
8. `Dashboard`: getDashboardWithCache, getEquipmentDueSoon, getLowStockAlerts, getQualitySummary
9. `Documents`: deleteDocument, downloadDocument, getDocuments, getDocumentTypes, getParts, uploadDocument
10. `Inventory`: getInventory, getInventoryLocations, getInventorySummary, getLowStockAlerts, getParts, receiveInventory, transferInventory
11. `MaterialsInventory`: createInventoryLocation, createPart, getInventory, getInventoryLocations, getInventorySummary, getLowStockAlerts, getParts, getSuggestedPartNumber, receiveInventory, transferInventory
12. `MRP`: getMRPActions, getMRPRuns, getMRPShortages, processMRPAction, runMRP
13. `Parts`: commitBOMImport, createCustomer, createPart, createRouting, deletePart, getBOMByPart, getBOMs, getCustomerNames, getCustomerStats, getParts, getRoutings, previewBOMImport, updatePart
14. `PartsInventory`: getInventory, getInventoryLocations, getInventorySummary, getLowStockAlerts, getParts, receiveInventory, transferInventory
15. `POUpload`: createPOFromUpload, getPOPdfUrl, searchPartsForPO, searchVendorsForPO, uploadPOPdf, uploadQuotePdf
16. `PrintPackingSlip`: getShipment
17. `PrintPurchaseOrder`: getPurchaseOrderPrintData
18. `PrintTraveler`: getMaterialRequirements, getPart, getWorkOrder
19. `Purchasing`: createPart, createPurchaseOrder, createVendor, deleteDocument, downloadDocument, getDocuments, getDocumentTypes, getInventoryLocations, getParts, getPendingInspection, getPurchaseOrders, getReceivingQueue, getVendors, inspectReceipt, receiveMaterial, sendPurchaseOrder, updateVendor, uploadDocument
20. `Quality`: createCAR, createFAI, createNCR, getCARs, getFAIs, getNCRs, getParts, getQualitySummary
21. `QuoteCalculator`: analyzeDXF, calculateCNCQuote, calculateSheetMetalQuote, getQuoteFinishes, getQuoteMaterials, seedQuoteDefaults
22. `Quotes`: convertQuote, createQuote, getParts, getQuotes, sendQuote
23. `Receiving`: getInspectionQueue, getOpenPOsForReceiving, getPOForReceiving, getReceiptDetail, getReceivingHistory, getReceivingLocations, getReceivingStats, inspectReceiptNew, receiveNewMaterial
24. `Reports`: getDailyOutput, getEmployeeTimeReport, getInventoryValue, getProductionSummary, getQualityMetrics, getVendorPerformance, getWorkCenterUtilization, getWorkOrderCosting
25. `Routing`: addRoutingOperation, createRouting, deleteRouting, deleteRoutingOperation, getBOMByPart, getPart, getParts, getRouting, getRoutingByPart, getRoutings, getWorkCenters, releaseRouting, updateRoutingOperation
26. `Scheduling`: getCapacityHeatmap, getSchedulableWorkOrders, getWorkCenters, scheduleWorkOrder, scheduleWorkOrderEarliest, updateOperationWorkCenter, updateWorkOrderPriority
27. `Shipping`: createShipment, getReadyToShip, getShipments, markShipped
28. `ShopFloor`: clockIn, clockOut, getMyActiveJob, getWorkCenterQueue, getWorkCenters, getWorkOrder, updateWorkOrderPriority
29. `ShopFloorSimple`: completeOperation, getDashboardWithCache, getOperationDetails, getShopFloorOperations, getWorkCenters, holdOperation, resumeOperation, startOperation, updateWorkOrderPriority
30. `Traceability`: searchLots, traceLot
31. `Users`: activateUser, createUser, deactivateUser, getUsers, resetUserPassword, updateUser
32. `WorkCenters`: createWorkCenter, getWorkCenters, getWorkCenterTypes, updateWorkCenter, updateWorkCenterStatus
33. `WorkOrderDetail`: completeWOOperation, completeWorkOrder, getActiveUsers, getMaterialRequirements, getUsers, getWorkOrder, releaseWorkOrder, startWorkOrder
34. `WorkOrderNew`: createCustomer, createWorkOrder, getCustomerNames, getParts, getRoutingByPart, getWorkCenters, previewWorkOrderOperations
35. `WorkOrders`: deleteWorkOrder, getWorkOrders, releaseWorkOrder
