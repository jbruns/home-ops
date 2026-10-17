# Issue tracker: GitHub

Issues and specs live in GitHub Issues for `jbruns/home-ops`. Use the `gh` CLI; it infers the repository from the clone's remote.

## Conventions

- Create: `gh issue create --title "..." --body-file <file>`.
- Read: `gh issue view <number> --comments`; fetch labels with `gh issue view <number> --json labels`.
- List: `gh issue list --state open --json number,title,body,labels,comments`, with appropriate label and state filters.
- Comment: `gh issue comment <number> --body-file <file>`.
- Label: `gh issue edit <number> --add-label "..."` / `--remove-label "..."`.
- Close: `gh issue close <number>`; publish any explanation as a comment first.

For issue and PR bodies or comments, write text to a temporary file using a quoted heredoc, pass `--body-file`, then remove the temporary file.

When a skill says "publish to the issue tracker", create a GitHub issue. When it says "fetch the relevant ticket", run `gh issue view <number> --comments`.

## Pull requests as a triage surface

**PRs as a request surface: no.** Set to `yes` to include external PRs in triage.

When enabled, use `gh pr view <number> --comments` and `gh pr diff <number>` to read; use `gh pr comment`, `gh pr edit`, and `gh pr close` for updates. List with `gh pr list --state open --json number,title,body,labels,author,authorAssociation,comments`, keeping only `CONTRIBUTOR`, `FIRST_TIME_CONTRIBUTOR`, or `NONE` author associations.

GitHub shares issue and PR numbers: resolve an ambiguous reference with `gh pr view <number>`, falling back to `gh issue view <number>`.

## Wayfinding operations

For `/wayfinder`, the map is one issue with child issues as tickets.

- Map: label `wayfinder:map`; body holds Notes, Decisions-so-far, and Fog.
- Child: link as a GitHub sub-issue using `gh api`. If unavailable, use a task list in the map and `Part of #<map>` in the child. Label `wayfinder:<type>` (`research`, `prototype`, `grilling`, or `task`).
- Blocking: use native issue dependencies: `gh api --method POST repos/<owner>/<repo>/issues/<child>/dependencies/blocked_by -F issue_id=<blocker-db-id>`. Obtain the database ID with `gh api repos/<owner>/<repo>/issues/<number> --jq .id`. If dependencies are unavailable, put `Blocked by: #<number>, #<number>` at the top of the child body.
- Frontier: inspect the map's open children in map order; choose the first with no assignee and no open blockers. Native `issue_dependencies_summary.blocked_by` counts open blockers; for fallback links, check each blocker's state.
- Claim: `gh issue edit <number> --add-assignee @me`, the session's first write.
- Resolve: comment with the answer, close the child, then append a gist and link to the map's Decisions-so-far.
