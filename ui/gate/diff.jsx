// The change set: what an approval at this gate would cover, as git tells it.

import { OkLine, Scroll, Subhead, Warn, short } from "../parts.jsx";

function lineClass(line) {
  if (line.startsWith("+++") || line.startsWith("---")) return "file";
  if (line.startsWith("@@")) return "hunk";
  if (line.startsWith("+")) return "add";
  if (line.startsWith("-")) return "del";
  return "";
}

// `.dl` spans are display:block, so the lines are emitted without separators — a "\n" between them
// would double the line height.
function Patch({ text }) {
  return (
    <pre className="patch">
      {(text || "").split("\n").map((line, i) => (
        <span className={"dl " + lineClass(line)} key={i}>
          {line}
        </span>
      ))}
    </pre>
  );
}

function FreshnessBadge({ meta }) {
  if (!meta) return null;
  if (meta.fresh) return <OkLine>✓ the machine review is bound to this HEAD ({short(meta.head)})</OkLine>;
  return (
    <Warn>
      The machine review is missing or stale (reviewed: {meta.reviewed_head ? short(meta.reviewed_head) : "none"},
      HEAD: {short(meta.head)}) — regenerate it before approving.
    </Warn>
  );
}

export default function Diff({ diff, meta }) {
  if (diff.error) return <Warn>{diff.error}</Warn>;
  if (diff.log) {
    return (
      <>
        <FreshnessBadge meta={meta} />
        <p className="note">{diff.note || ""}</p>
        <pre className="patch">{diff.log.join("\n")}</pre>
      </>
    );
  }
  return (
    <>
      <FreshnessBadge meta={meta} />
      <Subhead>
        Files (base {short(diff.base)} on {diff.base_ref || ""})
      </Subhead>
      <Scroll>
        <table>
          <tbody>
            {(diff.name_status || []).map((r, i) => (
              <tr key={i}>
                <td className="mono">{r[0]}</td>
                <td className="mono">{r[1]}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Scroll>
      {diff.truncated ? <Warn>Patch truncated at 200KB — read the rest in your editor.</Warn> : null}
      <div className="subhead" style={{ marginTop: ".8rem" }}>Patch</div>
      <Patch text={diff.patch} />
    </>
  );
}
