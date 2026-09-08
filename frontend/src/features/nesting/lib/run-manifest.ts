import { leftoversToFile } from './leftovers';
import { allowedRotations, effectiveRotationMode, orientationExplanation } from './orientation';
import { partArea, validateNest } from './nesting';
import { projectToFile, validateProject, type QuoteProject } from './quote-project';
import { stockFor, type Comparison } from './quoting';
import { canonicalJSON, geometryHash, sha256 } from './provenance';
import { mmToIn } from './units';

export type ComparisonSnapshot = { comparison: Comparison; signature: string };
export const SOLVER_VERSION = 'werco-contour-v4';

/** Downloadable draft evidence. This is never an approval, inventory claim or server audit record. */
export async function buildRunManifest(
  project: QuoteProject,
  snapshots: Record<string, ComparisonSnapshot>,
  identity: { companyId: number | null; estimatorId: number | null }
) {
  validateProject(project);
  const groups = project.groups.filter(group => group.quote.parts.length > 0);
  if (!groups.length) throw new Error('Add parts and compare sheets before exporting a review record.');
  const inputProject = projectToFile(project);
  const inputSha256 = await sha256(canonicalJSON(JSON.parse(JSON.stringify(inputProject))));
  const results = await Promise.all(
    groups.map(async group => {
      const snapshot = snapshots[group.id];
      if (!snapshot || snapshot.signature !== JSON.stringify(group.quote))
        throw new Error('Compare every material group again before exporting. An input has changed.');
      const { quote } = group;
      const expectedOptions = quote.options.filter(option => option.enabled);
      const comparison = snapshot.comparison;
      if (
        comparison.results.length !== expectedOptions.length ||
        new Set(comparison.results.map(result => result.option.id)).size !== expectedOptions.length
      )
        throw new Error('The comparison does not contain every enabled stock option. Recalculate it.');
      const parts = await Promise.all(
        quote.parts.map(async part => {
          const currentGeometrySha256 = await geometryHash(part);
          const flags: string[] = [];
          const orientationIssue = orientationExplanation(part, quote);
          if (orientationIssue) flags.push(orientationIssue);
          if (!part.revision?.trim()) flags.push('Part revision has not been assigned.');
          if (!part.provenance) flags.push('No imported source-file fingerprint is available.');
          else {
            if (currentGeometrySha256 !== part.provenance.geometrySha256)
              flags.push('Geometry differs from its imported fingerprint. Review the changed part.');
            if (part.provenance.unitDecision === 'assigned')
              flags.push('Source drawing was unitless; review the recorded unit assignment.');
            flags.push(...part.provenance.warnings);
          }
          return {
            partId: part.id,
            partNumber: part.name,
            revision: part.revision?.trim() || null,
            quantity: part.quantity,
            geometrySha256: currentGeometrySha256,
            source: part.provenance ?? null,
            rotationMode: effectiveRotationMode(part),
            rotationModeSource: part.rotationMode === undefined ? 'legacy_rotate_flag' : 'explicit_selection',
            sourceGrainAxis: part.grainAxis ?? null,
            sourceGrainDirection:
              part.grainAxis === 'x'
                ? 'Horizontal in source drawing'
                : part.grainAxis === 'y'
                  ? 'Vertical in source drawing'
                  : 'No grain requirement assigned',
            allowedRotations: allowedRotations(part, quote),
            mirrorAllowed: false,
            reviewFlags: Array.from(new Set(flags)),
          };
        })
      );
      const alternatives = comparison.results.map(result => {
        const option = expectedOptions.find(option => option.id === result.option.id);
        if (!option || canonicalJSON(option) !== canonicalJSON(result.option))
          throw new Error('Stock dimensions or prices differ from the comparison. Recalculate it.');
        const nest = result.nest;
        if (nest) validateNest(quote.parts, stockFor(quote, option), nest);
        if (result.leftovers && (!nest || result.leftoverError))
          throw new Error('Leftover report conflicts with its comparison status. Recalculate it.');
        const requested = quote.parts.reduce((count, part) => count + part.quantity, 0);
        const complete = !!nest && nest.placements.length === requested && nest.unplaced.length === 0;
        if (result.complete !== complete)
          throw new Error('The comparison completion status is inconsistent. Recalculate it.');
        const nominalAreaIn2 = nest
          ? nest.placements.reduce(
              (area, placement) => area + partArea(quote.parts.find(part => part.id === placement.partId)!),
              0
            ) /
            (25.4 * 25.4)
          : null;
        const grossAreaIn2 = nest ? mmToIn(option.width) * mmToIn(option.height) * nest.sheets : null;
        return {
          stockOptionId: option.id,
          status: complete ? 'complete_valid_layout' : nest ? 'partial_valid_layout' : 'calculation_failed',
          explanation:
            result.error ??
            (complete
              ? 'Every required instance was placed and geometrically validated.'
              : 'The heuristic search did not place every part; this is not proof of infeasibility.'),
          sheetCount: nest?.sheets ?? null,
          nominalPartAreaIn2: nominalAreaIn2,
          grossSheetAreaIn2: grossAreaIn2,
          grossUtilizationPct: grossAreaIn2 && nominalAreaIn2 !== null ? (100 * nominalAreaIn2) / grossAreaIn2 : null,
          enteredPricePerSheetUSD: option.price,
          estimatedMaterialCostUSD:
            complete && option.price !== null ? Number((option.price * nest!.sheets).toFixed(2)) : null,
          remnantCreditUSD: 0,
          leftovers: result.leftovers
            ? leftoversToFile(result.leftovers, { parts: quote.parts, stock: stockFor(quote, option), nest: nest! })
            : null,
          leftoverError:
            result.leftoverError ??
            (nest && !result.leftovers ? 'No leftover analysis is available for this comparison.' : null),
          unplaced: nest?.unplaced ?? [],
          placements:
            nest?.placements.map(placement => ({
              partId: placement.partId,
              instance: placement.instance,
              sheet: placement.sheet,
              xIn: mmToIn(placement.x),
              yIn: mmToIn(placement.y),
              widthIn: mmToIn(placement.width),
              heightIn: mmToIn(placement.height),
              rotationDegrees: placement.rotation,
            })) ?? [],
        };
      });
      if (
        comparison.recommendedId !== null &&
        !alternatives.some(
          option => option.stockOptionId === comparison.recommendedId && option.status === 'complete_valid_layout'
        )
      )
        throw new Error('The recommended alternative is not a complete valid layout. Recalculate it.');
      return {
        groupId: group.id,
        sheetGrainAxis: quote.grainAxis ?? null,
        sheetGrainDirection:
          quote.grainAxis === 'x' ? 'Along sheet length' : quote.grainAxis === 'y' ? 'Along sheet width' : 'Unknown',
        parts,
        recommendedOptionId: comparison.recommendedId,
        recommendationReason: comparison.reason,
        alternatives,
      };
    })
  );
  const content = {
    schemaVersion: 1,
    label: 'QUOTE LAYOUT — NOT AN NC PROGRAM',
    status: 'draft_estimator_review',
    authoritativeApproval: false,
    identity,
    inputSha256,
    inputProject,
    solver: {
      version: SOLVER_VERSION,
      build: process.env.REACT_APP_RELEASE || 'development',
      algorithm: 'Deterministic two-order contour placement; three-order rectangular-profile fast path',
      seed: null,
      seedExplanation:
        'This solver does not use randomness. Replay requires the exact saved inputs, part IDs and solver build.',
      maximumSearchOrders: 3,
      contourSearchOrders: 2,
      rectangularSearchOrders: 3,
      orientationPolicy: 'werco-orientation-v1',
      workerDeadlineMs: 120000,
      searchMode: 'bounded_deterministic',
      policyStatus: 'draft_configuration',
      clipperVersion: '6.4.2',
      integerGridMm: 0.0001,
      curveToleranceIn: 0.0001,
      partInPartAllowed: false,
    },
    results,
    limitations: [
      'This client-generated review record is not a signed approval or immutable server audit event.',
      'Source fingerprints are recorded; original CAD files are not embedded or uploaded by this export.',
      'Price and material source reviews do not establish approved grade, certification, inventory availability or currency metadata.',
      'Leftover material receives no remnant credit or inventory reservation.',
      'No machine program or machine instructions are created.',
    ],
  };
  // Remove optional properties consistently before canonical serialization.
  const frozenContent = JSON.parse(JSON.stringify(content)) as typeof content;
  return {
    exportedAt: new Date().toISOString(),
    contentSha256: await sha256(canonicalJSON(frozenContent)),
    content: frozenContent,
  };
}
