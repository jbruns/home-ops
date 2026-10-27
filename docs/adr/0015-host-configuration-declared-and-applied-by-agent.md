# The Host Configuration is declared in this repo, and the agent applies it through midclt

The Host's TrueNAS configuration had been edited in the UI since early SCALE releases, with no record of why each setting existed. An audit (#721) found settings that had outlived their purpose: exports open to any host, a home share, leftover datasets, and users whose shells and SMB access contradicted `host/identities.tsv`. Nothing in this repo described what the Host should look like, so nothing could show that it had drifted. We decided that **the Host Configuration is declared in `host/truenas/`, with a stated purpose for every item, and the agent applies it with `midclt` over SSH**. A change to the Host Configuration is a PR to that record, like any other declared state (ADR 0001). Once the PR merges, the agent applies it with `sudo -n midclt call <write method>` and verifies the result with `hops truenas call`.

- **Writes and reads are split.** `midclt` is used only for writes. Its output is discarded; the agent checks the exit status and the job state. Every read goes through `hops truenas call`, which refuses namespaces whose results may hold secrets and redacts credential fields. A `midclt` read prints whatever TrueNAS returns, secrets included, so the agent never uses `midclt` to read.
- **Scope of `sudo`.** The agent's login may run `docker` and `midclt` as root and nothing else. Docker already made the login root-equivalent on the Host, so `midclt` adds a supported, schema-checked way to make changes rather than a new privilege. The limit on which methods the agent calls is this rule and AGENTS.md, not `sudo`.
- **Safety net.** Before each batch of changes, the agent takes a Rollback Snapshot of every dataset it touches. The Operator downloads a TrueNAS configuration backup, keeps it off the Host, and deletes it once the batch is verified. A change that could cut off access, such as a network change, uses TrueNAS's own check-in rollback.

## Considered Options

- **The Operator applies changes in the UI from a runbook**: rejected. Every change becomes a manual step, and checking the result still needs the agent's reads.
- **Write commands in hops**: rejected for now. They would mean a schema-checked wrapper for each method in the Repo Tooling repo, while `midclt` already validates against the middleware's own schema.
- **Restrict `sudo` to an allowlist of `midclt` methods**: rejected. The list would sit in the TrueNAS UI and change with every batch, and it would add nothing while the same login has root-equivalent Docker.
