# Open-Chat build tools

Build-time tooling for the [Open-Chat](https://github.com/msgmate-io/open-chat-go)
project. Currently this ships the **integration manager** (`openchat-integrations`),
which reads `integrations.yaml` from the Open-Chat repository root and:

- fetches integration sources on demand (no git submodules required),
- generates the Go workspace (`backend/go.work`), the side-effect imports and
  the build tags for the selected profile,
- links integration-owned Vike frontend packages and exports their prerendered
  pages into the integrations' embedded `frontend_assets`.

## Install

Install into a local virtual environment from upstream:

```bash
python3 -m venv .venv
.venv/bin/pip install "git+https://github.com/msgmate-io/open-chat-go-build-tools.git"
.venv/bin/openchat-integrations list --profile core-only
```

Or, when working inside a checkout that vendors this repo (e.g. as the
`development/build-tools` submodule of `open-chat-go`), install the pinned copy:

```bash
.venv/bin/pip install ./development/build-tools
```

The package is pure Python and only requires `PyYAML` (declared as a
dependency). `python3 -m openchat_integrations` works as an alternative to the
`openchat-integrations` console script.

## Usage

Run from the Open-Chat repository root (or pass `--repo-root`):

```bash
openchat-integrations list    --profile core-only
openchat-integrations sync    --profile core-only   # fetch checkouts + write lock
openchat-integrations resolve --profile core-only   # go.work + imports_gen + tags
openchat-integrations frontend --profile core-only  # link integration Vike packages/pages
openchat-integrations export  --profile core-only --dist-dir frontend/dist/client
openchat-integrations prepare --profile core-only   # sync + resolve + frontend
openchat-integrations check   --profile core-only   # validate lock + checkouts + pages
openchat-integrations dev --integration git --path ../my-git-integration-fork
```

## Profiles

| Profile | Default? | Integrations (plus transitive `depends_on`) |
| --- | --- | --- |
| `core-only` | yes | `mcp`, `rest_api_tool`, `go_client` |
| `default` | | core + `matrix`, `docker_sandbox`, `git`, `kubernetes` |
| `full` | | every integration (includes private ones) |

Select a profile with `INTEGRATION_PROFILE`, `--profile`, or the manifest's
`default_profile`.

## Notes

- `sync --frozen` checks out the commits pinned in `integrations.lock.json`
  instead of the manifest `ref`. CI and release builds use `--frozen`.
- Frontend page sets live in the integration repositories: each integration
  checkout ships `frontend/pages` and an `integration.frontend.json`
  describing its prerendered pages:

  ```json
  {
    "pages": [
      { "source": "integrations/mcp/servers", "asset": "servers/index.html" }
    ]
  }
  ```

  `source` is the prerendered route directory relative to the client dist
  root; `asset` is the destination inside the integration's embedded
  `frontend_assets`. The public parent manifest only lists the publish name
  (`frontend: {name: ...}`), so closed-source integrations never leak their
  page paths.
- `resolve` regenerates `backend/go.work`,
  `backend/integrations/externalintegrations/imports_gen.go` and the build tags.
- `dev` writes a local override to the gitignored `integrations.local.yaml`;
  `OPENCHAT_INTEGRATION_<ID>_PATH=/path/to/checkout` also works.
- Private integrations (`private: true`) require authenticated git access;
  configure a token credential with
  `git config url."https://x-access-token:$TOKEN@github.com/".insteadOf "https://github.com/"`.

## License

AGPL-3.0-or-later. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
