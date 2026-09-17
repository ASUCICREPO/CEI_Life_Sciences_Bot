#!/usr/bin/env bash
# Builds the life sciences packages tarball for the Code Interpreter sandbox.
# Run from CodeBuild or locally. Requires uv.
#
# Usage:
#   ./build_packages.sh                        # output to /tmp/ci-packages-build
#   ./build_packages.sh /path/to/output        # custom output dir
#   S3_DEST=s3://bucket/key.tar.gz ./build_packages.sh   # build + upload

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUTPUT_DIR="${1:-/tmp/ci-packages-build}"
ARCHIVE_NAME="life-sciences-packages.tar.gz"

cd "$SCRIPT_DIR"

# Verify lockfile exists and matches pyproject.toml
uv lock --check

# Export pinned requirements with SHA256 hashes
uv export --frozen --no-dev --no-emit-project > /tmp/requirements.txt

# Clean and recreate target directory
rm -rf "$OUTPUT_DIR/site-packages"
mkdir -p "$OUTPUT_DIR/site-packages"

# Install to flat target directory
# --require-hashes ensures every package matches the lockfile SHA256
uv pip install \
  --target "$OUTPUT_DIR/site-packages" \
  --python-platform linux \
  --python-version 3.12 \
  --require-hashes \
  -r /tmp/requirements.txt

# Create archive
tar -czf "$OUTPUT_DIR/$ARCHIVE_NAME" -C "$OUTPUT_DIR/site-packages" .

echo "Archive: $OUTPUT_DIR/$ARCHIVE_NAME ($(du -h "$OUTPUT_DIR/$ARCHIVE_NAME" | cut -f1))"

# Upload to S3 if destination is set
if [[ -n "${S3_DEST:-}" ]]; then
  aws s3 cp "$OUTPUT_DIR/$ARCHIVE_NAME" "$S3_DEST"
  echo "Uploaded to $S3_DEST"
fi
