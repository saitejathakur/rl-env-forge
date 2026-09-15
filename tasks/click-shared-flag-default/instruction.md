# Task

You are working in a checkout of the `click` command-line library at
`/workspace`. The package is already installed in editable mode.

You have 30 minutes.

## The defect

`click` allows two or more options to target the **same parameter name** by
passing the same name as the second positional argument to `click.option`,
with each option supplying a different `flag_value`. Exactly one of those
options may declare `default=True`, which marks it as the value the command
should receive when the user passes no flags at all.

Today, that default is only honoured when the option declaring `default=True`
happens to be processed first. Declared in the other order, the command
receives no value at all instead of the declared default.

## Required behaviour

Given two options that share a parameter name, where exactly one declares
`default=True`:

1. Invoking the command with **no arguments** must pass the `flag_value` of the
   option that declared `default=True` to the command function.
2. Requirement 1 must hold **regardless of the order in which the two options
   are declared**. Declaration order must not change which default applies.
3. Invoking the command with either flag passed **explicitly** must pass that
   flag's own `flag_value`, in both declaration orders.

Concretely, both of these commands must print `green` when invoked with no
arguments, and must print `red` when invoked with `--red`:

```python
@click.command
@click.option("--red", "color", flag_value="red")
@click.option("--green", "color", flag_value="green", default=True)
def a(color):
    click.echo(color)

@click.command
@click.option("--green", "color", flag_value="green", default=True)
@click.option("--red", "color", flag_value="red")
def b(color):
    click.echo(color)
```

## Constraints

- Modify only files under `src/`.
- Do not change the public API: no new required arguments, no renamed
  parameters, no changes to existing option or command signatures.
- The existing test suite must continue to pass in full. Your change is
  incorrect if it fixes this behaviour by breaking anything else.

## Verification

Your work is graded by a test suite that is **not present in this container**.
It is supplied at grading time. Everything the grader checks is stated above.

You are done when `src/` implements the behaviour described in *Required
behaviour* and the existing suite under `tests/` still passes.
