# Releasing

How a version of `python-wrap` gets from this repository to PyPI.

Publishing uses **PyPI Trusted Publishing** from `.github/workflows/release.yml`. No API
token exists anywhere, and none should be created. (The old token-based `publish.yml`
was removed; revoke the token it used at <https://pypi.org/manage/account/token/>.)

## Once: set up Trusted Publishing

1. **Add a publisher on PyPI.** The project `python-wrap` already exists, so go to
   <https://pypi.org/manage/project/python-wrap/settings/publishing/> and add a GitHub
   publisher with exactly:

   | Field | Value |
   |---|---|
   | Owner | `LitePy` |
   | Repository name | `PythonWrap` |
   | Workflow name | `release.yml` |
   | Environment name | `pypi` |

2. **Add a pending publisher on TestPyPI.** The project does not exist there yet, so use
   <https://test.pypi.org/manage/account/publishing/> with the same values, project name
   `python-wrap`, and environment name `testpypi`.

3. **Create the GitHub environments** in the repository under Settings, Environments:
   `pypi` and `testpypi`. Add yourself as a required reviewer on `pypi` so every real
   upload waits for one click.

## Every release

1. **Version.** Set `__version__` in `src/python_wrap/__init__.py`. It is the only place
   the version lives. Use PEP 440 spelling: `0.2.1rc1`, `0.2.1`.
2. **Changelog.** Move the `Unreleased` entries under `## [<version>] - <date>`. The
   GitHub release notes are taken from this section.
3. **Check locally.**

   ```bash
   uv run pytest
   uv run ruff check src tests scripts/check_installed.py
   uv run ruff format --check src tests scripts/check_installed.py
   uv run mypy
   uv run mkdocs build --strict
   uv build && uvx twine check --strict dist/* && rm -rf dist
   ```

4. **Merge to `main`**, after the local checks pass. CI and docs run only on pushes
   to `main`, so PR updates do not start Actions runs. Wait for both workflows on the
   merged commit to pass before creating a release tag. If the repository requires
   PR status checks, a maintainer must align that rule with this post-merge policy;
   new PR commits will not receive CI/docs checks.
5. **Rehearse (recommended for a big release).** Tag a pre-release such as `v0.2.1rc1`
   (after setting `__version__ = "0.2.1rc1"`). Every tagged version is published to
   TestPyPI and installed back from that index
   in a clean environment. Pre-releases (`a`, `b`, `rc`, `.dev`) stop there; final
   versions continue to PyPI only after this verification succeeds. A version can never
   be reused on PyPI, so this is cheap insurance.
6. **Tag and push.**

   ```bash
   git tag -a v0.2.1 -m "python-wrap 0.2.1"
   git push origin v0.2.1
   ```

   The workflow refuses a tag that disagrees with `__version__`, builds, checks the
   wheel, uploads to TestPyPI and verifies the installed package, and then waits for
   your approval of the `pypi` environment. After you approve, it uploads the same
   artifacts to PyPI with attestations, verifies an installation from PyPI, and creates
   the GitHub release with the files attached. `workflow_dispatch` only builds and
   verifies artifacts; it never uploads.
7. **Smoke-test** from a fresh directory:

   ```bash
   uv venv /tmp/python-wrap-smoke
   uv pip install --python /tmp/python-wrap-smoke --no-deps --index-url https://pypi.org/simple/ python-wrap==0.2.1
   /tmp/python-wrap-smoke/bin/python -I scripts/check_installed.py 0.2.1
   ```

8. **Open the next cycle.** Add an empty `Unreleased` section to the changelog and bump
   `__version__` to the next `.dev0`.

## If something goes wrong

- **The upload failed halfway.** Re-run the failed job. TestPyPI uses `skip-existing`.
- **A broken version reached PyPI.** Yank it (PyPI, Manage, Release, Options, Yank) and
  release the fix under the next number.

## Recovering the first 0.2.0 release

The saved release notes record a failed upload (`invalid-publisher`) from an older
`v0.2.0` tag. Do not rerun that old tag to publish the completed extras: a rerun uses
the old source and workflow. First confirm the current project release history on
both indexes and configure both trusted publishers above. A pending TestPyPI
publisher creates the project on its first successful workflow upload; there is no
separate empty-project creation step.

Merge the release changes with green CI before creating a new tag. If `0.2.0` has
already reached PyPI, use a new version. If it has not, either have a maintainer
explicitly replace the old tag with the reviewed commit, or choose a fresh version
and update `__version__` and the changelog together. Do not rewrite a published tag
as part of routine release automation.

`invalid-publisher` means the index cannot match the workflow's OIDC identity to a
registered publisher. Check the owner (`LitePy`), repository (`PythonWrap`), workflow
filename (`release.yml`), environment (`pypi` or `testpypi`), and that the publisher
was registered on the correct index. PyPI and TestPyPI have separate accounts and
publisher configurations.
