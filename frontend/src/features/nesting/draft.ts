import { demoQuote, type Quote } from './lib/quoting';

// Only the current user's current-company draft is retained in this tab's
// memory. No localStorage, uploads, database records or cross-user draft list.
let draft: { owner: string; quote: Quote; savedSignature: string } | null = null;

export function readNestingDraft(owner: string): Quote {
  if (draft?.owner !== owner) draft = null;
  return draft?.quote ?? demoQuote;
}

export function readNestingSavedSignature(owner: string) {
  return draft?.owner === owner ? draft.savedSignature : JSON.stringify(demoQuote);
}

export function writeNestingDraft(owner: string, quote: Quote, savedSignature = JSON.stringify(demoQuote)) {
  draft = { owner, quote, savedSignature };
}
