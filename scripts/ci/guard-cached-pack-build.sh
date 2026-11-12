#!/bin/bash
# pre_build for a job that should have restored the GitHub Actions cache.
# The next `pack build` fails if it changes the restored .ttc files.
# Idris rewrites the executable on every build, so that file is not compared.
# Usage: guard-cached-pack-build.sh <executable-name>
set -euo pipefail

executable="${1:?executable name}"
BUILD_DIR="${BUILD_DIR:?BUILD_DIR is required}"
build_dir="$PWD/$BUILD_DIR"
exe="$build_dir/exec/$executable"
if [ ! -x "$exe" ]; then
	echo "error: GitHub Actions cache did not restore $exe" >&2
	find "$build_dir" -type f >&2 || true
	exit 1
fi

snapshot="$RUNNER_TEMP/cached-build.sha256"
find "$build_dir" -name '*.ttc' -type f -print0 |
	sort -z | xargs -0 sha256sum >"$snapshot"

real_pack=$(command -v pack)
wrap="$RUNNER_TEMP/pack-wrap"
mkdir -p "$wrap"
cat >"$wrap/pack" <<'EOF'
#!/bin/sh
set -eu
"$REAL_PACK" "$@"
rc=$?
after=$(find "$CACHED_BUILD_DIR" -name '*.ttc' -type f -print0 | sort -z | xargs -0 sha256sum)
before=$(cat "$CACHED_BUILD_SNAPSHOT")
if [ "$after" != "$before" ]; then
  echo "error: pack build changed .ttc files restored from the GitHub Actions cache" >&2
  exit 1
fi
exit "$rc"
EOF
chmod +x "$wrap/pack"
{
	echo "REAL_PACK=$real_pack"
	echo "CACHED_BUILD_DIR=$build_dir"
	echo "CACHED_BUILD_SNAPSHOT=$snapshot"
	echo "PATH=$wrap:$PATH"
} >>"$GITHUB_ENV"
