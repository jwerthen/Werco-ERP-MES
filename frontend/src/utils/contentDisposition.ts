/**
 * The download filename from a `Content-Disposition` header (RFC 6266).
 *
 * A server that names a file outside ASCII sends two parameters:
 *
 *   attachment; filename="ascii_fallback.csv"; filename*=UTF-8''%E2%82%AC%20rates.csv
 *
 * `filename*` is an RFC 5987 ext-value (`charset'language'percent-encoded`) and wins
 * whenever it decodes; the quoted `filename` (backslash escapes undone) is the
 * fallback, then the caller's default. A path separator or control character in the
 * result is replaced, so the name can only ever name a file, never a path.
 */

// One `; name=value` parameter: a quoted-string (with backslash escapes) or a token.
const PARAM_PATTERN = /;\s*([^\s=;]+)\s*=\s*("(?:[^"\\]|\\.)*"|[^;]*)/g;

function unquote(raw: string): string {
  const value = raw.trim();
  if (value.length >= 2 && value.startsWith('"') && value.endsWith('"')) {
    return value.slice(1, -1).replace(/\\(.)/g, '$1');
  }
  return value;
}

function parameters(header: string): Map<string, string> {
  const params = new Map<string, string>();
  const pattern = new RegExp(PARAM_PATTERN.source, 'g');
  let match: RegExpExecArray | null;
  while ((match = pattern.exec(header)) !== null) {
    const name = match[1].toLowerCase();
    // The first occurrence wins; a repeated parameter is malformed, not an override.
    if (!params.has(name)) params.set(name, match[2]);
  }
  return params;
}

/** Undo `%XX` escapes as single bytes, read as ISO-8859-1. */
function decodeLatin1(encoded: string): string {
  return encoded.replace(/%([0-9a-f]{2})/gi, (_, hex: string) => String.fromCharCode(parseInt(hex, 16)));
}

/** Decode an RFC 5987 ext-value, or null when it is malformed or in an unsupported charset. */
export function decodeExtValue(raw: string): string | null {
  // Some servers wrongly quote the ext-value; tolerate it.
  const match = /^([^']*)'[^']*'(.*)$/.exec(unquote(raw));
  if (!match) return null;
  const charset = match[1].trim().toLowerCase();
  const encoded = match[2];
  try {
    if (charset === 'utf-8') return decodeURIComponent(encoded);
    if (charset === 'iso-8859-1') return decodeLatin1(encoded);
  } catch {
    return null;
  }
  return null;
}

function safeFilename(name: string): string | null {
  const cleaned = name
    .replace(/[\u0000-\u001f\u007f]/g, '')
    .replace(/[\\/]/g, '_')
    .trim();
  return cleaned && cleaned !== '.' && cleaned !== '..' ? cleaned : null;
}

/** The filename a `Content-Disposition` header names, or null when it names none. */
export function filenameFromContentDisposition(header: string | null | undefined): string | null {
  if (!header) return null;
  const params = parameters(header);
  const extended = params.get('filename*');
  if (extended !== undefined) {
    const decoded = decodeExtValue(extended);
    const safe = decoded === null ? null : safeFilename(decoded);
    if (safe) return safe;
  }
  const plain = params.get('filename');
  return plain === undefined ? null : safeFilename(unquote(plain));
}

/** `filenameFromContentDisposition`, else `fallback`. */
export function downloadFilename(header: string | null | undefined, fallback: string): string {
  return filenameFromContentDisposition(header) ?? fallback;
}
