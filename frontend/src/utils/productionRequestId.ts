/** One ID per submitted batch, retained until its outcome is known. */
export function productionRequestId(): string {
  return globalThis.crypto?.randomUUID?.() || `production-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}
