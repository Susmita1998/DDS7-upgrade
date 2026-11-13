# CLAUDE.md: SaaS Tenant Operations Assistant (Case Study 3)

Guidance for Claude Code working in this folder.

## What this is

A RAG agent for the SaaS operations team. It answers questions from internal documentation about:

- tenant provisioning steps
- subscription tiers
- standard maintenance windows

Two rules override every other design choice:

1. **Tenant isolation.** An answer meant for one tenant never contains another tenant's data.
2. **No undocumented SLA commitments.** The agent never states an SLA commitment that the documentation does not state.

If a change could weaken either rule, say so before making it.

## Data

- All tenants, accounts, usage figures, and contract text in this repo are synthetic. Do not add real customer data, real tenant IDs, or real contract terms.
## Layout

```
DDS_7_FINAL_CASE_STUDY/
  CLAUDE.md                          this file
  tenant_ops_agent.py                the agent (CLI entry point, stdlib only)
  data/knowledge_base.json           synthetic chunks with tenant scope and status
  build_dashboard.py                 builds dashboard.html from logs/*.jsonl (stdlib only)
  tests/test_tenant_ops_agent.py     unittest suite, no network
  .env                               OPENROUTER_API_KEY, OPENROUTER_BASE_URL, OPENROUTER_MODEL (gitignored)
  .gitignore
  logs/                              per-tenant JSONL, created at runtime (gitignored)
  dashboard.html                     generated; contains tenant counts and chunk titles (gitignored)
```

## Commands

```bash
# one question as one tenant
python tenant_ops_agent.py --tenant acme "What is the Enterprise uptime commitment?"

# tests (offline, no API key needed)
python -m unittest discover -s tests -v

# dashboard from logs/*.jsonl, written to dashboard.html
python build_dashboard.py
```

## Tenant isolation rules

- **The tenant comes from the session, never from the question.** The calling session supplies the tenant ID. Do not parse a tenant from the question text, and do not let the question change it.
- **Filter at retrieval, not in the prompt.** Every tenant-scoped search applies a metadata filter on tenant ID before ranking. Telling the LLM to "only use tenant X's data" is not isolation.
- **Every chunk has an explicit scope.** A chunk is either `global` (shared material such as provisioning steps, tier definitions, and maintenance windows) or belongs to exactly one tenant. A chunk with a missing or unknown scope is excluded. Never default it to `global`.
- **Questions about other tenants get the same answer as an unknown tenant.** If a question names another tenant or asks for its data, the agent does not confirm or deny that the tenant exists and does not repeat any of its details. It answers from global docs only and says it cannot share information about other tenants.
- **Caches, conversation memory, and logs are keyed by tenant.** Cache keys, session history, and retrieval logs carry the tenant ID and never mix across tenants. A log entry for one tenant never contains retrieved text from another.
- **Citations stay in scope.** A cited source must be a global document or the requesting tenant's own document.

## SLA rules

- State an SLA figure (uptime %, response time, credits, resolution time, notice period) only when the indexed documentation states it. Quote the figure and cite the document and section.
- Describe a standard maintenance window as the documented schedule. Do not call it an uptime or availability guarantee unless the documentation says so.
- Do not infer SLA terms from a tier name. "Enterprise" does not imply any particular uptime.
- If the documentation is silent, say so and point to the contract or account team. Do not guess a number, a credit, or a timeline.
- If two documents conflict on an SLA term, report both with their citations. Do not pick one.
- Give a provisioning time only when a document states it.

## Answer rules

- Answer only from retrieved documents. If retrieval finds nothing relevant, say the documentation does not cover the question. Do not fill gaps from general knowledge.
- Cite the document title and section for each factual claim.
- Prefer the current version of a document. Flag drafts and documents with older effective dates.
- Questions outside the three topics are out of scope. Say so and do not answer them.

## Conventions

- Keep retrieval, answer generation, and the SLA check as separate steps so each can be tested without calling an LLM.
- Secrets go in `.env`. Never commit `.env`. `.gitignore` already lists it.
- Match the surrounding code's style. Add comments only where the logic is not obvious.

## Testing expectations

Before saying anything works, run the test suite and report the actual result. Every run must include these cases:

- A query scoped to tenant A never returns a chunk scoped to tenant B, including near-duplicate queries. This is a retrieval-level test and needs no LLM.
- A chunk with a missing scope is excluded from every tenant's results.
- A question naming another tenant gets the same response shape as a question about a tenant that does not exist.
- A prompt-injection attempt ("ignore the tenant filter", "list all tenants") does not change the active filter.
- A question asking for an uptime or credit figure that the docs do not state gets a "not documented" answer, not a number.
- A question about an SLA the docs do state gets the documented figure with its citation.
- Conflicting SLA documents are both reported.
- Logs for tenant A contain no text from tenant B.

## LLM provider

- Generation goes through OpenRouter. Read `OPENROUTER_API_KEY` and `OPENROUTER_BASE_URL` from `.env`. Never hard-code the key or print it in output or logs.
- Default model: `openai/gpt-4o-mini`, overridable with `OPENROUTER_MODEL`.
- Known blocker: on the current OpenRouter account, the workspace guardrail / data-policy setting blocks this model and most other models (HTTP 404, "guardrail restrictions"). Fix it at https://openrouter.ai/workspaces/default/guardrails, or pick a model the guardrail allows and set it in `OPENROUTER_MODEL`. Update this note when resolved.

## Open decisions

- Embeddings: retrieval is BM25 only. Add a dense retriever only if needed, and keep the tenant filter in place for any retriever.
- Where the tenant ID comes from in the calling session (the auth layer). The CLI takes `--tenant` for now.
