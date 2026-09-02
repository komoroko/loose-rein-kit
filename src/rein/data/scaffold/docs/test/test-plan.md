# Test Plan

> `/verify` runs and records functional tests (bugs) and non-functional requirement tests following this plan.
> Material for the human's release decision at **gate ⑤**.

## 1. Functional tests (requirement satisfaction)

Confirm each requirement's acceptance criteria are satisfied.

| Requirement | What to check | Means (auto/manual) | Result | Notes |
|------|----------|-------------------|------|------|
| R-1 | | auto | ⬜ | |
| R-2 | | | ⬜ | |

Legend: ✅ pass / ❌ fail / ⬜ not run

## 2. Non-functional requirement tests (criteria checklist)

> Criteria-based since it is stack-independent. Make it concrete for your product.
> One row per `NFR-N` from `docs/10-requirements.md` — `/verify` runs
> `rein dag --trace --test-plan docs/test/test-plan.md`, which mechanically fails any R/NFR
> that does not appear in this plan.

| NFR | What to check (criterion) | Means (auto/manual) | Result | Notes |
|-----|---------------------------|---------------------|--------|-------|
| NFR-1 | | | ⬜ | |
| NFR-2 | | | ⬜ | |

### Performance
- [ ] Main operations' response time within requirement
- [ ] No degradation at expected data volume

### Security (mandatory in `/verify`)

The change's own review is **carried from gate ④, not re-run here**: it was taken against the
commit under review, a blocking finding holds this gate shut, and readiness refuses a review whose
`subject_head_sha` is not this HEAD. Name it below rather than repeating it. The dependency audit
is the opposite case — its answer is not a function of the tree, so it must be taken again, and it
is only worth what its date and commit say it is.

- [ ] The carried gate-④ security review is named below (which review, and what it found)
- [ ] Run **`make audit`** and have no known dependency vulnerabilities (Python: pip-audit / frontend: pnpm audit)
- [ ] No plaintext storage / log output of secrets (gitleaks mechanically prevents this at the commit stage)
- [ ] Input validation / injection countermeasures

| Check | Result | Severity | Taken against (commit / date) | Notes |
|------|------|--------|------|------|
| gate-④ security review (carried) | ⬜ | | `subject_head_sha` | findings by severity |
| make audit (Python) | ⬜ | | commit + date it was run | |
| make audit (frontend) | ⬜ | | commit + date it was run | |

### Reliability / operations
- [ ] Behavior on error is as defined
- [ ] Logs/monitoring emit the necessary information

## 3. Manual verification checklist (human-run acceptance)

Acceptance that automated tests can't cover — a human runs these and records the result. Make it concrete for
your product; unrun items become remaining issues at gate ⑤.

| Check | How | Result | Notes |
|-------|-----|--------|-------|
| Real user-facing behaviour in the target environment | exercise the output where a real user would | ⬜ | |
| Output quality / correctness by eye | eyeball the output against the intent | ⬜ | |
| Supported-OS matrix | run on each Must-support OS | ⬜ | |
| Long-input / end-to-end performance | measure on a realistic full-size input | ⬜ | |

Legend: ✅ pass / ❌ fail / ⬜ not run

## 4. Defects found
| ID | Content | Severity | Task | Status |
|----|------|--------|-----------|------|
| | | | | |

## 5. Overall judgment (filled by the human)
- **Release decision**: hold / go / conditional go
- **Remaining issues**:
