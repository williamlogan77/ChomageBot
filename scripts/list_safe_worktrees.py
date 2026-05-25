"""Read-only audit of agent worktrees under .claude/worktrees/.

Categorises each agent worktree by whether its HEAD is already reachable
from `origin/feat/match-analysis` and whether the working tree is clean,
then prints the exact cleanup commands for the safe set. Nothing is
removed — copy/paste the commands manually after reviewing the report.

Usage:
    python scripts/list_safe_worktrees.py

Exits 0 if the audit ran, 1 only if the mainline ref is missing. Uses
stdlib + `git` subprocess calls only.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

MAINLINE = "origin/feat/match-analysis"

# Lines from `git status --porcelain` to treat as noise when classifying.
# `?? .claude/` shows up in older agent worktrees whose HEAD predates the
# repo-root .gitignore entry for .claude/ — it's session state, not work.
NOISE_PATTERNS = ("?? .claude/",)

CAT_SAFE = "SAFE_TO_REMOVE"
CAT_DIRTY = "HAS_UNCOMMITTED"
CAT_DIVERGENT = "NOT_IN_MAINLINE"
CAT_OTHER = "OTHER"


@dataclass
class Worktree:
    path: str
    name: str
    head: str
    branch: str | None
    locked: bool
    exists_on_disk: bool
    category: str = CAT_OTHER
    dirty_files: list[str] = field(default_factory=list)
    divergent_commits: list[str] = field(default_factory=list)
    note: str = ""


def git(*args: str, cwd: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def repo_root() -> str:
    res = git("rev-parse", "--path-format=absolute", "--show-toplevel")
    if res.returncode == 0:
        return res.stdout.strip()
    # Fallback for unusual setups: derive from the common git dir.
    common = git("rev-parse", "--git-common-dir")
    if common.returncode != 0:
        sys.exit(f"not a git repo: {common.stderr.strip()}")
    return str(Path(common.stdout.strip()).parent.resolve())


def parse_worktrees(root: str) -> list[Worktree]:
    res = git("worktree", "list", "--porcelain", cwd=root)
    if res.returncode != 0:
        sys.exit(f"git worktree list failed: {res.stderr.strip()}")

    out: list[Worktree] = []
    for block in res.stdout.strip().split("\n\n"):
        path = head = branch = ""
        locked = False
        for line in block.splitlines():
            if line.startswith("worktree "):
                path = line[len("worktree ") :].strip()
            elif line.startswith("HEAD "):
                head = line[len("HEAD ") :].strip()
            elif line.startswith("branch "):
                branch = line[len("branch ") :].strip()
            elif line == "locked" or line.startswith("locked "):
                locked = True
        if not path:
            continue
        out.append(
            Worktree(
                path=path,
                name=Path(path).name,
                head=head,
                branch=branch or None,
                locked=locked,
                exists_on_disk=Path(path).is_dir(),
            )
        )
    return out


def is_ancestor(sha: str, ref: str, cwd: str) -> bool:
    return git("merge-base", "--is-ancestor", sha, ref, cwd=cwd).returncode == 0


def dirty_status(path: str) -> list[str]:
    res = git("status", "--porcelain", cwd=path)
    if res.returncode != 0:
        return []
    return [ln for ln in res.stdout.splitlines() if ln.strip()]


def divergent_commits(sha: str, ref: str, cwd: str, limit: int = 10) -> list[str]:
    res = git("log", "--oneline", f"--max-count={limit}", f"{ref}..{sha}", cwd=cwd)
    if res.returncode != 0:
        return []
    return [ln for ln in res.stdout.splitlines() if ln.strip()]


def short(sha: str) -> str:
    return sha[:7] if sha else "-------"


def humanize_branch(branch: str | None) -> str:
    if not branch:
        return "(detached)"
    return branch.removeprefix("refs/heads/")


def classify(wt: Worktree, root: str) -> None:
    if not wt.exists_on_disk:
        wt.category = CAT_OTHER
        wt.note = "registered worktree path no longer exists on disk"
        return
    if not wt.head:
        wt.category = CAT_OTHER
        wt.note = "no HEAD reported by git"
        return

    in_mainline = is_ancestor(wt.head, MAINLINE, root)
    wt.dirty_files = dirty_status(wt.path)

    if not in_mainline:
        wt.category = CAT_DIVERGENT
        wt.divergent_commits = divergent_commits(wt.head, MAINLINE, root)
        return

    real_dirty = [ln for ln in wt.dirty_files if ln.strip() not in NOISE_PATTERNS]
    wt.category = CAT_DIRTY if real_dirty else CAT_SAFE


def is_agent_worktree(wt: Worktree, root: str) -> bool:
    try:
        rel = Path(wt.path).resolve().relative_to(Path(root).resolve())
    except ValueError:
        return False
    parts = rel.parts
    return len(parts) >= 3 and parts[0] == ".claude" and parts[1] == "worktrees"


def render(agents: list[Worktree], primary_count: int) -> None:
    by_cat: dict[str, list[Worktree]] = {
        CAT_SAFE: [],
        CAT_DIRTY: [],
        CAT_DIVERGENT: [],
        CAT_OTHER: [],
    }
    for w in agents:
        by_cat[w.category].append(w)

    print("=== Worktree cleanup audit ===\n")
    print(f"Mainline ref: {MAINLINE}")
    print(f"Noise filtered: lines matching {NOISE_PATTERNS} treated as clean")
    print("                (session state — gitignored in current HEAD, untracked in older HEADs)")
    print(f"Primary worktree(s): {primary_count} (skipped)")
    print(f"Total agent worktrees: {len(agents)}")
    print(f"  {CAT_SAFE}:    {len(by_cat[CAT_SAFE])}")
    print(f"  {CAT_DIRTY}:   {len(by_cat[CAT_DIRTY])}")
    print(f"  {CAT_DIVERGENT}: {len(by_cat[CAT_DIVERGENT])}")
    print(f"  {CAT_OTHER}:           {len(by_cat[CAT_OTHER])}")
    print()

    safe = sorted(by_cat[CAT_SAFE], key=lambda w: w.name)
    print(f"=== {CAT_SAFE} (commits already in mainline, clean working tree) ===\n")
    if not safe:
        print("(none)\n")
    else:
        for w in safe:
            print(f"  {w.name}  [{short(w.head)}] {humanize_branch(w.branch)}")
        print()
        print(f"To remove all {len(safe)} safe worktrees + their branches, run from the")
        print("primary worktree (one block per entry):\n")
        for w in safe:
            wpath = f".claude/worktrees/{w.name}"
            bname = humanize_branch(w.branch)
            print(f"git worktree unlock {wpath} 2>/dev/null")
            print(f"git worktree remove --force {wpath}")
            print(f"git branch -D {bname}")
        print()
        print("Or paste this one-liner (bash):\n")
        names = " ".join(w.name for w in safe)
        print("for w in " + names + "; do")
        print("    git worktree unlock .claude/worktrees/$w 2>/dev/null")
        print("    git worktree remove --force .claude/worktrees/$w 2>/dev/null")
        print("    git branch -D worktree-$w 2>/dev/null")
        print("done")
        print()

    dirty = sorted(by_cat[CAT_DIRTY], key=lambda w: w.name)
    print(f"=== {CAT_DIRTY} (review before removing) ===\n")
    if not dirty:
        print("(none)\n")
    else:
        for w in dirty:
            print(
                f"  {w.name}  [{short(w.head)}] {humanize_branch(w.branch)} "
                f"— {len(w.dirty_files)} uncommitted file(s):"
            )
            for line in w.dirty_files[:20]:
                print(f"    {line}")
            if len(w.dirty_files) > 20:
                print(f"    ... ({len(w.dirty_files) - 20} more)")
        print()

    divergent = sorted(by_cat[CAT_DIVERGENT], key=lambda w: w.name)
    print(f"=== {CAT_DIVERGENT} (commit isn't in {MAINLINE} — investigate) ===\n")
    if not divergent:
        print("(none)\n")
    else:
        for w in divergent:
            n = len(w.divergent_commits)
            extra = "" if n < 10 else "+"
            print(
                f"  {w.name}  [{short(w.head)}] {humanize_branch(w.branch)} "
                f"— {n}{extra} commit(s) not in mainline:"
            )
            for line in w.divergent_commits:
                print(f"    {line}")
        print()

    other = sorted(by_cat[CAT_OTHER], key=lambda w: w.name)
    print(f"=== {CAT_OTHER} (stale or unparseable — investigate) ===\n")
    if not other:
        print("(none)\n")
    else:
        for w in other:
            print(f"  {w.name}  [{short(w.head)}] {humanize_branch(w.branch)} — {w.note}")
        print()


def main() -> int:
    root = repo_root()
    if git("rev-parse", "--verify", MAINLINE, cwd=root).returncode != 0:
        print(
            f"error: ref {MAINLINE!r} not found. Run `git fetch origin` first.",
            file=sys.stderr,
        )
        return 1

    all_wts = parse_worktrees(root)
    agents = [wt for wt in all_wts if is_agent_worktree(wt, root)]
    primary_count = len(all_wts) - len(agents)

    for wt in agents:
        classify(wt, root)

    render(agents, primary_count)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
