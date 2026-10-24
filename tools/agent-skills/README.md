# agent-skills

Installs the agent skills pinned in [`.agent-skills.lock`](../../.agent-skills.lock) into the
gitignored `.agents/skills/`. Repo Tooling: it never runs on the Host.

`setup-agent-skills` here is a thin wrapper around the shared `setup-agent-skills` from
jbruns/homelab-components ([README](https://github.com/jbruns/homelab-components/blob/agent-skills-v1.0.0/repo-tools/agent-skills/README.md),
ADR 0012), pinned by commit with its tag beside it; Renovate bumps both. It runs the tool with `uvx`, so it
needs [uv](https://docs.astral.sh/uv/), Git 2.25+ and network access on first use (uv caches it afterwards).

## Run

```sh
tools/agent-skills/setup-agent-skills          # install or repair
tools/agent-skills/setup-agent-skills check    # offline check that the install matches the lock
tools/agent-skills/setup-agent-skills pin REF  # move the pin
tools/agent-skills/setup-agent-skills clean    # remove managed skills
```

## Configuration

`.agent-skills.lock` selects the source, ref, revision and `include`/`exclude` entries.
`AGENT_SKILLS_LOCK=/absolute/path` selects an alternate lock. See
[docs/agents/skills.md](../../docs/agents/skills.md) for details and recovery.

## Test

The tool's tests live with it in homelab-components.
