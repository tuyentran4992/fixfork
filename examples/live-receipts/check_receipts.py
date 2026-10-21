#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Validate the live model-call receipt samples in this folder.

Each ``receipt-*.json`` must hold at least one attempt record with an integer
latency, positive token counts, a positive cost and a 64-hex SHA-256 digest of
the raw response bytes. The digests across all samples must be distinct (the
same reply parsed twice would produce the same digest, so duplicates mean the
samples are not independent calls).

Exit code: 0 when every check passes, 1 otherwise.
"""
import glob
import json
import os
import re
import sys

HEX64 = re.compile(r"^[0-9a-f]{64}$")
HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    paths = sorted(glob.glob(os.path.join(HERE, "receipt-*.json")))
    if not paths:
        print("FAIL: no receipt-*.json files")
        return 1
    seen = {}
    ok = True
    rows = []
    for p in paths:
        name = os.path.basename(p)
        try:
            rec = json.load(open(p, encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 - report, do not crash
            print(f"FAIL {name}: not valid JSON ({exc})")
            ok = False
            continue
        for key in ("captured_at_utc", "endpoint", "model", "attempts"):
            if key not in rec:
                print(f"FAIL {name}: missing key {key!r}")
                ok = False
        attempts = rec.get("attempts") or []
        if not attempts:
            print(f"FAIL {name}: no attempt records")
            ok = False
            continue
        for i, a in enumerate(attempts):
            lat = a.get("latency_ms")
            digest = a.get("response_sha256") or ""
            ptok = a.get("prompt_tokens")
            ctok = a.get("completion_tokens")
            cost = a.get("cost_usd")
            if not isinstance(lat, int) or isinstance(lat, bool) or lat <= 0:
                print(f"FAIL {name}[{i}]: latency_ms must be a positive integer")
                ok = False
            if not isinstance(ptok, int) or ptok <= 0:
                print(f"FAIL {name}[{i}]: prompt_tokens must be a positive integer")
                ok = False
            if not isinstance(ctok, int) or ctok <= 0:
                print(f"FAIL {name}[{i}]: completion_tokens must be a positive integer")
                ok = False
            if not isinstance(cost, (int, float)) or cost <= 0:
                print(f"FAIL {name}[{i}]: cost_usd must be positive")
                ok = False
            if not HEX64.match(digest):
                print(f"FAIL {name}[{i}]: response_sha256 must be 64 lowercase hex chars")
                ok = False
            elif digest in seen:
                print(f"FAIL {name}[{i}]: digest duplicates {seen[digest]}")
                ok = False
            else:
                seen[digest] = f"{name}[{i}]"
            rows.append((name, i, lat, digest[:16], ctok, cost))
    for name, i, lat, dg, ctok, cost in rows:
        print(f"{name}[{i}]  latency={lat}ms  digest={dg}...  completion_tokens={ctok}  cost=${cost:.8f}")
    print(f"{'PASS' if ok else 'FAIL'}: {len(rows)} receipt record(s) from {len(paths)} sample file(s), "
          f"{len(seen)} distinct digest(s)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
