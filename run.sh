#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p runtime
exec /usr/bin/env python3 guest_gateway.py --poll >>runtime/guest-gateway.out.log 2>>runtime/guest-gateway.err.log
