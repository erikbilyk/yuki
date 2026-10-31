import { formatFen, formatFenExact, formatPct } from "./money.ts";
import { S } from "./strings.ts";
import type { PercentQuality } from "./types.ts";

export function Money({ fen, exact = false }: { fen: number; exact?: boolean }) {
  return <span className="money">{exact ? formatFenExact(fen) : formatFen(fen)}</span>;
}

export function Percent({ value, quality }: { value: string; quality: PercentQuality }) {
  return (
    <span className="percent">
      {formatPct(value)}
      <span className={`tag tag-${quality}`}>
        {quality === "estimated" ? S.card.estimated : S.card.confirmed}
      </span>
    </span>
  );
}
