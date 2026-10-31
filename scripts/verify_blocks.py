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
import time

import requests

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from scheduler_probe import Rpc, _leader_from_rewards  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run", help="path to data/run_live.json")
    ap.add_argument("--rpc", action="append", default=None,
                    help="repeatable; public RPCs rate-limit, so try several")
    args = ap.parse_args()

    run = json.loads(pathlib.Path(args.run).read_text())
    urls = args.rpc or [
        "https://solana-rpc.publicnode.com",
        "https://api.mainnet-beta.solana.com",
    ]
    rpc = Rpc(urls[0])
    rpc.read_urls = list(dict.fromkeys(urls))
    problems = 0

    def block(slot: int) -> dict | None:
        """Fetch a block, backing off across endpoints on rate limits."""
        for attempt in range(8):
            for url in rpc.read_urls:
                try:
                    got = rpc.block_at(url, slot)
                except requests.exceptions.RequestException:
                    continue
                if got:
                    return got
            time.sleep(1.5 * (attempt + 1))
        return None

    for m in run.get("measurements", []):
        wanted = {p["signature"]: p for p in m["probes"] if p.get("signature")}
        if not wanted:
            continue
        for slot_key, recorded in m.get("blocks", {}).items():
            blk = block(int(slot_key))
            if not blk:
                # Only a mismatch if the run claims our transactions are in it.
                print(f"slot {slot_key}: block not retrievable from this endpoint")
                problems += 1
                continue
            # A slot number alone does not identify a block: the same slot exists on
            # every cluster. Without this check, verifying a localnet run against
            # mainnet silently compares two unrelated blocks and reports nonsense.
            if recorded.get("blockhash") and blk.get("blockhash") != recorded["blockhash"]:
                print(
                    f"\nslot {slot_key}: wrong cluster or ledger - recorded blockhash "
                    f"{recorded['blockhash'][:12]}…, chain returned {str(blk.get('blockhash'))[:12]}…"
                )
                problems += 1
                continue
            leader = _leader_from_rewards(blk)
            txs = blk.get("transactions") or []
            print(f"\nslot {slot_key}  leader={leader}  txs={len(txs)}  blockhash={blk.get('blockhash')}")
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
            # A probe the run says is in this block but that the chain does not
            # contain is a wrong attribution, not a small sample. Checking only the
            # leader misses it: a block can be the right leader and still not hold
            # the probes the analysis counted.
            claimed = {
                p["signature"]: p
                for p in m["probes"]
                if p.get("tx_index") is not None and str(p.get("slot")) == slot_key and p.get("signature")
            }
            found = {sig for sig in claimed if sig in {t.get("transaction", {}).get("signatures", [None])[0] for t in txs}}
            missing = set(claimed) - found
            if missing:
                print(f"  !! {len(missing)} of {len(claimed)} claimed probes are not in this block")
                problems += 1

    print("\nverification:", "consistent" if problems == 0 else f"{problems} discrepancy(ies)")
    return 0 if problems == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
