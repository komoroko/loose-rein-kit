// The form controls the gate-④ stages ask with.
//
// Each card owns its own answer in component state. The old pane could not: every repaint rebuilt
// the DOM from strings, so a half-typed reason had to be read back out of the document at submit
// time with `data-scope` / `data-field` selectors, and a radio group had to be queried for its
// `:checked` member because otherwise the first option was reported as the answer whatever the
// reviewer clicked — which is exactly the kind of fabricated human input this pane must not emit.

export function SelectField({ label, value, options, onChange }) {
  return (
    <label className="fld">
      <span>{label}</span>
      <select value={value} onChange={(e) => onChange(e.target.value)}>
        <option value="">choose…</option>
        {options.map((o) => (
          <option key={o} value={o}>
            {o}
          </option>
        ))}
      </select>
    </label>
  );
}

export function ConfidencePicker({ value, onChange }) {
  return <SelectField label="how sure are you?" value={value} options={["low", "medium", "high"]} onChange={onChange} />;
}

export function TextField({ label, value, placeholder, onChange }) {
  return (
    <label className="fld">
      <span>{label}</span>
      <input type="text" value={value} placeholder={placeholder || ""} onChange={(e) => onChange(e.target.value)} />
    </label>
  );
}

export function TextArea({ label, value, rows, onChange }) {
  return (
    <label className="fld">
      <span>{label}</span>
      <textarea rows={rows || 3} value={value} onChange={(e) => onChange(e.target.value)} />
    </label>
  );
}
