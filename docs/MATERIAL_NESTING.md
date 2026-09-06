# Material Nesting

After deploying the updated frontend, open **Sales & Quoting → Material Nesting**
or `/nest` in the signed-in ERP.
The tool estimates sheet quantities for one material and thickness, with
optional USD prices for comparing material cost. Access uses `purchasing:view`,
the same permission as Quote Calculator; see [RBAC_PERMISSIONS.md](RBAC_PERMISSIONS.md#material-nesting).

## Prepare and compare an estimate

1. Start a new estimate or open a saved estimate file. Set the name, material,
   thickness, part spacing, and edge margin. Dimension fields accept decimal
   inches and fractions such as `1/4` or `1 1/2`.
2. Upload or drop DXFs, or add rectangles and circles by size. Confirm each
   imported part's dimensions and quantity, and set rotation/grain constraints.
   Read the import report: valid files stay in the estimate when other files
   are skipped.
3. In **Stock sizes & prices**, enable the sheet sizes available to your shop
   and enter optional prices per sheet. Changing a sheet's dimensions clears
   its price, so enter a price for the new size.
4. Choose **Least material to buy** or **Lowest material cost**, then **Compare sheets**.
   Only options that fit every requested part can be recommended. Cost
   comparison needs a price for every complete option. Select a result to
   inspect each sheet; recalculate after editing inputs.
5. **Save** downloads an editable `.estimate.json` file. **Save summary**
   downloads the material comparison as CSV; **Save nest preview** downloads
   the selected sheet as SVG.

The layout uses conservative rectangles around each part and tries three part
orderings per sheet size. It establishes a feasible estimate, but may use more
material than irregular-shape nesting and does not prove the minimum sheet
count. Each comparison uses one stock size for the entire order; it does not
combine stock sizes. Utilization uses part area less holes divided by total
purchased sheet area. Material prices exclude freight, tax, labor, cutting time,
and consumables; weight uses typical material density. Check dimensions,
quantities, supplier sheet sizes, and your shop's handling capacity before
ordering.

## Import limits and units

| Input | Limit or behavior |
|-------|-------------------|
| DXF batch | Up to 100 files; each file smaller than 5,000,000 bytes |
| Estimate | Up to 300 designs and 300 total parts, including quantities |
| Geometry | Up to 2,000 vertices per contour and 20,000 per estimate |
| Stock options | Up to 12 |
| Supported DXF geometry | ASCII DXF with closed straight `LWPOLYLINE` contours and `CIRCLE` entities; separate outer contours become separate designs and interior contours remain holes |
| Unsupported geometry | Open lines, arcs, bulged polylines, splines, blocks, wide lines, and 3D geometry are rejected with an explanation |
| DXF units | Inch and millimeter files retain their physical size; unitless files default to inches unless millimeters is selected before import |
| Saved estimates | Inches with optional USD prices; supported legacy job files can also be opened |

Split larger jobs into estimates. A file that would exceed an estimate limit is
skipped as a whole and listed in the import report.

## Storage and ERP integration

One draft is retained in this tab's memory for the current user and active
company. You can navigate to another ERP page and return to the estimate;
opening the workspace as a different user or company resets the draft.
There is no localStorage, sessionStorage, or server-side save.

Save an estimate file before refreshing, closing the tab, signing out, or
switching companies. While the workspace has unsaved edits, it requests the
browser's refresh/close warning. Reopen a saved file to resume later, then
compare sheets to calculate fresh results.

The estimator does not create ERP quotes, purchase orders, work orders,
production laser nest packages, inventory movements, or audit records. Its
local files and sheet previews are estimating outputs; it has no machine
connection, cutting recipes, or postprocessor.

The workspace is a native ERP route with feature-local styles isolated in a
ShadowRoot and notifications supplied by the existing ERP toast provider.
It uses the existing Vercel frontend deployment and SPA rewrite. No new API,
database migration, environment variable, or hosting project is required; see
[DEPLOYMENT.md](DEPLOYMENT.md#existing-vercel-frontend-material-nesting).
