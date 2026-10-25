# Custom Components live in a shared repo, ship as images, and justify their existence

Code we wrote that runs on the Host (exporters, hook scripts, startup-order) had grown inside Stack folders: bind-mounted into stock images, untested, unchecked against the upstream APIs it calls, and with no stated reason to exist or to go away. A sibling deployment repo needs much of the same code. Each Custom Component now lives in a shared repo (since ADR 0012 also the Repo Tooling repo), one folder per Component, and ships as an image `ghcr.io/<owner>/<component>` built from a minimal base and consumed by digest, so Renovate updates it like any other image and the Baseline applies unchanged. A Component that must run on the Host itself rather than in a container (startup-order) ships instead as a versioned release asset, which this repo pins by tag and sha256 and Renovate bumps the same way. A Component takes every site-specific value from its environment, so another host can adopt it as-is. Its README states **Why** (the gap no upstream or community option covers) and **Retire when** (its Retirement Condition); CI rejects a README missing either.

Every Component is tested, test-first for new behaviour. A Contract Test runs it against each upstream at the version this repo pins and gates merges, including Renovate bumps of that upstream. A weekly Canary runs the same tests against the newest stable upstream release and opens an issue on failure, warning ahead of a bump without blocking anything. Calls to the Docker Engine API pin an API version in the path so both checks mean something. Repo Tooling (code that runs only in CI or on a contributor's machine) stays in this repo under `tools/`, documented and tested, but needs no Retirement Condition.

## Considered Options

- **A `components/` folder in this repo, published from here**: rejected; a sibling deployment repo would depend on this repo's history, CI and SOPS keys, and Components would share this repo's release cadence.
- **Keep bind-mounting source into stock images**: rejected; nothing gates the code on tests, and the code cannot be versioned or reused apart from a Stack.
- **Copy the code into each repo**: rejected; fixes diverge.

Extended to shared Repo Tooling by [ADR 0012](0012-shared-repo-tooling.md).
