# idris2-actions

A reusable GitHub Actions workflow that builds and tests any
[pack](https://github.com/stefan-hoeck/idris2-pack)-managed Idris2 project.

It runs `pack build` and `pack test` against both the latest pack collection and the bleeding-edge Idris2 compiler
(the bleeding-edge run is skipped automatically when it matches the version from latest pack collection).

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

      # Optional. Default: true. Run `pack test` after building. Set false to only build and cache.
      # run_tests: true
```

## Outputs

The workflow exposes outputs so a downstream job can restore the build it cached:

| Output        | Description                                                       |
| ------------- | ----------------------------------------------------------------- |
| `variants`    | JSON array of `{ mode, cache_key }`, one entry per built variant. |
| `package`     | Package name parsed from the `.ipkg`.                             |
| `project_dir` | Directory holding the `.ipkg` and `pack.toml`.                    |
| `build_dir`   | The package's build dir (relative to `project_dir`).              |

## Restoring a cached build

The build job caches project build per variant.
A downstream job can restore that cache with the `restore-cache` action and use the build.

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
        variant: ${{ fromJSON(needs.build-for-cache.outputs.variants) }}
    steps:
      - name: Checkout
        uses: actions/checkout@v6
        with:
          persist-credentials: false

      - name: Restore the cached build
        id: restore
        uses: DepTyCheck/idris2-actions/.github/actions/restore-cache@v1
        with:
          mode: ${{ matrix.variant.mode }}
          cache_key: ${{ matrix.variant.cache_key }}
          project_dir: ${{ needs.build-for-cache.outputs.project_dir }}
          build_dir: ${{ needs.build-for-cache.outputs.build_dir }}

      - name: Run the package
        working-directory: ${{ needs.build-for-cache.outputs.project_dir }}
        run: pack run ${{ needs.build-for-cache.outputs.package }}
```

## Using a single variant

Sometimes you just want one build and to run a command against it.
The `variants` array is ordered: element 0 is always `latest-pack-collection` (the `bleeding-edge-compiler` entry, when present, is element 1).
So pick that variant by index and use a single job without a matrix.

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

      - name: Restore the latest-pack-collection build
        id: restore
        uses: DepTyCheck/idris2-actions/.github/actions/restore-cache@v1
        with:
          mode: ${{ fromJSON(needs.build.outputs.variants)[0].mode }}
          cache_key: ${{ fromJSON(needs.build.outputs.variants)[0].cache_key }}
          project_dir: ${{ needs.build.outputs.project_dir }}
          build_dir: ${{ needs.build.outputs.build_dir }}

      - name: Run the package
        working-directory: ${{ needs.build.outputs.project_dir }}
        run: pack run ${{ needs.build.outputs.package }}
```

## Real examples

Todo. idris2-summary-stat, DepTyCheck, verilog-model and more...
