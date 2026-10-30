# Failing Python CI fixture

This tiny repository has one deterministic failing CI check. `greeting()` trims whitespace but does not capitalize the name. The expected correction is to title-case the trimmed name and add a regression case if the agent proposes one.

The `tests` GitHub Actions workflow runs this pytest suite on pushes to `main` and on pull requests. Its failed `workflow_run` event can be used to exercise the platform's monitored-repository trigger.

## Reproduce the failure

```sh
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

The single test fails with `Hello, sam rivera!` instead of `Hello, Sam Rivera!`.

## Exercise the repair workflow

1. Push this directory as its own GitHub repository and install the configured GitHub App on it.
2. Enable monitoring for the repository in the dashboard and configure the `tests` workflow as a required check.
3. Push a commit containing the fixture. The workflow fails and produces a repair job.
4. With Nebius configured, a trusted Python validation image configured, and Docker running, the worker proposes a patch, validates it, promotes the verified fix, and opens a draft PR.
5. After the first repair is validated and saved to Milisa, make a README-only commit on `main` while leaving the failing source, test, and dependency files unchanged. The workflow run has the same failure summary; context gathering loads the test's local Python imports as well as the test and dependency files, and the cache fingerprint ignores documentation and workflow text. This exercises the exact Milisa hit followed by validation without changing the failing program.

The fixture tests the local failure deterministically. The end-to-end steps require the GitHub App, Nebius account, Redis/PostgreSQL, a running Docker daemon, and a trusted validation image.
