# Domain Docs

## Before exploring

This repository uses a single-context layout:

- Read root `GLOSSARY.md`.
- Read ADRs in `docs/adr/` relevant to the area being explored.

If a document is absent, proceed silently. `/domain-modeling` creates domain documentation lazily when terms or decisions are resolved.

## Use the glossary's vocabulary

Use glossary terms when naming domain concepts in issues, proposals, hypotheses, and tests. Respect its explicitly avoided synonyms.

If a concept is missing, reconsider whether it belongs to the domain; note genuine gaps for `/domain-modeling`.

## Flag ADR conflicts

Explicitly surface proposals that contradict an existing ADR, naming the ADR and explaining why the decision is worth reopening.
