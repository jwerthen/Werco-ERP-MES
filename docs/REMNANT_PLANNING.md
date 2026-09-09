# Recorded-piece nesting foundations

This document describes validated source selection and actual-domain geometry.
The workspace now integrates these foundations with conditional piece-versus-full-sheet
comparisons, saved projects and staged server calculations. See
[the recorded-piece workflow](RECORDED_PIECE_PLANNING.md). Existing ordinary
full-sheet geometry rules and historical profiles are preserved.

The read-only planning snapshot endpoint and permissions are documented in
[API.md](API.md) and [RBAC_PERMISSIONS.md](RBAC_PERMISSIONS.md). A recorded
observation does not establish availability, material eligibility, certification,
reservation or consumption. Selection requires an explicit material-family
assignment, exact known grade/thickness and current source evidence. No price,
quote allocation, material credit or machine control is introduced.

The original inch strings are parsed as exact nanoinches before translation to
the millimeter kernel. Exact integer/rational source checks reject self-crossings,
invalid physical-hole topology and reported unavailable zones outside material,
including one-nanoinch violations. Boundary contact by an unavailable zone is
allowed. No source repair, unit inference or polygonization establishes that claim.
Separate checks reject contours that collapse or become invalid on the kernel grid.

Derived `Stock.domain` is an internal representation, reconstructed from pinned
source evidence. It must not be accepted as an independent saved source. Its actual
outer profile and physical holes define material area; its rectangle bounds only
limit search. All coordinates share an exact translated origin, source grain is
preserved and capacity is one physical piece per scenario. Inward erosion can
produce multiple usable regions or no usable material.

Final placement requires vector difference plus exact integer boundary predicates
for the complete compensated envelope, and independent unrounded boundary-distance
checks. The original-boundary minimum is edge margin + half part gap + imported
curve tolerance + twice the base numerical protection. The additional numerical
protection is an engineering configuration, separate from the shop's physical
margin/web. No universal fiber-laser spacing standard is asserted.

Candidate search uses convex support half planes or actual boundary-segment sweeps
against reflected compensated convex pieces. Large generated candidate rings may
use conservative convex hulls; final geometry never uses that approximation to
approve a fit. Candidate, Boolean, source and predicate work have explicit profile
bounds. A bounded pose cache retains at most50,000 containment decisions; once full,
new poses are still validated and simply not cached. Cache identity includes the
immutable prepared domain, part orientation and exact grid translation. Search
failure is not a proof of infeasibility.

The separate normative `werco-remnant-domain-v1` wrapper binds these rules and its
unchanged `werco-compensated-v1` dependency. Both backend and isolated frontend
build gates verify both profile hashes and their linkage. Read
[REMNANT_LEFTOVER_LEDGER.md](REMNANT_LEFTOVER_LEDGER.md) for actual-material v4
area accounting and regenerated export verification. All leftovers remain potential,
review-only and zero credit; no physical inventory records are created.

Synthetic correctness tests cover circles/polygons, diagonals, concave sources,
physical holes, unavailable strips, split/empty erosion, grain, quantities,
determinism, sub-grid violations and budget refusal. A1,000-vertex/30-instance
benchmark produced identical complete Nest JSON before and after bounded caching
and edge-distance culling; this does not guarantee that every300-instance job will
finish within the shared120-second worker limit. The staged integration retains
completed full-sheet baselines if later recorded-piece work fails or times out.
