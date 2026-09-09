# Material Nesting

The separate **Warehouse → Inventory → Piece observations** register can retain
reported sheet/remnant measurements and immutable source history. A current
recorded piece can be explicitly selected for a conditional nesting comparison;
selection does not verify availability, reserve inventory or assign credit. See
[the observation workflow](STOCK_OBSERVATIONS.md) and
[recorded-piece planning](RECORDED_PIECE_PLANNING.md). Material Nesting still starts
with a fresh empty estimate; no observed or predicted stock is automatically loaded.

To retain the original bytes behind a saved nest, use **Team drafts → Original
DXFs** for that exact revision. Select up to 100 originals and review the matching
saved profiles before attaching them. See [the attachment workflow](NESTING_ORIGINAL_DXFS.md)
and [storage and recovery design](CAD_SOURCE_STORAGE.md). This action preserves
the saved input and current workspace; an attachment does not approve its geometry.

Open **Sales & Quoting → Material Nesting** or `/nest` in the signed-in ERP.
The tool estimates separate sheet orders for multiple material/thickness groups,
with optional USD prices for comparing material cost. Access uses `purchasing:view`,
the same permission as Quote Calculator; see [RBAC_PERMISSIONS.md](RBAC_PERMISSIONS.md#material-nesting).

## Prepare and compare an estimate

1. Each entry to Material Nesting starts with an empty estimate. Begin entering
   parts, use **Open** for a local estimate file, or explicitly choose a revision
   from **Team drafts**. Set the project name
   and the active group's material, thickness, part spacing, and edge margin.
   Dimension fields accept decimal inches and fractions such as `1/4` or `1 1/2`.
2. Upload or drop DXFs, or add rectangles and circles by size. Before files are
   parsed, select filename rows and apply their material, thickness, optional
   exact ERP catalog record, and units to use for unitless drawings. All files
   are imported with the assignments shown, including unchecked rows. Files with
   the same material identity and thickness join the same group; different thicknesses
   never share a sheet. Duplicate filenames remain separate rows. Confirm each
   imported part's dimensions and quantity, and set rotation/grain constraints.
   Read the import report: valid files stay when other files are skipped.
3. In **Stock sizes & prices**, enable the sheet sizes available to your shop
   for the active group and enter optional prices per sheet, or resolve them
   from **ERP material & pricing** as described below. Changing a sheet's
   dimensions clears its price. Changing a group's material/thickness clears
   its prices; matching groups combine their parts. New groups copy active stock
   sizes and enabled flags with independent options and blank prices.
4. Choose **Least material to buy** or **Lowest material cost**, then **Compare sheets**.
   All populated groups calculate separately in a background worker. Progress
   identifies the current group; **Cancel comparison** stops the current run.
   Completed group results remain available after cancellation. A group has a
   two-minute calculation limit; split a job into separate estimates if needed.
   Only options that fit every requested part can be recommended. Cost
   comparison needs a price for every complete option. Select a result to
   inspect each sheet; use the group selector/cards to review every separate
   order. Results are cached per group; changing inputs makes them stale until
   the next comparison. Opening a project or selecting a group does not run nesting.
5. **Save** downloads every group and the active selection in one editable
   `.estimate.json` file. **Save summary** downloads the active group's material
   comparison as CSV; **Save nest preview** downloads its selected sheet as SVG,
   including reference lines. **Export review record** downloads a draft JSON
   record covering the current inputs and all compared groups, including potential
   leftover geometry when analysis is available. **Team drafts** saves inputs
   to the ERP as a new draft or a new immutable revision; it does not save a
   validated comparison or change material inventory. Use a saved revision's
   **Saved calculations** action when you want the server to retain a calculation.

The steps above describe ordinary full-sheet comparison. An optional recorded-piece
selection adds one actual measured shape, including its physical holes, to one
material group. All full-sheet baselines remain visible. Each conditional alternative
uses that piece at most once, then full sheets for the exact remaining originals.
The selected project shares one 120-second calculation deadline and at most 36
stages across its baseline, piece and remaining-sheet alternatives. Completed stages
remain available if later work stops. Source/group changes require explicit refresh
and reaffirmation; file Open never silently reconfirms a piece. See
[the conditional workflow](RECORDED_PIECE_PLANNING.md) for saved history, partial
review exports and source-review requirements.

The layout places actual outer contours, including concave profiles, using
multiple part orderings and the permitted rotations after applying grain requirements. It checks contour
separation and sheet margins for the resulting placements. It establishes a
feasible estimate, but does not prove the minimum sheet count. Holes remain
visible and subtract from part area; other parts are not placed inside them.
Each group's comparison uses one stock size for its entire order and does not
combine stock sizes. Utilization uses contour area less holes divided by total
purchased sheet area. The preview and exports are estimating layouts, not
production toolpaths. Material prices exclude freight, tax, labor, cutting time, and consumables;
weight uses the selected catalog density when bound to an ERP source (unknown
when that source has no valid density), or typical density for a family-only
estimate. Check dimensions, quantities, supplier
sheet sizes, and your shop's handling capacity before ordering.

## Saved calculations for team review

1. Save the inputs through **Team drafts**. For that saved revision, choose
   **Saved calculations** and then **Calculate saved revision**. This uses the
   selected revision exactly, including its quantities, geometry, spacing and
   entered prices. Unsaved workspace changes are not submitted. New calculations
   require the current geometry profile in every populated material group. For
   older inputs, open the revision, select **Use current clearance rules**, and
   save a new revision; the earlier input and its saved runs remain unchanged.
2. **Review run** shows the number of stock options evaluated and how many fit
   their entire material group. **Finished** means the search ended; it does not
   mean all parts fit. Every stock size is an alternative scenario. Do not add
   the sheet counts of different alternatives together.
3. **View option** opens the actual saved contours and holes, sheet selector,
   inch dimensions, margin/gap, and potential-leftover regions. The view binds
   the saved input hash, stock, recorded solver/runtime/profile and result before rendering.
   Historical v4/v5/v6 runs retain their recorded validation rules; viewing history
   never repacks parts under the current profile. A sheet above the
   100,000-vertex preview budget remains in the saved report but is not drawn.
4. **Cancel calculation** retains already completed, checked option results.
   A timeout or work limit also keeps completed results and labels missing work.
   Leaving this panel stops browser polling, not the server's submitted job.
   Use the cancel action to stop that job explicitly. Entering the nesting section
   later still begins with empty workspace inputs.
5. **Download saved report** exports an unapproved JSON evidence snapshot:
   exact imperial input revision, server identity, settings, warnings and retained
   results. Its internal geometry payload explicitly uses millimeters; all
   on-screen dimensions and areas use inches/feet. This report is not an approved
   quote export, CAD-source signature, inventory record or machine program.

The server uses the same geometry kernel as the browser and checks each emitted
layout before retaining it. It permits one active calculation per company and
one nesting child per worker process. The initial technical profile allows 120
seconds for the entire project, 36 enabled stock options in saved order and a
512 MiB Node heap. Saved input remains limited to 5 MiB, 300 total part instances
and 20,000 source vertices. Output budgets are 8 MiB per option message and 24 MiB
of retained option content. These are computational limits, not shop-approved
manufacturing parameters or a proof of the optimal material order.

If the matching calculation service is starting or unavailable, refresh its
status. Local **Compare sheets** remains usable. An unconfirmed start shows
**Retry calculation request**, which recovers the same request instead of
creating another run. A stopped/expired run is never silently retried with a new
solver; explicitly create a new calculation. Permission loss or a changed
worker build can stop work while retaining earlier checked checkpoints.

Saved calculations remain **unapproved**. Source CAD bytes are not retained by
this workflow; material identity, original source hashes, drawing semantics,
shop rules and prices still need review. Potential remnants receive $0 credit.
There is no physical-material allocation, reservation, approval or quote-cost
writeback. The panel shows release, solver/bundle/runtime, input hash and limits
under **Calculation identity, limits and review notes**. Fixed code-unit ordering
replaces locale-dependent tie-breaking in solver v4; the recorded seed is null
because this search is not random.

## Rotation and grain requirements

Select a part to set **Allowed rotation** and **Part grain**. Rotation choices
are **Fixed (0°)**, **Half turns (0°, 180°)**, and **Quarter turns
(0°, 90°, 180°, 270°)**. Set **Sheet grain** for the active material group;
that direction applies to every stock size compared in that group.

- Part grain X means horizontal in the source drawing; Y means vertical.
- Sheet grain X runs along sheet length (horizontal in the nest); Y runs along
  sheet width (vertical in the nest). These are axes, so a 180° turn preserves
  grain alignment and a 90°/270° turn exchanges X and Y.
- **No grain requirement** permits the selected rotation policy without a grain
  restriction. **Unknown / not specified** sheet grain is not permission to
  rotate a part that requires grain alignment.

The solver intersects the rotation policy with grain alignment before creating
candidates, including rectangular and circular parts. For example, a part with
X grain on a Y-grain sheet can use 90° or 270° only when quarter turns are
permitted. Fixed and half-turn policies cannot satisfy that pairing. Required
part grain with unknown sheet grain remains unplaced with an explanation;
unrestricted parts can still be placed, but an incomplete order cannot be
recommended. Sheet-size changes do not resolve missing or conflicting grain.
The final placement validator checks the same rules, and the preview and CSV
show permitted orientations and sheet grain. Editing these settings invalidates
previous comparisons and draft review exports until the groups are compared again.

These are estimator-assigned requirements, not grain data inferred from a DXF,
material name, or ERP catalog record. Confirm the drawing and purchased sheet
specification. Free-angle rotation, mirroring, and nesting parts inside holes
remain unavailable.

## Excluded stock areas

In **Stock sizes & prices**, open **Excluded areas** on a stock option. Add a
rectangle or circle using decimal or fractional inches, an area label, a reason
the material is unavailable, and optional added clearance. Coordinates start at
the sheet's lower-left corner: X follows length and Y follows width. An existing
polygon imported through a saved estimate retains its exact outline; this first
editor supports changes to its label, reason and clearance, not polygon reshaping.

These are **estimator-reported quote inputs**, not physically verified stock or
machine settings. The regions repeat on **every hypothetical sheet of that size
option**. They do not identify a particular sheet, confirm damage or certification,
allocate inventory, reserve material, or configure a clamp or machine control.
Use a separate option/estimate when that repeat assumption is inappropriate.
New stock options and newly imported material/thickness groups start without
exclusions; source coordinates are not copied into a different material group.

Each option permits 16 regions, with up to 2,000 source polygon vertices in total;
a circle counts as one source vertex. All options, including disabled options,
share the project's 20,000-vertex budget with parts and reference geometry.
Region labels and reasons are required. Added clearance is 0–100 inches, an
engineering input bound rather than a recommended shop allowance. The complete
entered outline must lie inside the gross sheet. An incompatible sheet-size
reduction is rejected; the program never crops, translates, or deletes an area
to make the edit work. Overlapping exclusion regions are permitted and counted
once through their union.

Red outlines in the editor, layout and SVG are the **entered unavailable areas**.
The solver also reserves the entered clearance around each exclusion, the part's
half-gap and curve allowance, and numerical guards. It checks both nominal
profile distance and intersections of the same conservative vector envelopes
used by leftover analysis. Rectangle-only packing is disabled when nonempty
exclusions are present. Candidate simplification is bounded and final validation
uses the actual guarded shapes. A search that leaves parts unplaced is an
incomplete result, not proof that no arrangement can exist.

Adding, editing or removing an exclusion invalidates the comparison and its
review export until recalculation. Save/Open, team input revisions, server
calculations and review exports retain coordinates, clearance, label and reason.
Input revisions and their save audit are retained; approval, per-edit reason
codes for removal and an automatic reapproval workflow remain future work.

## Potential leftover regions

After a comparison, **Potential leftovers** overlays the selected sheet with
amber vector regions. The review panel shows each connected region's actual
outline and holes, its area, and its overall extents. Select a region to
highlight it; the thumbnail list pages eight regions at a time. Extents are
bounding measurements, not a guaranteed usable rectangle. A connected shape
can be a narrow skeleton that cannot be handled or reused.

Every region remains **review** and contributes **$0 credit**. This predicts
space left by the proposed placements; it does not prove that material has
been cut, remains recoverable, has the right traceability, or meets a remnant
eligibility rule. It creates no inventory, reservation, purchase/quote update,
remnant identifier, or production instruction. Sheet recommendations and
material costs receive no leftover credit.

Analysis uses the original validated placements, subtracts guarded full outer
part profiles and guarded exclusion regions from the usable rectangular sheet, and preserves disconnected
regions and holes through a polygon-tree difference. Internal part cutouts
stay reserved. It does not replace profiles with bounding boxes. **Area
breakdown and assumptions** distinguishes:

`gross sheet = edge margins + excluded stock + nominal finished parts + reserved internal cutouts + clearance/numerical protection + potential leftover regions`

For ordinary full sheets, **Usable sheet area** means the rectangle after the entered edge margins, before
stock exclusions. **Excluded stock within margins** measures the guarded union
inside the numerically protected usable boundary, so overlapping areas and areas
inside edge margins are not double-counted. The same reserved polygons are
subtracted from the potential leftovers. All resulting regions remain review-only
with zero credit, including disconnected pieces.

Nominal part/cutout areas use the geometry model, including analytic circle
areas. Numerical loss is assigned to clearance/protection, never described as
physical kerf. The draft JSON also records the signed reconciliation residual;
nontrivial negative allowances or residuals make analysis unavailable rather
than inventing recoverable area.

### Engineering analysis profile

New calculations use `werco-compensated-v1` and solver `werco-contour-v7`.
Recorded-piece calculations also require `werco-remnant-domain-v1`; their actual
boundary/hole containment and area ledger are documented in
[the remnant domain ledger](REMNANT_LEFTOVER_LEDGER.md).
The source profile ID and SHA-256 are explicit inputs, recorded in the worker
manifest/hello, saved run settings and review evidence. They identify engineering
rules; they do not approve a shop clearance or cutting policy.

Every full part envelope must lie inside the inward-protected usable sheet and
have no interior overlap with another part or an exclusion envelope. Nominal
full-gap/curve checks remain independent. Internal holes remain reserved, so
part-in-part placement is prohibited. Square-tangent corner protection is
conservative and can reduce yield compared with nominal-distance checks alone.

The edge margin is an edge band outside which the **entire envelope** must fit.
Before curve/numerical protection, a 3/8 in margin plus 1/8 in gap requires the
nominal part edge to be at least 7/16 in from the sheet edge. Do not subtract
half-gap from the margin to preserve an earlier layout. Selected gap/margin and
approved spacing-policy values are not silently changed by the profile.

Ordinary full-sheet calculations use `werco-leftovers-v3`, with a required `excludedArea`
field (zero when there are no zones). It subtracts the same prepared envelopes
used for final validation. Historical `werco-leftovers-v1` remains readable for
old inputs with no exclusion regions; v2 remains readable for nonempty legacy
exclusions. Empty legacy arrays retain v1. All versions remain review-only with
zero material credit. Recorded-piece outcomes use `werco-leftovers-v4`, retaining
the actual physical holes, protected area and usable domain. They also remain
review-only with zero credit; connected leftover geometry is not proof of a
physically usable or available remnant.

| Setting | Value or behavior |
|---------|-------------------|
| Integer grid | 0.0001 mm (approximately 0.000003937 in) |
| Current part-envelope frame | Normalize each allowed rotated source at the origin, quantize once, and translate its prepared envelope by exact integer-grid coordinates; nominal checks retain unrounded source geometry |
| Circle conversion | Circumscribed polygons with radial excess at most 0.0001 in; analytic nominal area is retained |
| Reserved distance | Half the selected part gap + imported curve tolerance + 0.0004 mm numerical protection; rounded outward to the grid |
| Offset corners | Square tangent joins, which enclose the round offset; convex corner radius can reach √2 times the reserved distance |
| Usable boundary | Inset an additional 0.0004 mm and round inward to the grid |
| Area arithmetic | Origin-relative integer polygon products; model areas retain normal analytical calculations |
| Reconciliation tolerance | Larger of 0.0000001 mm² or gross sheet area × 0.000000000001; the signed residual remains in draft evidence |
| Input budget | 60,000 vertices across placed outer profiles; circles at most 8,192 vertices each |
| Output budget | 30,000 vertices per sheet, 120,000 across the option, and 2,000 connected regions; guarded offset paths also have a 120,000-vertex per-sheet budget |
| Offset range | Reserved distance at most 40,000 mm; larger requests report analysis unavailable |
| Exclusion guards | Same grid, circumscribed circles, square tangent joins and 0.0004 mm numerical protection; entered exclusion clearance is separate from the part envelope |
| Exclusion expansion budget | At most 60,000 guarded vertices across one option's regions |
| Exact edge-crossing fallback | At most 8,000,000 candidate edge pairs per envelope comparison; exceeding the guard rejects the calculation |

Current source and derived geometry are separate: original CAD outlines are not
snapped or replaced by their envelope. No rectangle fast path bypasses the guarded
constraints. Final-placement validation failures cannot certify a candidate.
Exact bounding boxes and edge indexes accelerate vector checks; no raster result
is used for final acceptance. Boundary contact is allowed only when interiors
remain disjoint and the nominal minimum also passes.

Historical v4/v5 validation keeps its nominal full-gap/curve and edge rules;
v5 also enforces guarded part/exclusion separation. Those old reservations may
overlap at corners or be clipped at the sheet edge. Their immutable evidence is
preserved rather than retroactively rejected under v6 rules. A saved report's
leftover label cannot select a weaker validator: solver, runtime, source profile
and leftover identity must agree.

The normative profile is
`backend/app/data/nesting_profiles/werco-compensated-v1.json`. Its checksum covers
canonical ASCII `{id, profile}`; a generated frontend adapter is checked against
that source in CI and self-verified in standalone frontend/worker builds. The
current profile checksum is
`21e8689fb2ce80c72befbc5866f658cd74fe8ed336d1b5c070e182f3081aa55a`.
Changing these rules requires a new reviewed engineering profile; it is distinct
from company spacing-policy approval.

Analysis runs in the same comparison worker, outside rendering. A numerical
or complexity error appears as **Leftover analysis unavailable**; it does not
erase an otherwise valid nest or sheet recommendation, and no leftover area
or value is claimed. Input changes hide stale regions until recalculation.

The CSV includes per-sheet leftover areas and zero credits. SVG previews include
the amber overlay when it is enabled. Draft review JSON includes region geometry
in inches, area in square inches, the exact profile, review assumptions and
zero credit. Export validates cached report structure, geometry areas, and its
relationship to the current sheet/placements without rerunning offset work on
the UI thread. These checks do not turn the draft into an approved remnant.
Leftover results are not stored in the editable estimate; **Open** requires a
fresh comparison. Analysis output is separate from editable stock-exclusion inputs.

## ERP material and price review

**ERP material & pricing** reads the active company's existing quote-material
catalog. Select the exact **Catalog material** and **Price basis**, then
**Resolve sheet prices**. The tool does not infer a catalog record from the
family name, select a nearby thickness price, or use a different price basis
when the chosen price is missing. Per-square-foot pricing requires selecting
an exact catalog price key; the key does not verify the entered thickness.
The displayed catalog update time is not a price effective date.

Per-pound pricing uses sheet area × thickness × catalog density × price;
per-cubic-inch uses area × thickness × price; per-square-foot uses area × price
÷ 144. Dimensions are inches. Calculations use decimal arithmetic, with each
sheet cost rounded half-up to two places. Missing or invalid density prevents
per-pound conversion. A missing, zero, or invalid price stays unresolved.

The existing catalog lacks authoritative currency, structured grade/coating,
certification, inventory mapping, price effective/expiry dates, approved
revision, and verified thickness. Those gaps remain visible even when costs
can be calculated. Before **Apply reviewed USD prices**, acknowledge that you
are treating the values as USD and have reviewed the unresolved metadata for
this estimate. This is an estimator assumption; it does not approve material
compatibility, certification, inventory availability, or the source currency.
The server always reports `confirmed: false` and `currency: null`.

Changing source, thickness, or sheet dimensions clears applied catalog prices
and the acknowledgment. A changed source hash requires refreshing and resolving
again. **Open** also clears saved catalog prices and acknowledgment; refresh
the catalog and review them again in the current company. Family-only estimates
and geometry comparison remain available if catalog access fails. Clear the
catalog source to enter manual prices. All catalog access is read-only and does
not seed missing records. See the [API contract](API.md#quote-nesting-material-provenance).

## Quoting spacing allowances

**Unreviewed starting allowances** use the group's material thickness `t`, in inches:

- Part-to-part gap: `max(0.125, t)` inches, measured between part contours.
- Sheet edge margin: `max(0.375, 2 × t)` inches, reserved at each sheet edge.

| Thickness | Part gap | Edge margin |
|-----------|----------|-------------|
| 1/16 in | 1/8 in | 3/8 in |
| 1/8 in | 1/8 in | 3/8 in |
| 3/16 in | 3/16 in | 3/8 in |
| 1/4 in | 1/4 in | 1/2 in |
| 1/2 in | 1/2 in | 1 in |
| 1 in | 1 in | 2 in |

These are estimator-chosen conservative starting allowances, not a universal
fiber-laser standard or an Ermaksan 6 kW cutting recipe. Carbon steel,
stainless steel, and aluminum start with the same formula. Use manual values
for each material/thickness group when the shop's validated allowances differ.
Editing either allowance switches that group to manual mode. Manual values
remain when its thickness changes; **Auto quoting allowance** restores the
formula and follows future thickness edits. If changing a specification merges
parts into an existing group, that destination group's spacing settings remain;
review them and re-enter its cleared sheet prices.
The estimator does not calculate kerf compensation, pierce locations, leads,
thermal behavior, clamps, or automatic part handling. A production CAM setup
must account for those conditions separately.

### Apply a reviewed company policy

**Check current spacing policy** reads the active company's currently effective
approval for this material family and thickness. Review the returned revision,
band and calculated gap/margin, then explicitly apply it. Checking alone changes
no estimate. Applying locks material, thickness and spacing so the displayed
conformance cannot drift. An exact ERP source can still be reviewed within the
same material/thickness; a family policy does not approve that source's grade,
certification, price or inventory suitability.

To change an applied policy, choose **Use custom spacing**, enter an estimator
reason and select **Use custom allowances**. This removes the conformance claim,
unlocks the fields and retains the reason in the next saved revision. The auto
toggle stays disabled for a recorded override; applying a current policy replaces
the override explicitly. Ordinary legacy/manual estimates remain unreviewed.
Newly imported groups start unreviewed and never inherit an approval silently.

The server independently verifies the exact company, publication, revision,
content hash, band, thickness and arithmetic when saving inputs or starting a
new saved calculation. A withdrawn or superseded approval requires applying a
current policy or recording custom spacing. Existing saved revisions and completed
reports remain unchanged; a calculation already accepted into the queue retains
its frozen settings. Opening a historical revision does not certify its policy
as current. Older saved evidence remains readable.

### Admin policy history and decisions

**Review spacing policies** shows immutable drafts and approval/withdrawal history.
Permitted readers can inspect it. An Admin with nesting write access can:

1. Create a named draft with material-family/thickness bands. **Copy starting
   allowances** is an explicit convenience action, not a shop-approved standard.
   No policy is seeded on deployment or first visit.
2. Review an exact saved revision, then approve it with a reason. A blank effective
   date means the server's approval time; a future date starts at midnight Central.
   Past dates are refused. The current workflow permits an Admin to approve their
   own draft; this policy decision does not approve any nest or quote.
3. Withdraw an approval with a reason. History is preserved. The latest effective
   approval is selected before withdrawal is checked: withdrawing it does not
   reactivate an older policy. A withdrawn future approval blocks policy use from
   its scheduled time unless replaced; the prior current policy applies until then.

Draft, approval and withdrawal commands record the actor, effective time, exact
content, revision/version and required audit evidence. Concurrent edits require
refreshing; uncertain requests offer an identical retry instead of a second
command. Switching company clears prior policy targets and pending UI actions.

Policy bands are lower-inclusive/upper-exclusive intervals, in inches, between
0 and 4 in. Up to 128 nonoverlapping bands may cover the three supported material
families; gaps are allowed and resolve as unmatched. Decimal values use at most
nine fractional places. Thickness is normalized half-up to 0.000000001 in;
`max(minimum, thickness × multiplier)` is rounded upward to that quantum.
The browser and server use exact decimal arithmetic for this rule. This precision
is for consistent policy accounting, not a claim of cutting or CAD accuracy.
Geometry tolerances remain the separate recorded solver profile.

Legacy policy snapshots or recorded overrides use estimate version 9 inside
project version 10. Current profile-bearing inputs use quote14/project15 while
retaining the same policy/override data. Other groups can retain their older supported format. Old readers
must reject these new versions rather than ignore the policy meaning. The saved
calculation and review records carry the snapshot or override reason; neither
creates physical inventory, reserves material, credits remnants or approves a quote.

The source documents establish configurable CAM behavior, not these numeric
defaults:

- [Lantek Expert Cut](https://www.lantek.com/ca/cad-cam-nesting-software-oxycut-plasma-laser-waterjet)
  configures separation and leads using material/thickness tables and cutting
  quality. It does not publish a universal separation formula there.
- [AMADA AP100US Sheet Wizard](https://amada.com/amadasoftware/ap100us_help_file/Sheet_Wizard.htm)
  distinguishes geometry-to-geometry spacing and sheet borders from optional
  beam diameter and lead-in/out allowances. Borders can reserve clamp zones.
- [Friendess CypCutE manual, section 3.17](https://d.fscut.com/wordpress-fscut/2022/12/CypCutE-User-Manual-7.0-2.pdf)
  provides independent part-gap and plate-margin fields. Its example shows
  2 mm (0.078740 in) for both with thickness set to zero; that screenshot is
  not a thickness-dependent recommendation.
- [Ermaksan HAWK LASER](https://www.ermaksan.com.tr/en-US/products/laser-cutting-machines/hawk-laser-en)
  lists Lantek CAD/CAM options without a public thickness-based spacing table.
- [Hypertherm Plate Saver for XPR](https://xnet.hypertherm.com/Xnet/library/download/?file=HYP258264)
  describes a ProNest part-separation default of `0.75 × thickness` for its
  plasma context. That value is not used as fiber-laser guidance here.

## Import limits and units

| Input | Limit or behavior |
|-------|-------------------|
| DXF batch | Up to 100 files; each file smaller than 5,000,000 bytes |
| Estimate | Up to 300 designs and 300 total parts, including quantities, across all groups |
| Geometry | Up to 2,000 vertices per contour/reference path after curve conversion; 20,000 source vertices including reference paths and all options' exclusion regions per project; 20,000 geometry vertices and 300 closed contours per parsed DXF |
| DXF records | Up to 20,000 entity records per file, including legacy polyline vertex records |
| Stock options | Up to 12 per group |
| Contour import | ASCII DXF model-space `LINE`, `ARC`, `CIRCLE`, `LWPOLYLINE`, ordinary legacy 2D `POLYLINE`/`VERTEX`/`SEQEND`, and supported clamped planar `SPLINE`; polylines may include circular bulges |
| Open internal geometry | Paths contained by exactly one outer profile are retained as visible reference lines, with a warning to review their marking/cutting intent |
| Omitted metadata | `VIEWPORT` records and all entities on the `FORMAT` annotation layer, with an import warning for omitted FORMAT entities |
| Rejected files | Open or ambiguous outer profiles, touching/intersecting contours, reference paths outside or spanning parts, malformed data, unsupported entities, blocks/`INSERT`, wide polylines, sloped/nonplanar geometry, tilted extrusion, or paper-space cut geometry; other text/annotations are not silently discarded |
| DXF units | Inch and millimeter files retain their physical size; unitless files default to inches unless millimeters is selected before import |
| Saved estimates | Current geometry profiles require project15/quote14 or standalone job16. A recorded-piece selection uses project18; the ordinary writer remains project15. Project18 may omit the selection, but explicit null and selection fields in older formats are refused. Standalone job files cannot represent a recorded-piece domain. Project15 may retain older quote3/7/9/11 groups, but every populated group must be explicitly upgraded before any new calculation. Legacy exclusions use project12/quote11; legacy policy snapshots use project10/quote9; orientation uses project6/quote7/job8. Without those constraints, ERP-bound projects use5 and family-only projects4. Jobs with exclusions use13. Older inputs are readable without automatic profile insertion. Legacy `drawing-bounds` parts require DXF re-import |

Legacy `rotate: false` means fixed and `rotate: true` means quarter turns;
when `rotationMode` exists, it is authoritative. Axis metadata remains X/Y
through inch/millimeter conversion. Versions 6/7/8/9/10/11/12/13/14/15/16 deliberately differ between
project/quote/job files so older readers reject the new constraints instead of
silently relaxing them. Do not edit a file's version to force an older release
to open it; retain the file and use a compatible release.

Split larger jobs into estimates. Malformed or unsupported files and files
that exceed a resource or size limit are skipped as a whole with an explanation;
the importer does not add their readable contours as a partial file.

### How drawing geometry becomes parts

Closed outer contours become separate designs at quantity 1; interior contours
remain holes. Uniquely paired endpoints within **0.0001 inch** join into closed
paths. Small join gaps retain both endpoints instead of snapping the drawing
smaller. Circular arcs and polyline bulges are approximated by straight segments
at **0.0001-inch curve tolerance**, with exact X/Y extrema included to preserve
their sheet-fit bounds. Supported spline spans are adaptively converted within
the same tolerance. Curved imports carry an approximation allowance, reserved
in addition to the selected gap and sheet margin. Circles remain circles. These
physical tolerances apply equally to inch and millimeter files; highly detailed
curves can reach the vertex limit during conversion.

Flat geometry may be translated away from Z=0. Both +Z and -Z object-coordinate
systems are supported: -Z circular/polyline geometry is transformed into world
coordinates, while `LINE` endpoints already use world coordinates. Geometry
on one common XY-parallel plane can form contours. Several parallel elevations
are aligned in Z without changing X/Y profiles, with an import warning; a sloped
entity or an entity whose points do not share a flat plane is rejected.

If paths cannot form unambiguous closed outer contours, or contours overlap or
intersect, the importer rejects that file. It never substitutes a rectangular
drawing footprint. Duplicate lines/closed contours may be removed with
an import warning. Open internal paths are retained as thin contrasting lines;
they do not define holes or part area, and the importer does not claim they are
etching. Review their intent before production.

The importer accepts validated planar `SPLINE` data with degree 1–10, control points, valid knots,
clamped end knots, and positive weights (or omitted weights, treated as 1).
Rational spans are converted to their actual curved profiles within the stated
tolerance; their control-hull rectangles are not used as parts. Unsupported,
discontinuous, malformed, or excessively detailed spline data is rejected.

Family-only groups use exact material labels; ERP-bound groups also include
company and catalog record IDs, so distinct catalog materials cannot combine
solely because their family names match. Thickness is rounded to 0.000001 mm
for the grouping key; displayed dimensions and saved files remain
in inches. Duplicate group/part IDs, duplicate stock specifications, incomplete
upload assignments, and project-wide resource overruns are rejected without
partially applying the assignment.

### Source provenance and draft review records

DXF imports retain the filename, a SHA-256 source fingerprint, declared or
assigned units, the normalized geometry fingerprint, and import warnings.
Fingerprints use original file bytes when available, or explicitly identify
the UTF-8 text basis. Unitless drawings retain the estimator's inch/millimeter
assignment for review. Add **Part revision (if known)** to each part; an empty
revision remains a review flag rather than being invented.

For ordinary full-sheet projects, **Export review record** requires fresh comparisons
for every populated group. Conditional recorded-piece projects can export a current,
validated partial stage prefix, explicitly labelled with its evaluated stage count;
see [recorded-piece exports](RECORDED_PIECE_PLANNING.md).
It records the input project and hash, estimator/company IDs, source/material
snapshots, quantities and revisions, effective rotation policies, source and
sheet grain axes, permitted rotations, solver/build/settings, compared stock
alternatives, validated placements, utilization and entered material costs.
Changed inputs or inconsistent results require recomparison. The solver is
deterministic, so the record stores no random seed and explains what replay
requires. The manifest identifies orientation policy `werco-orientation-v1`
and solver `werco-contour-v7`; contour search uses up to two deterministic
orders for every part shape. The rectangle fast path is disabled. Geometry fingerprints
remain about shape; the input-project fingerprint also covers orientation
requirements, the explicit geometry profile and exact stock-exclusion inputs.
Incomplete heuristic results are not
proof that a layout is impossible.

This locally downloaded record is labeled **QUOTE LAYOUT — NOT AN NC PROGRAM**
and remains `draft_estimator_review`. Its content hash can detect changes but
does not make it signed, immutable, or server-approved. Source CAD bytes are
not embedded or uploaded. There is no remnant credit, inventory reservation,
quote cost writeback, machine configuration approval, or production program.

## Storage and ERP integration

Material Nesting starts with a fresh, empty estimate each time you enter the
section, without demo parts or an automatically restored draft. Edits remain
while you work within the page, including switching material groups and its
workspace, stock, and help tabs. Leaving for another ERP page clears the current estimate; returning
starts empty. Changing the user or active company also starts a fresh estimate.
No workspace is automatically restored from localStorage, sessionStorage, or the server.

**Save before leaving the section**, refreshing, closing the tab, signing out,
or switching companies. While the workspace has unsaved edits, it requests the
browser's refresh/close warning; ordinary ERP navigation does not prompt.
Use **Open** to restore an estimate from a saved file, or **Team drafts** to
select a saved ERP revision. Both clear applied ERP catalog prices and prior
comparisons. Refresh the catalog, review pricing, and compare sheets again.

### Team drafts and revision history

**Team drafts** only loads its list when selected. **Save team draft** creates
a company-scoped DRAFT; **Save next revision** appends to the opened draft.
Earlier revisions remain readable through **History**. Saving a historical
revision while a newer one exists returns a conflict: open the latest revision
or explicitly use **Save as new draft**. No collaborator's work is overwritten.
**New** and opening a local file detach the current team-draft selection.
Opening a team revision asks before replacing unsaved inputs.

Each revision retains imperial estimate inputs, source metadata/hashes,
material/thickness groups, quantities, stock settings, orientation restrictions,
estimator identity, UTC timestamp (displayed in Central time), a server content
hash, and source-review notes. The server hash binds its canonical JSON snapshot;
it does not authenticate original DXF bytes or approve geometry. Original CAD
bytes, placements, solver output, and leftover regions are not stored in this
input-draft revision. **Original DXFs** stores separately verified source attachments;
**Saved calculations** retains server calculation evidence. Neither action rewrites
that revision or automatically restores the current workspace.

The server accepts project formats 4/5/6/10/12/15/18, up to 5 MiB per JSON file,
with the same 300-part/20,000-vertex/12-stock-option limits and bounded metadata.
It validates structure, unique identities, and active-company catalog references.
Stale or inactive catalog snapshots can be retained with review notes so work
can be saved, but all source values remain client assertions. Missing or foreign
catalog IDs are refused. Significant geometry is not repaired or approved by Save;
Open also runs the browser's geometry validation before replacing the workspace.
Inputs containing recorded-piece evidence also require effective `inventory:view`
for reads and writes. New saves and calculation requests recheck the current source;
historical reports retain their original source evidence without asserting current
availability. Selected projects use staged server protocol2; ordinary projects keep
protocol1 with the same v7 runtime. See [recorded-piece planning](RECORDED_PIECE_PLANNING.md).

Listing/opening requires effective `purchasing:view`; saving also requires
`purchasing:create`, including tenant role overrides. Draft writes preserve the
existing read-only company, kiosk, and API-token restrictions. The request binds
the intended company, preventing a refreshed session from saving into another
company. A revision and its required `AuditService` event commit together; audit
failure rolls back the save. The event records identifiers, hashes, and review
codes rather than geometry. Hash-chain runtime settings keep their existing meaning.
Identical same-actor request-key retries return the original revision. If a save's
network outcome is uncertain, **Retry previous save** checks that exact snapshot;
it does not save newly edited inputs until the prior outcome is resolved.

PostgreSQL UPDATE, DELETE, and TRUNCATE guards protect revision history, with
application mapper guards and SQLite UPDATE/DELETE guards for local tests.
There is no revision-edit, delete, approval, archive, or inventory command.
Drafts are currently shared with permitted users in their company; this is not a
customer/job-level access-control or certification-segregation implementation.

The estimator does not create ERP quotes, purchase orders, work orders,
production laser nest packages, or inventory movements. Only explicit team
draft saves add draft/revision records and their required audit events. Explicit
source attachments add their own immutable intent, attempt, receipt and part-binding
history with required audit; they do not create inventory movements. Its
local files and sheet previews are estimating outputs; it has no machine
connection, cutting recipes, or postprocessor.

The workspace is a native ERP route with feature-local styles isolated in a
ShadowRoot and notifications supplied by the existing ERP toast provider.
It uses the existing Vercel frontend deployment and SPA rewrite, plus two
read-only ERP catalog/resolution endpoints plus authenticated draft endpoints.
Team drafts require migration `101_quote_nesting_drafts` on the existing backend.
No environment variable, separate service, or hosting project is added; see
[DEPLOYMENT.md](DEPLOYMENT.md#existing-vercel-frontend-material-nesting).
