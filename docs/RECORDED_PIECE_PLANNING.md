# Recorded-piece planning

The nesting workspace can explicitly compare one recorded material piece with its
full-sheet baseline. This is a conditional material plan. The piece remains
availability-unverified: selection does not reserve it, consume inventory, approve
its grade or measurement, or assign financial credit. A new workspace still opens
empty and never restores a piece automatically.

## Select and compare

1. Add actual part outlines, quantities and current clearance rules. Enable the
   full-sheet sizes needed for a baseline.
2. With effective `inventory:view`, choose **Choose recorded piece** for the active
   material group. Inspect the immutable measured observation and source warning.
   Selection requires a current, unchanged source and a recorded shape, known
   thickness and known reported grade. Declare the required grade exactly and the
   material family explicitly, with a reason. No alloy aliases or inferred grade
   compatibility are used.
3. Confirm the additional unavailable-zone clearance in inches. Its initial value
   comes from the selected group's edge margin. Only the decimal representation
   bridge rounds upward to the next nanoinch when needed; this is not a shop rule.
4. Compare sheets. All full-sheet baseline stages run first, followed by the one
   recorded piece and one remaining-full-sheet stage per enabled option of its
   assigned group. The combined schedule is limited to 36 stages. Each conditional
   alternative uses the piece at most once, then places its remaining original
   instances on full sheets. Other groups retain their full-sheet baselines.

Cards show material/thickness, actual imperial sheet sizes and counts. Counts are
alternative-local; do not add alternatives together. If all assigned instances fit
on the piece, the remaining-sheet stage explicitly says zero full sheets and has no
fabricated stock or layout. If the piece cannot be reconstructed or does not fit any
parts, the full-sheet baseline remains available. A failed or incomplete heuristic
search does not prove infeasibility.

The piece preview draws its actual measured outer boundary, physical holes and
unavailable regions. Bounding extents are labelled as extents. Amber connected
leftover regions are predictions requiring physical review, with zero credit.
They do not create new inventory observations or reusable stock automatically.
See [the domain ledger](REMNANT_LEFTOVER_LEDGER.md) for the actual material,
protected material and final usable-domain area equations.

## Changes, cancellation and files

Changes to the assigned group's complete serialized inputs invalidate its material
assignment. **Refresh and reaffirm assignment** makes an explicit source check and
requires renewed confirmation. File Open preserves source evidence but always
requires this step before another local conditional calculation. A baseline-only
comparison remains available while an assignment needs refresh. **Remove conditional
piece** returns to ordinary full-sheet planning.

One local worker shares a 120-second deadline and bounded output across all stages.
Cancel, timeout or output-limit failure retains earlier completed baseline options
and conditional stages. Late replies after cancellation, replacement or unmount
cannot publish a new result. A partial review export is labelled with its evaluated
stage count; it is never represented as a finished search.

Save writes project version18 only when a selection exists. Selection omission is
allowed when reading version18, but explicit null and selection fields in older
formats are refused. The ordinary writer continues to emit version15 without a
selection. Before explicit Save, Open or conditional calculation, the exact raw
imperial target-group fingerprint and typed observation snapshot digest are checked.
An mm-to-inch round trip is never substituted for the original saved fingerprint.
Server save/start also enforce current source and inventory-evidence permissions.

## Saved server results and review exports

A selected project uses server protocol2 and the v7 solver. Saved preview binds the
run's runtime and both geometry profiles, immutable input, checkpoint receipt and
input-derived stage identity. A remaining-sheet preview fetches the recorded-piece
predecessor and verifies the complete original-instance partition. Historical
ordinary v4/v5/v6 and ordinary v7 layouts retain their original reader paths.

**Export review record** rechecks a current retained stage prefix and exports actual
stock/domain geometry, placements, original-instance mappings and leftover regions
in inches. Domain-leftover export regenerates the bounded remainder to reject stale
or counterfeit polygons; complex exports can therefore take extra time. Local
exports are draft evidence, not server audits. **Download saved report** retains the
server report/hash and validates its staged relationships before download. Saved
reports retain historical source evidence; opening a historical report never proves
that the physical piece is available now.

The SVG retains its millimeter viewBox coordinates, declares its physical width
and height in inches, and embeds imperial dimensions, part count, margin and gap
in its review title. It is a review drawing, not an NC program, cutting path or manufacturing
approval. No pricing or quoting integration is introduced by this workflow.
