# Prose names roles, never repositories, people or Hosts

This repo is public, and its prose had come to name the Operator, their logins, the Host's hostname and domains, and the other repositories it works with. None of that is needed to read or run the repo, and together it maps out one person's infrastructure. Prose (Markdown, code comments, docstrings, CLI help and output, and templates) therefore names roles instead: "this repo", "the Repo Tooling repo", "a sibling deployment repo", "the Operator", "the Host" or "another Host", and `<domain>` for a public service domain. Issue references are bare `#N`. Tool output states the rule it enforces rather than an ADR number; comments may cite this repo's own ADRs.

Load-bearing identifiers stay as they are, because renaming them would break something: paths, `hosts.yaml` keys, `.env` key names, config values, `uses:` refs, `.sops.yaml` recipients, Renovate config, the LICENSE, install commands that must name the repository they install from, and test data. A load-bearing identifier the privacy check would flag is exempted by a narrow allowlist pattern in `tools/validate/privacy-allow`.

CI enforces the rule with the shared `privacy` check, in fail mode. Its denylist of names lives only in a repository secret, never in the repo, since committing it would publish the very names it protects.

## Considered Options

- **Commit the denylist**: rejected; it would list every name the rule keeps out.
- **Rewrite history to remove the names**: rejected; it breaks every clone and link for little gain, since the history is already public.
- **Generalize config values too** (Grafana datasource names, image owners, the doco-cd poll URL): rejected; they are load-bearing, and renaming them changes runtime state for no reader's benefit.
