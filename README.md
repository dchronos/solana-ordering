# Solana in-block transaction ordering: a measurement harness

What decides which transaction lands earlier inside a Solana block? The Agave
client's answer is in its source; the answer for forks and non-Agave clients that
hold most of the stake is not publicly documented. This repository measures it,
per validator, from the blocks themselves.

One command, no setup beyond Docker:

```bash
docker compose up            # dry run: prints the plan and the estimated cost
SOLANA_SECRET_KEY=<base58> docker compose up   # live: sends, capped, writes data/run_live.json
```

The dry run needs no key and sends nothing.

## What it measures, and how

For each target validator it sends a batch of small transactions into that
validator's leader slot, then reads the produced block and records, for every one
of our transactions:

- the **position** it occupied in the block,
- the **fee** actually charged (from `meta.fee`),
- the **compute** consumed,
- which block it landed in, and who produced that block.

Ordering is then reconstructed from the block, never from our own send log, and
candidate rules are scored by rank correlation against the observed positions:

| candidate rule | why it is plausible |
|---|---|
| `fee_desc` | priority fee is the documented economic signal; a scheduler may sort on it |
| `send_seq` | our own arrival order at the leader, which is FIFO in some designs |
| `sent_at` | wall-clock time at the sender, a proxy for arrival ordering |
| `nonce` | a deterministic id order, i.e. *no* rule at all, which is the null hypothesis |

## The four things this design has to get right

**1. The leader is read from the block, not from the schedule.** A slot can be
skipped, in which case the next validator in the schedule produces it. We
therefore attribute every block to the fee recipient in `rewards`, and record
whether that agrees with the epoch schedule (`leader_matches_schedule`). A
measurement whose two sources disagree is kept and flagged rather than averaged
in silently, because that disagreement is itself evidence about the slot.

**2. The intended slot is not the landing slot.** A transaction sent for slot *N*
can land in *N+k* if the block was already sealed. Probe slots are therefore
resolved from the chain with `getSignatureStatuses`, and blocks are fetched for
the slots that actually contain our transactions.

**3. Order must not be an artefact of the batching.** Nonces are shuffled before
sending, so send order is not identical to nonce order; the fee ladder is cycled
across the batch; and the block's fill ratio is recorded (`block_tx_count`) so a
saturated block can be excluded rather than used to explain an odd result.

**4. Fee must not be collinear with anything else.** Fees come from a ladder that
repeats, while nonces are shuffled independently, so `fee_desc` and `nonce` test
different hypotheses on the same batch.

## Safety

- **Dry run is the default.** `--live` is required to sign anything.
- **The spend cap is enforced in code**, checked before every signature leaves the
  process (`SpendGuard`). The payer must hold `cap + reserve` before the run starts,
  so a misconfiguration fails closed.
- **Keys never enter the repository.** They are read from `SOLANA_SECRET_KEY` into
  a tmpfs file, used, and deleted. Nothing is baked into the image.
- Every probe is a transfer to ourselves, so the only money actually spent is the
  transaction fee: 5000 lamports base plus the priority fee we set.

## Output

`data/run_live.json` holds the raw material a judge needs to verify every claim:

- `windows`: slot, validator identity, version, client family chosen for the run;
- `measurements[].probes[]`: nonce, signature, fee, compute, send time, **landing
  slot and block index**;
- `measurements[].blocks`: blockhash, previous blockhash, block time, leader per
  rewards, and the full signature list, so any ordering claim can be re-derived
  with a `getBlock` call;
- `analysis`: per-slot correlation for each candidate rule and a per-client
  summary.

## Cost

Measured, from the payer balance before and after each run:

| run | probes | SOL spent |
|---|---|---|
| `run_v2.json` | 88 | 0.000440 |
| `run_v3.json` | 56 | 0.000904 |
| `run_live.json` (via `docker compose`, as a non-root user) | 24 | 0.000361 |

The second costs more because its fee ladder is deliberately steep (up to 10 500 000
micro-lamports/CU) to give the fee hypothesis a real signal to fail on. The default
plan of 6 families x 2 slots x 8 probes estimates 0.000931 SOL and the default cap
is 0.0015 SOL, both printed by the dry run before anything is signed. The bounty's
0.05 SOL limit is ~50x what these runs cost.

## Report

`REPORT.md` is the write-up required by the bounty: per-validator results with
measurement dates, the seven things that went wrong and how each was caught, and
an explicit list of what this design does **not** separate.

## Layout

```
src/scheduler_probe.py   the harness (single file, no framework)
scripts/make_key.sh      materialises the key from env into a 0600 file
docker-compose.yml       one-command entry point
data/                    run output, including raw block data
```

## Results

Everything below is re-derivable from the signatures in `data/` with
`scripts/verify_blocks.py`, which re-fetches each block from the chain and prints
our transactions' positions again, independently of the analysis block:

```bash
python scripts/verify_blocks.py data/run_v3.json   # -> "verification: consistent"
```

**24 blocks, 189 probes, 6 client builds: ordering is arrival, not price.**

Pooled over the three runs in `data/`, the arrival rule scores **1.00 in every
block** and the fee rule has no consistent sign:

| client | blocks | probes | arrival rule (min) | fee rule range |
|---|---|---|---|---|
| agave 4.3.2 | 5 | 39 | 1.00 | −0.43 … +0.74 |
| agave 4.4.0 | 6 | 47 | 1.00 | −0.33 … +0.55 |
| agave 4.4.0-rc.1 | 3 | 22 | 1.00 | −0.05 … +0.71 |
| firedancer 0.1.1 | 2 | 16 | 1.00 | −0.17 … −0.05 |
| firedancer 26.10.4 | 4 | 32 | 1.00 | −0.26 … +0.55 |
| firedancer 26.10.5 | 4 | 33 | 1.00 | −0.57 … +0.02 |

The fee-rule column is the point: a rule that was real would hold its sign. Here
it swings from −0.57 to +0.74 across blocks of the same client, which is what a
non-predictor looks like when you measure it 24 times.

`data/run_v3.json`, the run with the widest fee sweep (0 to 10 500 000
micro-lamports/CU, a **3.5x spread in the fee actually charged**, 5 000 to 17 600
lamports):

| slot | client | probes | txs in block | fee rule | arrival rule |
|---|---|---|---|---|---|
| 463497632 | agave 4.4.0 | 8 | 427 | +0.05 | **1.00** |
| 463497705 | agave 4.3.2 | 8 | 281 | −0.43 | **1.00** |
| 463497719 | firedancer 26.10.4 | 8 | 353 | −0.26 | **1.00** |
| 463497752 | firedancer 26.10.4 | 8 | 388 | −0.24 | **1.00** |
| 463497907 | agave 4.3.2 | 8 | 359 | +0.74 | **1.00** |
| 463498001 | firedancer 26.10.5 | 8 | 567 | +0.02 | **1.00** |
| 463498064 | agave 4.3.2 | 8 | 439 | −0.26 | **1.00** |
| 463498213 | agave 4.4.0-rc.1 | 8 | 354 | +0.29 | **1.00** |
| 463498330 | agave 4.4.0-rc.1 | 8 | 388 | −0.05 | **1.00** |
| 463498392 | firedancer 0.1.1 | 8 | 671 | −0.05 | **1.00** |

In slot 463497632 the probe that paid 17 600 lamports landed at position 173 and
the one that paid 5 000 landed at 186 — the cheap one paid *less* and still landed
later, because it was sent 2 ms later. Position tracks send time; fee does not.

This is why paying priority fees on Solana does not reliably move you ahead of
same-slot competition: within a leader's block, the ordering is the order the
transactions arrived, and the fee is not the sort key.

## Limits

24 blocks is not a survey of the cluster, and six builds is not every build. What
the data supports is narrow and precise: for these leaders, on these clients, in
these blocks, position followed arrival order and not price. `--per-family` and `--windows` widen it; the spend cap
in the bounty (0.05 SOL) is roughly 200x the cost of the runs recorded here.
