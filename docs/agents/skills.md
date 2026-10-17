# Pinned agent skills

## Install

From the repository root, with Bash 3.2+, Git 2.25+ (sparse-checkout support),
Python 3, and access to the upstream Git repository:

```sh
scripts/setup-agent-skills
scripts/setup-agent-skills check
```

Start a fresh agent session after installing or changing skills so the agent
discovers them. Setup is local development tooling; it needs no Host access,
Docker, or secrets and does not deploy anything.

`.agent-skills.lock` is the source of truth: `source` selects the upstream Git
repository, `ref` names its release or commit, and `revision` pins the full commit
SHA. Setup rejects a ref that no longer resolves to that SHA. The repository
tracks the lock and installer, not the upstream skill contents or installation
state; both live in the gitignored `.agents/skills/`.

The installed set comes from the pinned source's `.claude-plugin/plugin.json`
`skills` array. Paths must be under `skills/`; each directory installs by its
final segment (`engineering/tdd` becomes `.agents/skills/tdd`). Supporting files
inside each skill directory are included. Providers without that manifest need
explicit `include` entries.

## Change the pin or selection

```sh
scripts/setup-agent-skills pin <upstream-tag-or-commit>
scripts/setup-agent-skills check
git diff -- .agent-skills.lock
```

Review the upstream changes before adopting a release, then commit the lock
change. `pin` resolves the ref, installs it, and updates the lock only after
installation succeeds. Skills added or removed in the upstream manifest follow
the new pin.

To customize the set, edit the lock and rerun setup:

```text
exclude wizard
exclude engineering/tdd
include misc/setup-pre-commit
```

A bare exclusion matches the final name in any category; a path matches exactly.
An unmatched exclusion warns, a missing inclusion fails, and colliding installed
names fail. To change providers, edit `source`, then run `pin` with a ref from
that provider. `AGENT_SKILLS_LOCK=/absolute/path/to/lock` selects an alternate lock;
it still manages this clone's `.agents/skills/`.

## Check and recover

`scripts/setup-agent-skills check` works offline: it compares the lock settings
and hashes every managed skill, including supporting files. Rerun setup to repair
missing or changed files. Setup replaces local edits to managed skills and removes
previously managed skills no longer selected. Keep custom work outside managed
skill directories; an unmanaged directory with a selected name blocks setup.

```sh
scripts/setup-agent-skills clean
```

Clean removes only skills listed in the local management state, preserving
unmanaged skills and the tracked lock. Keep that state file for safe cleanup.
If the state is lost, move the conflicting skill directories aside before
reinstalling; the helper will not claim unmanaged directories.

## Installer maintenance

```sh
scripts/test-setup-agent-skills
```

The regression suite uses temporary local Git repositories without network
access. CI runs it as part of the required `validate` check. CI does not install
upstream workflows or require a developer's ignored installation.
