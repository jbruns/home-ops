## Agent skills

Before repository work, run `tools/agent-skills/setup-agent-skills check`. If it fails,
run `tools/agent-skills/setup-agent-skills`, then start a fresh agent session to discover
the pinned skills. Invoke applicable installed skills before proceeding.
For installation, pin updates, or recovery, read `docs/agents/skills.md`.

### Issue tracker

Issues and specs live in GitHub Issues for `jbruns/home-ops`. Before reading or publishing tickets, read `docs/agents/issue-tracker.md`.

### Triage labels

Use the five default triage labels. Before triaging issues, read `docs/agents/triage-labels.md`.

### Domain docs

Single-context: root `GLOSSARY.md` and `docs/adr/`. Before exploring the codebase, read `docs/agents/domain.md`.

### Host access

Host access settings (`TRUENAS_*`, `HOPS_*`) come from the gitignored repo-root `.env`; `.env.example` lists every key, its purpose and its consumer. Never print, log or commit its values, and never edit `.env` (only @jbruns does). Secrets a Stack needs go in SOPS, not `.env`.
