# Curated Answer Library — Plan

**Status:** on hold (decided 2026-10-02). Nothing below is built yet.

## Why

Today the drafter reads raw history only: every past answer, as written. That causes three problems:

- **Conflicts leak into drafts.** When past answers to the same requirement disagree (e.g. WCAG 2.1 AA: Vero Beach "Standard", Dayton "Not supported"), the AI has to guess between them.
- **Inconsistent comments.** Whether a row gets a comment depends on whether the closest past answers happened to have one.
- **Stale answers.** Nothing marks an answer as outdated after the product changes.

The library holds **one approved answer per recurring requirement**. The drafter would trust it ahead of raw history.

## Where it fits

```
Completed worksheets ──► Raw history (built)               ──┐
                         every past answer, as written       │  backup evidence
                                                             ▼
                         Curated library (planned)        ──► Drafter ──► Review ──► Submitted worksheet
                         one approved answer per                  ▲                         │
                         recurring requirement ───────────────────┘                         │
                              ▲                                                             │
                              └──────── reviewers' corrections / approvals ◄────────────────┘
```

## Library entry

| Field | Purpose |
|---|---|
| `canonical_id` | Stable ID (raw records already have an empty `canonical_id` column for this link) |
| `requirement` | Standard wording, e.g. "Supports SSO via SAML 2.0 / Azure Entra ID" |
| `status` | Approved answer on the internal scale (`kb/statuses.py`); the drafter converts it to each worksheet's own scale |
| `comment` | Approved comment, or an explicit "no comment needed" |
| `conditions` | e.g. "Enterprise tier only", "via Paymentus" |
| `module` | CIS / ERP / MDM / CEP … |
| `linked_records` | The raw past answers this entry was built from (traceability) |
| `owner` | Person or role responsible for keeping it accurate |
| `last_verified`, `product_version` | Re-check answers as the product changes; flag stale entries |
| `client_specific` | Deal-specific answers that must not be reused |
| `approved_by`, `approved_at` | Audit trail |

Store it in the knowledge-base database alongside `records`, plus an embedding per entry for search.

## How drafting changes

1. For each row, search the library first (same hybrid search and parent-text handling as today).
2. A close match to an approved entry becomes the primary evidence, labelled as approved in the prompt. The row can be **high confidence** without the similarity cap.
3. Raw history still goes in as supporting evidence, and is the only evidence when there's no library match.
4. The approved entry decides whether a comment belongs and what it says.
5. Expired or stale entries (past `last_verified`) count as ordinary evidence, not approved answers.

## How entries get created

1. **Automatic grouping.** Group near-identical requirements across worksheets (cosine ≥ ~0.92) and propose a master answer: the newest, or the majority. A person approves or edits it.
2. **Conflicts.** Each pair on the Overview page's *Conflicting answers* list becomes a library decision.
3. **Review loop (biggest source over time).** A *Save as approved answer* checkbox in the drafting review grid turns a reviewer's correction into a library entry.

## Baseline (measured 2026-10-02, 7,136 answered requirements, 7 worksheets)

- They group into **5,255** distinct requirements at 0.92 similarity.
- **815** groups were asked by 2+ clients (1,788 rows); almost all by exactly 2 (mainly Frankfort ↔ Kansas City and Dayton ↔ Vero Beach).
- **16** of those groups have disagreeing answers (yes vs. qualified vs. no).
- **147** of those groups have inconsistent comments.

Most requirements have appeared only once so far, so automatic grouping alone yields a small library. Its value grows as more past worksheets are loaded.

## Build steps (when resumed)

1. **Schema + storage:** `library` table and its embeddings; link `records.canonical_id`.
2. **Library page in the dashboard:** review queue in priority order: conflicts → requirements asked by several clients → reviewer-flagged items. Approve / edit / reject, plus search and browse.
3. **Review-grid hook:** a *Save as approved answer* checkbox on the drafting page.
4. **Drafter integration:** library-first retrieval, approved-evidence labelling, the confidence rule, comment policy from the entry.
5. **Upkeep:** stale-entry report (by `last_verified` / product version) and an owner per module.
6. **Re-run the held-out Dayton test** to measure the improvement (baseline: 349/382 rows matched; high 67/67, medium 230/239, low 52/76).

## Open decisions

- **Who approves entries:** anyone on the bid team, or designated owners per module (CIS, ERP, MDM…) with a sign-off step?
- **Master-answer proposal rule:** newest answer, majority answer, or an AI-merged draft for a person to edit?
- **Re-verification interval** for approved answers (e.g. every 12 months or every major release).
- **Should client-specific answers** be stored in the library (flagged) or kept out entirely?
