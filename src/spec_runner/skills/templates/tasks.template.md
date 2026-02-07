# Tasks

> Tasks with priorities, dependencies, and traceability to requirements

## Legend

**Priority:**
- 🔴 P0 — Critical, blocks release
- 🟠 P1 — High, needed for full functionality
- 🟡 P2 — Medium, improves experience
- 🟢 P3 — Low, nice to have

**Status:**
- ⬜ TODO
- 🔄 IN PROGRESS
- ✅ DONE
- ⏸️ BLOCKED

**Estimate:**
- Use days (d) or hours (h)
- Prefer a range: 3-5d

---

## Definition of Done (for EVERY task)

> ⚠️ A task is NOT considered complete without meeting these criteria:

- [ ] **Unit tests** — coverage ≥80% of new code
- [ ] **Tests pass** — all tests pass locally
- [ ] **Integration test** — if public interfaces are changed
- [ ] **CI green** — pipeline passes
- [ ] **Docs updated** — documentation is up to date
- [ ] **Code review** — PR approved

---

## Testing Tasks (required)

### TASK-100: Test Infrastructure Setup
🔴 P0 | ⬜ TODO | Est: 2d

**Description:**  
Set up the test infrastructure.

**Checklist:**
- [ ] Test framework setup (pytest/jest/etc.)
- [ ] Coverage reporting
- [ ] CI workflow
- [ ] Test fixtures structure
- [ ] Pre-commit hooks

**Traces to:** [NFR-000]  
**Depends on:** —  
**Blocks:** All other tasks

---

## Milestone 1: MVP

### TASK-001: {{TASK_NAME}}
🔴 P0 | ⬜ TODO | Est: {{X}}d

**Description:**  
{{TASK_DESCRIPTION}}

**Checklist:**
- [ ] {{SUBTASK_1}}
- [ ] {{SUBTASK_2}}
- [ ] {{SUBTASK_3}}

**Tests (Definition of Done):**
- [ ] Unit tests: {{test_scope_1}}
- [ ] Unit tests: {{test_scope_2}}
- [ ] Integration test (if needed)
- [ ] Coverage ≥80%

**Traces to:** [REQ-XXX], [REQ-YYY]  
**Depends on:** [TASK-100]  
**Blocks:** [TASK-XXX]

---

### TASK-002: {{TASK_NAME}}
🔴 P0 | ⬜ TODO | Est: {{X}}d

**Description:**  
{{TASK_DESCRIPTION}}

**Checklist:**
- [ ] {{SUBTASK_1}}
- [ ] {{SUBTASK_2}}

**Traces to:** [REQ-XXX]  
**Depends on:** [TASK-001]  
**Blocks:** [TASK-XXX]

---

### TASK-003: {{TASK_NAME}}
🟠 P1 | ⬜ TODO | Est: {{X}}d

**Description:**  
{{TASK_DESCRIPTION}}

**Checklist:**
- [ ] {{SUBTASK_1}}
- [ ] {{SUBTASK_2}}

**Traces to:** [REQ-XXX]  
**Depends on:** [TASK-001], [TASK-002]  
**Blocks:** —

---

## Milestone 2: Beta

### TASK-010: {{TASK_NAME}}
🟠 P1 | ⬜ TODO | Est: {{X}}d

**Description:**  
{{TASK_DESCRIPTION}}

**Checklist:**
- [ ] {{SUBTASK_1}}
- [ ] {{SUBTASK_2}}

**Traces to:** [REQ-XXX]  
**Depends on:** [TASK-XXX]  
**Blocks:** —

---

### TASK-011: {{TASK_NAME}}
🟡 P2 | ⬜ TODO | Est: {{X}}d

**Description:**  
{{TASK_DESCRIPTION}}

**Checklist:**
- [ ] {{SUBTASK_1}}
- [ ] {{SUBTASK_2}}

**Traces to:** [REQ-XXX]  
**Depends on:** [TASK-XXX]  
**Blocks:** —

---

## Milestone 3: GA

### TASK-020: {{TASK_NAME}}
🟡 P2 | ⬜ TODO | Est: {{X}}d

**Description:**  
{{TASK_DESCRIPTION}}

**Checklist:**
- [ ] {{SUBTASK_1}}
- [ ] {{SUBTASK_2}}

**Traces to:** [REQ-XXX]  
**Depends on:** [TASK-XXX]  
**Blocks:** —

---

### TASK-021: {{TASK_NAME}}
🟢 P3 | ⬜ TODO | Est: {{X}}d

**Description:**  
{{TASK_DESCRIPTION}}

**Checklist:**
- [ ] {{SUBTASK_1}}

**Traces to:** [REQ-XXX]  
**Depends on:** [TASK-XXX]  
**Blocks:** —

---

## Dependency Graph

```
TASK-001 ({{name}})
    │
    ├──► TASK-002 ({{name}})
    │        │
    │        └──► TASK-003 ({{name}})
    │
    └──► TASK-010 ({{name}})
             │
             └──► TASK-011 ({{name}})
                      │
                      └──► TASK-020 ({{name}})
```

---

## Summary by Milestone

### MVP
| Priority | Count | Est. Total |
|----------|-------|------------|
| 🔴 P0 | {{X}} | {{Y}}d |
| 🟠 P1 | {{X}} | {{Y}}d |
| 🟡 P2 | {{X}} | {{Y}}d |
| **Total** | **{{X}}** | **~{{Y}}d** |

### Beta
| Priority | Count | Est. Total |
|----------|-------|------------|
| 🔴 P0 | {{X}} | {{Y}}d |
| 🟠 P1 | {{X}} | {{Y}}d |
| 🟡 P2 | {{X}} | {{Y}}d |
| **Total** | **{{X}}** | **~{{Y}}d** |

### GA
| Priority | Count | Est. Total |
|----------|-------|------------|
| 🔴 P0 | {{X}} | {{Y}}d |
| 🟠 P1 | {{X}} | {{Y}}d |
| 🟡 P2 | {{X}} | {{Y}}d |
| 🟢 P3 | {{X}} | {{Y}}d |
| **Total** | **{{X}}** | **~{{Y}}d** |

---

## Risk Register

| Risk | Impact | Probability | Mitigation |
|------|--------|-------------|------------|
| {{RISK_1}} | High/Med/Low | High/Med/Low | {{MITIGATION}} |
| {{RISK_2}} | High/Med/Low | High/Med/Low | {{MITIGATION}} |

---

## Notes

- {{NOTE_1}}
- {{NOTE_2}}
