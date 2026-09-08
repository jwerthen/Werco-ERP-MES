# Material Nesting

After deploying the updated frontend, open **Sales & Quoting → Material Nesting**
or `/nest` in the signed-in ERP.
The tool estimates sheet quantities for one material and thickness, with
optional USD prices for comparing material cost. Access uses `purchasing:view`,
the same permission as Quote Calculator; see [RBAC_PERMISSIONS.md](RBAC_PERMISSIONS.md#material-nesting).

## Prepare and compare an estimate

1. Each entry to Material Nesting starts with an empty estimate. Begin entering
   parts, or use **Open** to restore a saved estimate file. Set the name,
   material, thickness, part spacing, and edge margin. Dimension fields accept
   decimal inches and fractions such as `1/4` or `1 1/2`.
2. Upload or drop DXFs, or add rectangles and circles by size. Confirm each
   imported part's dimensions and quantity, and set rotation/grain constraints.
   Read the import report: valid files stay in the estimate when other files
   are skipped. A **Footprint** result represents the entire drawing as one
   rectangular design; verify its overall size and set quantity for the whole
   drawing, rather than for one of its internal contours.
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
combine stock sizes. For closed contours, utilization uses contour area less
holes divided by total purchased sheet area. A whole-drawing footprint uses
its full rectangle, including openings and empty space: part area and
utilization may be overstated, and unused area understated. The workspace
labels the combined area **Estimated footprint area** whenever any part uses
this fallback; the CSV summary also identifies each part's geometry basis.
Material prices exclude freight, tax, labor, cutting time, and consumables;
weight uses typical material density. Check dimensions, quantities, supplier
sheet sizes, and your shop's handling capacity before ordering.

## Import limits and units

| Input | Limit or behavior |
|-------|-------------------|
| DXF batch | Up to 100 files; each file smaller than 5,000,000 bytes |
| Estimate | Up to 300 designs and 300 total parts, including quantities |
| Geometry | Up to 2,000 vertices per contour after curve conversion, 20,000 geometry vertices per parsed file and estimate, and 300 closed contours per file |
| DXF records | Up to 20,000 entity records per file, including legacy polyline vertex records |
| Stock options | Up to 12 |
| Contour import | ASCII DXF model-space `LINE`, `ARC`, `CIRCLE`, `LWPOLYLINE`, and ordinary legacy 2D `POLYLINE`/`VERTEX`/`SEQEND`; polylines may include circular bulges |
| Footprint import | Open, branching, touching, or intersecting paths; supported planar `SPLINE` geometry; or otherwise flat geometry on several parallel Z planes produce one rectangle around the whole drawing |
| Omitted metadata | `VIEWPORT` records; `TEXT` and `LEADER` only on the `FORMAT` layer, with an import warning for those annotations |
| Rejected files | Malformed coordinates or entity data, unknown/unsupported cut entities, blocks/`INSERT`, wide polylines, sloped or nonplanar geometry, tilted extrusion directions, or paper-space cut geometry; other text/annotation entities are not silently discarded |
| DXF units | Inch and millimeter files retain their physical size; unitless files default to inches unless millimeters is selected before import |
| Saved estimates | Inches with optional USD prices; whole-drawing parts retain `importMode: "drawing-bounds"` when saved and reopened; supported legacy job files can also be opened |

Split larger jobs into estimates. Malformed or unsupported files and files
that exceed a resource or size limit are skipped as a whole with an explanation;
the importer does not add their readable contours as a partial file.

### How drawing geometry becomes parts

Closed outer contours become separate designs at quantity 1; interior contours
remain holes. Uniquely paired endpoints within **0.0001 inch** join into closed
paths. Small join gaps retain both endpoints instead of snapping the drawing
smaller. Circular arcs and polyline bulges are approximated by straight segments
at **0.0001-inch curve tolerance**, with exact X/Y extrema included to preserve
their sheet-fit bounds. Circles remain circles. These physical tolerances apply
equally to inch and millimeter files; highly detailed curves can reach the
vertex limit during conversion.

Flat geometry may be translated away from Z=0. Both +Z and -Z object-coordinate
systems are supported: -Z circular/polyline geometry is transformed into world
coordinates, while `LINE` endpoints already use world coordinates. Geometry
on one common XY-parallel plane can form contours. Several parallel elevations
use the full XY projection as one footprint, with an import warning; a sloped
entity or an entity whose points do not share a flat plane is rejected.

If paths cannot form unambiguous closed contours, or contours overlap or
intersect, the importer uses the bounds of **all supported geometry in the
file** as one footprint. It does not infer separate parts from that drawing.
The import report explains the fallback, and the parts list labels it
**Whole drawing footprint · verify size**. Verify the overall dimensions and
quantity even if some internal contours look like individual parts.

Supported splines also force a whole-drawing footprint. The importer accepts
validated planar `SPLINE` data with degree 1–10, control points, valid knots,
and positive weights (or omitted weights, treated as 1). It uses the bounding
rectangle of the control-point hull, which encloses the curve but can be larger
than the curve itself. It does not reconstruct an exact spline profile or
subtract spline-shaped openings. Unsupported or malformed spline data is
rejected rather than approximated from incomplete data.

## Storage and ERP integration

Material Nesting starts with a fresh, empty estimate each time you enter the
section, without demo parts or an automatically restored draft. Edits remain
while you work within the page, including switching its workspace, stock, and
help tabs. Leaving for another ERP page clears the current estimate; returning
starts empty. Changing the user or active company also starts a fresh estimate.
There is no localStorage, sessionStorage, or server-side save.

**Save before leaving the section**, refreshing, closing the tab, signing out,
or switching companies. While the workspace has unsaved edits, it requests the
browser's refresh/close warning; ordinary ERP navigation does not prompt.
Use **Open** to restore an estimate from a saved file, then compare sheets to
calculate fresh results.

The estimator does not create ERP quotes, purchase orders, work orders,
production laser nest packages, inventory movements, or audit records. Its
local files and sheet previews are estimating outputs; it has no machine
connection, cutting recipes, or postprocessor.

The workspace is a native ERP route with feature-local styles isolated in a
ShadowRoot and notifications supplied by the existing ERP toast provider.
It uses the existing Vercel frontend deployment and SPA rewrite. No new API,
database migration, environment variable, or hosting project is required; see
[DEPLOYMENT.md](DEPLOYMENT.md#existing-vercel-frontend-material-nesting).
