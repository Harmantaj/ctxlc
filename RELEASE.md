# Releasing ctxlc

Everything up to step 1 is staged and checked. Steps 2 onward publish something and run only on the maintainer's go.

## 1. Check (safe, publishes nothing)

```bash
python3 -W ignore -m unittest discover -s tests
python3 tests/e2e.py
scripts/release_check.sh          # builds dist/release/{whl,tar.gz,ctxlc-plugin.zip} and tests them clean
python3 -m twine check dist/release/*.whl dist/release/*.tar.gz   # needs: pip install twine
```

Bump `__version__` in `ctxlc/__init__.py` and the heading in `CHANGELOG.md` first for any release after 0.1.0.

## 2. Git repository and GitHub (publishes the source)

```bash
git init -b main
git add -A && git status          # confirm dist/, .claude/context/ and settings.local.json are NOT listed
git commit -m "ctxlc 0.1.1"
gh repo create ctxlc --public --source . --push --description "Context lifecycle manager for Claude Code"
```

Then add the repository URL to `pyproject.toml` under `[project.urls]` (`Homepage`, `Issues`) and rebuild
(`scripts/release_check.sh`) so PyPI links to it.

## 3. PyPI (publishes the package; a version can never be re-uploaded)

The name `ctxlc` was free on PyPI and TestPyPI on 2026-10-04. Optional dry run on TestPyPI first:

```bash
python3 -m twine upload --repository testpypi dist/release/*.whl dist/release/*.tar.gz
pipx install --index-url https://test.pypi.org/simple/ ctxlc && ctx --help && pipx uninstall ctxlc
```

Real upload (needs a PyPI account and an API token in `~/.pypirc` or `TWINE_PASSWORD`):

```bash
python3 -m twine upload dist/release/*.whl dist/release/*.tar.gz
```

## 4. GitHub release with the Cowork plugin

```bash
git tag v0.1.1 && git push origin v0.1.1
gh release create v0.1.1 dist/release/ctxlc-plugin.zip dist/release/*.whl dist/release/*.tar.gz \
  --title "ctxlc 0.1.1" --notes-file CHANGELOG.md
```

`dist/release/ctxlc-plugin.zip` is the generic build (per-folder sync, no path baked in). Never attach a zip built
with `--sync-folder`: it carries a path on the builder's computer.

## Known limits to state in the release notes

* Windows: verified in CI (Windows Server runners, Python 3.9 and 3.13), not yet in a live Claude Code session on
  Windows; the in-app status line is untested there.
* Python 3.9 is covered by CI only; local checks ran on 3.10 and 3.14.
* Cowork: state carries between tasks only for tasks that work in a folder on the user's computer, and folder access
  is approved per task. Saving relies on the model following the injected instructions.
