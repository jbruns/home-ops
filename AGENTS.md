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

SSH and API access to each Host comes from the gitignored repo-root `.env`; see `.env.example` for its keys. Only the Operator edits `.env`.

Keep every secret value out of tool output, logs and commits, whether it comes from `.env`, a decrypted SOPS file or a Host. Read a secret file by key name (`sops -d <file> | cut -d= -f1`). Compare values by exit code or hash. Pipe a new secret from a script straight into `sops set` and its consumer. Read TrueNAS only through `hops truenas call`, which refuses and redacts methods that return credentials, and Proxmox VE only through `hops proxmox call`/`batch`, never `pvesh get`, which prints credentials.

### Host access

Reach the Hosts through `hops`, shared Repo Tooling from the Repo Tooling repo (install with
`uv tool install "git+https://github.com/jbruns/homelab-components@hops-v1.4.0#subdirectory=repo-tools/hops"`;
read its README, `repo-tools/hops/README.md` in the Repo Tooling repo at that tag, before first use), with
`--json` when you parse its output. `hosts.yaml` declares two Hosts: `truenas`, the TrueNAS Host, which runs the
Stacks, and `proxmox`, the Proxmox Host, which runs Guests and no Stacks. Stack commands target the TrueNAS Host
without `--host`; pass `--host proxmox` for the Proxmox Host. `hops` changes runtime state only; declared state
changes through a PR (ADR 0001).

**The TrueNAS Host.** For anything `hops --help` lacks, use read-only `ssh … sudo -n docker …`. Reach TrueNAS
through `hops` too (`hops truenas call <method>` for reads it lacks), never with the REST API or `curl` to
`/api/v2.0`. Its Host Configuration is declared in `host/truenas/` (ADR 0015). Change it with a PR. After the PR
merges, apply it with `ssh … sudo -n midclt call <write method> … >/dev/null` and verify the result with
`hops truenas call`. Use `midclt` only to write, never to read: it prints secrets unredacted. Before each batch,
take a Rollback Snapshot of every dataset the batch touches, and ask the Operator for a configuration backup.

**The Proxmox Host.** It is declared in `host/proxmox/` (ADR 0019). It is often off, and that is normal: `hops`
reports it as `off` and nothing alerts. Read it only with `hops host --host proxmox`, `hops strays --host proxmox`
and `hops proxmox call`/`batch`. Change it with a PR. After the PR merges and the Operator agrees, apply it with
`ssh … sudo -n pvesh`/`qm`/`pct`/`vzdump` and verify the result with `hops proxmox call`. Before each batch, run
vzdump of every Kept Guest the batch touches. Never create a Guest without the `scratch` tag unless the PR
declares it.

When an agent runs `hops bump`, pass its co-author line with `--trailer` (see the "bump" section of the hops README).

### Privacy

Prose (Markdown, code comments, docstrings, CLI help and output, templates) names roles, never repositories,
people, logins, hostnames or domains (ADR 0013): "this repo", "the Repo Tooling repo", "a sibling deployment repo",
"the Operator", "the Host", `<domain>`. Load-bearing identifiers (paths, `hosts.yaml` keys, `.env` key names,
`uses:` refs, config values) stay. Refer to issues as bare `#N`.
