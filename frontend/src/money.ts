// Money is integer fen and percents are decimal strings; nothing here uses floating-point arithmetic.

const FEN_PER_YUAN = 100n;

export function formatFen(fen: number): string {
  if (!Number.isSafeInteger(fen)) {
    throw new RangeError("fen must be a safe integer");
  }
  const abs = BigInt(Math.abs(fen));
  const yuan = (abs / FEN_PER_YUAN).toString().replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  const cents = abs % FEN_PER_YUAN;
  const tail = cents === 0n ? "" : `.${cents.toString().padStart(2, "0")}`;
  return `${fen < 0 ? "-" : ""}¥${yuan}${tail}`;
}

/** Parse a typed amount in yuan ("1,200", "¥85.5") to fen; null when it is not a positive amount. */
export function parseYuanToFen(text: string): number | null {
  const cleaned = text.replace(/[¥￥,\s]/g, "");
  const match = /^(\d{1,9})(?:\.(\d{1,2}))?$/.exec(cleaned);
  if (!match) {
    return null;
  }
  const fen = BigInt(match[1]) * FEN_PER_YUAN + BigInt((match[2] ?? "").padEnd(2, "0") || "0");
  return fen > 0n ? Number(fen) : null;
}

/** Show a decimal percent string without trailing zeros: "45.5000" becomes "45.5%". */
export function formatPct(value: string): string {
  const trimmed = value.includes(".") ? value.replace(/0+$/, "").replace(/\.$/, "") : value;
  return `${trimmed}%`;
}
