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
   the version lives. Use PEP 440 spelling: `0.2.0rc1`, `0.2.0`.
2. **Changelog.** Move the `Unreleased` entries under `## [<version>] - <date>`. The
   GitHub release notes are taken from this section.
3. **Check locally.**

   ```bash
   uv run pytest
   uv run ruff check src tests && uv run ruff format --check src tests
   uv run mypy
   uv run mkdocs build --strict
   uv build && uvx twine check --strict dist/* && rm -rf dist
   ```

4. **Merge to `main`**, with CI green.
5. **Rehearse (recommended for a big release).** Tag a pre-release such as `v0.2.0rc1`
   (after setting `__version__ = "0.2.0rc1"`). Pre-releases (`a`, `b`, `rc`, `.dev`) are
   published to TestPyPI only. A version can never be reused on PyPI, so this is cheap
   insurance.
6. **Tag and push.**

   ```bash
   git tag -a v0.2.0 -m "python-wrap 0.2.0"
   git push origin v0.2.0
   ```

   The workflow refuses a tag that disagrees with `__version__`, builds, checks the
   wheel, and then waits for your approval of the `pypi` environment. After you approve
   it uploads to PyPI with attestations and creates the GitHub release with the files
   attached.
7. **Smoke-test** from a fresh directory:

   ```bash
   uv run --no-project --with python-wrap==0.2.0 python -c "import python_wrap; print(python_wrap.__version__)"
   ```

8. **Open the next cycle.** Add an empty `Unreleased` section to the changelog and bump
   `__version__` to the next `.dev0`.

## If something goes wrong

- **The upload failed halfway.** Re-run the failed job. TestPyPI uses `skip-existing`.
- **A broken version reached PyPI.** Yank it (PyPI, Manage, Release, Options, Yank) and
  release the fix under the next number.
