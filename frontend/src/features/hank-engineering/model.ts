/** Drawing values are proposals until a person reviews the source. No model tools run here. */
import type { Part, PartType } from '../../types';

export const UOMS = ['each', 'feet', 'inches', 'pounds', 'kilograms', 'sheets', 'gallons', 'liters'];
export const TYPES: PartType[] = ['manufactured', 'assembly', 'purchased', 'raw_material', 'hardware', 'consumable'];
export interface Evidence {
  page: number;
  note: string;
  confirmed: boolean;
}
export interface DrawingPart extends Evidence {
  pn: string;
  revision: string;
  name: string;
  type: PartType | '';
  uom: string;
  grade: string;
  description: string;
}
export interface Component extends Evidence {
  pn: string;
  quantity: string;
  uom: string;
}
export interface Operation extends Evidence {
  sequence: number;
  component: string;
  name: string;
  center: string;
  setup: string;
  run: string;
  instructions: string;
  inspection: boolean;
}
export interface Requirement extends Evidence {
  text: string;
}
export interface DrawingPlan {
  filename: string;
  sha256: string;
  pages: string[];
  number: string;
  revision: string;
  title: string;
  customer: string;
  owner: string;
  parts: DrawingPart[];
  components: Component[];
  requirements: Requirement[];
  operations: Operation[];
  flags: string[];
  reviewNotes: string;
  screened: boolean;
  reviewed: boolean;
  quantity: string;
  quantitySource: string;
  requestedWO: boolean;
}
export const restriction =
  /\bITAR\b|\bCUI\b|controlled\s+unclassified|export[ -]+controlled|do\s+not\s+transmit|distribution\s+statement\s+[B-F]\b/i;
export const key = (value: string) => value.trim().toUpperCase();
export const blankPart = (): DrawingPart => ({
  pn: '',
  revision: '',
  name: '',
  type: '',
  uom: '',
  grade: '',
  description: '',
  page: 1,
  note: '',
  confirmed: false,
});
export const blankComponent = (): Component => ({ pn: '', quantity: '', uom: '', page: 1, note: '', confirmed: false });
export const blankOperation = (sequence: number): Operation => ({
  sequence,
  component: '',
  name: '',
  center: '',
  setup: '',
  run: '',
  instructions: '',
  inspection: false,
  page: 1,
  note: '',
  confirmed: false,
});

export function extractDrawing(filename: string, sha256: string, pages: string[]): DrawingPlan {
  const plan: DrawingPlan = {
    filename,
    sha256,
    pages,
    number: '',
    revision: '',
    title: '',
    customer: '',
    owner: '',
    parts: [],
    components: [],
    requirements: [],
    operations: [],
    flags: [],
    reviewNotes: '',
    screened: false,
    reviewed: false,
    quantity: '',
    quantitySource: '',
    requestedWO: false,
  };
  // Only explicit labels and unambiguous column tables. Geometry is never interpreted.
  const labels: [keyof Pick<DrawingPlan, 'number' | 'revision' | 'title' | 'customer'>, RegExp][] = [
    ['number', /^(?:DRAWING(?:\s*(?:NO\.?|NUMBER|#))?|DWG\s*(?:NO\.?|NUMBER|#))(?:\s*:\s*|\t+|\s{2,})(.+)$/i],
    ['revision', /^(?:REV(?:ISION)?\.?)(?:\s*:\s*|\t+|\s{2,})(\S+)$/i],
    ['title', /^(?:TITLE|DESCRIPTION)(?:\s*:\s*|\t+|\s{2,})(.+)$/i],
    ['customer', /^CUSTOMER(?:\s*:\s*|\t+|\s{2,})(.+)$/i],
  ];
  pages.forEach((text, index) => {
    let table: string[] | null = null;
    let partNumber = '';
    let rev = '';
    let title = '';
    const lines = text
      .split(/\r?\n/)
      .map(line => line.trim())
      .filter(Boolean);
    for (const line of lines) {
      for (const [field, pattern] of labels) {
        const value = pattern.exec(line)?.[1]?.trim();
        if (!value) continue;
        if (!plan[field]) plan[field] = value;
        else if (plan[field] !== value)
          plan.flags.push(`Page ${index + 1}: ${field} differs (${value}). Resolve ownership/revision before entry.`);
        if (field === 'revision') rev = value;
        if (field === 'title') title = value;
      }
      const pn = /^(?:PART\s*(?:NUMBER|NO\.?|#)|P\/N)(?:\s*:\s*|\t+|\s{2,})(\S+)$/i.exec(line);
      if (pn) partNumber = pn[1];
      if (
        /\b(?:material|finish|weld|deburr|tolerance|inspect|heat treat|coat|paint|bend|form|torque|note|specification)\b/i.test(
          line
        )
      ) {
        plan.requirements.push({ text: line, page: index + 1, note: line, confirmed: false });
      }
      const columns = line.split(/\s*\|\s*|\t+|\s{2,}/).map(value => value.trim());
      const header = columns.map(value => value.toUpperCase().replace(/\./g, ''));
      const pnColumn = header.findIndex(value => /^(?:PART NUMBER|PART NO|P\/N)$/.test(value));
      const qtyColumn = header.findIndex(value => /^(?:QTY|QUANTITY)$/.test(value));
      const unitColumn = header.findIndex(value => /^(?:UOM|UNIT|UNITS)$/.test(value));
      const descriptionColumn = header.findIndex(value => /^(?:DESCRIPTION|PART NAME)$/.test(value));
      if ([pnColumn, qtyColumn, unitColumn, descriptionColumn].every(value => value >= 0)) {
        table = header;
        continue;
      }
      if (table) {
        const field = (pattern: RegExp) => columns[table!.findIndex(value => pattern.test(value))] || '';
        const component = field(/^(?:PART NUMBER|PART NO|P\/N)$/);
        const quantity = field(/^(?:QTY|QUANTITY)$/);
        const uom = field(/^(?:UOM|UNIT|UNITS)$/);
        const name = field(/^(?:DESCRIPTION|PART NAME)$/);
        if (columns.length === table.length && component && /^\d+(?:\.\d+)?$/.test(quantity) && uom && name) {
          plan.components.push({
            pn: component,
            quantity,
            uom: uom.toLowerCase() === 'ea' ? 'each' : uom.toLowerCase(),
            page: index + 1,
            note: line,
            confirmed: false,
          });
          if (!plan.parts.some(part => key(part.pn) === key(component)))
            plan.parts.push({ ...blankPart(), pn: component, name, page: index + 1, note: line });
        } else {
          table = null;
        }
      }
    }
    if (partNumber) {
      const existing = plan.parts.find(part => key(part.pn) === key(partNumber));
      if (existing) {
        existing.revision = rev;
        existing.name = title || existing.name;
      } else
        plan.parts.unshift({
          ...blankPart(),
          pn: partNumber,
          revision: rev,
          name: title,
          page: index + 1,
          note: `Part number/title block on page ${index + 1}`,
        });
      if (!plan.owner) plan.owner = partNumber;
    }
    if (text.trim().length < 20)
      plan.flags.push(
        `Page ${index + 1} has little or no native text. Read the rendered sheet and enter its evidence manually.`
      );
  });
  plan.flags.push(
    'Extraction is partial. Check every sheet, detail, BOM row and note; document omissions, assumptions and their resolution below.'
  );
  return plan;
}

export function issues(plan: DrawingPlan, includeOperations = true): string[] {
  const errors: string[] = [];
  if (restriction.test([...plan.pages, ...plan.requirements.map(r => r.text)].join('\n')))
    errors.push('Restriction marking found. Stop and ask Jon; no ERP entry or export.');
  if (!plan.screened) errors.push('Inspect every sheet for restrictions and confirm screening.');
  if (!plan.reviewed || !plan.reviewNotes.trim())
    errors.push('Resolve omissions/conflicts and confirm drawing review.');
  if (!plan.number.trim() || !plan.revision.trim() || !plan.title.trim())
    errors.push('Drawing number, revision and title are required.');
  if (!plan.parts.length || plan.parts.length > 100) errors.push('Enter between 1 and 100 reviewed parts.');
  const seen = new Set<string>();
  const evidence = (row: Evidence, label: string) => {
    if (
      !row.confirmed ||
      !row.note.trim() ||
      !Number.isInteger(row.page) ||
      row.page < 1 ||
      row.page > plan.pages.length
    )
      errors.push(`${label}: confirm page evidence and any explicit approval for supplied values.`);
  };
  for (const part of plan.parts) {
    const pn = key(part.pn);
    if (!pn || seen.has(pn)) errors.push(`Part ${pn || '(blank)'}: missing or duplicate PN.`);
    seen.add(pn);
    if (
      !/^[A-Z0-9][A-Z0-9._/-]{0,99}$/.test(pn) ||
      !/^[A-Z0-9]{1,20}$/.test(key(part.revision)) ||
      part.name.trim().length < 2 ||
      !TYPES.includes(part.type as PartType) ||
      !UOMS.includes(part.uom)
    )
      errors.push(`Part ${pn}: review PN, revision, name, type and stocking unit.`);
    if (
      part.type === 'raw_material' &&
      (!part.grade.trim() || pn !== key(part.grade) || /\bASTM\b|\s/.test(pn) || !part.description.trim())
    )
      errors.push(
        `${pn}: use only the reviewed grade as SKU; preserve the full material specification in Description.`
      );
    evidence(part, pn);
  }
  const owner = plan.parts.find(part => key(part.pn) === key(plan.owner));
  if (!owner || !['manufactured', 'assembly'].includes(owner.type))
    errors.push('Select a reviewed manufactured/assembly owner.');
  if (owner && key(owner.revision) !== key(plan.revision))
    errors.push('Owner and drawing revisions disagree. Resolve before entry.');
  const components = new Set<string>();
  for (const line of plan.components) {
    if (!seen.has(key(line.pn)) || key(line.pn) === key(plan.owner) || components.has(key(line.pn)))
      errors.push(`${line.pn}: component must be a distinct reviewed part, separate from its owner.`);
    components.add(key(line.pn));
    if (
      !line.quantity.trim() ||
      !Number.isFinite(Number(line.quantity)) ||
      Number(line.quantity) <= 0 ||
      !UOMS.includes(line.uom)
    )
      errors.push(`${line.pn}: enter a positive quantity per parent and its unit.`);
    if (plan.parts.find(p => key(p.pn) === key(line.pn))?.uom !== line.uom)
      errors.push(`${line.pn}: BOM and stocking units differ; resolve the conversion before entry.`);
    evidence(line, `BOM ${line.pn}`);
  }
  for (const req of plan.requirements) {
    if (!req.text.trim()) errors.push('Requirement text is blank.');
    if (!plan.operations.some(op => op.instructions.includes(req.text)))
      errors.push('Carry each confirmed manufacturing requirement into at least one operation instruction.');
    evidence(req, 'Requirement');
  }
  if (plan.requirements.length > 100 || plan.components.length > 100)
    errors.push('Split this package: at most 100 requirements/components per review.');
  if (plan.operations.some(op => op.name.length + op.component.length + 3 > 255 || op.instructions.length > 4000))
    errors.push('Shorten operation titles/instructions to fit the engineering record.');
  if (plan.components.length && plan.operations.some((op, index) => op.sequence !== (index + 1) * 10))
    errors.push('Assembly operations must use consecutive sequence numbers 10, 20, 30.');
  if (includeOperations) {
    if (!plan.operations.length || plan.operations.length > 99)
      errors.push('Review at least one routing operation (maximum 99).');
    const sequences = new Set<number>();
    for (const op of plan.operations) {
      if (
        !Number.isInteger(op.sequence) ||
        op.sequence < 10 ||
        op.sequence > 990 ||
        op.sequence % 10 ||
        sequences.has(op.sequence)
      )
        errors.push('Operation sequences must be unique multiples of 10, from 10 to 990.');
      sequences.add(op.sequence);
      if (!seen.has(key(op.component)) || (key(op.component) !== key(plan.owner) && !components.has(key(op.component))))
        errors.push('Operation part must be the owner or one of its BOM components.');
      if (
        !op.name.trim() ||
        /\bQTY\b|\bquantity\b/i.test(op.name) ||
        !Number.isInteger(Number(op.center)) ||
        Number(op.center) <= 0
      )
        errors.push('Each operation needs a description and an observed work center; keep quantities out of titles.');
      if ([op.setup, op.run].some(v => !v.trim() || !Number.isFinite(Number(v)) || Number(v) < 0))
        errors.push('Supply reviewed setup/run minutes; unknown times cannot default to zero.');
      evidence(op, `Operation ${op.sequence}`);
    }
  }
  if (
    plan.requestedWO &&
    (!plan.quantity.trim() ||
      !Number.isFinite(Number(plan.quantity)) ||
      Number(plan.quantity) <= 0 ||
      !plan.quantitySource.trim())
  )
    errors.push('A Draft WO requires an explicit job quantity and its authorization source.');
  return Array.from(new Set(errors));
}

export function matchPart(part: DrawingPart, records: Part[], plan: DrawingPlan): Part | undefined {
  const matches = records.filter(row => key(row.part_number) === key(part.pn));
  if (matches.length > 1) throw new Error(`${part.pn}: multiple matches. Review existing records.`);
  const found = matches[0];
  if (!found) return undefined;
  if (
    !found.is_active ||
    key(found.revision) !== key(part.revision) ||
    found.part_type !== part.type ||
    found.unit_of_measure !== part.uom ||
    found.name.trim() !== part.name.trim() ||
    (key(part.pn) === key(plan.owner) &&
      ((found.drawing_number || '') !== plan.number || (found.customer_name || '') !== plan.customer))
  )
    throw new Error(
      `${part.pn}: existing record conflicts with the reviewed revision, type, unit, name, drawing or customer. Open Parts for revision review.`
    );
  return found;
}
