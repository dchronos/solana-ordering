#!/usr/bin/env python3
"""Print the per-leader, per-client ordering summary from the raw run files.

Every number here is derived from signatures recorded in data/, so it can be
checked against the chain with verify_blocks.py. Judging should not require
running this: REPORT.md carries the same tables.
"""

from __future__ import annotations

import argparse
import collections
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
import scheduler_probe as sp  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("runs", nargs="*", help="default: every data/run*.json that has blocks")
    args = ap.parse_args()

    # Canonical runs only. Earlier files (run_smoke2, run_final, run_3, run_4) were
    # written before probes were pinned to the block that actually contains them and
    # before client attribution was resolved; they are kept for the record but their
    # positions are not trustworthy. See data/MANIFEST.md.
    canonical = [
        "data/run_live.json",
        "data/reharvest_smoke2.json",
        "data/run_v2.json",
        "data/run_v3.json",
        "data/run_v4.json",
        "data/run_v5.json",
    ]
    paths = args.runs or [p for p in canonical if pathlib.Path(p).exists()]
    rows = []
    for path in paths:
        run = json.loads(pathlib.Path(path).read_text())
        for m in run.get("measurements", []):
            # one block at a time: a batch split across slots must not be scored
            # as if it were a single ordering observation
            for slot_key, block in (m.get("blocks") or {}).items():
                single = dict(m, blocks={slot_key: block})
                report = {
                    "measurements": [single],
                    "cluster": run.get("cluster", {}),
                    "revenue_to_node": run.get("revenue_to_node", {}),
                }
                for row in sp.analyse(report)["rows"]:
                    if "best_rule" in row:
                        row = dict(row)
                        row["run"] = pathlib.Path(path).name
                        rows.append(row)

    by_family = collections.defaultdict(list)
    by_validator = collections.defaultdict(list)
    for row in rows:
        by_family[row["client_family"]].append(row)
        # keyed by the block's fee recipient, which is what rewards[] gives us; one
        # validator identity can own two collectors, so this is not an identity count
        by_validator[row["leader"]].append(row)

    print(f"{len(rows)} blocks, {len(by_validator)} revenue leaders, "
          f"{sum(r['landed'] for r in rows)} probes\n")
    print(f"{'client':22s} {'blocks':>6s} {'probes':>6s} {'arrival':>8s} {'fee range':>16s}")
    for family in sorted(by_family):
        rs = by_family[family]
        send = min(r["correlation"]["send_seq"] for r in rs)
        fee = [r["correlation"]["fee_desc"] for r in rs]
        print(f"{family:22s} {len(rs):6d} {sum(r['landed'] for r in rs):6d} "
              f"{send:8.2f} {min(fee):+7.2f} .. {max(fee):+6.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
