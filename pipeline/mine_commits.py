#!/usr/bin/env python3
"""
Stage 4 of the pipeline: TASK MINING.

Scans a repository's git history for commits that are candidate RL-environment
tasks. A candidate is a commit that fixes a defect AND ships the test that
proves the fix, in the same commit.

Why that shape: the original engineering team wrote the ground truth for us.
The pre-commit state is the task, the test is the verifier, and the diff is
the golden patch. We are not inventing correctness -- we are recovering it.

Usage:
    python3 mine_commits.py <repo_path> [--limit N] [--json out.json]
"""

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, asdict, field

# Message patterns that suggest a defect fix rather than a feature or chore.
FIX_PATTERNS = [
    r"\bfix(e[sd])?\b",
    r"\bbug(fix)?\b",
    r"\bregression\b",
    r"\bbroken\b",
    r"\bincorrect(ly)?\b",
    r"\bfail(s|ed|ing)?\b",
    r"\bcrash(es|ed)?\b",
    r"\braise[sd]?\s+\w*error\b",
    r"\bshould\s+not\b",
    r"\bno\s+longer\b",
]

# Messages that disqualify: not a behavioural defect fix.
EXCLUDE_PATTERNS = [
    r"^merge\b",
    r"\btypo\b",
    r"\bdocs?\b.*\bonly\b",
    r"\brelease\b",
    r"\bbump\b",
    r"\bchangelog\b",
    r"\bpre-commit\b",
    r"\bruff\b",
    r"\bblack\b",
    r"\bformat(ting)?\b",
    r"\blint(ing)?\b",
]

TEST_FILE = re.compile(r"(^|/)(tests?|testing)/|(^|/)test_[^/]+\.py$|_test\.py$")
DOC_FILE = re.compile(r"\.(md|rst|txt|cfg|toml|ini|yaml|yml|json)$|^docs?/|^CHANGES|^CHANGELOG")


@dataclass
class Candidate:
    sha: str
    parent: str
    subject: str
    date: str
    src_files: list = field(default_factory=list)
    test_files: list = field(default_factory=list)
    src_lines_changed: int = 0
    test_lines_changed: int = 0
    score: float = 0.0
    reasons: list = field(default_factory=list)


def run(repo, *args):
    return subprocess.run(
        ["git", "-C", repo, *args],
        capture_output=True, text=True, check=True,
    ).stdout


def looks_like_fix(subject, body):
    text = f"{subject}\n{body}".lower()
    if any(re.search(p, text, re.M) for p in EXCLUDE_PATTERNS):
        return False, None
    for p in FIX_PATTERNS:
        if re.search(p, text):
            return True, p
    return False, None


def classify(paths):
    src, tests, docs = [], [], []
    for p in paths:
        if not p:
            continue
        if TEST_FILE.search(p):
            tests.append(p)
        elif DOC_FILE.search(p) or p.startswith(("docs/", "examples/")):
            docs.append(p)
        elif p.endswith(".py"):
            src.append(p)
    return src, tests, docs


def numstat(repo, sha):
    """Return {path: lines_changed} for a commit against its first parent."""
    out = run(repo, "show", "--numstat", "--format=", "-m", "--first-parent", sha)
    counts = {}
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        add, dele, path = parts
        if add == "-" or dele == "-":      # binary
            continue
        counts[path] = int(add) + int(dele)
    return counts


def mine(repo, limit=4000):
    log = run(
        repo, "log", f"-n{limit}", "--no-merges",
        "--format=%H%x1f%P%x1f%ad%x1f%s%x1f%b%x1e", "--date=short",
    )
    candidates = []

    for record in log.split("\x1e"):
        record = record.strip("\n")
        if not record:
            continue
        try:
            sha, parents, date, subject, body = record.split("\x1f")
        except ValueError:
            continue

        parent = parents.split()[0] if parents.split() else None
        if not parent:
            continue

        is_fix, matched = looks_like_fix(subject, body)
        if not is_fix:
            continue

        counts = numstat(repo, sha)
        src, tests, docs = classify(counts.keys())

        # Hard requirements.
        if not src or not tests:
            continue

        src_lines = sum(counts[p] for p in src)
        test_lines = sum(counts[p] for p in tests)

        c = Candidate(
            sha=sha, parent=parent, subject=subject.strip(), date=date,
            src_files=src, test_files=tests,
            src_lines_changed=src_lines, test_lines_changed=test_lines,
        )

        # Scoring: we want SMALL, ISOLATED, WELL-TESTED fixes.
        score = 0.0
        if len(src) == 1:
            score += 3.0
            c.reasons.append("single source file touched")
        elif len(src) <= 2:
            score += 1.5
            c.reasons.append("two source files touched")

        if src_lines <= 20:
            score += 3.0
            c.reasons.append(f"tight source diff ({src_lines} lines)")
        elif src_lines <= 60:
            score += 1.5
            c.reasons.append(f"moderate source diff ({src_lines} lines)")
        else:
            score -= 2.0
            c.reasons.append(f"large source diff ({src_lines} lines)")

        if test_lines >= 5:
            score += 2.0
            c.reasons.append(f"substantive test added ({test_lines} lines)")

        if len(tests) == 1:
            score += 1.0
            c.reasons.append("single test file")

        if docs:
            score -= 0.5
            c.reasons.append("also touches docs/config")

        if re.search(r"#\d+", subject + body):
            score += 1.0
            c.reasons.append("references an issue number")

        c.score = round(score, 2)
        candidates.append(c)

    candidates.sort(key=lambda c: c.score, reverse=True)
    return candidates


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("repo")
    ap.add_argument("--limit", type=int, default=4000, help="commits to scan")
    ap.add_argument("--top", type=int, default=15, help="candidates to print")
    ap.add_argument("--json", help="write full candidate list here")
    args = ap.parse_args()

    cands = mine(args.repo, args.limit)
    print(f"{len(cands)} candidate tasks found in {args.repo}\n")
    for c in cands[: args.top]:
        print(f"[{c.score:5.2f}] {c.sha[:10]}  {c.date}  {c.subject[:64]}")
        print(f"         src:  {', '.join(c.src_files)}  (+/- {c.src_lines_changed})")
        print(f"         test: {', '.join(c.test_files)}  (+/- {c.test_lines_changed})")
        print(f"         why:  {'; '.join(c.reasons)}")
        print()

    if args.json:
        with open(args.json, "w") as f:
            json.dump([asdict(c) for c in cands], f, indent=2)
        print(f"wrote {args.json}")


if __name__ == "__main__":
    sys.exit(main())
