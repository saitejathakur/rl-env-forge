#!/usr/bin/env python3
"""
Stage 7 of the pipeline: QA CONTROLS.

An environment is not shippable because it looks right. It is shippable
because these gates hold:

  GATE 1  NO-OP CONTROL      An empty submission must FAIL.
                             If it passes, the verifier is not measuring
                             anything and every rollout is noise.

  GATE 2  ORACLE CONTROL     The golden patch must PASS.
                             If it fails, the task is unsolvable as packaged
                             and every rollout burns money for nothing.

  GATE 3  REGRESSION CLEAN   The full suite must stay green under the golden
                             patch. Guards against a task whose "fix" is only
                             achievable by breaking something else.

  GATE 4  TEST-DELETION      A submission that deletes or neuters the hidden
                             test must FAIL. This is the pass-to-pass set
                             doing its job.

  GATE 6  TIMEOUT PIN       The budget stated in instruction.md must equal
                             agent_timeout_sec in task.toml. A mismatch means
                             the agent is told one budget and graded under
                             another. Cheap to check, and in review it is the
                             single most common pin defect.

  GATE 5  SEAL               No VCS metadata, no hidden test, no golden patch
                             anywhere in the agent context.

Gates 1 and 2 together bracket the verifier: one proves it can say no, the
other proves it can say yes. A verifier that has never been shown to do both
has not been tested, it has merely been run.

This runs the controls against a local virtualenv checkout, which is the same
logic the container executes. Run it before any Docker build, because a failed
control here costs seconds and a failed control in a rollout costs an hour of
agent time.
"""

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

GREEN = "PASS"
RED = "FAIL"


def sh(cmd, cwd, check=False, timeout=900):
    return subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, check=check, timeout=timeout
    )


class Validator:
    def __init__(self, env_dir: Path, workdir: Path):
        self.env = env_dir
        self.root = workdir
        self.results = []

    def log(self, gate, status, detail):
        self.results.append({"gate": gate, "status": status, "detail": detail})
        marker = " ok " if status == GREEN else "FAIL"
        print(f"  [{marker}] {gate}: {detail}")

    # -- workspace management ------------------------------------------------
    def fresh_workspace(self, name):
        ws = self.root / name
        if ws.exists():
            shutil.rmtree(ws)
        shutil.copytree(self.env / "context", ws)
        return ws

    def install(self, ws):
        venv = ws / ".venv"
        sh([sys.executable, "-m", "venv", str(venv)], cwd=ws, check=True)
        py = venv / "bin" / "python"
        sh([str(py), "-m", "pip", "install", "-q", "pytest==8.3.5"], cwd=ws, check=True)
        sh([str(py), "-m", "pip", "install", "-q", "-e", "."], cwd=ws, check=True)
        return py

    def apply_hidden_tests(self, ws):
        patch = self.env / "verifier" / "tests" / "test_patch.diff"
        r = sh(["patch", "-p1", "-i", str(patch)], cwd=ws)
        return r.returncode == 0

    def run_tests(self, py, ws, targets):
        return sh([str(py), "-m", "pytest", *targets, "-q", "--no-header"], cwd=ws)

    # -- gates ---------------------------------------------------------------
    def gate_seal(self):
        ctx = self.env / "context"
        bad = []
        for vcs in (".git", ".hg", ".svn"):
            if (ctx / vcs).exists():
                bad.append(vcs)
        if (ctx / "tests" / "test_defaults.py").exists():
            text = (ctx / "tests" / "test_defaults.py").read_text()
            if "prefers_first_default" in text:
                bad.append("hidden test present in context")
        for p in ctx.rglob("*.patch"):
            bad.append(f"patch file in context: {p.name}")

        if bad:
            self.log("GATE 5 seal", RED, "; ".join(bad))
        else:
            self.log("GATE 5 seal", GREEN,
                     "no VCS metadata, no hidden test, no golden patch in context")

    def gate_noop_and_oracle(self):
        # --- no-op ---
        ws = self.fresh_workspace("ws_noop")
        py = self.install(ws)
        assert self.apply_hidden_tests(ws), "could not install hidden tests"
        r = self.run_tests(py, ws, ["tests/test_defaults.py::test_shared_param_prefers_first_default"])
        if r.returncode == 0:
            self.log("GATE 1 no-op control", RED,
                     "empty submission PASSED -- verifier measures nothing")
        else:
            self.log("GATE 1 no-op control", GREEN,
                     "empty submission fails as required")

        # --- oracle ---
        ws = self.fresh_workspace("ws_oracle")
        py = self.install(ws)
        golden = self.env / "solution" / "golden.patch"
        r = sh(["patch", "-p1", "-i", str(golden)], cwd=ws)
        if r.returncode != 0:
            self.log("GATE 2 oracle control", RED,
                     f"golden patch did not apply: {r.stderr[:200]}")
            return
        assert self.apply_hidden_tests(ws), "could not install hidden tests"
        r = self.run_tests(py, ws, ["tests/test_defaults.py::test_shared_param_prefers_first_default"])
        if r.returncode == 0:
            self.log("GATE 2 oracle control", GREEN, "golden patch passes the hidden test")
        else:
            self.log("GATE 2 oracle control", RED,
                     f"golden patch FAILED the hidden test -- task unsolvable as packaged")
            return

        # --- regression, same workspace ---
        r = self.run_tests(py, ws, ["tests/"])
        tail = (r.stdout or "").strip().splitlines()[-1:]
        if r.returncode == 0:
            self.log("GATE 3 regression clean", GREEN, f"full suite green under oracle -- {tail[0] if tail else ''}")
        else:
            self.log("GATE 3 regression clean", RED, f"regressions under oracle -- {tail[0] if tail else ''}")

    def gate_timeout_pin(self):
        """Instruction budget must match the runtime contract."""
        import re as _re
        import tomllib as _toml

        toml_path = self.env / "task.toml"
        instr_path = self.env / "instruction.md"
        if not toml_path.exists() or not instr_path.exists():
            self.log("GATE 6 timeout pin", RED, "task.toml or instruction.md missing")
            return

        cfg = _toml.loads(toml_path.read_text())
        declared = cfg.get("runtime", {}).get("agent_timeout_sec")
        if declared is None:
            self.log("GATE 6 timeout pin", RED, "agent_timeout_sec absent from task.toml")
            return

        text = instr_path.read_text()
        stated = None
        m = _re.search(r"You have\s+(\d+)\s*(minutes?|hours?|seconds?)", text, _re.I)
        if m:
            n, unit = int(m.group(1)), m.group(2).lower()
            stated = n * {"s": 1, "m": 60, "h": 3600}[unit[0]]

        if stated is None:
            self.log("GATE 6 timeout pin", GREEN,
                     f"instruction states no budget; contract is {declared}s (nothing to contradict)")
        elif stated == declared:
            self.log("GATE 6 timeout pin", GREEN,
                     f"instruction {stated}s == task.toml {declared}s")
        else:
            self.log("GATE 6 timeout pin", RED,
                     f"instruction says {stated}s but task.toml says {declared}s")

    def gate_test_deletion(self):
        """Simulate the classic reward hack: delete the test that fails."""
        ws = self.fresh_workspace("ws_hack")
        py = self.install(ws)
        assert self.apply_hidden_tests(ws), "could not install hidden tests"

        target = ws / "tests" / "test_defaults.py"
        text = target.read_text()
        marker = "def test_shared_param_prefers_first_default(runner):"
        idx = text.find(marker)
        if idx == -1:
            self.log("GATE 4 test-deletion", RED, "could not locate hidden test to simulate deletion")
            return
        target.write_text(text[:idx])   # amputate the failing test

        r = self.run_tests(py, ws, ["tests/test_defaults.py::test_shared_param_prefers_first_default"])
        if r.returncode != 0:
            self.log("GATE 4 test-deletion", GREEN,
                     "deleting the hidden test does not yield a pass (collection error)")
        else:
            self.log("GATE 4 test-deletion", RED,
                     "deleting the hidden test produced a PASS -- exploitable")

    def run(self):
        print(f"\nvalidating {self.env.name}\n" + "-" * 62)
        self.gate_seal()
        self.gate_timeout_pin()
        self.gate_noop_and_oracle()
        self.gate_test_deletion()
        print("-" * 62)

        failed = [r for r in self.results if r["status"] == RED]
        verdict = "SHIPPABLE" if not failed else "REJECTED"
        print(f"verdict: {verdict}  ({len(self.results) - len(failed)}/{len(self.results)} gates passed)\n")

        out = self.env / "CONTROLS.json"
        out.write_text(json.dumps(
            {"verdict": verdict, "gates": self.results}, indent=2) + "\n")
        return 0 if not failed else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("env_dir", type=Path)
    ap.add_argument("--workdir", type=Path, default=None)
    args = ap.parse_args()

    workdir = args.workdir or Path(tempfile.mkdtemp(prefix="validate_"))
    workdir.mkdir(parents=True, exist_ok=True)
    return Validator(args.env_dir.resolve(), workdir).run()


if __name__ == "__main__":
    sys.exit(main())
