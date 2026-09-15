# rl-env-forge

Turning a real code repository into a sealed, calibrated RL environment — and
proving it cannot be gamed.

This is a working pipeline, not a demo. One environment is built end to end
from a real upstream commit, and every claim below is backed by a generated
artifact in this repo rather than by assertion.

```
$ python3 pipeline/validate_env.py envs/click-shared-flag-default

  [ ok ] GATE 5 seal: no VCS metadata, no hidden test, no golden patch in context
  [ ok ] GATE 6 timeout pin: instruction 1800s == task.toml 1800s
  [ ok ] GATE 1 no-op control: empty submission fails as required
  [ ok ] GATE 2 oracle control: golden patch passes the hidden test
  [ ok ] GATE 3 regression clean: full suite green under oracle -- 1302 passed
  [ ok ] GATE 4 test-deletion: deleting the hidden test does not yield a pass

  verdict: SHIPPABLE  (6/6 gates passed)
```

---

## Why this exists

I spend my working week on the QA side of RL environments: reviewing
environments other people built and deciding whether they are sound enough to
ship to a frontier lab. Most do not ship on the first pass.

This repo is the other half of that job — building one, and holding it to the
bar I apply to everyone else's work. The interesting output is not the task. It
is `EXPLOIT_ANALYSIS.md`, which documents every route I could find to a passing
score without solving the problem, and what closed each one.

---

## The environment

**Task:** a real defect in [`pallets/click`](https://github.com/pallets/click).
When two options target the same parameter name via `flag_value` and one
declares `default=True`, the default is honoured only if that option happens to
be processed first. Declared the other way round, the command receives nothing.

| | |
|---|---|
| Source | `pallets/click`, BSD-3-Clause |
| Base commit (agent sees) | `6a1c0d07` |
| Fix commit (oracle, never shipped) | `1c20dc6e`, 2025-09-22 |
| Fail-to-pass | `test_shared_param_prefers_first_default` |
| Pass-to-pass | 6 test files spanning defaults, options, arguments, context, commands |
| Baseline suite | 1,301 passing, ~2s |
| Under oracle | 1,302 passing, no regressions |

The task was not chosen by hand. It came out of `mine_commits.py`, which scored
119 candidate commits in this repository and ranked this one near the top on
diff tightness, single-file isolation, test substance and issue linkage.

---

## The pipeline

```
sources/<repo>                     upstream clone, full history
   │
   ├─ pipeline/mine_commits.py     find commits that fix a defect AND ship the
   │                               test proving it -- the original engineers
   │                               wrote our ground truth; we recover it
   │
   ├─ specs/<task>.toml            the only hand-written input: source commit,
   │                               patch split, scrub rules, leak needles
   │
   ├─ tasks/<task>/                authored assets under source control:
   │                               instruction, Dockerfile, task.toml, grader,
   │                               exploit analysis
   │
   ├─ pipeline/build_env.py        seal the context, split the fix into hidden
   │                               tests + oracle, scrub hint surfaces, scan
   │                               for leaks, hash the verifier payload
   │
   ├─ pipeline/validate_env.py     six control gates -- ship or reject
   │
   └─ harness/rollout.py           k rollouts, pass@1, failure histogram
```

Reproduce the whole thing:

```bash
git clone https://github.com/pallets/click sources/click
python3 pipeline/mine_commits.py sources/click --top 10
python3 pipeline/build_env.py --spec specs/click-shared-flag-default.toml
python3 pipeline/validate_env.py envs/click-shared-flag-default
```

`envs/` is a build output directory and is wiped on every build. Everything
hand-written lives in `tasks/` and `specs/`.

---

## The design rule everything follows

> **The answer must not be reachable from inside the container.**

Three consequences:

**The context is built with `git archive`, never a clone or a copy.** A worktree
keeps its `.git` link, and from that link the entire future history is
reachable — including the fix commit. In the first build of this environment,
`git show 1c20dc6e72` from inside the container printed the exact fix and the
exact hidden test. 3,373 commits were reachable from a container meant to see
one. Every functional check was green while the answer sat in plain view.

**The fail-to-pass test is not in the image.** It is delivered at grade time
from a read-only path with a SHA-256 manifest. An agent cannot weaken a test it
never had.

**Hint surfaces are scrubbed, and every scrub is logged.** `CHANGES.rst`
referenced the adjacent sentinel-normalization work and pointed straight at the
defect area. It is removed by a declared rule, recorded in `BUILD_REPORT.json`
with a reason, so a buyer can audit what we altered rather than trust us.

---

## The gates

An environment is not shippable because it looks right. It is shippable because
these hold:

| Gate | Requires |
|---|---|
| 1 · no-op | An empty submission **fails**. If it passes, the verifier measures nothing and every rollout is noise. |
| 2 · oracle | The golden patch **passes**. If it fails, the task is unsolvable as packaged. |
| 3 · regression | The full suite stays green under the oracle. |
| 4 · test-deletion | Deleting the hidden test does **not** yield a pass. |
| 5 · seal | No VCS metadata, hidden test or golden patch in the context. |
| 6 · timeout pin | The budget in `instruction.md` equals `agent_timeout_sec` in `task.toml`. |

Gates 1 and 2 bracket the verifier: one proves it can say no, the other proves
it can say yes. A verifier shown to do only one of those has not been tested.

The gates themselves are negative-tested. Injecting a `.git` directory and
desyncing the stated timeout produces:

```
  [FAIL] GATE 5 seal: .git
  [FAIL] GATE 6 timeout pin: instruction says 3600s but task.toml says 1800s
```

---

## Calibration

`harness/rollout.py` runs k attempts and reports pass@1 **and the failure
histogram**. The second number is the one people skip and the one that catches
broken tasks.

Three or more distinct failure modes is healthy — different attempts got stuck
in different places, which is what a real reasoning gap looks like. One dominant
bucket, where every failure is identical, almost always means a specification
defect: the instruction is missing something that every attempt then guesses
wrong the same way.

**A task that lands perfectly in band with one dominant failure bucket is not
shippable.** The pass rate says "calibrated"; the histogram says "ambiguous
instruction". The histogram is right.

```bash
export ANTHROPIC_API_KEY=...
python3 harness/rollout.py envs/click-shared-flag-default --model <model> --k 10
```

`task.toml` ships with `status = "UNCALIBRATED"` and an empty `pass_at_1`. That
field stays empty until rollouts are actually run. Writing a plausible number
there would be precisely the fabrication this pipeline exists to prevent.

---

## Three things I got wrong building this

Kept in because the corrections are the useful part.

**1. The first context leaked the entire repository history.** Covered above.
Found by scanning for it rather than by a failing test, which is the point — no
functional gate catches this.

**2. The integrity check was hollow.** The manifest was generated *before* the
grader was placed in the verifier payload, so `MANIFEST.sha256` covered the test
patch but not `grade.py`. The grader's self-verification was checking everything
except itself, and reported success. A control that runs, passes, and verifies
nothing is worse than no control, because it converts an unchecked surface into
a documented-as-checked one. Fixed by copying the grader in before the manifest
is computed.

**3. The second build silently destroyed my hand-written files.** `envs/` is a
build output and gets wiped; the instruction, Dockerfile and exploit analysis
were living there. That is exactly the failure a buyer hits when they try to
rebuild an environment you sold them. Authored assets now live in `tasks/` under
source control and are copied in at build time, and the build report lists
anything missing.

**4. The first leak scanner cried wolf.** A bare `3071` needle matched a wheel
hash inside `uv.lock`. Needles are now anchored to issue-reference forms. This
is the precision/recall tradeoff that governs any scrubber, PII included: too
loose and every build shows findings until humans stop reading them, too
aggressive and you silently destroy the asset you are trying to sell. Both
failure modes are expensive.

---

## The limitation worth stating

`click` is one of the most-starred Python repositories in existence. Its history
is in every major pre-training corpus. Any environment built on popular
open-source code carries permanent contamination risk, and it worsens with each
model generation. Choosing a 2025-09-22 commit mitigates it; nothing eliminates
it.

The structural answer is not better engineering — it is better provenance.
Environments built from **private production code that was never public** cannot
be scraped, are in no corpus, and cannot become contaminated later. That is the
one defect on the list that no pipeline closes and the right asset closes
completely.

---

## Layout

```
pipeline/mine_commits.py    candidate task discovery from git history
pipeline/build_env.py       sealing, patch split, scrub, leak scan, manifest
pipeline/validate_env.py    six control gates
harness/rollout.py          calibration: pass@1 + failure histogram
specs/                      build specs (hand-written input)
tasks/                      authored assets: instruction, Dockerfile, grader
envs/                       build output -- wiped and regenerated
```

Per environment: `BUILD_REPORT.json` (what was scrubbed and why),
`CONTROLS.json` (gate results), `EXPLOIT_ANALYSIS.md` (attack surface and open
items), `MANIFEST.sha256` (verifier integrity).

## License

Pipeline code: MIT. The vendored `click` context is BSD-3-Clause, upstream
license retained in the build output.
