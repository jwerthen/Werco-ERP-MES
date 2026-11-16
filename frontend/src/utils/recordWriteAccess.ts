import type { UserRole } from '../types';

type Actor = { role?: UserRole; is_superuser?: boolean } | null | undefined;

function hasWriteRole(actor: Actor, roles: UserRole[]): boolean {
  return (
    !!actor &&
    (actor.is_superuser === true ||
      actor.role === 'platform_admin' ||
      (actor.role !== undefined && roles.includes(actor.role)))
  );
}

// Mirror the fixed server role gates. Company permission overrides do not grant
// these financial/document verbs; the API also enforces active-company context.
export const canWriteJobCosts = (actor: Actor): boolean => hasWriteRole(actor, ['admin', 'manager']);
export const canPublishDocuments = (actor: Actor): boolean => hasWriteRole(actor, ['admin', 'manager', 'quality']);
export const canDeleteDocuments = (actor: Actor): boolean => hasWriteRole(actor, ['admin', 'manager']);
export const canEditFAIs = (actor: Actor): boolean =>
  hasWriteRole(actor, ['admin', 'manager', 'supervisor', 'quality']);
// Customer quote writes (edit terms / accept / reject / expire, mark sent, convert to work
// orders) — mirrors QUOTE_WRITE_ROLES in backend/app/api/endpoints/quotes.py.
export const canWriteQuotes = (actor: Actor): boolean => hasWriteRole(actor, ['admin', 'manager', 'supervisor']);
// FAI final disposition and the customer-approval verb — mirrors fai_service.APPROVE_ROLES.
export const canApproveFAIs = (actor: Actor): boolean => hasWriteRole(actor, ['admin', 'manager', 'quality']);
// Server-side spreadsheet exports (GET /exports/*) — mirrors EXPORT_ROLES in
// backend/app/api/endpoints/exports.py. Quality is deliberately NOT in it.
export const canExportRecords = (actor: Actor): boolean => hasWriteRole(actor, ['admin', 'manager']);
// Production calendar (backend/app/api/endpoints/production_calendar.py): saving the
// calendar is CALENDAR_EDIT_ROLES (Admin); the working-calendar sync is
// WORKING_CALENDAR_SYNC_ROLES, the roles of the scheduling calendar PUT it writes through.
export const canEditProductionCalendar = (actor: Actor): boolean => hasWriteRole(actor, ['admin']);
export const canSyncWorkingCalendars = (actor: Actor): boolean =>
  hasWriteRole(actor, ['admin', 'manager', 'supervisor']);
