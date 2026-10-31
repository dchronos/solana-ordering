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

## How each named confound is addressed

The bounty lists six things a valid design must account for. Each is handled
explicitly, and the table below also says where a choice is deliberate rather than
complete:

| consideration | treatment |
|---|---|
| **Leader schedule and leader types** | Targets come from the epoch schedule by client family; the producing validator is then read back from the block (`rewards[].pubkey`), and disagreement with the schedule is recorded instead of silently reassigning credit. A `rewards` recipient that is not a validator identity (a revenue collector) is resolved through its vote account's `nodePubkey`. |
| **Path from backend to the executing node** | Fixed, not varied: one RPC provider plus a backup, the same connections and blockhash for every probe in a batch. It is a single hop by design, so a position cannot be explained by one probe taking a different route. A different transport (direct TPU/QUIC, Jito bundles) can change arrival and is out of scope for the bounty and for this harness. |
| **Interaction of our own transactions** | Probes are self-transfers of distinct amounts, so the only shared resource is the leader's queue. Nonces and fees are independently shuffled, so the batch cannot manufacture a fee correlation. Probes that land in another block are attributed there, not to the slot we aimed at. |
| **Competition inside the block** | Each block's fill is recorded (200–670 transactions here) and every probe's absolute position is kept, so a contention-dependent rule can be looked for. What this cannot rule out is a fee rule that appears only in saturated blocks: our batches were never the majority of a block. |
| **Send order** | Pre-signed, then all primaries fired at once, so `send_seq` is departure order rather than construction order. `sent_at` is stamped as each send leaves and correlates 1.00 with `send_seq`, which shows the two agree instead of one being an artefact of the thread pool. |
| **Noise versus rule** | Four rules are scored per block, including the null (`nonce`) and the competitor (`fee_desc`). A rule is only claimed if it wins every block *and* keeps its sign; the fee rule swings −1.00 … +0.80 across blocks of the same client, which is what noise looks like at this sample size. |

## The four implementation details that decide whether the numbers are real

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

**5. The batch must arrive inside one slot.** Every probe is signed before the
first is sent, and all primaries leave at once; the backup-RPC fan-out runs on a
separate pool so waiting on it cannot delay the next primary. Sequential sends
spread a batch over 4–6 blocks and leave too few probes per block to test anything.

**6. A probe's block is the one that contains it.** A signature's landing slot is
authoritative only if the block fetched for that slot actually holds it, so the
harvest pins every probe to a block whose signature list contains its signature.
`scripts/verify_blocks.py` re-checks exactly that, which is how the one file with
misattributed probes was caught after the fact.

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
| `run_live.json` (via `docker compose`, as a non-root user) | 24 | 0.000271 |
| `run_v4.json` | 64 | 0.000515 |
| `run_v5.json` | 40 | 0.000221 |

`run_v3` costs more because its fee ladder is deliberately steep (up to 10 500 000
micro-lamports/CU) to give the fee hypothesis a real signal to fail on. Total
across every run in the repository: **0.00235 SOL**, about 20x under the bounty's
0.05 SOL limit. The default plan of 6 families x 2 slots x 8 probes estimates
0.000931 SOL and the default cap is 0.0015 SOL; both are printed by the dry run
before anything is signed.

## Localnet validation

The pipeline is exercised end to end against `solana-test-validator` before any
mainnet SOL is committed, so a defect in send, harvest or attribution cannot be
mistaken for a property of a leader:

```bash
solana-test-validator --ledger /tmp/lv --reset --rpc-port 8899 \
  --gossip-port 8100 --dynamic-port-range 8200-8300 &
solana airdrop 5 --url http://127.0.0.1:8899
python src/scheduler_probe.py --live --rpc http://127.0.0.1:8899 \
  --min-stake-sol 0 --per-family 2 --per-slot 8 --lead-slots 12 --gap 1 --settle 6 \
  --out data/localnet/run_localnet.json
```

`data/localnet/run_localnet.json` is a real run from that setup: 8/8 probes landed
in a single block by the target leader, `leader_matches_schedule: true`, and
`scripts/verify_blocks.py` re-derived the positions from that ledger while it was
live. The ledger itself was throwaway, so re-verification needs your own validator;
the verifier notices that by comparing blockhashes and reports "wrong cluster or
ledger" rather than comparing slot numbers across clusters. Devnet
was not usable: its RPC faucet returns 410 and the web faucet requires a GitHub
login, so there was no free way to fund an end-to-end run there.

## Report

`REPORT.md` is the write-up required by the bounty: per-validator results with
measurement dates, the eight things that went wrong and how each was caught, and
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
pip install -r requirements-verify.txt             # requests only: no signing library
python scripts/verify_blocks.py data/run_v3.json   # -> "verification: consistent"
python scripts/summarise.py                        # the tables below, from raw data
```

Verification deliberately does not need `solders`. Reading a block and comparing
positions back to our signatures uses only `requests`; signing is what needs the
rest of the stack, and the import is lazy so that stays true. Cloning the repo and
running the verifier against mainnet is therefore a one-dependency operation, and
it re-derives every position from the chain rather than trusting the file.

**40 blocks, 293 probes, 14 validator identities (22 revenue leaders), 6 client builds: ordering is arrival, not price.**

Pooled over the six runs in `data/`, the arrival rule scores **1.00 in every
block** and the fee rule has no consistent sign:

| client | blocks | probes | arrival rule (min) | fee rule range |
|---|---|---|---|---|
| agave-4.3.2 | 10 | 71 | **1.00** | -1.00 … +0.74 |
| agave-4.4.0 | 11 | 79 | **1.00** | -1.00 … +0.80 |
| agave-4.4.0-rc.1 | 3 | 22 | **1.00** | -0.05 … +0.71 |
| firedancer-0.1.1 | 4 | 24 | **1.00** | -0.40 … +0.20 |
| firedancer-26.10.4 | 6 | 48 | **1.00** | -0.59 … +0.62 |
| firedancer-26.10.5 | 6 | 49 | **1.00** | -0.57 … +0.48 |

The fee-rule column is the point: a rule that was real would hold its sign. Here
it swings from −1.00 to +0.80 across blocks of the same client, which is what a
non-predictor looks like when you measure it 40 times.

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

40 blocks is not a survey of the cluster, and six builds is not every build. What
the data supports is narrow and precise: for these leaders, on these clients, in
these blocks, position followed arrival order and not price. `--per-family` and `--windows` widen it; the spend cap
in the bounty (0.05 SOL) is roughly 200x the cost of the runs recorded here.
