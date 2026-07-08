# idris2-actions

A reusable GitHub Actions workflow that builds and tests any
[pack](https://github.com/stefan-hoeck/idris2-pack)-managed Idris2 project.

It runs `pack build` and `pack test` against both the `latest` and `HEAD` pack collections
(the `HEAD` run is skipped automatically when it matches the compiler version from the `latest` collection).

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

      # Optional. Default: true. Run `pack test` after building. Set false to only build and cache.
      # run_tests: true
```

## Outputs

The workflow exposes outputs so a downstream job can restore the build it cached.

| Output        | Description                                             |
| ------------- | ------------------------------------------------------- |
| `collections` | JSON array of pack collection names.                    |
| `pack_states` | JSON array of restore handles, one per pack collection. |
| `package`     | Package name parsed from the `.ipkg`.                   |
| `project_dir` | Directory holding the `.ipkg` and `pack.toml`.          |
| `build_dir`   | The package's build dir (relative to `project_dir`).    |

## Restoring pack state

The build job caches project build per collection.
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
With the automatic selection, the `collections` array is ordered: element 0 is always `latest` (the `HEAD` entry, when present, is element 1).
So pick that collection by index and use a single job without a matrix.

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

      - name: Restore the latest collection build
        id: restore
        uses: DepTyCheck/idris2-actions/.github/actions/restore-pack-state@v1
        with:
          state: ${{ fromJSON(needs.build.outputs.pack_states)[0] }}

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
          package: my-package
          collection: latest

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

## Real examples

Todo. idris2-summary-stat, DepTyCheck, verilog-model and more...
