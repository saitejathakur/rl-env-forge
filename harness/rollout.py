#!/usr/bin/env python3
"""
Stage 6 of the pipeline: CALIBRATION.

Runs an agent against a built environment k times and reports pass@1 plus the
failure histogram.

Two numbers come out of this, and the second one matters more than people
expect:

  pass@1            Where the task sits against the target difficulty band.
                    Too easy is worthless. 0% usually means the task is broken
                    or under-specified, not that it is hard.

  failure histogram How the failures are DISTRIBUTED. Three or more distinct
                    failure modes is a healthy task -- different attempts got
                    stuck in different places, which is what a real reasoning
                    gap looks like. One dominant bucket, where every failure is
                    identical, almost always means a specification defect: the
                    instruction is missing something every attempt then guesses
                    wrong in the same way.

    A task that lands perfectly in band with one dominant failure bucket is
    NOT shippable. The pass rate says "calibrated"; the histogram says
    "ambiguous instruction". The histogram is right.

Requires ANTHROPIC_API_KEY in the environment. The key is read from the
environment only -- never pass it on the command line, never commit it.

Usage:
    export ANTHROPIC_API_KEY=sk-...
    python3 harness/rollout.py envs/click-shared-flag-default \\
        --model claude-sonnet-4-6 --k 10
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

MAX_TURNS = 40

SYSTEM = """You are a software engineer working in a checked-out repository at /workspace.
Solve the task described in INSTRUCTION.md.

You have these tools: read_file, write_file, list_dir, run_command.
Work incrementally. Read before you write. Run the existing tests to check
you have not broken anything. When you believe the task is complete, say
DONE and stop."""


def sh(cmd, cwd, timeout=300):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                          timeout=timeout, shell=isinstance(cmd, str))


# --------------------------------------------------------------------------
# Tool surface exposed to the model. Deliberately small: file IO plus a shell.
# --------------------------------------------------------------------------
TOOLS = [
    {
        "name": "read_file",
        "description": "Read a file from the workspace.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Write the full contents of a file in the workspace.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
        },
    },
    {
        "name": "list_dir",
        "description": "List a directory in the workspace.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "run_command",
        "description": "Run a shell command in the workspace. No network access.",
        "input_schema": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    },
]


def dispatch(name, args, ws: Path, python: str):
    try:
        if name == "read_file":
            target = (ws / args["path"]).resolve()
            if ws.resolve() not in target.parents and target != ws.resolve():
                return "error: path escapes workspace"
            return target.read_text()[:60000]

        if name == "write_file":
            target = (ws / args["path"]).resolve()
            if ws.resolve() not in target.parents:
                return "error: path escapes workspace"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(args["content"])
            return f"wrote {args['path']} ({len(args['content'])} bytes)"

        if name == "list_dir":
            target = (ws / args["path"]).resolve()
            return "\n".join(sorted(p.name for p in target.iterdir()))[:20000]

        if name == "run_command":
            cmd = args["command"].replace("python3", python).replace("pytest", f"{python} -m pytest")
            r = sh(cmd, cwd=ws)
            out = (r.stdout or "") + (r.stderr or "")
            return f"exit={r.returncode}\n{out[-8000:]}"

    except Exception as exc:  # noqa: BLE001 - surface tool errors to the model
        return f"error: {exc}"
    return "error: unknown tool"


def classify_failure(grade: dict) -> str:
    """Bucket a failed rollout. Buckets are the histogram."""
    if grade.get("error"):
        return f"infra:{grade['error'][:40]}"
    f2p = grade.get("fail_to_pass", {}).get("passed")
    p2p = grade.get("pass_to_pass", {}).get("passed")
    if not f2p and p2p:
        return "target-test-still-failing"
    if f2p and not p2p:
        return "regression-introduced"
    if not f2p and not p2p:
        return "target-failing-and-regressions"
    return "unclassified"


def one_rollout(env_dir: Path, model: str, idx: int, workroot: Path, client):
    ws = workroot / f"rollout_{idx}"
    if ws.exists():
        shutil.rmtree(ws)
    shutil.copytree(env_dir / "context", ws)
    shutil.copy(env_dir / "instruction.md", ws / "INSTRUCTION.md")

    venv = ws / ".venv"
    sh([sys.executable, "-m", "venv", str(venv)], cwd=ws)
    python = str(venv / "bin" / "python")
    sh([python, "-m", "pip", "install", "-q", "pytest==8.3.5"], cwd=ws)
    sh([python, "-m", "pip", "install", "-q", "-e", "."], cwd=ws)

    instruction = (env_dir / "instruction.md").read_text()
    messages = [{"role": "user", "content":
                 f"Here is INSTRUCTION.md:\n\n{instruction}\n\nBegin."}]

    turns = 0
    while turns < MAX_TURNS:
        turns += 1
        resp = client.messages.create(
            model=model, max_tokens=4096, system=SYSTEM,
            tools=TOOLS, messages=messages,
        )
        messages.append({"role": "assistant", "content": resp.content})

        tool_uses = [b for b in resp.content if b.type == "tool_use"]
        if not tool_uses:
            break

        results = []
        for block in tool_uses:
            out = dispatch(block.name, block.input, ws, python)
            results.append({"type": "tool_result", "tool_use_id": block.id,
                            "content": str(out)})
        messages.append({"role": "user", "content": results})

    # Grade.
    grader = env_dir / "verifier" / "grade.py"
    r = sh([sys.executable, str(grader), "--workspace", str(ws),
            "--verifier-dir", str(env_dir / "verifier"),
            "--python", python, "--out", str(ws / "grade.json")], cwd=ws)
    try:
        grade = json.loads((ws / "grade.json").read_text())
    except Exception:
        grade = {"solved": False, "error": f"grader crashed: {r.stderr[:200]}"}

    shutil.rmtree(ws, ignore_errors=True)
    return {"rollout": idx, "turns": turns, "solved": bool(grade.get("solved")),
            "bucket": "solved" if grade.get("solved") else classify_failure(grade)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("env_dir", type=Path)
    ap.add_argument("--model", default="claude-sonnet-4-6")
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--band", default="0.0-0.5", help="target pass@1 band, low-high")
    args = ap.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY is not set. Export it and re-run.", file=sys.stderr)
        return 2

    try:
        import anthropic
    except ImportError:
        print("pip install anthropic", file=sys.stderr)
        return 2

    client = anthropic.Anthropic()
    workroot = Path(tempfile.mkdtemp(prefix="rollout_"))
    low, high = (float(x) for x in args.band.split("-"))

    rollouts = []
    for i in range(args.k):
        r = one_rollout(args.env_dir.resolve(), args.model, i, workroot, client)
        rollouts.append(r)
        mark = "solved" if r["solved"] else r["bucket"]
        print(f"  rollout {i + 1}/{args.k}: {mark} ({r['turns']} turns)")

    solved = sum(1 for r in rollouts if r["solved"])
    pass_at_1 = solved / len(rollouts)
    buckets = Counter(r["bucket"] for r in rollouts if not r["solved"])

    in_band = low <= pass_at_1 <= high
    distinct = len(buckets)
    dominant = buckets.most_common(1)[0] if buckets else None
    dominant_share = (dominant[1] / max(1, len(rollouts) - solved)) if dominant else 0

    print("\n" + "=" * 58)
    print(f"pass@1:            {pass_at_1:.2%}  ({solved}/{len(rollouts)})")
    print(f"target band:       {low:.0%}-{high:.0%}  -> {'IN BAND' if in_band else 'OUT OF BAND'}")
    print(f"failure buckets:   {distinct} distinct")
    for name, count in buckets.most_common():
        print(f"    {count:>2}x  {name}")

    verdict = []
    if not in_band:
        verdict.append("pass@1 outside target band")
    if distinct == 1 and (len(rollouts) - solved) >= 3:
        verdict.append("single dominant failure bucket -- likely a specification defect, not difficulty")
    elif dominant_share > 0.8 and (len(rollouts) - solved) >= 4:
        verdict.append(f"failures {dominant_share:.0%} concentrated in one bucket -- review the instruction")

    print("\nverdict: " + ("CALIBRATED" if not verdict else "NEEDS WORK"))
    for v in verdict:
        print(f"  - {v}")
    print("=" * 58)

    out = args.env_dir / "CALIBRATION.json"
    out.write_text(json.dumps({
        "model": args.model,
        "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "k": args.k,
        "pass_at_1": pass_at_1,
        "band": [low, high],
        "in_band": in_band,
        "failure_buckets": dict(buckets),
        "rollouts": rollouts,
        "verdict": "CALIBRATED" if not verdict else "NEEDS WORK",
        "notes": verdict,
    }, indent=2) + "\n")
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
