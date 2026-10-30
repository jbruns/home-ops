# Owned Applications live in their own repos, without a Retirement Condition

[ADR 0008](0008-custom-components-in-shared-repo.md) puts every piece of code we wrote that runs on the Host in the Repo Tooling repo, each with a Retirement Condition. That suits small gap-fillers (exporters, hooks) but not an application we mean to keep as a product: Fuzzbin already lives in its own repo, and the planned Title Select (#734) is a full application with its own toolchain (Rust alongside other languages), database, web UI and release cadence. We decided that such an **Owned Application** lives in its own repo and states its **Why** but no **Retire when**. Everything else ADR 0008 requires still holds: it ships as an image `ghcr.io/<owner>/<application>` consumed by digest and bumped by Renovate, takes every site-specific value from its environment so another host can adopt it, is tested test-first with Contract Tests against the upstreams it calls, and meets the Baseline like any other Service. Its deployment declarations stay in the deploying repo.

## Considered Options

- **A Component folder in the Repo Tooling repo**: rejected; that repo's shared CI, base image, test harness and healthcheck rules assume small stdlib-only Python Components, and every other Component would carry the cost of a second toolchain.
- **Keep it a Custom Component with a token Retirement Condition**: rejected; it would hollow out the term, which means the code is kept only until an upstream covers the need.
- **Split it, with the engine in the Repo Tooling repo and the application elsewhere**: rejected; one product would release across two repos.
