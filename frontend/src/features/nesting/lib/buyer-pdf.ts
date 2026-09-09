import {
  BUYER_PDF_MAX_BYTES,
  BUYER_PDF_MAX_SHEETS,
  BUYER_PDF_MAX_VERTICES,
  type BuyerPdfGroupChoices,
  type BuyerPdfInputs,
  type BuyerPdfReport,
  type BuyerPdfSheet,
} from './buyer-pdf-types';
import { transformLoops, rect, type Loop, type Nest, type Part, type Stock } from './nesting';
import { canonicalJSON, sha256 } from './provenance';
import { projectFromFile, projectToFile, requireCurrentProjectGeometry, validateProject } from './quote-project';
import { comparisonFromResults, stockFor } from './quoting';
import { buildRemnantReview } from './remnant-review';
import type { InstanceMap, RemnantStageMessage } from './remnant-planning';
import { validatePlanningStage } from './saved-remnant-layout';
import { mmToIn } from './units';

const check = (ok: unknown, message: string): void => {
  if (!ok) throw new Error(`Buyer PDF: ${message}`);
};
const dimensions = (width: number, height: number) =>
  `${Number(mmToIn(height).toFixed(4))} × ${Number(mmToIn(width).toFixed(4))} in`;
const isComplete = (result: { complete: boolean; error: string | null; nest: Nest | null } | null | undefined) =>
  !!result?.complete && result.error === null && !!result.nest && result.nest.unplaced.length === 0;
const choiceId = (stage: RemnantStageMessage) => `stage:${stage.stage_id}`;

/** Cheap presentation filter only. The export boundary revalidates all selected geometry. */
export function getBuyerPdfChoices(input: BuyerPdfInputs): BuyerPdfGroupChoices[] {
  return input.project.groups
    .filter(group => group.quote.parts.length > 0)
    .map(group => {
      const quote = group.quote;
      const label = `${quote.materialBinding?.catalog.name ?? quote.material} · ${Number(mmToIn(quote.thickness).toFixed(4))} in`;
      if (!input.project.remnantPlan) {
        const snapshot = input.comparisons[group.id];
        const current = snapshot?.signature === JSON.stringify(quote);
        return {
          groupId: group.id,
          label,
          choices: current
            ? snapshot.comparison.results
                .filter(
                  result =>
                    isComplete(result) && quote.options.some(option => option.enabled && option.id === result.option.id)
                )
                .map(result => ({
                  id: `full:${result.option.id}`,
                  label: `${result.nest!.sheets} sheet${result.nest!.sheets === 1 ? '' : 's'} · ${dimensions(result.option.width, result.option.height)}`,
                  recommended: result.option.id === snapshot.comparison.recommendedId,
                  conditional: false,
                }))
            : [],
        };
      }
      const baseline = input.stages.filter(
        stage => stage.group_id === group.id && stage.stage_kind === 'baseline' && isComplete(stage.result)
      );
      const piece = input.stages.find(stage => stage.group_id === group.id && stage.stage_kind === 'recorded_piece');
      const residual =
        baseline.length && piece?.result?.nest?.placements.length
          ? input.stages.filter(
              stage =>
                stage.group_id === group.id &&
                stage.stage_kind === 'residual' &&
                (stage.requested === 0 ? stage.result === null : isComplete(stage.result))
            )
          : [];
      return {
        groupId: group.id,
        label,
        choices: [
          ...baseline.map(stage => ({
            id: choiceId(stage),
            label: `${stage.result!.nest!.sheets} sheet${stage.result!.nest!.sheets === 1 ? '' : 's'} · ${dimensions(stage.stock!.width, stage.stock!.height)}`,
            recommended: false,
            conditional: false,
          })),
          ...residual.map(stage => ({
            id: choiceId(stage),
            label: `Existing recorded piece + ${stage.result?.nest?.sheets ?? 0} new sheet${stage.result?.nest?.sheets === 1 ? '' : 's'}${stage.stock ? ` · ${dimensions(stage.stock.width, stage.stock.height)}` : ''} (conditional)`,
            recommended: false,
            conditional: true,
          })),
        ],
      };
    });
}

const loopInches = (loop: Loop): Loop =>
  loop.type === 'circle'
    ? { type: 'circle', cx: mmToIn(loop.cx), cy: mmToIn(loop.cy), r: mmToIn(loop.r) }
    : { type: 'poly', points: loop.points.map(p => ({ x: mmToIn(p.x), y: mmToIn(p.y) })) };

function sheetsFor(
  parts: Part[],
  stock: Stock,
  nest: Nest,
  source: BuyerPdfSheet['source'],
  sourceLabel: string,
  originalMap?: InstanceMap
): BuyerPdfSheet[] {
  const originals = new Map(originalMap?.map(item => [item.part_id, item.originals]));
  return Array.from({ length: nest.sheets }, (_, sheet) => ({
    number: sheet + 1,
    source,
    sourceLabel,
    widthIn: mmToIn(stock.height),
    lengthIn: mmToIn(stock.width),
    marginIn: mmToIn(stock.margin),
    gapIn: mmToIn(stock.gap),
    outer: loopInches(stock.domain?.outer ?? rect(stock.width, stock.height)),
    holes: (stock.domain?.holes ?? []).map(loopInches),
    exclusions: (stock.exclusions ?? []).map(region => ({
      outline: loopInches(region.outline),
      clearanceIn: mmToIn(region.clearance),
    })),
    placements: nest.placements
      .filter(placement => placement.sheet === sheet)
      .map(placement => {
        const part = parts.find(item => item.id === placement.partId)!;
        const originalInstance = originalMap ? originals.get(part.id)?.[placement.instance] : placement.instance;
        check(originalInstance !== undefined, 'the original part-instance map is incomplete.');
        return {
          partId: part.id,
          originalInstance: originalInstance!,
          loops: transformLoops(part, placement).map(loopInches),
        };
      }),
  }));
}

/** Own a frozen snapshot before hashing/validation; never export a partial or stale order. */
export async function buildBuyerPdfReport(
  inputs: BuyerPdfInputs,
  selections: Record<string, string>,
  metadata: { projectName: string; notes: string }
): Promise<BuyerPdfReport> {
  const input = JSON.parse(JSON.stringify(inputs)) as BuyerPdfInputs;
  const selected = { ...selections },
    fields = { ...metadata };
  const { project, companyId } = input;
  validateProject(project);
  requireCurrentProjectGeometry(project);
  check(Number.isSafeInteger(companyId) && companyId > 0, 'an active company is required.');
  check(
    fields.projectName.trim().length > 0 && fields.projectName.length <= 200,
    'enter a job/project reference of at most 200 characters.'
  );
  check(fields.notes.length <= 2000, 'limit buyer notes to 2,000 characters.');
  check(
    !project.groups.some(group => group.quote.parts.some(part => part.importMode === 'drawing-bounds')),
    'import actual part contours before exporting.'
  );
  check(
    project.groups.every(group => !group.quote.materialBinding || group.quote.materialBinding.companyId === companyId),
    'material belongs to another company.'
  );
  const populated = project.groups.filter(group => group.quote.parts.length > 0);
  check(
    populated.every(
      group =>
        group.id.trim() &&
        group.id.length <= 200 &&
        group.quote.parts.every(part => part.id.trim() && part.id.length <= 200)
    ),
    'a group or part ID is blank or too long. Reimport the affected parts before exporting.'
  );
  check(
    populated.every(group => group.quote.name.trim() && group.quote.parts.every(part => part.name.trim())),
    'give each material group and part a visible name before exporting.'
  );
  check(
    populated.length > 0 &&
      Object.keys(selected).length === populated.length &&
      populated.every(group => Object.hasOwn(selected, group.id)),
    'choose exactly one complete plan for every material group.'
  );
  const currentFile = projectToFile(project);
  let inputSha256: string;
  if (project.remnantPlan) {
    check(input.remnantInput, 'the recorded-piece comparison source is missing.');
    check(
      canonicalJSON(projectToFile(projectFromFile(input.remnantInput))) === canonicalJSON(currentFile),
      'inputs changed after the recorded-piece comparison. Compare again.'
    );
    const review = await buildRemnantReview(input.remnantInput, input.stages, { companyId, estimatorId: null });
    inputSha256 = review.content.inputSha256;
  } else inputSha256 = await sha256(canonicalJSON(currentFile));
  const choices = getBuyerPdfChoices(input);
  const groups = populated.map(group => {
    check(
      choices.find(item => item.groupId === group.id)?.choices.some(choice => choice.id === selected[group.id]),
      'a selected plan is missing, incomplete, or stale. Compare again.'
    );
    const { quote } = group;
    let sheets: BuyerPdfSheet[],
      conditional = false;
    const baselinePurchaseSheets: BuyerPdfReport['groups'][number]['baselinePurchaseSheets'] = [];
    if (!project.remnantPlan) {
      const result = input.comparisons[group.id].comparison.results.find(
        item => `full:${item.option.id}` === selected[group.id]
      )!;
      comparisonFromResults(quote, [result]);
      check(isComplete(result), 'the selected full-sheet plan does not contain every required part.');
      sheets = sheetsFor(
        quote.parts,
        stockFor(quote, result.option),
        result.nest!,
        'purchase',
        dimensions(result.option.width, result.option.height)
      );
    } else {
      const stage = input.stages.find(item => item.group_id === group.id && choiceId(item) === selected[group.id])!;
      const predecessor = input.stages.find(item => item.stage_kind === 'recorded_piece');
      const checked = validatePlanningStage(input.remnantInput, stage, predecessor);
      conditional = stage.stage_kind === 'residual';
      sheets = [];
      if (conditional) {
        check(
          predecessor?.group_id === group.id && predecessor.stock && predecessor.result.nest,
          'the exact recorded-piece predecessor is missing.'
        );
        const source = project.remnantPlan.snapshot;
        sheets.push(
          ...sheetsFor(
            quote.parts,
            predecessor!.stock!,
            predecessor!.result.nest!,
            'recorded_piece',
            `${source.label} · Piece ${source.pieceId} · Observation ${source.observationNumber}`
          )
        );
        const fallback =
          input.stages.find(
            item =>
              item.group_id === group.id &&
              item.stage_kind === 'baseline' &&
              item.option_id === stage.option_id &&
              isComplete(item.result)
          ) ??
          input.stages.find(
            item => item.group_id === group.id && item.stage_kind === 'baseline' && isComplete(item.result)
          );
        check(
          fallback?.stock && fallback.result?.nest,
          'a complete full-sheet fallback is required for this conditional plan.'
        );
        baselinePurchaseSheets.push({
          widthIn: mmToIn(fallback!.stock!.height),
          lengthIn: mmToIn(fallback!.stock!.width),
          quantity: fallback!.result!.nest!.sheets,
        });
      }
      if (stage.requested) {
        check(stage.stock && isComplete(stage.result), 'the selected plan leaves required parts unplaced.');
        sheets.push(
          ...sheetsFor(
            checked.quote.parts,
            stage.stock!,
            stage.result!.nest!,
            'purchase',
            dimensions(stage.stock!.width, stage.stock!.height),
            checked.instanceMap
          )
        );
      }
    }
    sheets.forEach((sheet, index) => {
      sheet.number = index + 1;
    });
    for (const part of quote.parts) {
      const instances = sheets.flatMap(sheet =>
        sheet.placements.filter(item => item.partId === part.id).map(item => item.originalInstance)
      );
      check(
        instances.length === part.quantity &&
          new Set(instances).size === part.quantity &&
          instances.every(index => Number.isSafeInteger(index) && index >= 0 && index < part.quantity),
        'part quantities do not reconcile across the selected sheets.'
      );
    }
    return {
      id: group.id,
      name: quote.name,
      material: quote.material,
      materialDescription:
        quote.materialBinding?.catalog.name ?? 'Not specified — confirm grade/specification before ordering',
      thicknessIn: mmToIn(quote.thickness),
      selectionKind: conditional ? ('recorded_piece' as const) : ('full_sheet' as const),
      partRequirements: quote.parts.map((part, index) => ({
        id: part.id,
        label: `P${index + 1}`,
        name: part.name,
        revision: part.revision ?? '',
        quantity: part.quantity,
      })),
      sheets,
      baselinePurchaseSheets,
    };
  });
  const report: BuyerPdfReport = {
    version: 1,
    units: 'in',
    expectedCompanyId: companyId,
    projectName: fields.projectName.trim(),
    notes: fields.notes,
    inputSha256,
    solverVersion: 'werco-contour-v7',
    groups,
  };
  const sheets = groups.flatMap(group => group.sheets);
  check(
    sheets.length <= BUYER_PDF_MAX_SHEETS,
    'this report exceeds the 300-sheet PDF limit. Split it into smaller jobs.'
  );
  const loops = sheets.flatMap(sheet => [
    sheet.outer,
    ...sheet.holes,
    ...sheet.exclusions.map(region => region.outline),
    ...sheet.placements.flatMap(part => part.loops),
  ]);
  check(
    loops.reduce((count, loop) => count + (loop.type === 'circle' ? 1 : loop.points.length), 0) <=
      BUYER_PDF_MAX_VERTICES,
    'this report exceeds the 200,000-vertex PDF limit. Split it into smaller jobs.'
  );
  check(
    new TextEncoder().encode(JSON.stringify(report)).length <= BUYER_PDF_MAX_BYTES,
    'this report exceeds the 8 MiB PDF input limit. Split it into smaller jobs.'
  );
  return report;
}
