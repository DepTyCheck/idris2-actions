# idris2-actions

A reusable GitHub Actions workflow that builds and tests any
[pack](https://github.com/stefan-hoeck/idris2-pack)-managed Idris2 project.

It runs `pack build` and `pack test` against both the `latest` and `HEAD` pack collections
(the `HEAD` run is skipped automatically when it matches the compiler version from the `latest` collection).

## Real examples

- Trivial usage: [buzden/idris2-summary-stat](https://github.com/buzden/idris2-summary-stat)
- Advanced usage:
  - [buzden/deptycheck](https://github.com/buzden/deptycheck) — custom test runs and installation of extra libs into the pack environment
  - [DepTyCheck/verilog-model](https://github.com/DepTyCheck/verilog-model) — advanced usage of the results of `pack run`

## Cache key

The build job uses the GitHub Actions cache to reuse a successful build across later workflow runs.
A matching key restores the project build directory and pack state; a miss builds from scratch and saves a new entry.

The Actions cache key and the uploaded artifact name are the same string:

```text
pack-state-v3-$CONTAINER_TAG-$OS-$CHEZ-$HASH-$COLLECTION-$effective_collection-$pack_hash[-$compiler_version][-$COMPILER_HASH]
```

`pack_state` and `pack_states` are that string, one per collection.

| Segment              | Example                        | Meaning                                                                                                       |
| -------------------- | ------------------------------ | ------------------------------------------------------------------------------------------------------------- |
| prefix               | `pack-state-v3`                | Cache namespace so older entries cannot match                                                                 |
| container tag        | `latest`                       | `container_tag` input (default `latest`), the `ghcr.io/stefan-hoeck/idris2-pack` image tag                    |
| OS                   | `ubuntu-24.04`                 | `ID-VERSION_ID` from the image's `/etc/os-release`                                                            |
| Chez                 | `10.0.0`                       | Version number from `chezscheme --version` in that image                                                      |
| source hash          | `6434353b1c9684f0`             | First 16 hex chars of a hash of the `.ipkg`, `src/`, and `pack.toml`                                          |
| requested collection | `latest`                       | The matrix collection (`COLLECTION`)                                                                          |
| effective collection | `nightly-261111`               | The pack DB file that will actually be used                                                                   |
| pack DB hash         | `9aa50ce230b3a755`             | First 16 hex chars of the SHA-256 of `$PACK_STATE_DIR/db/$effective_collection.toml`                          |
| compiler revision    | `c24d53e41`                    | First 9 chars of Idris2 `main`, appended only when the requested collection is `HEAD`                         |
| compiler hash        | appended for a custom compiler | First 16 hex chars of the SHA-256 of `["<url>", "<commit>"]` from `pack.toml`. Omitted for the stock compiler |

## Custom compiler

The workflow reads `pack.toml` beside the selected `.ipkg`.
To use a compiler fork or a specific commit, add:

```toml
[idris2]
url = "https://github.com/..."
commit = "..."
```

A local `idris2.url` or `idris2.commit` override selects only `latest` by default.
Explicit `pack_collections` are used as supplied, but including `HEAD` together with a custom compiler is an error.

A custom compiler appends `-$COMPILER_HASH` on that same string.
`upload-pack-state` uses the `state` you pass as the artifact name.

## Usage

Add a workflow to your repository (for example `.github/workflows/ci.yml`) that calls it:

```yaml
---
name: Build and test

on:
  push:
    branches:
      - main
      - master
    tags:
      - "**"
  pull_request:
    branches:
      - main
      - master
  schedule:
    - cron: "0 1 * * *"

permissions: read-all

jobs:
  ci:
    uses: DepTyCheck/idris2-actions/.github/workflows/build-and-test.yml@v1
    # with:
      # Optional. Default: "". Path to the .ipkg to build and test.
      # By default the workflow expects a single .ipkg in the repo root.
      # ipkg_path: ""

      # Optional. Default: latest. Tag for the ghcr.io/stefan-hoeck/idris2-pack container.
      # container_tag: latest

      # Optional. Default: []. JSON array of pack collections to build.
      # Empty array keeps the automatic latest/HEAD selection.
      # pack_collections: '["latest"]'

      # Optional. Default: []. JSON array of pack collections whose pre-build,
      # pack build, pack-state require, pre-test, and pack test steps may fail
      # without failing the job. Empty array keeps every failure failing the run.
      # continue_on_error_collections: '["HEAD"]'

      # Optional. Default: true. Run `pack test` after building. Set false to only build and cache.
      # run_tests: true

      # Optional. Default: "". Space-separated pack libraries to `pack install` before `pack build`.
      # pack_deps: ""

      # Optional. Default: "". Space-separated pack libraries to `pack install` after `pack build`.
      # pack_extras: ""

      # Optional. Default: "". Bash script to run before `pack build`.
      # pre_build: ""

      # Optional. Default: "". Bash script to run before `pack test`.
      # pre_test: ""

      # Optional. Default: true. Upload pack-state artifacts after building
      # so other jobs can restore them.
      # upload_pack_state_artifacts: true
```

## Outputs

The workflow exposes outputs so a downstream job can restore the build it cached.

| Output        | Description                                                                  |
| ------------- | ---------------------------------------------------------------------------- |
| `collections` | JSON array of pack collection names.                                         |
| `pack_states` | JSON array of restore handles, one per pack collection.                      |
| `pack_state`  | First restore handle from `pack_states`, or empty if `pack_states` is empty. |
| `package`     | Package name parsed from the `.ipkg`.                                        |
| `project_dir` | Directory holding the `.ipkg` and `pack.toml`.                               |
| `build_dir`   | The package's build dir (relative to `project_dir`).                         |

## Restoring pack state

The build job caches the project build and pack state together per collection.
A downstream job can restore that pack state with the `restore-pack-state` action and use the build.

```yaml
jobs:
  build-for-cache:
    uses: DepTyCheck/idris2-actions/.github/workflows/build-and-test.yml@v1

  run-cached:
    needs: build-for-cache
    runs-on: ubuntu-latest
    container: ghcr.io/stefan-hoeck/idris2-pack:latest
    strategy:
      fail-fast: false
      matrix:
        state: ${{ fromJSON(needs.build-for-cache.outputs.pack_states) }}
    steps:
      - name: Checkout
        uses: actions/checkout@v6
        with:
          persist-credentials: false

      - name: Restore the cached build
        id: restore
        uses: DepTyCheck/idris2-actions/.github/actions/restore-pack-state@v1
        with:
          state: ${{ matrix.state }}

      - name: Run the package
        working-directory: ${{ needs.build-for-cache.outputs.project_dir }}
        run: pack run ${{ needs.build-for-cache.outputs.package }}
```

## Using a single collection

Sometimes you just want one build and to run a command against it.
By default the `collections` array is ordered: element 0 is always `latest` (the `HEAD` entry, when present, is element 1).
So pick the state by index and use a single job without a matrix.

```yaml
jobs:
  build:
    uses: DepTyCheck/idris2-actions/.github/workflows/build-and-test.yml@v1

  run:
    needs: build
    runs-on: ubuntu-latest
    container: ghcr.io/stefan-hoeck/idris2-pack:latest
    steps:
      - uses: actions/checkout@v6
        with:
          persist-credentials: false

      # Basic way to restore
      - name: Restore the latest collection build using index
        uses: DepTyCheck/idris2-actions/.github/actions/restore-pack-state@v1
        with:
          state: ${{ fromJSON(needs.build.outputs.pack_states)[0] }}

      # Beautiful way to restore first pack state
      - name: Restore the latest collection build using pack_state
        uses: DepTyCheck/idris2-actions/.github/actions/restore-pack-state@v1
        with:
          state: ${{ needs.build.outputs.pack_state }} # pack_state is an alias for pack_states[0]

      - name: Run the package
        working-directory: ${{ needs.build.outputs.project_dir }}
        run: pack run ${{ needs.build.outputs.package }}
```

## Custom pack-state jobs

If you build the package yourself, call `upload-pack-state` at the end of that job
and expose its `state` output on the job. A later job can pass that single value to `restore-pack-state`.

```yaml
jobs:
  build-pack-state:
    runs-on: ubuntu-latest
    container: ghcr.io/stefan-hoeck/idris2-pack:latest
    # 1. Expose the output
    outputs:
      state: ${{ steps.upload-pack-state.outputs.state }}
    steps:
      - uses: actions/checkout@v6
        with:
          persist-credentials: false

      - run: pack build my-package

      # 2. Call upload action
      - name: Upload pack state
        id: upload-pack-state
        uses: DepTyCheck/idris2-actions/.github/actions/upload-pack-state@v1
        with:
          state: pack-state-my-package-latest

  use-pack-state:
    # 3. Declare build job as dependency
    needs: build-pack-state
    runs-on: ubuntu-latest
    container: ghcr.io/stefan-hoeck/idris2-pack:latest
    steps:
      - uses: actions/checkout@v6
        with:
          persist-credentials: false

      # 4. Restore pack state using build job reference
      - uses: DepTyCheck/idris2-actions/.github/actions/restore-pack-state@v1
        with:
          state: ${{ needs.build-pack-state.outputs.state }}

      - run: pack run my-package
```

## Development

Install `jq` and `tomlq`, then:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
./scripts/format.sh
.venv/bin/python -m unittest discover -s tests -v
```

Linter configs live in `.github/linters`.
