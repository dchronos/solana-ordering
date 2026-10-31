# Report: how Solana leaders order transactions inside a block

Measurement dates: **2026-10-30 22:47 UTC – 2026-10-31 01:38 UTC**. All runs are
after the Alpenglow upgrade of 2026-10-21, so no measurement mixes pre- and
post-upgrade behaviour. Network: Solana mainnet-beta throughout.

## Answer

**Ordering is arrival order. The priority fee is not the sort key.**

Across 40 distinguishable blocks, 14 validator identities (22 distinct
block-revenue leaders: an identity can route its block revenue to a separate
collector, which is why the per-validator table has 22 rows) and 6 client builds
(Agave 4.3.2, 4.4.0, 4.4.0-rc.1; Firedancer 0.1.1, 26.10.4, 26.10.5), the rank
correlation between our transactions' send order and their position in the block
is **1.00 in every single block**. The same correlation against the fee actually
charged averages near zero and does not hold a sign: within one client it ranges
from −1.00 to +0.80. That is a non-predictor, measured 40 times.

The strictness is worth stating precisely: 1.00 with 4–12 probes per block means
the block order is the send order **exactly**, not approximately. There is no
reordering, no interleaving of our batch with our own later sends, and no evidence
of fee-priority insertion *within* a batch.

## Method, in one paragraph

For each target validator we detect its upcoming leader slot from the epoch
schedule, pre-sign a batch of 8–12 self-transfers worth 5 000 lamports plus a
swept priority fee, and fire every primary send at once about 6 slots before the
target so the batch arrives inside the leader window. After settling we ask the
chain, per signature, which slot it landed in (`getSignatureStatuses`), fetch
those blocks, attribute each block to the fee recipient in `rewards`, and score
four candidate rules by Spearman rank correlation against the observed positions.

## Per-validator results

Rows are keyed by the block's fee recipient (`rewards[].pubkey`), which is what
the chain lets us attribute a block to. Where a vote account routes revenue to a
separate collector, one identity owns two rows; 22 rows cover 14 identities.

| fee recipient | version | blocks | probes | arrival | fee range | measured (UTC) |
|---|---|---|---|---|---|---|
| `Fd7btg…69Nk` | 4.4.0 | 5 | 32 | **1.00** | -1.00 … +0.80 | 2026-10-30 23:51 – 01:38 |
| `FBbqKv…Se89` | 0.1.1 | 4 | 24 | **1.00** | -0.40 … +0.20 | 2026-10-30 23:51 – 01:25 |
| `E1r4Ps…dxHL` | 4.3.2 | 4 | 27 | **1.00** | -1.00 … -0.07 | 2026-10-30 23:51 – 01:26 |
| `CQdLgw…ow12` | 26.10.4 | 3 | 24 | **1.00** | -0.24 … +0.62 | 2026-10-31 01:02 – 01:38 |
| `9eGrDo…8FoY` | 26.10.5 | 3 | 23 | **1.00** | -0.57 … +0.02 | 2026-10-31 00:57 – 01:39 |
| `87CcUx…LJL5` | 26.10.4 | 2 | 16 | **1.00** | -0.26 … +0.55 | 2026-10-30 23:58 – 01:07 |
| `JUPiTE…1h4b` | 26.10.5 | 2 | 15 | **1.00** | -0.14 … +0.48 | 2026-10-31 00:57 – 01:39 |
| `CAo1dC…Sve4` | 4.3.2 | 2 | 16 | **1.00** | -0.14 … +0.74 | 2026-10-31 01:07 – 01:38 |
| `GoeW4a…tDK9` | 4.4.0-rc.1 | 2 | 14 | **1.00** | -0.05 … +0.71 | 2026-10-31 01:04 – 01:09 |
| `pSo1KZ…nkfc` | 26.10.4 | 1 | 8 | **1.00** | -0.59 … -0.59 | 2026-10-31 01:38 |
| `JD549H…HybB` | 26.10.5 | 1 | 11 | **1.00** | -0.04 … -0.04 | 2026-10-30 23:58 |
| `5pPRHn…HzSm` | 4.3.2 | 1 | 4 | **1.00** | +0.00 … +0.00 | 2026-10-30 23:51 |
| `9UM8wQ…Jd9i` | 4.3.2 | 1 | 8 | **1.00** | -0.43 … -0.43 | 2026-10-31 01:07 |
| `EvnRmn…qDo4` | 4.3.2 | 1 | 8 | **1.00** | -0.48 … -0.48 | 2026-10-31 01:39 |
| `5Cchr1…q9Ux` | 4.3.2 | 1 | 8 | **1.00** | +0.38 … +0.38 | 2026-10-31 01:43 |
| `EUcJwf…XctB` | 4.4.0 | 1 | 7 | **1.00** | +0.46 … +0.46 | 2026-10-31 00:56 – 00:56 |
| `HEL1US…e2TU` | 4.4.0 | 1 | 8 | **1.00** | +0.38 … +0.38 | 2026-10-31 00:57 |
| `shftkx…Vikj` | 4.4.0 | 1 | 8 | **1.00** | +0.55 … +0.55 | 2026-10-31 00:57 |
| `J1to5o…Mr4X` | 4.4.0 | 1 | 8 | **1.00** | +0.52 … +0.52 | 2026-10-31 00:57 |
| `C8Bey3…JP1k` | 4.4.0 | 1 | 8 | **1.00** | -0.02 … -0.02 | 2026-10-31 01:43 |
| `Ed9WjP…a2Zv` | 4.4.0 | 1 | 8 | **1.00** | +0.41 … +0.41 | 2026-10-31 01:45 |
| `hy1oMa…Xnwu` | 4.4.0-rc.1 | 1 | 8 | **1.00** | +0.29 … +0.29 | 2026-10-31 01:08 |

`arrival rule` is the Spearman correlation between send order and in-block
position; `fee rule` is the same against the fee paid. A value of 1.00 means the
block reproduced our send order exactly.

## What went wrong

Every one of these produced a plausible, wrong answer before it was caught. This
section exists because the bounty asks for it and because each one is a way for a
judge to be misled by data that looks fine.

1. **The payer was below the rent-exempt minimum and nothing said so.** With
   655 175 lamports, the runtime rejects every transaction with
   `InsufficientFundsForRent`. Our client sends with `skipPreflight` after the
   first attempt, so the RPC returned signatures anyway; the harvest then found
   no blocks and the analysis reported "0 landed". Read naively, that is a
   measurement of "this validator includes nothing", which is false. The run now
   refuses to start unless the payer holds the 890 880-lamport floor plus reserve.
2. **Sequential sending split the batch across blocks.** Six sends one after
   another took about a second, which is several slots, so each block held one or
   two probes and no rule could be tested. With 3 send workers a 10-probe batch
   still landed in 4–6 blocks. Fixed by firing all primaries at once and running
   the backup-RPC fan-out on a separate pool; 18 of the 27 measured batches now
   land entirely in one block, the rest in two.
3. **A probe intended for slot N landed in slot N−1 or N+1.** In one early run 5
   of 12 probes were included by a different validator. Attribution is therefore
   per *block* (`rewards[].pubkey`), never per intended slot, and off-target
   blocks are kept in the data and excluded from per-validator statistics.
4. **The block's fee recipient is not always the validator identity.** One block
   credited a pubkey absent from the leader schedule: its vote account routes
   block revenue to a separate collector at 100 percent commission. Treated as an
   identity it yields an "unknown client" row and silently drops a real
   measurement. It is resolved through the vote account's `nodePubkey`.
5. **A stale getBlock schema broke the harvest silently.** Asking for
   `maxSupportedTransactionVersion: 0` fails the whole call when the block
   contains v1 transactions, so the block fetch returned null and the probes were
   recorded as not found. Fixed to version 1.
6. **The first fee ladder could not test the fee hypothesis.** A step of 2 500
   micro-lamports/CU at a 1 200 CU limit changes the fee by 3 lamports per step:
   a 5 000 to 5 033 spread. The null result for fee on that data meant nothing.
   The ladder now spans 5 000 to 17 600 lamports (3.5x) in the wide run.
7. **The verifier did not check the thing it claimed to check.** It compared the
   block's leader against the recorded one and printed positions, but never
   asserted that the probes the analysis counted are actually *in* that block. In
   `run_smoke2.json` (kept for the record, excluded from all numbers here) 12 of 24
   counted probes were attributed to a block that does not contain them, and the
   verifier still printed "consistent". It now compares claimed signatures against
   the fetched block and fails; that file reports 2 discrepancies.
8. **A single public RPC aborted a run after the fees were already paid.** The
   harvest now retries reads across endpoints; the write path stays on the
   configured RPC.

## What this design does not separate

Stated plainly, because a null result about fee is only as good as its confounds:

- **Arrival order and send order are the same signal here.** We measure the order
  in which our own sends reached the leader; we do not instrument the leader's
  internal queue. That is the correct operationalisation of FIFO, but it means
  this experiment cannot distinguish "FIFO by arrival" from "FIFO by receipt" —
  only that fee is not the key.
- **Our batch is not competing for a full block.** Blocks held 200 to 670
  transactions; we contribute 7 to 11. A fee rule could exist that only activates
  when the block is oversubscribed, and this design would not see it.
- **Single-hop send path.** Everything went through one RPC provider plus a
  backup. A different path (TPU QUIC directly to the leader, Jito bundles) can
  change arrival, and therefore position, without any scheduler rule changing.
  Jito tips and bundles are explicitly out of scope for the bounty and for us.
- **No stake-weighted survey.** 14 identities is not 14 representative
  identities; large-stake leaders appear more often in the sample simply because
  they get more slots.
- **Client version is read from the cluster snapshot at analysis time**, which is
  not necessarily the version the node ran at block time if it upgraded between
  the measurement and the snapshot. The gap here is minutes, not days.

## Reproduce

```bash
python scripts/verify_blocks.py data/run_v3.json   # re-derive positions from chain
docker compose up                                  # dry run, prints the plan and cost
SOLANA_SECRET_KEY=<base58> docker compose up       # live, capped, writes data/
```

Raw data for every claim is in `data/`: per-probe nonce, signature, fee, compute,
send timestamp, landing slot and block index, plus per-block signature lists.
`data/MANIFEST.md` says which files are canonical and which are superseded (with
the reason), so nothing stale can be mistaken for evidence.
Total spent across all runs in this repository: **0.00235 SOL**, against the
bounty's 0.05 SOL limit.
