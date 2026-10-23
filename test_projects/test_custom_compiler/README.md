# Custom compiler fixture

Builds and tests a library using the compiler fork and commit in `pack.toml`.
The reusable workflow should select only `latest` and cache the custom compiler.
A separate job restores its artifact, checks the compiler revision, and runs
`pack test` with `make` blocked. This fails if pack tries to rebuild the compiler.
Repeat the workflow to also exercise a warm GitHub build cache.

Run locally with `pack test test_custom_compiler` from this directory.
