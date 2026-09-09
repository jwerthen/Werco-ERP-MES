# Actual-shape recorded-piece leftover ledger

This describes the actual-domain area ledger used by conditional material planning,
including saved projects and staged server calculations. See
[the recorded-piece workflow](RECORDED_PIECE_PLANNING.md) for selection and review.
A reported piece remains availability and eligibility unverified. Analysis and
export create no inventory, reservation, consumption, physical child piece or
monetary credit.

`analyzeLeftovers(parts, stock, nest)` emits `werco-leftovers-v4` only when the
validated stock contains an actual `domain`. The domain preserves the reported
outer boundary and physical holes. Its rectangular extents are search/display
bounds, never a substitute material shape. A recorded piece has capacity one;
entirely unplaced work produces no used piece and no leftover regions.

Full-sheet reports retain their existing v1, v2 and v3 versions, input signatures,
area definitions and export behavior. A new actual-domain report cannot be
relabeled as one of these historical versions.

## Area definitions

Areas are square millimeters internally and square inches in the review export.
The v4 report carries these definitions in immutable assumptions:

| Field | Meaning |
| --- | --- |
| `grossArea` | Analytical outer area minus physical holes. A circular piece retains its analytical circular area. |
| `protectedArea` | Actual vector material after inward physical-edge margin and conservative numerical/curve protection, before unavailable zones. |
| `usableArea` | Protected material after the union of guarded reported unavailable zones. |
| `edgeMarginArea` | Gross minus protected area, including the numerical/curve protection at physical boundaries. |
| `excludedArea` | Protected minus usable area. Overlapping unavailable zones count once. |
| `nominalPartArea` | Sum of placed nominal part areas after their internal cutouts. |
| `reservedCutoutArea` | Placed part internal cutouts; these remain reserved because part-in-part placement is disabled. |
| `clearanceAndProtectionArea` | Usable minus remaining, nominal parts and reserved cutouts. This accounts for compensated part envelopes and numerical protection; it is not physical kerf. |
| `remainingArea` | Sum of actual connected leftover polygons, including their holes. |

The two intermediate identities are `gross = edge + protected` and
`protected = excluded + usable`. The complete reconciliation is
`gross = edge + excluded + nominal parts + reserved cutouts + clearance/protection + remaining`.
A significant negative allowance or reconciliation residual is an explicit error.
Unavailable zones are already removed from usable material and are not subtracted
again during part-envelope removal.

The report preserves every connected polygon and its holes within the engineering
budgets. Deterministic region IDs describe this particular layout only. Every
region remains `review`, every credit is zero, and region bounds do not establish
a usable rectangle or physical reuse approval.

## Profile and export validation

`LEFTOVER_DOMAIN_PROFILE` binds the existing compensated profile identity, the
separate remnant-domain profile identity, and the full remnant-domain profile
payload. The numerical source remains the checked
[remnant domain profile](../backend/app/data/nesting_profiles/werco-remnant-domain-v1.json).
No profile number represents an approved shop eligibility rule.

The existing compensated leftover budgets remain in force: 60,000 derived input
vertices, 120,000 aggregate output vertices, 30,000 displayed vertices per piece,
and 2,000 connected regions. The domain profile separately limits source and
usable-domain complexity. Exceeding a budget refuses analysis; it does not create
a rectangular fallback or an eligibility claim.

`leftoversToFile` requires the original parts, stock and nest for v4. It binds the
exact source domain, rules and placements, regenerates the remainder with the
same bounded kernel, and requires canonical equality with the cached report.
It also validates the full region polygons against actual usable material,
including holes, with the exact integer containment checks. Matching area or
bounding extents alone is insufficient. Rewritten geometry that covers a placed
part is rejected even if its area balances.

Regeneration adds calculation work during v4 export; it is intentional and bounded
by the same geometry budgets. There is no unchecked public export path. This
verification establishes reproducible engineering evidence, not current physical
availability, inventory allocation, manufacturing approval or certification.

Focused checks are in
[leftovers.domain.test.ts](../frontend/src/features/nesting/lib/leftovers.domain.test.ts),
with historical full-sheet coverage in the existing leftover geometry/export and
stock-exclusion suites.
