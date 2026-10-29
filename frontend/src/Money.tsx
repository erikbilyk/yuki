import { formatFen, formatPct } from "./money.ts";
import { S } from "./strings.ts";
import type { PercentQuality } from "./types.ts";

export function Money({ fen }: { fen: number }) {
  return <span className="money">{formatFen(fen)}</span>;
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
