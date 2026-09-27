# Open-Chat build tools

Build-time tooling for the [Open-Chat](https://github.com/msgmate-io/open-chat-go)
project. Currently this ships the **integration manager** (`openchat-integrations`),
which reads `integrations.yaml` from the Open-Chat repository root and:

- fetches integration sources on demand (no git submodules required),
- generates the Go workspace (`backend/go.work`), the side-effect imports and
  the build tags for the selected profile,
- links integration-owned Vike frontend packages and exports their prerendered
  pages into the integrations' embedded `frontend_assets`,
- materializes profile-scoped private repositories and symlinks (`profile
  setup`) so the public repository stays free of private source.

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
openchat-integrations setup   --profile full        # repos + symlinks + private manifest
openchat-integrations sync    --profile full        # fetch checkouts + write lock
openchat-integrations resolve --profile full        # go.work + imports_gen + tags
openchat-integrations frontend --profile full       # link integration Vike packages/pages
openchat-integrations export  --profile full --dist-dir frontend/dist/client
openchat-integrations prepare --profile full        # sync + resolve + frontend
openchat-integrations check   --profile full        # validate lock + checkouts + pages
openchat-integrations dev --integration git --path ../my-git-integration-fork
```

Every command triggers `setup` automatically when the profile's marker is
missing or stale; `--no-setup` skips it and `setup --force-setup` re-runs it.

## Profiles and profile setup

Public profiles live in `integrations.yaml`; private integrations and the
private profiles live in a private manifest fragment inside the private `ci`
repository. `profile_setup.yaml` declares, per profile, the extra repositories
and symlinks that `setup` materializes:

| Profile | Integrations | Extra repos |
| --- | --- | --- |
| `core-only` | public core | – |
| `default` | core + selected private | private manifest fragment (sparse) |
| `full` | every integration | private manifest fragment (sparse) |
| `full-ci` | every integration | full private CI tooling + Helm chart |
| `full-android` | every integration | private manifest fragment + mobile client |

- The private fragment (`integrations.private.yaml`) and its lockfile are read
  from a sparse checkout of `openchat/` in the `ci` repository and mirrored into
  `.integrations/private/` (gitignored). The public `integrations.lock.json`
  never contains private pins.
- A single extra repo can be materialized without a profile:
  `openchat-integrations setup --repo llm_coding_agents`.

Select a profile with `INTEGRATION_PROFILE`, `--profile`, or the manifest's
`default_profile`.

## Notes

- `sync --frozen` checks out the commits pinned in `integrations.lock.json`
  instead of the manifest `ref`. CI and release builds use `--frozen`.
- `resolve` regenerates `backend/go.work`,
  `backend/integrations/externalintegrations/imports_gen.go` and the build tags.
- `dev` writes a local override to the gitignored `integrations.local.yaml`;
  `OPENCHAT_INTEGRATION_<ID>_PATH=/path/to/checkout` also works.
- Private integrations (`private: true`) require authenticated git access;
  configure a token credential with
  `git config url."https://x-access-token:$TOKEN@github.com/".insteadOf "https://github.com/"`.

## License

AGPL-3.0-or-later. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
