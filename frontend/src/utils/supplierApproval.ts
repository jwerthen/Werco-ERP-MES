/**
 * Approved Supplier List vocabulary for display.
 *
 * TRANSCRIBED from backend/app/services/supplier_approval_service.py (STATUS_LABELS,
 * SCOPE_LABELS, BASIS_LABELS, REEVALUATION_STATUSES), in the backend's enum order, so the
 * dropdowns list the QL 7.4 values exactly as the controlled form does. The server owns the
 * vocabulary — it normalizes labels to slugs and 422s anything else — so a drift here would
 * only mislabel, never write a bad value; `supplierApproval.parity.test.ts` reads the Python
 * and fails CI the moment the two disagree.
 */

import type { AslStatus, VendorAslFields } from '../types/supplierApproval';
import { isDateBeforeTodayInCentral } from './centralTime';
import { variantClass, type StatusVariant } from './statusColors';

export const ASL_STATUS_LABELS: Record<AslStatus, string> = {
  approved: 'Approved',
  conditional: 'Conditional',
  disqualified: 'Disqualified',
  approved_customer_directed: 'Approved - Customer Directed',
};

export const ASL_STATUSES = Object.keys(ASL_STATUS_LABELS) as AslStatus[];

export const NOT_ON_ASL_LABEL = 'Not on the ASL';

/** The 19 "APPROVED SCOPE OF SUPPLY" values, in the dropdown's canonical order. */
export const ASL_SCOPE_LABELS: Record<string, string> = {
  adhesives_bonding: 'Adhesives / Bonding Materials',
  calibration_services: 'Calibration Services',
  customer_directed_supplier: 'Customer-Directed Supplier',
  electronic_components: 'Electronic Components',
  engineering_design_services: 'Engineering / Design Services',
  hardware: 'Hardware',
  honeycomb_core_panels: 'Honeycomb Core / Panels',
  machining: 'Machining',
  ndt_inspection_laboratory: 'NDT / Inspection / Laboratory Services',
  paint_powders: 'Paint / Powders',
  raw_material_composite: 'Raw Material - Composite',
  raw_material_metallics: 'Raw Material - Metallics',
  raw_material_other: 'Raw Material - Other',
  screen_printing: 'Screen Printing',
  special_process_passivation: 'Special Process - Passivation',
  special_process_welding: 'Special Process - Welding',
  special_process_heat_treatment: 'Special Process - Heat Treatment',
  special_process_plating_coating: 'Special Process - Plating / Coating',
  welding_materials: 'Welding Materials',
};

/** The 7 "APPROVAL BASIS" values, in the dropdown's canonical order. */
export const ASL_BASIS_LABELS: Record<string, string> = {
  as9100: 'AS9100 Cert',
  as9120: 'AS9120 Cert',
  iso9001: 'ISO 9001 Cert',
  iso17025: 'ISO 17025 Cert',
  nadcap: 'NADCAP Cert',
  questionnaire: 'Questionnaire',
  customer_requirement: 'Customer Requirement',
};

export const ASL_SCOPES = Object.keys(ASL_SCOPE_LABELS);
export const ASL_BASES = Object.keys(ASL_BASIS_LABELS);

/** Statuses whose vendor may be bought from: the server derives `is_approved` = true. */
export const APPROVED_STATUSES: ReadonlySet<AslStatus> = new Set<AslStatus>(['approved', 'approved_customer_directed']);

/** Statuses that carry a re-evaluation obligation (the overdue list). Disqualified does not. */
export const REEVALUATION_STATUSES: ReadonlySet<AslStatus> = new Set<AslStatus>([
  'approved',
  'conditional',
  'approved_customer_directed',
]);

export const aslStatusLabel = (status: AslStatus | string | null | undefined): string =>
  status ? ((ASL_STATUS_LABELS as Record<string, string>)[status] ?? status) : NOT_ON_ASL_LABEL;

export const scopeLabel = (slug: string): string => ASL_SCOPE_LABELS[slug] ?? slug;
export const basisLabel = (slug: string): string => ASL_BASIS_LABELS[slug] ?? slug;

export const scopeLabels = (slugs: string[] | null | undefined): string => (slugs ?? []).map(scopeLabel).join(', ');
export const basisLabels = (slugs: string[] | null | undefined): string => (slugs ?? []).map(basisLabel).join(', ');

const STATUS_VARIANT: Record<AslStatus, StatusVariant> = {
  approved: 'green',
  approved_customer_directed: 'blue',
  conditional: 'amber',
  disqualified: 'red',
};

/** bg/text classes for an ASL status pill; slate for "not on the ASL". */
export const aslStatusClass = (status: AslStatus | string | null | undefined): string =>
  variantClass[(status && STATUS_VARIANT[status as AslStatus]) || 'slate'];

/**
 * What `is_approved` becomes for a status, mirroring `derived_is_approved`: true for
 * Approved / Approved - Customer Directed, false for Conditional / Disqualified, and
 * `null` (= left exactly as it is) for "not on the ASL".
 */
export const derivedIsApproved = (status: AslStatus | null | undefined): boolean | null =>
  status ? APPROVED_STATUSES.has(status) : null;

/** True when the vendor owes a re-evaluation whose due date (Central) is already past. */
export const isReevaluationOverdue = (vendor: VendorAslFields): boolean =>
  !!vendor.approval_status &&
  REEVALUATION_STATUSES.has(vendor.approval_status) &&
  !!vendor.reevaluation_due &&
  isDateBeforeTodayInCentral(vendor.reevaluation_due);

/** Roles that may record a supplier-approval decision or load the ASL (ADMIN/MANAGER/QUALITY —
 *  the form's owner is "Purchasing / Quality"). PLATFORM_ADMIN is omitted, matching the
 *  purchasing write controls elsewhere: a hidden button rather than one that 403s. */
export const canWriteAsl = (
  user: { role?: string | null; is_superuser?: boolean | null } | null | undefined
): boolean => !!user && (!!user.is_superuser || ['admin', 'manager', 'quality'].includes(user.role ?? ''));
