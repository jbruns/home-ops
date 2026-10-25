## Agent skills

Before repository work, run `tools/agent-skills/setup-agent-skills check`. If it fails,
run `tools/agent-skills/setup-agent-skills`, then start a fresh agent session to discover
the pinned skills. Invoke applicable installed skills before proceeding.
For installation, pin updates, or recovery, read `docs/agents/skills.md`.

### Issue tracker

Issues and specs live in this repo's GitHub Issues. Before reading or publishing tickets, read `docs/agents/issue-tracker.md`.

### Triage labels

Use the five default triage labels. Before triaging issues, read `docs/agents/triage-labels.md`.

### Domain docs

Single-context: root `GLOSSARY.md` and `docs/adr/`. Before exploring the codebase, read `docs/agents/domain.md`.

### Secrets

SSH and API access to the Host comes from the gitignored repo-root `.env`; see `.env.example` for its keys. Only the Operator edits `.env`.

Keep every secret value out of tool output, logs and commits, whether it comes from `.env`, a decrypted SOPS file or the Host. Read a secret file by key name (`sops -d <file> | cut -d= -f1`). Compare values by exit code or hash. Pipe a new secret from a script straight into `sops set` and its consumer. Read TrueNAS only through `hops truenas call`, which refuses and redacts methods that return credentials.

### Host access

Reach the Host through `hops`, shared Repo Tooling from the Repo Tooling repo (install with
`uv tool install "git+https://github.com/jbruns/homelab-components@hops-v1.1.1#subdirectory=repo-tools/hops"`;
read its README, `repo-tools/hops/README.md` in the Repo Tooling repo at that tag, before first use), with
`--json` when you parse its output. `hops` targets the only Host `hosts.yaml` declares. It changes runtime state
only; declared state changes through a PR (ADR 0001). For anything `hops --help` lacks, use read-only
`ssh … sudo -n docker …`. Reach TrueNAS through `hops` too (`hops truenas call <method>` for reads it lacks),
never with the REST API or `curl` to `/api/v2.0`.

When an agent runs `hops bump`, pass its co-author line with `--trailer` (see the "bump" section of the hops README).

### Privacy

Prose (Markdown, code comments, docstrings, CLI help and output, templates) names roles, never repositories,
people, logins, hostnames or domains (ADR 0013): "this repo", "the Repo Tooling repo", "a sibling deployment repo",
"the Operator", "the Host", `<domain>`. Load-bearing identifiers (paths, `hosts.yaml` keys, `.env` key names,
`uses:` refs, config values) stay. Refer to issues as bare `#N`.
