# Repo Tooling is shared, repo- and Host-agnostic, and lives in the Repo Tooling repo

A sibling deployment repo is adopting this repo's structure for its own Host: doco-cd Deploys, the Baseline with Exemptions, Stack per app, and the same CI. It needs the same Repo Tooling (`hops`, `validate.py`, declared-containers, the CI workflows, Renovate rules and agent-skills setup). That tooling assumed one TrueNAS Host, a `tn/` stack root and this repo. [ADR 0008](0008-custom-components-in-shared-repo.md) covers only code that runs on the Host, so this extends it to Repo Tooling. Shared Repo Tooling lives in the Repo Tooling repo, the shared repo that already holds the Custom Components. Python tools are consumed by pinned tag or commit, and CI by reusable workflows and a Renovate preset pinned the same way. Renovate bumps them like any other pin. Tooling written for one repo only stays under that repo's `tools/`.

Every tool takes what is specific to a repo from that repo:

- **The repo's Hosts.** A committed `hosts.yaml` at the repo root declares each Host's name, stack root, deployer (`doco-cd`) and capabilities (such as `truenas` or `zfs`). A Host the file does not declare is out of the tools' reach: that sibling repo's other Host is simply left out.
- **Connection details.** These stay in the gitignored `.env`, keyed by Host name.
- **The target Host.** A command picks its Host with `--host`, from the stack path, or as the repo's only Host.
- **Host-specific features.** Snapshots and TrueNAS calls are gated on the Host's capabilities, never assumed.

The tool owns the list of Baseline items, so "Baseline" means the same thing in every repo that uses it. Each Host's stack root holds its own `_baseline.yaml` with the values, and a repo can waive an item only per Service, through an Exemption.

This repo moves to the shared tooling first, with no change in behaviour. Its existing tests and CI prove the extraction is faithful before a second repo depends on it.

## Considered Options

- **Copy the tooling into each repo**: rejected; fixes diverge, which is what ADR 0008 rejected copying Custom Components for.
- **This repo stays upstream, and other repos pin its tooling**: rejected; other repos would depend on this repo's history and release cadence, which ADR 0008 ruled out.
- **A new repo just for shared tooling**: rejected; it duplicates the pinning, release and Renovate flow the Repo Tooling repo already has.
- **Each repo chooses its own Baseline items**: rejected; the Baseline would stop being one enforced minimum.
- **Find Hosts by directory convention, probing capabilities at runtime**: rejected; it is implicit, CI cannot read it, and it cannot leave a Host out deliberately.
