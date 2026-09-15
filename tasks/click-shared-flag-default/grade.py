#!/usr/bin/env python3
"""
Grader for click-shared-flag-default.

Runs at grade time, from a read-only mount. Never present in the agent image.

Soundness properties this grader is built to hold:

  * DETERMINISTIC -- no clocks, no randomness, no network, no ordering
    dependence. The same submission always produces the same verdict.

  * ISOLATED -- imports nothing from the solution path. It shells out to
    pytest in a subprocess and reads exit codes and structured report output.
    A grader that imports the code it is grading can be subverted by that code.

  * TWO-SIDED -- a submission must make the fail-to-pass test pass AND keep
    every pass-to-pass test passing. Fail-to-pass alone rewards an agent that
    deletes or weakens the rest of the suite to get its change through.

  * SELF-CHECKING -- verifies its own integrity against MANIFEST.sha256 before
    it runs, so a tampered verifier is a hard error rather than a pass.

Exit codes:  0 = solved, 1 = not solved, 2 = infrastructure error.
"""

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

FAIL_TO_PASS = [
    "tests/test_defaults.py::test_shared_param_prefers_first_default",
]

# Guard set. Deliberately includes the parsing and default machinery the fix
# touches, so a submission that "fixes" the target by breaking adjacent
# behaviour is caught rather than rewarded.
PASS_TO_PASS_PATHS = [
    "tests/test_defaults.py",
    "tests/test_options.py",
    "tests/test_arguments.py",
    "tests/test_basic.py",
    "tests/test_context.py",
    "tests/test_commands.py",
]


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_self(verifier_dir: Path) -> list:
    """Confirm the verifier payload matches its manifest."""
    manifest = verifier_dir / "MANIFEST.sha256"
    if not manifest.exists():
        return ["MANIFEST.sha256 missing"]

    problems = []
    for line in manifest.read_text().strip().splitlines():
        expected, rel = line.split("  ", 1)
        target = verifier_dir / rel
        if not target.exists():
            problems.append(f"missing from verifier payload: {rel}")
        elif sha256_file(target) != expected:
            problems.append(f"checksum mismatch: {rel}")
    return problems


def run_pytest(workspace: Path, python: str, targets: list, report_path: Path):
    cmd = [
        python, "-m", "pytest", *targets,
        "-p", "no:randomly", "-p", "no:cacheprovider",
        "-q", "--no-header", "-rN",
        f"--junit-xml={report_path}",
    ]
    proc = subprocess.run(
        cmd, cwd=workspace, capture_output=True, text=True, timeout=1800,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", "PYTHONHASHSEED": "0",
             "PYTHONDONTWRITEBYTECODE": "1", "NO_COLOR": "1", "TERM": "dumb"},
    )
    return proc


def check_scope(workspace: Path) -> list:
    """The instruction restricts edits to src/. Report violations.

    Reported, not auto-failed: scope is a judgement call that belongs in the
    audit trail. A grader that silently fails on scope teaches agents nothing.
    """
    violations = []
    tests_dir = workspace / "tests"
    if not tests_dir.exists():
        violations.append("tests/ directory is missing from the workspace")
    return violations


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workspace", default="/workspace", type=Path)
    ap.add_argument("--verifier-dir", default=Path(__file__).parent, type=Path)
    ap.add_argument("--python", default="/opt/venv/bin/python")
    ap.add_argument("--out", default="/tmp/grade_result.json", type=Path)
    args = ap.parse_args()

    result = {
        "task_id": "click-shared-flag-default",
        "solved": False,
        "fail_to_pass": {},
        "pass_to_pass": {"passed": 0, "failed": 0, "failures": []},
        "integrity": [],
        "scope_notes": [],
        "error": None,
    }

    # 1. Verifier integrity.
    problems = verify_self(args.verifier_dir)
    if problems:
        result["integrity"] = problems
        result["error"] = "verifier integrity check failed"
        args.out.write_text(json.dumps(result, indent=2))
        print(json.dumps(result, indent=2))
        return 2

    # 2. Install the hidden tests over the workspace.
    patch = args.verifier_dir / "tests" / "test_patch.diff"
    applied = subprocess.run(
        ["git", "apply", "--unsafe-paths", f"--directory={args.workspace}", str(patch)],
        capture_output=True, text=True, cwd=args.workspace,
    )
    if applied.returncode != 0:
        # No VCS in the workspace by design, so fall back to plain patch.
        applied = subprocess.run(
            ["patch", "-p1", "-i", str(patch)],
            capture_output=True, text=True, cwd=args.workspace,
        )
        if applied.returncode != 0:
            result["error"] = f"could not install hidden tests: {applied.stderr[:400]}"
            args.out.write_text(json.dumps(result, indent=2))
            print(json.dumps(result, indent=2))
            return 2

    result["scope_notes"] = check_scope(args.workspace)

    # 3. Fail-to-pass.
    f2p = run_pytest(args.workspace, args.python, FAIL_TO_PASS, Path("/tmp/f2p.xml"))
    result["fail_to_pass"] = {
        "targets": FAIL_TO_PASS,
        "passed": f2p.returncode == 0,
        "tail": f2p.stdout.strip().splitlines()[-8:] if f2p.stdout else [],
    }

    # 4. Pass-to-pass.
    p2p = run_pytest(args.workspace, args.python, PASS_TO_PASS_PATHS, Path("/tmp/p2p.xml"))
    summary = (p2p.stdout or "").strip().splitlines()
    result["pass_to_pass"] = {
        "paths": PASS_TO_PASS_PATHS,
        "passed": p2p.returncode == 0,
        "tail": summary[-8:],
    }

    result["solved"] = bool(
        result["fail_to_pass"]["passed"] and result["pass_to_pass"]["passed"]
    )

    args.out.write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))
    return 0 if result["solved"] else 1


if __name__ == "__main__":
    sys.exit(main())
