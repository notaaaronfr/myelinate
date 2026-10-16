# Contributing to Myelinate

Thanks for helping. Bug reports, docs fixes, integrations for more frameworks and research-backed improvements to
learning are all welcome.

## Development setup

You need Python 3.12+ and [uv](https://docs.astral.sh/uv/). Node 24 is only needed for the OpenClaw plugin.

```bash
git clone https://github.com/notaaaronfr/myelinate.git
cd myelinate
uv sync --all-extras
uv run pytest -q                      # 50+ tests on SQLite, a few seconds
MYELINATE_ENV=dev uv run myelinate serve --port 8080
```

Run the suite against PostgreSQL too (CI does both):

```bash
docker run -d --name myl-pg -e POSTGRES_PASSWORD=postgres -p 5432:5432 postgres:17
MYELINATE_TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/postgres uv run pytest -q
```

Tests use a scripted LLM, so they are free and deterministic. To exercise real learning, put `ANTHROPIC_API_KEY` in
`.env` and run the gateway (see the [quickstart](https://notaaaronfr.github.io/myelinate/getting-started/quickstart/)).

## Checks before opening a pull request

```bash
uv run ruff check src tests --select E,F,B,ASYNC --ignore E501,B008
uv run pytest -q
uv run python scripts/gen_reference.py            # regenerates docs/reference/{configuration,api}.md
uv run --group docs mkdocs build --strict          # docs build without warnings
cd adapters/openclaw && npm install && npm run typecheck   # if you touched the plugin (Node 24)
```

CI runs all of these, plus a Docker build and smoke test.

## Guidelines

- **Keep the hot path LLM-free.** `recall` and `record` must stay database-only.
- **Every knowledge change goes through a changeset.** Don't write to skills or rules directly.
- **Tenant isolation is non-negotiable.** Every query is scoped by the authenticated tenant, never by a request body.
- **Settings are documented in code.** Add a comment above each new field in `config.py` and regenerate the reference.
- **Tests:** add a test for every bug fix (see `tests/test_regressions.py`) and for every new behaviour.
- **Learning changes need evidence.** Cite the paper or experiment, and prefer changes that can be measured with the
  holdout lift.

## Reporting security issues

Please don't open public issues for vulnerabilities. See [SECURITY.md](https://github.com/notaaaronfr/myelinate/blob/main/SECURITY.md).

## Releasing (maintainers)

1. Bump the version in `pyproject.toml`, `src/myelinate/__init__.py` and `adapters/openclaw/package.json`. Move the
   `Unreleased` changelog entries under the new version.
2. Merge to `main`, then tag and push: `git tag v0.3.0 && git push origin v0.3.0`.
3. The `Release` workflow checks that the versions match, then:
   - publishes the multi-arch image `ghcr.io/notaaaronfr/myelinate` with signed build provenance;
   - publishes to PyPI and npm through trusted publishing (when the `PUBLISH_PYPI` / `PUBLISH_NPM` repository
     variables are `true`);
   - creates the GitHub release from the changelog.

To re-run a release (for example after enabling a registry): **Actions → Release → Run workflow**, then enter the tag.
