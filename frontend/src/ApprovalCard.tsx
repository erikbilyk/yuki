import { formatTime } from "./format.ts";
import { Money, Percent } from "./Money.tsx";
import { S } from "./strings.ts";
import type { Card, CardChange } from "./types.ts";

function Change({ change }: { change: CardChange }) {
  const none = <span className="muted">{S.card.none}</span>;
  if (change.unit === "fen") {
    return (
      <tr>
        <th scope="row">{change.label}</th>
        <td>{change.before === null ? none : <Money fen={change.before} />}</td>
        <td className="arrow" aria-hidden="true">
          →
        </td>
        <td>
          <Money fen={change.after} />
        </td>
      </tr>
    );
  }
  return (
    <tr>
      <th scope="row">{change.label}</th>
      <td>{change.before === null ? none : <Percent value={change.before} quality={change.quality} />}</td>
      <td className="arrow" aria-hidden="true">
        →
      </td>
      <td>
        <Percent value={change.after} quality={change.quality} />
      </td>
    </tr>
  );
}

/** The server-rendered card, shown as received; the buttons live in the caller. */
export function ApprovalCard({ card }: { card: Card }) {
  return (
    <article className={`card tier-${card.tier.toLowerCase()}`} data-testid="approval-card">
      <header>
        <span className="trusted-mark">{S.card.trusted}</span>
        <span className={`tier tier-${card.tier.toLowerCase()}`}>{S.card.tierNames[card.tier]}</span>
      </header>
      <h3>{card.title}</h3>
      {card.lines.length > 0 && (
        <dl className="card-lines">
          {card.lines.map((line) => (
            <div key={line.key}>
              <dt>{line.label}</dt>
              <dd>{line.text}</dd>
            </div>
          ))}
        </dl>
      )}
      {card.changes.length > 0 && (
        <>
          <h4>{S.card.changes}</h4>
          <table className="changes">
            <thead>
              <tr>
                <th scope="col" />
                <th scope="col">{S.card.before}</th>
                <th scope="col" />
                <th scope="col">{S.card.after}</th>
              </tr>
            </thead>
            <tbody>
              {card.changes.map((change) => (
                <Change key={change.key} change={change} />
              ))}
            </tbody>
          </table>
        </>
      )}
      {card.sources.length > 0 && (
        <>
          <h4>{S.card.sources}</h4>
          <ul className="sources">
            {card.sources.map((source) => (
              <li key={source.slot} className={source.provenance === "untrusted_text" ? "untrusted" : undefined}>
                {source.slot}：{source.label}
              </li>
            ))}
          </ul>
        </>
      )}
      {card.warnings.length > 0 && (
        <>
          <h4>{S.card.warnings}</h4>
          <ul className="warnings">
            {card.warnings.map((warning) => (
              <li key={warning}>{warning}</li>
            ))}
          </ul>
        </>
      )}
      {card.requires_code && <p className="notice">{S.card.needsCode}</p>}
      <footer className="muted small">
        {S.card.expires} {formatTime(card.expires_at)}
      </footer>
    </article>
  );
}
