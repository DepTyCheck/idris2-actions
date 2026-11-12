#!/bin/bash
# Run a package whose build was restored from an artifact.
# Fail if the executable is missing or pack run rewrites it.
set -euo pipefail

BUILD_DIR="${BUILD_DIR:?BUILD_DIR is required}"
PACKAGE="${PACKAGE:?PACKAGE is required}"
exe="$BUILD_DIR/exec/$PACKAGE"
if [ ! -x "$exe" ]; then
	echo "error: restored build is missing $exe" >&2
	find "$BUILD_DIR" -type f >&2 || true
	exit 1
fi

before=$(sha256sum "$exe")
pack run "$PACKAGE"
after=$(sha256sum "$exe")
if [ "$before" != "$after" ]; then
	echo "error: pack run rebuilt $exe from the restored build" >&2
	exit 1
fi
