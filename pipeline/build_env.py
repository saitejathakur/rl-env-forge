#!/usr/bin/env python3
"""
Stage 5 of the pipeline: ENVIRONMENT PACKAGING.

Takes a (repository, fix_commit) pair identified by mine_commits.py and emits a
sealed, self-contained RL environment directory.

The central design rule, and the reason most repo-derived environments get
rejected by buyers:

    THE ANSWER MUST NOT BE REACHABLE FROM INSIDE THE CONTAINER.

That rule drives three concrete decisions here:

  1. The agent context is produced with `git archive`, never `git clone` and
     never a directory copy. A worktree carries a .git link, and from a .git
     link the *entire future history is reachable* -- including the fix commit
     itself. An agent can simply read the answer. This is the single most
     common catastrophic leak in environments derived from real repositories.

  2. The fail-to-pass tests are NOT in the image. They are delivered at grade
     time from a read-only path with a SHA-256 manifest, so the agent cannot
     read the assertions, weaken them, or delete them.

  3. Documented hint surfaces (changelogs, release notes, issue references)
     are scrubbed, and every scrub is recorded in the build report so the
     decision is auditable rather than silent.

Usage:
    python3 build_env.py --spec specs/click-shared-flag-default.toml
"""

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tarfile
import tomllib
from datetime import datetime, timezone
from pathlib import Path


def git(repo, *args, check=True):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=check,
    ).stdout


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def export_tree(repo, commit, dest: Path):
    """Export a commit's tree WITHOUT any VCS metadata.

    git archive writes only tracked file contents. No .git, no reflog, no
    reachable objects, no future commits. This is the sealing step.
    """
    dest.mkdir(parents=True, exist_ok=True)
    tar_path = dest.parent / "_tree.tar"
    with open(tar_path, "wb") as f:
        subprocess.run(
            ["git", "-C", str(repo), "archive", "--format=tar", commit],
            stdout=f, check=True,
        )
    with tarfile.open(tar_path) as tf:
        tf.extractall(dest, filter="data")
    tar_path.unlink()


def scrub(context: Path, rules, report):
    """Remove documented hint surfaces. Every action is recorded."""
    for rule in rules:
        target = context / rule["path"]
        if not target.exists():
            report.append({"rule": rule["path"], "action": "skipped", "why": "not present"})
            continue

        if rule["mode"] == "delete":
            target.unlink()
            report.append({"rule": rule["path"], "action": "deleted", "why": rule["why"]})

        elif rule["mode"] == "truncate_section":
            text = target.read_text(encoding="utf-8", errors="replace")
            start = re.search(rule["start"], text)
            end = re.search(rule["end"], text)
            if start and end and start.end() < end.start():
                removed = text[start.end():end.start()]
                text = text[: start.end()] + "\n\n" + text[end.start():]
                target.write_text(text, encoding="utf-8")
                report.append({
                    "rule": rule["path"], "action": "section removed",
                    "why": rule["why"], "bytes_removed": len(removed),
                })
            else:
                report.append({"rule": rule["path"], "action": "skipped",
                               "why": "section markers not matched"})


def leak_scan(context: Path, needles, report):
    """Fail the build if any answer-revealing string survives in the context."""
    findings = []
    for path in context.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() in {".png", ".jpg", ".gif", ".ico", ".whl", ".gz"}:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except (OSError, ValueError):
            continue
        for needle in needles:
            if re.search(needle, text, re.I):
                findings.append({"file": str(path.relative_to(context)), "pattern": needle})

    # A VCS directory in the context is an automatic build failure.
    for vcs in (".git", ".hg", ".svn"):
        if (context / vcs).exists():
            findings.append({"file": vcs, "pattern": "VCS metadata present -- history reachable"})

    report.extend(findings)
    return findings


def build(spec_path: Path, out_root: Path):
    spec = tomllib.loads(spec_path.read_text())
    task_id = spec["task"]["id"]
    repo = Path(spec["source"]["repo_path"]).expanduser().resolve()
    fix = spec["source"]["fix_commit"]

    parent = git(repo, "rev-parse", f"{fix}^").strip()
    subject = git(repo, "log", "-1", "--format=%s", fix).strip()
    authored = git(repo, "log", "-1", "--format=%ad", "--date=short", fix).strip()

    env_dir = out_root / task_id
    if env_dir.exists():
        shutil.rmtree(env_dir)
    (env_dir / "verifier" / "tests").mkdir(parents=True)
    (env_dir / "solution").mkdir(parents=True)

    report = {
        "task_id": task_id,
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_repo": spec["source"]["url"],
        "fix_commit": fix,
        "parent_commit": parent,
        "fix_subject": subject,
        "fix_date": authored,
        "scrub_actions": [],
        "leak_findings": [],
    }

    # --- agent-facing context: sealed tree, no VCS metadata -----------------
    context = env_dir / "context"
    export_tree(repo, parent, context)

    # --- patches ------------------------------------------------------------
    test_paths = spec["patches"]["test_paths"]
    src_paths = spec["patches"]["src_paths"]

    test_patch = git(repo, "show", fix, "--", *test_paths)
    golden_patch = git(repo, "show", fix, "--", *src_paths)

    (env_dir / "verifier" / "tests" / "test_patch.diff").write_text(test_patch)
    (env_dir / "solution" / "golden.patch").write_text(golden_patch)

    # --- authored assets ----------------------------------------------------
    # envs/ is a build output and is wiped on every build, so every
    # hand-written artifact is copied in from source control. If it is not
    # copied here, it does not survive a rebuild -- and an environment a buyer
    # cannot rebuild is an environment we cannot support.
    authored = (spec_path.parent / spec["authored"]["dir"]).resolve()

    grader_src = authored / spec["authored"]["grader"]
    shutil.copy(grader_src, env_dir / "verifier" / "grade.py")
    report["grader_sha256"] = sha256_file(env_dir / "verifier" / "grade.py")

    for name in spec["authored"]["copy"]:
        src = authored / name
        if src.exists():
            shutil.copy(src, env_dir / name)
            report.setdefault("authored_copied", []).append(name)
        else:
            report.setdefault("authored_missing", []).append(name)

    # --- scrub + leak scan --------------------------------------------------
    scrub(context, spec.get("scrub", {}).get("rules", []), report["scrub_actions"])
    findings = leak_scan(context, spec["leak_scan"]["needles"], report["leak_findings"])

    # --- manifest over everything delivered at grade time -------------------
    manifest_lines = []
    for path in sorted((env_dir / "verifier").rglob("*")):
        if path.is_file() and path.name != "MANIFEST.sha256":
            rel = path.relative_to(env_dir / "verifier")
            manifest_lines.append(f"{sha256_file(path)}  {rel}")
    (env_dir / "verifier" / "MANIFEST.sha256").write_text("\n".join(manifest_lines) + "\n")

    (env_dir / "BUILD_REPORT.json").write_text(json.dumps(report, indent=2) + "\n")

    print(f"task:    {task_id}")
    print(f"source:  {spec['source']['url']} @ {parent[:10]} (parent of {fix[:10]})")
    print(f"context: {sum(1 for _ in context.rglob('*') if _.is_file())} files, no VCS metadata")
    print(f"scrub:   {len(report['scrub_actions'])} rules applied")

    if findings:
        print(f"\nLEAK SCAN FAILED -- {len(findings)} finding(s):")
        for f in findings:
            print(f"  {f['file']}: {f['pattern']}")
        print("\nBuild rejected. The answer is reachable from inside the context.")
        return 1

    print("leak scan: clean")
    print(f"\nbuilt -> {env_dir}")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec", required=True, type=Path)
    ap.add_argument("--out", default=Path("envs"), type=Path)
    args = ap.parse_args()
    return build(args.spec, args.out)


if __name__ == "__main__":
    sys.exit(main())
