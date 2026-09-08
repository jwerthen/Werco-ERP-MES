/** UTF-16 code-unit order is independent of the host locale and ICU version. */
export function compareStableText(a: string, b: string): number {
  return a < b ? -1 : a > b ? 1 : 0;
}
