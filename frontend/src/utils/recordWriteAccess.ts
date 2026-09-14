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
