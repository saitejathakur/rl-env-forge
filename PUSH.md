# Pushing this to your GitHub

The repo is already initialised and has one commit. To publish:

```bash
tar xzf rl-env-forge.tar.gz
cd rl-env-forge

# create an empty repo named rl-env-forge on github.com first, then:
git remote add origin https://github.com/saitejathakur/rl-env-forge.git
git branch -M main
git push -u origin main
```

## Before you push — two things worth doing

**1. Run the calibration.** `task.toml` currently says `status = "UNCALIBRATED"`
and that is honest, but real numbers are better:

```bash
git clone https://github.com/pallets/click sources/click
python3 pipeline/build_env.py --spec specs/click-shared-flag-default.toml
pip install anthropic
export ANTHROPIC_API_KEY=...
python3 harness/rollout.py envs/click-shared-flag-default --model <model> --k 10
```

That writes `CALIBRATION.json`. Then update the `[calibration]` block in
`tasks/click-shared-flag-default/task.toml` with the real pass@1 and buckets,
rebuild, and commit. If the pass rate comes back at 0% or 100%, do not massage
it — write down what you found and why. That honesty is the whole point of the
repo.

**2. Build the Docker image once.** This container had no Docker, so the
Dockerfile is written and statically checked but has never been built. Run:

```bash
cd envs/click-shared-flag-default
docker build -t click-shared-flag-default .
docker run --rm --network none -it click-shared-flag-default \
  python -m pytest tests/ -q
```

Fix anything that breaks before you show it to anyone. An unbuilt Dockerfile in
a repo about reproducibility is the one thing a reviewer will actually test.

## Talking about it

The README is written to be read top to bottom in three minutes. If someone
asks you to walk through it, go in this order:

1. The design rule — the answer must not be reachable from inside the container
2. The `.git` leak you found, with the concrete proof (`git show` printed the fix)
3. The six gates, and why gates 1 and 2 bracket the verifier
4. The failure histogram argument — in-band with one dominant bucket is not shippable
5. The contamination limitation, and why private assets are the structural answer

Point five is the one that connects your work to their business. Land it.
