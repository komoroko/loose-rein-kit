// The small shared pieces every view draws with. All of them used to be string builders whose
// output was concatenated into `innerHTML`; as components they compose without anybody having to
// remember which of their arguments were already escaped.

export function Chip({ id, status, critical, onClick }) {
  const cls = ["chip", status, critical ? "critical" : "", onClick ? "clk" : ""].filter(Boolean).join(" ");
  return (
    <span className={cls} title={status} onClick={onClick}>
      {id}
    </span>
  );
}

/** A horizontally scrollable table. Wide content scrolls in its own box, never the page. */
export function Scroll({ children }) {
  return <div className="scroll">{children}</div>;
}

export function Table({ head, children }) {
  return (
    <Scroll>
      <table>
        <tbody>
          {head ? <tr>{head.map((h) => <th key={h}>{h}</th>)}</tr> : null}
          {children}
        </tbody>
      </table>
    </Scroll>
  );
}

export function Empty({ children }) {
  return <div className="empty">{children}</div>;
}

export function Warn({ children }) {
  return <div className="warn">{children}</div>;
}

export function OkLine({ children }) {
  return <div className="okline">{children}</div>;
}

export function Subhead({ children, spaced }) {
  return <div className="subhead" style={spaced ? { marginTop: "1.2rem" } : undefined}>{children}</div>;
}

const RISKY = new Set(["high", "critical"]);

export function RiskBadge({ risk }) {
  return <span className={"conf " + (RISKY.has(risk) ? "low" : "medium")}>{risk || "low"}</span>;
}

export function ConfBadge({ level }) {
  return level ? <span className={"conf " + level}>{level}</span> : null;
}

// The honest label for where a sentence came from. `machine_inferred` is an AI (or a template)
// talking, and models.py requires it not be rendered with the weight of an observation.
export function EpistemicBadge({ status }) {
  const weak = status === "machine_inferred" || status === "unknown" || status === "conflicted";
  return <span className={"epi" + (weak ? " weak" : "")}>{status || "unknown"}</span>;
}

export function paths(list) {
  if (!list || !list.length) return "—";
  return list.map((p, i) => (
    <span key={p + i}>
      {i ? <br /> : null}
      {p}
    </span>
  ));
}

export const short = (sha) => (sha ? String(sha).slice(0, 12) : "");
