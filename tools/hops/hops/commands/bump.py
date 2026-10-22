"""hops bump: pin a Service's image to a newer tag and its index digest, through a PR.

Repo-changing: it never touches the Host. The edit happens on a new branch from origin/main in a
new worktree under .worktrees/, so the checkout hops runs from is never modified (whatever branch it
is on); nothing is pushed without --pr.
"""
import argparse
import io
import json
import re
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

from hops import config
from hops.declared import Declared
from hops.errors import HopsError, UsageError
from hops.registry import Registry
from hops.tags import propose

HELP = "pin a Service's image to a newer tag and its index digest, on a branch for a PR"
DESCRIPTION = """\
Pin a Service's image to <tag>, or to the newest tag shaped like the current one (same v prefix,
number of version parts and variant suffix; pre-releases never qualify, nor a jump of more than one
major version), with the tag's index digest as Renovate pins it. Every Service in the same Stack
that pins the same image moves too.

The Service and its current pin are read from origin/main (fetched first), and the edit is
committed on branch hops/bump-<service>-<tag> from origin/main, in a new worktree under
.worktrees/, after tools/validate/validate.py passes there. The checkout hops runs from is never
modified and must be clean. Nothing is pushed unless --pr is given. Agents pass their co-author
line with --trailer."""
VALIDATE = ["secrets", "env", "compose"]
BASE = "main"
RENOVATE = "app/renovate"


def register(subparsers) -> None:
    p = subparsers.add_parser("bump", help=HELP, description=DESCRIPTION,
                              formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("service", help="stack/service or a unique Service name")
    p.add_argument("tag", nargs="?", help="the tag to pin (default: the newest tag shaped like the current one)")
    p.add_argument("--pr", action="store_true", help=f"push the branch and open a PR against {BASE}")
    p.add_argument("--trailer", action="append", default=[], metavar="TEXT",
                   help='a commit trailer, e.g. "Co-authored-by: Name <email>" (repeatable)')
    p.set_defaults(func=run)


def _git(cwd: Path, *args: str) -> str:
    try:
        r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    except OSError as exc:
        raise HopsError(f"git: {exc}") from exc
    if r.returncode != 0:
        raise HopsError(f"git {' '.join(args)}: {r.stderr.strip() or r.stdout.strip()}")
    return r.stdout.strip()


def _gh(cwd: Path, *args: str, input: str | None = None) -> str:
    try:
        r = subprocess.run(["gh", *args], cwd=cwd, input=input, capture_output=True, text=True)
    except OSError as exc:
        raise HopsError(f"gh: {exc}") from exc
    if r.returncode != 0:
        raise HopsError(f"gh {' '.join(args[:2])}: {r.stderr.strip() or r.stdout.strip()}")
    return r.stdout.strip()


def renovate_prs(cwd: Path, repository: str) -> list[dict]:
    """Renovate's open PRs that update `repository` (as written in Compose)."""
    prs = json.loads(_gh(cwd, "pr", "list", "--state", "open", "--author", RENOVATE,
                         "--json", "number,title,url,body", "--limit", "100") or "[]")
    # Renovate's PR body has a table whose first column is the package: `| [name](link) | ...`.
    package = re.compile(r"^\|\s*\[?([^\]\s|]+)", re.M)
    return [pr for pr in prs if repository in package.findall(pr.get("body") or "")]


def rewrite(stack_dir: Path, old: str, new: str) -> list[Path]:
    """Replace the image reference `old` with `new` on every `image:` line in one Stack's files."""
    pattern = re.compile(r"(?m)^(\s*image:\s*[\"']?)" + re.escape(old) + r"(?=[\"']?\s*(?:#.*)?$)")
    changed = []
    for f in sorted([*stack_dir.rglob("*.yaml"), *stack_dir.rglob("*.yml")]):
        text = f.read_text()
        updated = pattern.sub(lambda m: m.group(1) + new, text)
        if updated != text:
            f.write_text(updated)
            changed.append(f)
    return changed


def declared_at(root: Path, ref: str, stack_root: str, into: Path) -> Declared:
    """The declared Stacks as of `ref`, extracted into `into` without a checkout."""
    r = subprocess.run(["git", "archive", "--format=tar", ref, stack_root], cwd=root, capture_output=True)
    if r.returncode != 0:
        raise HopsError(f"git archive {ref} {stack_root}: {r.stderr.decode().strip()}")
    with tarfile.open(fileobj=io.BytesIO(r.stdout)) as tar:
        # filter= exists from Python 3.11.4; git archive output is trusted either way.
        tar.extractall(into, **({"filter": "data"} if hasattr(tarfile, "data_filter") else {}))
    return Declared.load(into, stack_root)


def run(args, ctx) -> int:
    root = ctx.repo_root
    # The bump ignores local changes (it starts from origin/main); refusing them avoids the surprise.
    if dirty := _git(root, "status", "--porcelain"):
        raise UsageError(f"{root} has uncommitted changes; commit or stash them first:\n{dirty}")
    _git(root, "fetch", "-q", "origin", BASE)
    base = f"origin/{BASE}"
    stack_root = config.stack_root_name(ctx.env)
    with tempfile.TemporaryDirectory(prefix="hops-bump-") as tmp:
        service = declared_at(root, base, stack_root, Path(tmp)).resolve_one(args.service)
        service_file = service.file.relative_to(tmp)
    image = service.image
    if not image or not image.tag:
        raise UsageError(f"{service.key} has no image with a tag to bump")

    registry = Registry()
    tag = args.tag
    if not tag:
        tag, rejected = propose(image.tag, registry.tags(image.repository))
        if tag is None:
            raise UsageError(f"{image.tag!r} is not a version tag hops can compare; name a tag: "
                             f"hops bump {service.key} <tag>")
        if rejected:
            other = f"the newest acceptable is {tag}" if tag != image.tag else f"no newer tag than {tag} qualifies"
            raise UsageError(f"the newest {image.tag}-shaped tag, {rejected}, is more than one major version "
                             f"ahead (a stray tag?); {other}. Name the tag: hops bump {service.key} {tag}")
    new_ref = f"{image.repository}:{tag}@{registry.digest(image.repository, tag)}"
    if new_ref == image.ref:
        ctx.stdout.write(f"{service.key} already pins {new_ref}\n")
        return 0

    try:
        superseded = renovate_prs(root, image.repository)
    except (HopsError, ValueError) as exc:
        superseded = []
        ctx.stderr.write(f"hops: warning: could not check Renovate's open PRs: {exc}\n")
    for pr in superseded:
        ctx.stderr.write(f"hops: warning: Renovate already has #{pr['number']} for {image.repository}: "
                         f"{pr['title']} ({pr['url']}); continuing\n")

    branch = f"hops/bump-{service.name}-{tag}"
    _git(root, "check-ref-format", "--branch", branch)
    main_root = Path(_git(root, "rev-parse", "--path-format=absolute", "--git-common-dir")).parent
    wt = main_root / ".worktrees" / f"bump-{service.name}-{tag}"
    if wt.exists() or _git(root, "branch", "--list", branch):
        raise UsageError(f"{wt} or branch {branch} already exists; discard them first: "
                         f"git worktree remove --force {wt}; git branch -D {branch}")
    _git(root, "worktree", "add", "-q", "--no-track", "-b", branch, str(wt), base)

    changed = rewrite(wt / stack_root / service.stack, image.ref, new_ref)
    if wt / service_file not in changed:
        raise HopsError(f"no `image: {image.ref}` line in {service_file} to rewrite (in {wt})")
    files = [str(f.relative_to(wt)) for f in changed]
    ctx.stdout.write(f"{service.key}: {image.ref}\n  -> {new_ref}\n  in {', '.join(files)}\n")

    v = subprocess.run([sys.executable, "tools/validate/validate.py", *VALIDATE], cwd=wt,
                       capture_output=True, text=True)
    if v.returncode != 0:
        ctx.stderr.write(v.stdout + v.stderr)
        raise HopsError(f"validate.py failed; the uncommitted edit is in {wt} (branch {branch}). "
                        f"Discard: git worktree remove --force {wt}; git branch -D {branch}")

    subject = f"chore(deps): bump {image.repository} to {tag}"
    body = f"Pinned with `hops bump {service.key}`:\n\n- from {image.ref}\n- to {new_ref}"
    _git(wt, "commit", "-q", "-a", "-m", subject, "-m", body, *(f"--trailer={t}" for t in args.trailer))
    ctx.stdout.write(f"committed {_git(wt, 'rev-parse', '--short', 'HEAD')} on {branch} in {wt}\n")

    if not args.pr:
        ctx.stdout.write(f"not pushed. Open a PR: git -C {wt} push -u origin {branch} && gh pr create ...\n"
                         f"or discard: git worktree remove {wt} && git branch -D {branch}\n")
        return 0
    _git(wt, "push", "-q", "-u", "origin", branch)
    url = _gh(wt, "pr", "create", "--base", BASE, "--head", branch, "--title", subject, "--body-file", "-",
              input=pr_body(service, image.ref, new_ref, files, superseded))
    ctx.stdout.write(f"{url}\n")
    return 0


def pr_body(service, old: str, new: str, files: list[str], superseded: list[dict]) -> str:
    lines = ["## Summary", "", f"Pins `{service.key}` with `hops bump` ({', '.join(f'`{f}`' for f in files)}):",
             "", "```diff", f"-image: {old}", f"+image: {new}", "```", ""]
    for pr in superseded:
        lines += [f"Supersedes #{pr['number']} (Renovate closes it once `{BASE}` has this version or newer).", ""]
    lines += ["## Evidence", "",
              f"`tools/validate/validate.py {' '.join(VALIDATE)}` passed. The digest is the registry's index "
              "digest for the tag, the one Renovate pins, so Renovate's next run sees no diff.", "",
              "## Merge Danger", "",
              "**Door:** two-way. Revert this PR to go back to the previous pin.", "",
              f"**Blast Radius:** the `{service.stack}` Stack. doco-cd recreates its Services that use this image.",
              ""]
    return "\n".join(lines)
