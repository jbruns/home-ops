"""hops bump: pin a Service's image to a new tag and its index digest, on a branch in a new worktree."""
import json
import os
import stat
import subprocess
import sys
import textwrap

import pytest

from hops_fakes import git

SONARR = "tn/sonarr/compose.yaml"
OLD = "ghcr.io/home-operations/sonarr:4.0.20.3012@sha256:ffff"
DIGEST_5 = "sha256:" + "5" * 64
DIGEST_4 = "sha256:" + "4" * 64
TAGS = ["latest", "4.0.20.3012", "5.0.0.2742", "5.0.0.2701", "5.0.1.2800-develop", "rolling"]
TRAILER = "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
RENOVATE_PR = {
    "number": 42, "title": "chore(deps): update ghcr.io/home-operations/sonarr docker tag to v5.0.0.2742",
    "url": "https://github.com/jbruns/home-ops/pull/42",
    "body": "| Package | Update | Change |\n|---|---|---|\n"
            "| [ghcr.io/home-operations/sonarr](https://redirect.github.com/Sonarr/Sonarr) | major |"
            " `4.0.20.3012` → `5.0.0.2742` |\n",
}
OTHER_RENOVATE_PR = {
    "number": 7, "title": "chore(deps): update traefik docker tag to v3.7.16", "url": "u",
    "body": "| [traefik](https://hub.docker.com/_/traefik) | patch | `v3.7.15` → `v3.7.16` |\n",
}

FAKE_VALIDATE = """\
import json, os, sys
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write(json.dumps({"tool": "validate", "argv": sys.argv[1:], "cwd": os.getcwd()}) + "\\n")
sys.exit(int(os.environ.get("FAKE_VALIDATE_EXIT", "0")))
"""
FAKE_GH = """\
#!{python}
import json, os, sys
stdin = sys.stdin.read() if "-" in sys.argv else None
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write(json.dumps({{"tool": "gh", "argv": sys.argv[1:], "stdin": stdin}}) + "\\n")
if sys.argv[1:3] == ["pr", "list"]:
    print(os.environ.get("FAKE_GH_PRS", "[]"))
elif sys.argv[1:3] == ["pr", "create"]:
    print("https://github.com/jbruns/home-ops/pull/999")
"""


class FakeRegistry:
    def __init__(self, tags, digests):
        self._tags, self._digests, self.calls = tags, digests, []

    def tags(self, repository):
        self.calls.append(("tags", repository))
        return self._tags

    def digest(self, repository, tag):
        self.calls.append(("digest", repository, tag))
        return self._digests[tag]


@pytest.fixture
def registry(monkeypatch):
    from hops.commands import bump

    fake = FakeRegistry(TAGS, {"5.0.0.2742": DIGEST_5, "4.0.20.3012": DIGEST_4})
    monkeypatch.setattr(bump, "Registry", lambda: fake)
    return fake


@pytest.fixture
def checkout(repo, tmp_path, monkeypatch, registry):
    """The fixture repo as a git checkout of main with an origin, a stub validate.py and a fake gh."""
    log = tmp_path / "log.jsonl"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(FAKE_GH.format(python=sys.executable))
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    (repo / "tools/validate").mkdir(parents=True)
    (repo / "tools/validate/validate.py").write_text(FAKE_VALIDATE)
    (repo / ".gitignore").write_text(".worktrees/\n")
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    git(repo, "remote", "add", "origin", str(origin))
    git(repo, "push", "-q", "origin", "main")

    class Checkout:
        root = repo
        remote = origin

        @staticmethod
        def calls(tool):
            if not log.exists():
                return []
            return [c for c in map(json.loads, log.read_text().splitlines()) if c["tool"] == tool]

    return Checkout


def land(repo, message):
    """Commit everything and push it to origin's main, as a merged PR would."""
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    git(repo, "push", "-q", "origin", "main")


def worktree(checkout, tag):
    return checkout.root / ".worktrees" / f"bump-sonarr-{tag}"


def test_bump_pins_the_newest_same_shape_tag_and_its_digest_on_a_branch_in_a_new_worktree(hops, checkout):
    main = git(checkout.root, "rev-parse", "HEAD")
    outcome = hops("bump", "sonarr/sonarr")
    assert outcome.code == 0, outcome.err
    wt = worktree(checkout, "5.0.0.2742")
    assert git(wt, "branch", "--show-current") == "hops/bump-sonarr-5.0.0.2742"
    assert f"image: ghcr.io/home-operations/sonarr:5.0.0.2742@{DIGEST_5}\n" in (wt / SONARR).read_text()
    assert git(wt, "status", "--porcelain") == ""
    assert git(wt, "rev-parse", "HEAD~1") == main
    assert git(wt, "show", "--name-only", "--format=", "HEAD").split() == [SONARR]
    assert str(wt) in outcome.out and "hops/bump-sonarr-5.0.0.2742" in outcome.out


def test_without_pr_nothing_is_pushed_and_the_checkout_is_untouched(hops, checkout):
    head = git(checkout.root, "rev-parse", "HEAD")
    assert hops("bump", "sonarr/sonarr").code == 0
    assert git(checkout.root, "rev-parse", "HEAD") == head
    assert git(checkout.root, "branch", "--show-current") == "main"
    assert git(checkout.root, "status", "--porcelain") == ""
    assert OLD in (checkout.root / SONARR).read_text()
    assert git(checkout.remote, "--git-dir=.", "branch", "--format=%(refname:short)") == "main"
    assert [c["argv"][:2] for c in checkout.calls("gh")] == [["pr", "list"]]


def test_bump_validates_the_worktree_before_committing(hops, checkout):
    assert hops("bump", "sonarr/sonarr").code == 0
    (call,) = checkout.calls("validate")
    assert call["cwd"] == str(worktree(checkout, "5.0.0.2742"))
    assert call["argv"] == ["secrets", "env", "compose"]


def test_a_failed_validation_leaves_the_edit_uncommitted_in_the_worktree(hops, checkout, monkeypatch):
    monkeypatch.setenv("FAKE_VALIDATE_EXIT", "1")
    head = git(checkout.root, "rev-parse", "HEAD")
    outcome = hops("bump", "sonarr/sonarr")
    assert outcome.code == 1
    wt = worktree(checkout, "5.0.0.2742")
    assert "validate.py failed" in outcome.err and str(wt) in outcome.err
    assert git(wt, "rev-parse", "HEAD") == head
    assert git(wt, "status", "--porcelain") == f"M {SONARR}"  # git() strips the leading space


def test_an_explicit_tag_is_pinned_as_given(hops, checkout, registry):
    assert hops("bump", "sonarr/sonarr", "4.0.20.3012").code == 0
    wt = worktree(checkout, "4.0.20.3012")
    assert f"image: ghcr.io/home-operations/sonarr:4.0.20.3012@{DIGEST_4}\n" in (wt / SONARR).read_text()
    assert ("tags", "ghcr.io/home-operations/sonarr") not in registry.calls


@pytest.mark.parametrize("dirt", ["modified", "untracked"])
def test_a_dirty_tree_is_refused_before_anything_else(hops, checkout, registry, dirt):
    if dirt == "modified":
        (checkout.root / SONARR).write_text((checkout.root / SONARR).read_text() + "# wip\n")
    else:
        (checkout.root / "tn/new.yaml").write_text("services: {}\n")
    outcome = hops("bump", "sonarr/sonarr")
    assert outcome.code == 2
    assert "uncommitted changes" in outcome.err
    assert registry.calls == []
    assert not (checkout.root / ".worktrees").exists()
    assert "hops/bump-sonarr" not in git(checkout.root, "branch", "--list")


def test_a_pin_already_current_changes_nothing(hops, checkout, repo):
    text = (repo / SONARR).read_text().replace(OLD, f"ghcr.io/home-operations/sonarr:5.0.0.2742@{DIGEST_5}")
    (repo / SONARR).write_text(text)
    land(repo, "pin")
    outcome = hops("bump", "sonarr/sonarr")
    assert outcome.code == 0
    assert "already pins" in outcome.out
    assert not (checkout.root / ".worktrees").exists()


def test_a_pin_that_is_not_a_version_asks_for_an_explicit_tag(hops, checkout, repo):
    (repo / SONARR).write_text((repo / SONARR).read_text().replace(OLD, "ghcr.io/home-operations/sonarr:rolling"))
    land(repo, "rolling")
    outcome = hops("bump", "sonarr/sonarr")
    assert outcome.code == 2
    assert "'rolling' is not a version tag" in outcome.err and "name a tag" in outcome.err


def test_a_registry_without_the_pinned_tag_never_proposes_a_downgrade(hops, checkout, registry):
    registry._tags = ["4.0.19.2900", "latest"]
    assert hops("bump", "sonarr/sonarr").code == 0
    assert f"sonarr:4.0.20.3012@{DIGEST_4}" in (worktree(checkout, "4.0.20.3012") / SONARR).read_text()


def test_a_leftover_branch_is_refused_with_how_to_discard_it(hops, checkout):
    git(checkout.root, "branch", "hops/bump-sonarr-5.0.0.2742")
    outcome = hops("bump", "sonarr/sonarr")
    assert outcome.code == 2
    assert "git branch -D hops/bump-sonarr-5.0.0.2742" in outcome.err


def test_every_service_in_the_stack_pinning_the_same_image_moves_together(hops, checkout, repo):
    (repo / SONARR).write_text((repo / SONARR).read_text() + textwrap.indent(textwrap.dedent(f"""\
        sonarr-cron:
          image: {OLD}  # same image, run as a job
          labels: {{tier: info}}
    """), "  "))
    other = "tn/radarr/compose.yaml"
    (repo / other).parent.mkdir()
    (repo / other).write_text(f"services:\n  radarr-sonarr:\n    image: {OLD}\n")
    land(repo, "cron")
    assert hops("bump", "sonarr/sonarr").code == 0
    wt = worktree(checkout, "5.0.0.2742")
    text = (wt / SONARR).read_text()
    assert OLD not in text
    assert f"image: ghcr.io/home-operations/sonarr:5.0.0.2742@{DIGEST_5}  # same image" in text
    assert OLD in (wt / other).read_text(), "another Stack's pin needs its own bump"


def test_trailer_is_added_to_the_commit(hops, checkout):
    assert hops("bump", "sonarr/sonarr", "--trailer", TRAILER).code == 0
    message = git(worktree(checkout, "5.0.0.2742"), "log", "-1", "--format=%B")
    assert message.splitlines()[0] == "chore(deps): bump ghcr.io/home-operations/sonarr to 5.0.0.2742"
    assert message.splitlines()[-1] == TRAILER


def test_an_open_renovate_pr_for_the_image_is_reported(hops, checkout, monkeypatch):
    monkeypatch.setenv("FAKE_GH_PRS", json.dumps([OTHER_RENOVATE_PR, RENOVATE_PR]))
    outcome = hops("bump", "sonarr/sonarr")
    assert outcome.code == 0
    assert "Renovate already has #42" in outcome.err
    assert "#7" not in outcome.err


def test_pr_pushes_the_branch_and_opens_a_pr_that_supersedes_renovates(hops, checkout, monkeypatch):
    monkeypatch.setenv("FAKE_GH_PRS", json.dumps([RENOVATE_PR]))
    outcome = hops("bump", "sonarr/sonarr", "--pr")
    assert outcome.code == 0, outcome.err
    branch = "hops/bump-sonarr-5.0.0.2742"
    assert git(checkout.remote, "--git-dir=.", "rev-parse", branch) == git(worktree(checkout, "5.0.0.2742"), "rev-parse", "HEAD")
    create = checkout.calls("gh")[-1]
    argv = create["argv"]
    assert argv[:2] == ["pr", "create"]
    assert argv[argv.index("--base") + 1] == "main" and argv[argv.index("--head") + 1] == branch
    assert argv[argv.index("--body-file") + 1] == "-"
    assert argv[argv.index("--title") + 1] == "chore(deps): bump ghcr.io/home-operations/sonarr to 5.0.0.2742"
    assert "Supersedes #42" in create["stdin"]
    assert f"ghcr.io/home-operations/sonarr:5.0.0.2742@{DIGEST_5}" in create["stdin"]
    assert "https://github.com/jbruns/home-ops/pull/999" in outcome.out


def test_a_stray_tag_more_than_one_major_ahead_is_refused_before_any_worktree(hops, checkout, registry):
    registry._tags = [*TAGS, "20.04.1.0"]
    outcome = hops("bump", "sonarr/sonarr")
    assert outcome.code == 2
    assert "20.04.1.0" in outcome.err and "5.0.0.2742" in outcome.err
    assert "hops bump sonarr/sonarr 5.0.0.2742" in outcome.err
    assert not any(c[0] == "digest" for c in registry.calls)
    assert not (checkout.root / ".worktrees").exists()
    assert git(checkout.root, "branch", "--list", "hops/*") == ""


def test_an_explicit_tag_skips_the_major_version_check(hops, checkout, registry):
    registry._digests["20.04.1.0"] = DIGEST_5
    assert hops("bump", "sonarr/sonarr", "20.04.1.0").code == 0


def test_the_bump_starts_from_origin_main_not_the_callers_branch(hops, checkout, repo, tmp_path):
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(checkout.remote), str(other)], check=True)
    git(other, "config", "user.name", "Test")
    git(other, "config", "user.email", "test@example.com")
    (other / "README.md").write_text("merged elsewhere\n")
    land(other, "merged on main")
    git(repo, "checkout", "-q", "-b", "feature")
    (repo / SONARR).write_text((repo / SONARR).read_text().replace(OLD, "ghcr.io/home-operations/sonarr:9.9.9.9@sha256:9999"))
    (repo / "feature.txt").write_text("wip\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "feature work")

    outcome = hops("bump", "sonarr/sonarr")
    assert outcome.code == 0, outcome.err
    wt = worktree(checkout, "5.0.0.2742")
    assert git(wt, "rev-parse", "HEAD~1") == git(other, "rev-parse", "HEAD")
    assert not (wt / "feature.txt").exists()
    assert OLD in outcome.out  # the pin on main, not the feature branch's 9.9.9.9
    assert git(repo, "branch", "--show-current") == "feature"
    assert "9.9.9.9" in (repo / SONARR).read_text()
