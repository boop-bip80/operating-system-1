#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0
set -euo pipefail

# shellcheck source=common.sh
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
phase1_require python3
exec python3 "$PHASE_DIR/scripts/smoke-test.py" \
    --output "$OUTPUT_DIR" --phase-dir "$PHASE_DIR" "$@"
