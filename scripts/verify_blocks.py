#!/usr/bin/env python3
"""Re-derive every ordering claim from raw block data.

Judges should not have to trust the analysis block in run.json. This script takes
the raw signatures recorded by the probe, re-fetches each block from the chain,
and prints the ordering evidence directly: for every slot, the validator that
produced it and the positions our transactions actually occupy.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from scheduler_probe import Rpc, _leader_from_rewards  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run", help="path to data/run_live.json")
    ap.add_argument("--rpc", default="https://api.mainnet-beta.solana.com")
    args = ap.parse_args()

    run = json.loads(pathlib.Path(args.run).read_text())
    rpc = Rpc(args.rpc)
    problems = 0

    for m in run.get("measurements", []):
        wanted = {p["signature"]: p for p in m["probes"] if p.get("signature")}
        if not wanted:
            continue
        for slot_key, recorded in m.get("blocks", {}).items():
            block = rpc.block(int(slot_key))
            if not block:
                print(f"slot {slot_key}: block no longer retrievable")
                problems += 1
                continue
            leader = _leader_from_rewards(block)
            txs = block.get("transactions") or []
            print(f"\nslot {slot_key}  leader={leader}  txs={len(txs)}  blockhash={block.get('blockhash')}")
            if leader != recorded.get("leader_per_rewards"):
                print(f"  !! recorded leader {recorded.get('leader_per_rewards')} != chain {leader}")
                problems += 1
            hits = []
            for idx, tx in enumerate(txs):
                sigs = tx.get("transaction", {}).get("signatures") or []
                if sigs and sigs[0] in wanted:
                    meta = tx.get("meta") or {}
                    hits.append((idx, wanted[sigs[0]]["nonce"], meta.get("fee"), meta.get("computeUnitsConsumed")))
            for idx, nonce, fee, cu in hits:
                print(f"  pos {idx:4d}  nonce {nonce:3d}  fee {fee} lamports  cu {cu}")
            if len(hits) < 3:
                print(f"  note: only {len(hits)} of our transactions in this block")

    print("\nverification:", "consistent" if problems == 0 else f"{problems} discrepancy(ies)")
    return 0 if problems == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
