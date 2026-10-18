# agent-skills

Installs the agent skills pinned in [`.agent-skills.lock`](../../.agent-skills.lock) into the
gitignored `.agents/skills/`. Repo Tooling: it never runs on the Host.

## Run

```sh
tools/agent-skills/setup-agent-skills          # install or repair
tools/agent-skills/setup-agent-skills check    # offline check that the install matches the lock
tools/agent-skills/setup-agent-skills pin REF  # move the pin
tools/agent-skills/setup-agent-skills clean    # remove managed skills
```

Needs Bash 3.2+, Git 2.25+ and Python 3.

## Configuration

`.agent-skills.lock` selects the source, ref, revision and `include`/`exclude` entries.
`AGENT_SKILLS_LOCK=/absolute/path` selects an alternate lock. See
[docs/agents/skills.md](../../docs/agents/skills.md) for details and recovery.

## Test

```sh
tools/agent-skills/test-setup-agent-skills
```

Uses throwaway local Git repositories; no network access.
