# Which data files are canonical

| file | status |
|---|---|
| `run_live.json` | **canonical** — produced through `docker compose` as a non-root user |
| `reharvest_smoke2.json` | **canonical** — the smoke run, re-read from chain with every probe pinned to the block that actually contains it |
| `run_v2.json` | **canonical** — first run with parallel primary sends |
| `run_v3.json` | **canonical** — widest fee sweep (5 000 to 17 600 lamports) |
| `run_v4.json`, `run_v5.json` | **canonical** — gap-filling runs (`--only-identity`) over the largest validators |
| `localnet/run_localnet.json` | **canonical** — pipeline validation against `solana-test-validator` |
| `run_smoke.json`, `run_smoke2.json`, `run_final.json`, `run_3.json`, `run_4.json` | superseded, kept for the record |
| `leader_schedule.json`, `plan*.json`, `dryrun.json`, `docker_dry.json` | generated, gitignored |

The superseded files were written before two fixes: probes were not pinned to the
block that actually contains them, and a block's fee recipient was not resolved to
a validator identity. Their recorded positions can therefore disagree with the
chain. They are not used by `scripts/summarise.py` and are excluded from every
number in `README.md` and `REPORT.md`.
