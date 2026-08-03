"""Human-facing renderings of the task DAG — the /status text view and the Mermaid graph.

Split from dag.py (the model/validation half) so the presentation can change without touching
graph semantics. Consumers keep addressing these through `dag.render` / `dag.mermaid` — dag.py
re-exports them lazily (PEP 562), so import this module directly only from dag itself.
"""

from __future__ import annotations

from rein.dag import STATUS_ORDER, Graph


def render(graph: Graph) -> str:
    """Deterministic rendering of the human-facing DAG view (task table, layers, critical path, frontier).

    `/status` and `rein dag --render` print it. It is deliberately not embedded in a state
    file: a view inside the SSOT is a second copy of the truth, and the two drift.
    """
    lines: list[str] = []
    counts = graph.counts()
    lines.append("Counts: " + " / ".join(f"{s}={counts[s]}" for s in STATUS_ORDER))
    lines.append("")
    lines.append("### Task table")
    if graph.tasks:
        fan = graph.fan_out()
        lines.append("| ID | Title | Kind | blockedBy | risk | claims | fan-out | status |")
        lines.append("|----|-------|------|-----------|------|--------|---------|--------|")
        for t in graph.tasks:
            blocked = ", ".join(t.blocked_by) if t.blocked_by else "-"
            claims = ", ".join(t.claim_ids) or "-"
            row = f"| {t.id} | {t.title} | {t.kind} | {blocked} | {t.risk} | {claims} | {fan[t.id]} | {t.status} |"
            lines.append(row)
    else:
        lines.append("- (no tasks)")
    lines.append("")
    lines.append("### Execution layers (within a layer, parallel is possible)")
    layers = graph.layers()
    if layers:
        for i, layer in enumerate(layers):
            lines.append(f"- L{i}: {', '.join(layer)}")
    else:
        lines.append("- (no tasks)")
    lines.append("")
    critical = graph.critical_path()
    lines.append("### Critical path (longest chain)")
    lines.append("- " + (" → ".join(critical) if critical else "(no tasks)"))
    lines.append("")
    lines.append("### Current executable frontier (optimal consumption order)")
    ordered = graph.order_frontier()
    if ordered:
        fan = graph.fan_out()
        for t in ordered:
            lines.append(f"- {t.id} [{t.kind}, risk={t.risk}, fan-out={fan[t.id]}] {t.title}")
    else:
        lines.append("- (no startable todo)")
    return "\n".join(lines)


# status -> Mermaid classDef (fill=status color, critical=bold border). The class name replaces `-` in status with `_`.
_STATUS_CLASSDEFS = (
    "classDef todo fill:#eeeeee,stroke:#999999,color:#333333;",
    "classDef in_progress fill:#cfe8ff,stroke:#3b82f6,color:#06325e;",
    "classDef blocked fill:#ffd6d6,stroke:#ee2233,color:#7a0010;",
    "classDef needs_revision fill:#ffe9c7,stroke:#f59e0b,color:#7a4a00;",
    "classDef done fill:#d7f5dd,stroke:#22a04b,color:#0b3d1d;",
    "classDef critical stroke-width:3px;",
)


def _node_key(task_id: str) -> str:
    """Sanitize for a Mermaid node ID (`-` cannot be used in an identifier, so → `_`)."""
    return task_id.replace("-", "_")


def mermaid(graph: Graph) -> str:
    """Deterministically output the dependency graph as Mermaid (graph TD). Color-coded by status, critical path bold.

    Returns Mermaid text (wrapped in a ```mermaid fence) that renders directly in GitHub / VS Code / Markdown
    (rasterizing would break offline-ness, so leave rendering to the client).
    """
    tasks = sorted(graph.tasks, key=lambda t: t.id)
    lines: list[str] = ["```mermaid", "graph TD"]
    if not tasks:
        lines.append('  empty["(no tasks)"]')
        lines.append("```")
        return "\n".join(lines)
    for t in tasks:
        label = f"{t.id}: {t.title}".replace('"', "'")
        lines.append(f'  {_node_key(t.id)}["{label}"]')
    for t in tasks:
        for dep in t.blocked_by:
            lines.append(f"  {_node_key(dep)} --> {_node_key(t.id)}")
    lines.extend(f"  {cd}" for cd in _STATUS_CLASSDEFS)
    for status in STATUS_ORDER:
        ids = [_node_key(t.id) for t in tasks if t.status == status]
        if ids:
            lines.append(f"  class {','.join(ids)} {status.replace('-', '_')};")
    critical = graph.critical_path()
    if critical:
        lines.append(f"  class {','.join(_node_key(i) for i in critical)} critical;")
    lines.append("```")
    return "\n".join(lines)
