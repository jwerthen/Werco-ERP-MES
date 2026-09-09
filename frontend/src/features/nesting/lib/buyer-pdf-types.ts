import type { Loop } from './nesting';
import type { QuoteProject } from './quote-project';
import type { ComparisonSnapshot } from './run-manifest';
import type { RemnantStageMessage } from './remnant-planning';

/** All geometry is in inches; X is sheet length and Y is sheet width. */
export type BuyerPdfSheet = {
  number: number;
  source: 'purchase' | 'recorded_piece';
  sourceLabel: string;
  widthIn: number;
  lengthIn: number;
  marginIn: number;
  gapIn: number;
  outer: Loop;
  holes: Loop[];
  exclusions: { outline: Loop; clearanceIn: number }[];
  placements: { partId: string; originalInstance: number; loops: Loop[] }[];
};
export type BuyerPdfReport = {
  version: 1;
  units: 'in';
  expectedCompanyId: number;
  projectName: string;
  notes: string;
  inputSha256: string;
  solverVersion: 'werco-contour-v7';
  groups: {
    id: string;
    name: string;
    material: string;
    materialDescription: string;
    thicknessIn: number;
    selectionKind: 'full_sheet' | 'recorded_piece';
    partRequirements: { id: string; label: string; name: string; revision: string; quantity: number }[];
    sheets: BuyerPdfSheet[];
    baselinePurchaseSheets: { widthIn: number; lengthIn: number; quantity: number }[];
  }[];
};
export type BuyerPdfInputs = {
  project: QuoteProject;
  comparisons: Record<string, ComparisonSnapshot>;
  remnantInput: unknown | null;
  stages: RemnantStageMessage[];
  companyId: number;
};
export type BuyerPdfChoice = { id: string; label: string; recommended: boolean; conditional: boolean };
export type BuyerPdfGroupChoices = { groupId: string; label: string; choices: BuyerPdfChoice[] };
export const BUYER_PDF_MAX_BYTES = 8 * 1024 * 1024;
export const BUYER_PDF_MAX_VERTICES = 200_000;
export const BUYER_PDF_MAX_SHEETS = 300;
