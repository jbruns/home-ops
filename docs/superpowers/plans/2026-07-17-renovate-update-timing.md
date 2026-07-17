# Renovate Update Timing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent Renovate updates from remaining indefinitely pending while retaining the five-day cooldown where release timestamps are available.

**Architecture:** Adjust the repository-level Renovate policy for timestamp-less releases, preserve timestamps in the Portainer custom datasource, and widen the weekly branch-creation schedule. Validate both the static configuration and the transformed Portainer release data.

**Tech Stack:** Renovate JSON configuration, JSONata custom datasource transform, Node.js assertions, Renovate config validator

---

### Task 1: Update Renovate timing policy

**Files:**
- Modify: `renovate.json:6-8`
- Modify: `renovate.json:59-61`

- [ ] **Step 1: Verify the current configuration lacks the required behavior**

Run:

```bash
node -e 'const c=require("./renovate.json"); if(c.minimumReleaseAgeBehaviour!=="timestamp-optional") throw new Error("timestamp behavior not configured"); if(c.schedule?.[0]!=="* 10-13 * * 5") throw new Error("schedule not widened");'
```

Expected: FAIL with `timestamp behavior not configured`.

- [ ] **Step 2: Configure release-age behavior and schedule**

Add the following beside `minimumReleaseAge`:

```json
"minimumReleaseAgeBehaviour": "timestamp-optional",
"schedule": ["* 10-13 * * 5"],
```

- [ ] **Step 3: Preserve the Portainer release timestamp**

Change the Portainer transform to emit:

```json
{ "version": $v.tag_name, "releaseTimestamp": $v.published_at, "sourceUrl": "https://github.com/portainer/portainer", "changelogUrl": $v.html_url }
```

- [ ] **Step 4: Verify the intended configuration values**

Run:

```bash
node -e 'const c=require("./renovate.json"); if(c.minimumReleaseAgeBehaviour!=="timestamp-optional") process.exit(1); if(c.schedule?.[0]!=="* 10-13 * * 5") process.exit(1); if(!c.customDatasources["portainer-lts"].transformTemplates[0].includes("\"releaseTimestamp\": $v.published_at")) process.exit(1);'
```

Expected: exit code 0.

- [ ] **Step 5: Validate the Renovate configuration**

Run:

```bash
npx --yes --package renovate renovate-config-validator renovate.json
```

Expected: `Config validated successfully`.

- [ ] **Step 6: Commit the implementation**

```bash
git add renovate.json
git commit -m "fix: unblock timely Renovate updates" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```
