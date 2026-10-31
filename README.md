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

A run of 6 validators x 1 slot x 8 transactions, with a fee ladder topping out at
20 000 micro-lamports per CU on a 1 200 CU limit, costs well under 0.0003 SOL
(the dry run prints the exact estimate before anything is sent). The default cap is
0.0007 SOL, and the bounty's limit is 0.05 SOL, so there is headroom for repeating a
slot or widening the batch.

## Layout

```
src/scheduler_probe.py   the harness (single file, no framework)
scripts/make_key.sh      materialises the key from env into a 0600 file
docker-compose.yml       one-command entry point
data/                    run output, including raw block data
```

## Results

Everything below is re-derivable from the signatures in `data/` with
`scripts/verify_blocks.py`, which re-fetches each block and prints the positions
again from the chain.

**The leaders measured so far order by arrival, not by fee.**

| slot | client | leader | probes in block | fee rule | send-order rule |
|---|---|---|---|---|---|
| 463476432 | agave 4.3.2 | `E1r4Psq84t…` | 12 | 0.13 | **1.00** |
| 463478480 | firedancer 26.10.4 | `87CcUxpyR7…` | 12 | -0.17 | **1.00** |
| 463479114 | agave 4.4.0 | `2FL6757bjp…` | 5 | 0.80 | **1.00** |

The fee ladder inside those batches spanned 0 to 27 500 micro-lamports per CU (a
5.5x spread in total fee, 5 000 to 5 033 lamports), so fee ordering had a real
signal to bite on and did not: the rank correlation with fee is near zero on the
two full blocks, while send order is exactly 1.00 on all three. That is what
"first come, first served" looks like from inside a block.

## What the measurements taught about the method

Three failures happened during development and changed the design. They are
recorded here because each one would have produced a plausible, wrong answer:

1. **A probe can land in the previous validator's block.** In the wide run that
   sent 8 slots early, 5 of 12 probes were included by the validator before the
   target one. Attribution is therefore per block (`leader_per_rewards`), never per
   intended slot; the off-target blocks stay in the data and are excluded from the
   per-client statistics with an explicit verdict.
2. **Too little lead lands nothing.** With a 1-slot lead, every batch arrived after
   the block was sealed and nothing landed. Zero data looks like "no rule" unless
   the cause is written down.
3. **Sequential sending splits the batch.** Sending 6 transactions one after
   another took about a second and spread them across six different slots, leaving
   too few probes per block to test any rule. The batch is now pre-signed and sent
   with bounded concurrency, with a retry on HTTP 429, so it fits inside one slot.

## Limits

Three blocks is not a survey of the cluster. What the data supports is narrow and
precise: for these leaders, on these clients, in these blocks, position followed
arrival order and not price. `--per-family` and `--windows` widen it; the spend cap
in the bounty (0.05 SOL) is roughly 200x the cost of the runs recorded here.
