# Screenshots Guide for Landing Page

This guide explains what screenshots to capture from the ERP/MES application for the landing page.

## Required Screenshots

Place your screenshots in: `public/screenshots/`

### 1. Dashboard (`dashboard.png`)
- Capture the main dashboard showing:
  - KPI cards (Active WOs, On Schedule, Alerts)
  - Work center status grid
  - Recent activity or action items

### 2. Work Orders List (`work-orders.png`)
- Show the work orders table with:
  - Filter controls
  - Status badges
  - Action buttons
  - Multiple rows showing different work orders

### 3. Work Order Detail (`work-order-detail.png`)
- Show a detailed work order view with:
  - Work order header info (WO#, Part, Status)
  - Routing details with operations
  - Timeline/Gantt view
  - Documents section

### 4. Shop Floor Dashboard (`shop-floor.png`)
- Capture the shop floor simplified view:
  - My Active Job box
  - Work center queue
  - Priority sorting
  - Clock in/out buttons

### 5. Quality/NCR (`quality.png`)
- Show the quality management screen:
  - NCR list or NCR detail
  - NCR form fields
  - Status workflow
  - Attachments section

### 6. Scheduling/Gantt (`scheduling.png`)
- Capture the Gantt scheduling view:
  - Drag-and-drop schedule bars
  - Work centers on Y-axis
  - Timeline on X-axis
  - Gantt bars with different colors

### 7. Parts & BOM (`parts-bom.png`)
- Show parts management:
  - Parts list
  - BOM view with multi-level structure
  - Make vs. buy indicators

### 8. Purchasing (`purchasing.png`)
- Show purchasing management:
  - PO list
  - Receiving workflow
  - Vendor information

### 9. Reports/Analytics (`reports.png`)
- Capture analytics dashboard:
  - Charts and graphs
  - Production metrics
  - Quality metrics
  - Inventory reports

### 10. Mobile View (`mobile.png`)
- Show the app on mobile device or narrow window:
  - Responsive design
  - Touch-friendly interface
  - Key features accessible on mobile

## How to Take Screenshots

### Option 1: Using the Running Application

If your frontend app is running (typically on http://localhost:3000):

1. Open your web browser
2. Navigate to `http://localhost:3000`
3. Log in with your credentials
4. Navigate to each section you want to capture
5. Take a screenshot:
   - **Windows**: Win+Shift+S for Snipping Tool
   - **Mac**: Cmd+Shift+4 for screenshot selection
6. Save the screenshot as the appropriate filename in `landing/public/screenshots/`

### Option 2: Full Page Screenshots

For full page screenshots (recommended):

- **Chrome**: Inspect → Device Toolbar (Ctrl+Shift+M) → Capture screenshot
- Use browser extensions like "Full Page Screen Capture"
- Command line tools like `selenium` or `puppeteer`

## Recommended Screenshot Settings

- **Resolution**: 1920x1080 or higher
- **Format**: PNG (for best quality)
- **Show cursor**: No
- **Browser zoom**: 100%
- **Dark mode**: Default to light mode (unless you want to showcase both)
- **Data**: Use realistic sample data or your existing seed data

## Tips for Better Screenshots

1. **Use realistic data**: Your seed data provides good examples
2. **Show variety**: Capture different states (pending, in-progress, complete)
3. **Focus on UI elements**: Make sure buttons, badges, and inputs are visible
4. **Maximize window**: Use browser in full screen or maximized mode
5. **Chrome settings**: Hide unnecessary Chrome UI elements
6. **Consistent styling**: Keep same time/date across screenshots if possible

## Naming Convention

Use descriptive filenames:
- `dashboard.png` - Main dashboard
- `work-orders.png` - Work orders list
- `work-order-detail.png` - Single work order view
- `shop-floor.png` - Shop floor simplified view
- `quality.png` - Quality management
- `scheduling.png` - Gantt scheduling
- `parts-bom.png` - Parts and BOM
- `purchasing.png` - Purchasing module
- `reports.png` - Analytics/reports
- `mobile.png` - Mobile responsive view

## Testing Screenshots

Once screenshots are placed:
1. Open `landing/index.html`
2. Scroll to the "Screenshots" section
3. Verify all images load correctly
4. Check that images are display properly and are crisp

## Notes

- Screenshots will be served from `public/screenshots/` directory
- Images are optimized for web (consider compression if file size > 500KB)
- Alternative text descriptions are provided for accessibility
- Screenshots are displayed in a grid layout with captions
