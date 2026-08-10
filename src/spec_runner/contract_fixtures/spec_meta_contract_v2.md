---
traces_to:
- design
- REQ-001
upstream_hashes:
  design: 8b3e7d0a1c2f4e6b8d0a2c4e6f8a0b2d4e6f8a0b
spec_stage: tasks
status: approved
version: 3
generated_by: claude@claude-opus-5
generated_at: '2026-07-26T00:00:00Z'
source_prompt_version: sha256:0000000000000000
validation: pass
approved_by: andrei
approved_at: '2026-07-26T00:00:00Z'
owner_role: platform
---
# Golden fixture for SpecMeta contract v2

Consumers (steward) parse this file and compare against the documented field
table in docs/CONTRACTS.md. Extras appear first, canonical fields last —
that ordering is part of the render contract.

The two governance extras show the shapes spec-runner writes since DEC-008:
`traces_to` is a LIST (the direct upstream stage, then ids that resolve in it)
and `upstream_hashes` maps the DIRECT upstream stage to a full git blob hash —
the value `git hash-object <file>` prints for the bytes that were approved.
