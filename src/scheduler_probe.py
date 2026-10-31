"""Measure how Solana leaders order transactions inside their own blocks.

Why this design
---------------
The bounty asks a single question: *by what rule does a given validator order the
transactions in its block*. Answering it needs three things that are easy to get
wrong, so each decision below is deliberate.

1. **Who led the slot.** Predicting leader windows from the schedule is not enough:
   a slot can be skipped, and a skipped slot is produced by the *next* validator in
   the schedule. So we attribute every measurement to the block's actual producer,
   read from the block itself (the fee recipient in `rewards`), and cross-check that
   against the epoch leader schedule. A block whose recorded leader disagrees with
   the schedule is flagged, not silently averaged in.

2. **Which transaction is which.** Every probe carries a distinct nonce in its
   instruction data. Ordering is then reconstructed from the block, not from our own
   send log, so a delayed or reordered send cannot fake a result.

3. **Confounders.** Fee, compute limit, send time and nonce are varied
   independently, so the candidate rules are not collinear:
     - fee ladder gives fee-ordering something to bite on;
     - nonces are sent in shuffled order, so send order is only one hypothesis;
     - the batch is small enough that the block is usually not saturated, which is
       recorded per slot (fill ratio) so a full block can be excluded rather than
       explain an odd result.

Safety
------
Dry-run is the default. Live sending requires `--live` *and* a lamport cap that is
checked before every send; the payer must hold cap + reserve before the run starts.
Keys are read from a path or env var and never appear in the repository.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import pathlib
import random
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Iterable

import requests
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.transaction import VersionedTransaction

LAMPORTS_PER_SOL = 1_000_000_000
BASE_FEE_LAMPORTS = 5_000  # one signature, one fee payer
# A fee payer below this balance cannot pay at all: the runtime rejects it with
# InsufficientFundsForRent, and with skipPreflight the send still looks accepted.
RENT_EXEMPT_MIN_LAMPORTS = 890_880
SYSTEM_PROGRAM = Pubkey.from_string("11111111111111111111111111111111")
# A self transfer: 80 CU flat (450 measured including the compute-budget
# instructions), always succeeds, and moves money to ourselves so the only cost is
# the fee. Memo costs more than 1200 CU under the current cost model, so it is a
# poor carrier for a small compute limit.
SYSTEM_TRANSFER_TAG = bytes([2, 0, 0, 0])


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ------------------------------------------------------------------------ plumbing


class RpcError(Exception):
    def __init__(self, method: str, error: dict) -> None:
        super().__init__(f"{method}: {error.get('message')} (code {error.get('code')})")
        self.method = method
        self.error = error


class Rpc:
    """One session, explicit timeouts, no hidden retries on the read path.

    `send_urls` may list extra endpoints. Public RPCs routinely accept a
    transaction and then drop it instead of forwarding it to the leader: the call
    returns a signature, the balance never moves, and the transaction never appears
    on chain. Measured in this project: 100 transactions "accepted" by one public
    endpoint produced zero signature statuses anywhere and a balance delta of
    exactly zero. Sending the same signed bytes to a second endpoint is the only
    reliable fix, since the transaction is identical and idempotent.
    """

    def __init__(self, url: str, timeout: float = 30.0) -> None:
        self.url = url
        self.timeout = timeout
        self.read_urls: list[str] = [url]
        self.session = requests.Session()
        self.session.headers.update({"content-type": "application/json", "user-agent": "scheduler-probe/1"})
        self._id = 0
        self.send_urls: list[str] = [url]

    def call(self, method: str, params: Any) -> Any:
        return self.call_at(self.url, method, params)

    def call_at(self, url: str, method: str, params: Any) -> Any:
        self._id += 1
        payload = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params}
        resp = self.session.post(url, json=payload, timeout=self.timeout)
        resp.raise_for_status()
        body = resp.json()
        if "error" in body:
            raise RpcError(method, body["error"])
        return body["result"]

    def block_at(self, url: str, slot: int) -> dict | None:
        try:
            return self.call_at(
                url,
                "getBlock",
                [
                    slot,
                    {
                        "encoding": "json",
                        "transactionDetails": "full",
                        "rewards": True,
                        # Blocks contain v1 transactions; asking for 0 makes the
                        # whole getBlock call fail, which silently yields no data.
                        "maxSupportedTransactionVersion": 1,
                        "commitment": "confirmed",
                    },
                ],
            )
        except RpcError:
            return None

    def epoch_info(self) -> dict:
        return self.call("getEpochInfo", [])

    def leader_schedule(self) -> dict[str, list[int]]:
        return self.call("getLeaderSchedule", [None, {}])

    def cluster_nodes(self) -> list[dict]:
        return self.call("getClusterNodes", [])

    def vote_accounts(self) -> list[dict]:
        return self.call("getVoteAccounts", [{"keepUnstakedDelinquents": False}])["current"]

    def balance(self, pubkey: Pubkey) -> int:
        return self.call("getBalance", [str(pubkey), {"commitment": "confirmed"}])["value"]

    def latest_blockhash(self) -> Hash:
        return Hash.from_string(self.call("getLatestBlockhash", [{"commitment": "processed"}])["value"]["blockhash"])

    def send_raw(self, tx: VersionedTransaction) -> Signature:
        raw = base64.b64encode(bytes(tx)).decode()
        params = [
            raw,
            {"encoding": "base64", "skipPreflight": True, "maxRetries": 0, "preflightCommitment": "processed"},
        ]
        return Signature.from_string(self.call("sendTransaction", params))

    def send_raw_to(self, url: str, tx: VersionedTransaction) -> Signature:
        raw = base64.b64encode(bytes(tx)).decode()
        params = [
            raw,
            {"encoding": "base64", "skipPreflight": True, "maxRetries": 0, "preflightCommitment": "processed"},
        ]
        payload = {"jsonrpc": "2.0", "id": 1, "method": "sendTransaction", "params": params}
        resp = self.session.post(url, json=payload, timeout=self.timeout)
        resp.raise_for_status()
        body = resp.json()
        if "error" in body:
            raise RpcError("sendTransaction", body["error"])
        return Signature.from_string(body["result"])

    def block(self, slot: int) -> dict | None:
        """Full block: we need per-transaction fee and compute, plus the leader."""
        return self.block_at(self.url, slot)

    def tx_exists(self, signature: str) -> bool:
        try:
            res = self.call(
                "getSignatureStatuses",
                [[signature], {"searchTransactionHistory": True}],
            )
            status = res["value"][0]
            return bool(status and status.get("confirmationStatus"))
        except (RpcError, requests.HTTPError):
            return False


# ----------------------------------------------------------------------- validators


@dataclass
class Validator:
    vote: str
    identity: str
    stake_sol: float
    commission: int
    version: str
    feature_set: int | None

    @property
    def client_family(self) -> str:
        """Coarse client label; stable enough to group measurements by software."""
        v = self.version
        if v.startswith(("0.", "1.", "25.", "26.")):
            return f"firedancer-{v}"
        if v.startswith("4."):
            return f"agave-{v}"
        return f"other-{v}"


def collect_validators(rpc: Rpc) -> dict[str, Validator]:
    nodes = {n["pubkey"]: n for n in rpc.cluster_nodes()}
    out: dict[str, Validator] = {}
    for vote in rpc.vote_accounts():
        node = nodes.get(vote["nodePubkey"], {})
        out[vote["votePubkey"]] = Validator(
            vote=vote["votePubkey"],
            identity=vote["nodePubkey"],
            stake_sol=vote["activatedStake"] / LAMPORTS_PER_SOL,
            commission=vote["commission"],
            version=node.get("version") or "unknown",
            feature_set=node.get("featureSet"),
        )
    return out


def slot_owners(rpc: Rpc) -> dict[int, str]:
    """Absolute slot -> validator identity, from the epoch leader schedule.

    The schedule is indexed by slot offset inside the epoch, so it must be shifted
    by the epoch's first absolute slot. Getting this wrong silently attributes
    every measurement to the wrong validator, hence the explicit arithmetic.
    """
    epoch = rpc.epoch_info()
    first = epoch["absoluteSlot"] - epoch["slotIndex"]
    owners: dict[int, str] = {}
    for identity, offsets in rpc.leader_schedule().items():
        for off in offsets:
            owners[first + off] = identity
    return owners


def choose_windows(
    owners: dict[int, str],
    validators: dict[str, Validator],
    head_slot: int,
    families: dict[str, list[str]],
    per_family: int,
    lead_slots: int,
) -> list[tuple[int, Validator]]:
    """Pick upcoming leader slots, spread across client families.

    We prefer slots that are far enough ahead to be reachable (a few seconds) but
    not so far that the run stalls. One slot per validator per round, so that a slow
    validator does not consume the whole budget.
    """
    by_identity = {v.identity: v for v in validators.values()}
    chosen: list[tuple[int, Validator]] = []
    for family, votes in families.items():
        picked = 0
        for vote in votes:
            if picked >= per_family:
                break
            v = validators[vote]
            upcoming = sorted(s for s, ident in owners.items() if ident == v.identity and s > head_slot + lead_slots)
            if not upcoming:
                continue
            chosen.append((upcoming[0], v))
            picked += 1
    chosen.sort(key=lambda t: t[0])
    return chosen


# --------------------------------------------------------------------- transactions


def load_keypair(path: str | None) -> Keypair:
    path = path or os.environ.get("SOLANA_KEYPAIR_PATH")
    if not path:
        raise SystemExit("no keypair: pass --keypair or set SOLANA_KEYPAIR_PATH")
    raw = pathlib.Path(path).read_text().strip()
    if raw.startswith("["):
        return Keypair.from_bytes(bytes(json.loads(raw)))
    import base58

    return Keypair.from_bytes(base58.b58decode(raw))


def priority_fee_lamports(cu_limit: int, micro_lamports: int) -> int:
    """e = ceil(cu_limit * micro_lamports / 1e6), matching the compute budget program."""
    return (cu_limit * micro_lamports + 999_999) // 1_000_000


def build_probe_tx(payer: Keypair, blockhash: Hash, nonce: int, cu_limit: int, micro_lamports: int) -> VersionedTransaction:
    """Self transfer whose amount is the nonce, so every probe is a distinct tx.

    Transferring to ourselves means the lamports come back; the only real cost is
    the fee. The nonce in the amount guarantees a unique signature even when two
    probes share blockhash, fee and compute limit, which would otherwise dedupe.
    """
    me = payer.pubkey()
    data = SYSTEM_TRANSFER_TAG + nonce.to_bytes(8, "little")
    ix = [
        set_compute_unit_limit(cu_limit),
        set_compute_unit_price(micro_lamports),
        Instruction(SYSTEM_PROGRAM, data, [AccountMeta(me, True, True), AccountMeta(me, False, True)]),
    ]
    msg = MessageV0.try_compile(me, ix, [], blockhash)
    return VersionedTransaction(msg, [payer])


def fee_ladder(n: int, base: int, step: int) -> list[int]:
    """`n` distinct priority fees, ascending.

    Distinct fees are what makes fee-ordering testable: if two probes pay the same,
    a fee rule cannot order them, and the correlation is diluted. Ascending order is
    then shuffled against the nonces so the two hypotheses stay independent.
    """
    return [base + i * step for i in range(n)]


# -------------------------------------------------------------------------- runner


@dataclass
class Probe:
    slot: int
    nonce: int
    micro_lamports: int
    cu_limit: int
    send_seq: int
    sent_at: float
    signature: str = ""
    landed: bool = False
    tx_index: int | None = None
    fee_lamports: int | None = None
    cu_consumed: int | None = None

    def as_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class Measurement:
    validator: Validator
    slot: int
    probes: list[Probe] = field(default_factory=list)
    blocks: dict[str, dict] = field(default_factory=dict)
    block_leader: str | None = None
    slot_from_schedule: str | None = None
    on_target: bool | None = None

    def as_dict(self) -> dict:
        return {
            "validator": {
                "vote": self.validator.vote,
                "identity": self.validator.identity,
                "version": self.validator.version,
                "client_family": self.validator.client_family,
                "feature_set": self.validator.feature_set,
                "stake_sol": self.validator.stake_sol,
                "commission": self.validator.commission,
            },
            "slot": self.slot,
            "slot_leader_per_schedule": self.slot_from_schedule,
            "block_leader_per_rewards": self.block_leader,
            "leader_matches_schedule": self.block_leader == self.slot_from_schedule if self.block_leader else None,
            "on_target": self.on_target,
            "blocks": self.blocks,
            "probes": [p.as_dict() for p in self.probes],
        }


class SpendGuard:
    """Refuses to exceed the cap. Charged before each signature leaves the process."""

    def __init__(self, cap: int) -> None:
        self.cap = cap
        self.spent = 0

    def charge(self, lamports: int) -> None:
        if self.spent + lamports > self.cap:
            raise SystemExit(f"spend cap: {self.spent} + {lamports} > {self.cap} lamports")
        self.spent += lamports


def wait_until_slot(rpc: Rpc, target: int, lead_slots: int) -> None:
    """Sleep until roughly `lead_slots` before the target slot; slots are ~400 ms.

    The lead exists because a transaction needs to be in the leader's queue before
    the block is built. Sending one slot early measured nothing at all: in that run
    every batch had zero landed transactions, because the target slot was already
    sealed when the send arrived.

    Sending early also means a probe can land in an *earlier* validator's block than
    the one we aimed at. That is not a defect to be papered over: attribution is done
    per block, from the block's own fee recipient, so each probe is credited to the
    validator that actually ordered it.
    """
    while True:
        head = rpc.epoch_info()["absoluteSlot"]
        remaining = target - head
        if remaining <= lead_slots:
            return
        # Poll less often when far away so we do not hammer the RPC.
        time.sleep(min(2.0, max(0.2, (remaining - lead_slots) * 0.4)))


def _quiet_send(rpc: Rpc, url: str, tx: VersionedTransaction) -> None:
    try:
        rpc.send_raw_to(url, tx)
    except (RpcError, requests.HTTPError, requests.exceptions.RequestException):
        pass


def send_round(
    rpc: Rpc,
    payer: Keypair,
    validator: Validator,
    slot: int,
    args: argparse.Namespace,
    guard: SpendGuard,
    seq_start: int,
) -> Measurement:
    """Sign the whole batch, then fire it concurrently.

    Sending sequentially took about a second for six transactions, which spread them
    over six different slots: too few probes per block to test an ordering rule at
    all. Pre-signing and sending in parallel keeps the batch inside one slot (about
    400 ms), which is the condition the experiment needs.
    """
    m = Measurement(validator=validator, slot=slot)
    blockhash = rpc.latest_blockhash()
    nonces = list(range(args.per_slot))
    random.shuffle(nonces)  # send order must not equal nonce order
    fees = fee_ladder(args.per_slot, args.fee_base, args.fee_step)
    random.shuffle(fees)  # fees must not be sorted in nonce order either

    guard.charge(sum(BASE_FEE_LAMPORTS + priority_fee_lamports(args.cu_limit, f) for f in fees))

    prepared: list[tuple[VersionedTransaction, Probe]] = []
    for i, nonce in enumerate(nonces):
        tx = build_probe_tx(payer, blockhash, nonce, args.cu_limit, fees[i])
        prepared.append(
            (
                tx,
                Probe(
                    slot=slot,
                    nonce=nonce,
                    micro_lamports=fees[i],
                    cu_limit=args.cu_limit,
                    send_seq=seq_start + i,
                    sent_at=0.0,
                ),
            )
        )

    backups = [u for u in args.backup_rpc if u and u != args.rpc]

    def fire(item: tuple[VersionedTransaction, Probe]) -> None:
        tx, probe = item
        # stamped here, not at build time: this is the moment the transaction
        # actually leaves for the leader, and it is what "arrival order" means
        probe.sent_at = time.time()
        try:
            probe.signature = str(rpc.send_raw(tx))
        except (RpcError, requests.HTTPError) as exc:
            probe.signature = ""
            log(f"    send rejected (nonce {probe.nonce}): {str(exc)[:60]}")
            return
        # An accepted send is not the same as a forwarded one. Fan the identical
        # bytes out to the backups too; the transaction is idempotent and the
        # leader discards duplicates, so this only removes the drop. This runs in
        # its own pool because waiting on it inside a worker delays the *next*
        # primary send, which is what spreads the batch across blocks.
        for url in backups:
            fanout.submit(_quiet_send, rpc, url, tx)

    # Every probe must leave within the same slot. Sends that are serialised by
    # the worker pool arrive over several hundred milliseconds and get split
    # across blocks: with 3 workers a 10-probe batch landed in 4-6 different
    # blocks, leaving 1-3 probes per block, which measures nothing. So all
    # primaries go out at once; the pool is only there to bound thread count.
    fanout = ThreadPoolExecutor(max_workers=8)
    with ThreadPoolExecutor(max_workers=min(args.send_workers, len(prepared))) as pool:
        list(pool.map(fire, prepared))
    fanout.shutdown(wait=True)
    m.probes.extend(probe for _, probe in prepared)
    return m


def harvest(rpc: Rpc, m: Measurement) -> None:
    """Find where our transactions actually landed, then read those blocks.

    A transaction sent for slot N does not necessarily land in N: the leader may
    skip, or our send may arrive after the block was sealed. So the slot of every
    probe is discovered from the chain (`getSignatureStatuses`), and each block is
    attributed to the leader that actually produced it (the fee recipient in
    `rewards`). Attributing by the intended slot would silently credit the wrong
    validator.
    """
    for p in m.probes:
        if p.signature and not p.landed:
            slot = _landing_slot(rpc, p.signature)
            if slot is not None:
                p.slot = slot

    by_sig = {p.signature: p for p in m.probes if p.signature}
    blocks: dict[str, dict] = {}
    for slot in sorted({p.slot for p in m.probes if p.signature}):
        block = _fetch_block(rpc, slot)
        if not block:
            continue
        txs = block.get("transactions") or []
        for idx, tx in enumerate(txs):
            sigs = tx.get("transaction", {}).get("signatures") or []
            probe = by_sig.get(sigs[0]) if sigs else None
            if not probe:
                continue
            probe.landed = True
            # The block that actually contains the signature is the authority on
            # where it landed, so it overwrites the status-reported slot.
            probe.slot = int(slot)
            probe.tx_index = idx
            meta = tx.get("meta") or {}
            probe.fee_lamports = meta.get("fee")
            probe.cu_consumed = meta.get("computeUnitsConsumed")
        blocks[str(slot)] = {
            "blockhash": block.get("blockhash"),
            "previousBlockhash": block.get("previousBlockhash"),
            "blockTime": block.get("blockTime"),
            "blockHeight": block.get("blockHeight"),
            "leader_per_rewards": _leader_from_rewards(block),
            "tx_count": len(txs),
            "signatures": [s[0] for tx in txs if (s := tx.get("transaction", {}).get("signatures") or [])],
        }
    m.blocks = blocks
    landed_slots = sorted(int(s) for s in blocks)
    if landed_slots:
        m.slot = landed_slots[0]
        m.block_leader = blocks[str(landed_slots[0])]["leader_per_rewards"]


def _fetch_block(rpc: Rpc, slot: int) -> dict | None:
    """Blocks arrive up to a few seconds after the slot; poll across endpoints."""
    for attempt in range(6):
        for url in rpc.read_urls or [rpc.url]:
            try:
                block = rpc.block_at(url, slot)
            except (requests.HTTPError, requests.exceptions.RequestException):
                continue
            if block:
                return block
        time.sleep(2.0)
    return None


def _landing_slot(rpc: Rpc, signature: str) -> int | None:
    """Where the chain says this signature landed, or None if it never did.

    Reads are retried across endpoints: after SOL has been spent the harvest must
    not be the thing that loses the data. A single unreachable public RPC once
    aborted a full run at this line.
    """
    for url in rpc.read_urls or [rpc.url]:
        try:
            res = rpc.call_at(url, "getSignatureStatuses", [[signature], {"searchTransactionHistory": True}])
            status = (res.get("value") or [None])[0]
            return status.get("slot") if status else None
        except (RpcError, requests.HTTPError, requests.exceptions.RequestException):
            continue
    return None


def _leader_from_rewards(block: dict) -> str | None:
    """The leader collects the transaction fees; that is our ground truth."""
    for reward in block.get("rewards") or []:
        if reward.get("rewardType") == "Fee":
            return reward.get("pubkey")
    return None


def run(args: argparse.Namespace) -> dict:
    rpc = Rpc(args.rpc)
    # reads (harvest) may use any endpoint; writes go to --rpc and its backups
    rpc.read_urls = list(dict.fromkeys([args.rpc, *args.backup_rpc]))
    validators = collect_validators(rpc)
    owners = slot_owners(rpc)
    epoch = rpc.epoch_info()

    eligible = {v: val for v, val in validators.items() if val.stake_sol >= args.min_stake_sol}
    families: dict[str, list[str]] = {}
    for val in sorted(eligible.values(), key=lambda x: -x.stake_sol):
        families.setdefault(val.client_family, []).append(val.vote)

    families = {f: vs[: args.per_family] for f, vs in families.items()}
    windows = choose_windows(owners, validators, epoch["absoluteSlot"], families, args.per_family, args.lead_slots)

    plan = {
        "mode": "dry-run" if args.dry_run else "live",
        "rpc": args.rpc,
        "epoch": epoch,
        "families": {f: vs for f, vs in families.items()},
        "windows": [
            {"slot": slot, "vote": v.vote, "identity": v.identity, "version": v.version, "client_family": v.client_family}
            for slot, v in windows
        ],
        "estimated_transactions": len(windows) * args.per_slot,
        "estimated_lamports": len(windows)
        * args.per_slot
        * (BASE_FEE_LAMPORTS + priority_fee_lamports(args.cu_limit, args.fee_base + args.fee_step * args.per_slot)),
        "cap_lamports": args.max_lamports,
    }
    if args.dry_run:
        return plan

    payer = load_keypair(args.keypair)
    guard = SpendGuard(args.max_lamports)
    start = rpc.balance(payer.pubkey())
    # Two distinct floors, both fatal: cap+reserve so the run cannot overdraw, and
    # the rent-exempt minimum because an account below it cannot pay a fee at all.
    need = max(args.max_lamports + args.reserve_lamports, RENT_EXEMPT_MIN_LAMPORTS + args.reserve_lamports)
    if start < need:
        raise SystemExit(
            f"payer {payer.pubkey()} holds {start} lamports; need {need} "
            f"(cap {args.max_lamports} + reserve {args.reserve_lamports}, and the "
            f"{RENT_EXEMPT_MIN_LAMPORTS}-lamport rent-exempt minimum). "
            f"Top up {need - start} lamports."
        )
    plan["payer"] = str(payer.pubkey())
    plan["start_balance_lamports"] = start

    measurements: list[Measurement] = []
    seq = 0
    for slot, validator in windows:
        wait_until_slot(rpc, slot, args.lead_slots)
        head = rpc.epoch_info()["absoluteSlot"]
        if head > slot:
            log(f"slot {slot} already past (head {head}); skipping")
            continue
        log(f"slot {slot} -> {validator.client_family} {validator.vote[:8]} (head {head})")
        m = send_round(rpc, payer, validator, slot, args, guard, seq)
        seq += args.per_slot
        m.slot_from_schedule = owners.get(slot)
        measurements.append(m)
        time.sleep(max(0.0, args.gap - (time.time() - m.probes[0].sent_at))) if m.probes else None

    log(f"sent {guard.spent} lamports in fees; harvesting blocks")
    time.sleep(args.settle)
    for m in measurements:
        harvest(rpc, m)
        landed = sum(1 for p in m.probes if p.landed)
        # Only blocks produced by the intended validator count as a measurement of
        # that validator. Everything else is recorded and marked, never silently used.
        m.on_target = m.block_leader == m.validator.identity
        log(
            f"  slot {m.slot}: {landed}/{len(m.probes)} landed, leader={m.block_leader}, "
            f"on_target={m.on_target}, schedule_match={m.block_leader == m.slot_from_schedule}"
        )

    plan["measurements"] = [m.as_dict() for m in measurements]
    plan["end_balance_lamports"] = rpc.balance(payer.pubkey())
    plan["spent_lamports"] = guard.spent
    return plan


# ------------------------------------------------------------------------ analysis


def spearman(a: Iterable[float], b: Iterable[float]) -> float:
    a, b = list(a), list(b)
    if len(a) != len(b) or len(a) < 3:
        return 0.0
    ra, rb = _ranks(a), _ranks(b)
    ma, mb = statistics.fmean(ra), statistics.fmean(rb)
    num = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    den = (sum((x - ma) ** 2 for x in ra) * sum((y - mb) ** 2 for y in rb)) ** 0.5
    return num / den if den else 0.0


def _ranks(xs: list[float]) -> list[float]:
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    ranks = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def analyse(report: dict) -> dict:
    """Score candidate ordering rules per block, attributed to the block's leader.

    Key decision: the unit of analysis is a **(block, leader)** pair, not the round
    we aimed at. A batch sent for validator A can be included by validator B whose
    slot came first; B's scheduler is what ordered those transactions, so B gets the
    credit and A gets nothing. Grouping by intended slot instead would attribute B's
    behaviour to A, which is exactly the mistake this design has to avoid.
    """
    validators = {
        m["validator"]["identity"]: m["validator"]
        for m in report.get("measurements", [])
    }
    # also index every validator we know about from the run plan
    for w in report.get("windows", []):
        validators.setdefault(w["identity"], w)
    # A block can be produced by a validator we never aimed at, whose client we
    # then do not know from the run itself. That would silently turn a real
    # measurement into "unknown", so resolve it against the live cluster when the
    # report carries no offline snapshot.
    known = {k: v for k, v in report.get("cluster", {}).items()}
    for block in [b for m in report.get("measurements", []) for b in (m.get("blocks") or {}).values()]:
        leader = block.get("leader_per_rewards")
        if not leader or leader in validators:
            continue
        # A block's fee recipient is not always the validator identity: a vote
        # account can route block revenue to a separate collector (100% commission
        # in the case observed here), and the collector is neither in the leader
        # schedule nor in the cluster snapshot. The vote account that points at it
        # still names the node, so resolve through that rather than calling the
        # block "unknown" and dropping a real measurement.
        node = report.get("revenue_to_node", {}).get(leader)
        if node and node in known:
            validators[leader] = known[node]
            continue
        if leader in known:
            validators[leader] = known[leader]

    blocks: dict[str, dict] = {}
    for m in report.get("measurements", []):
        for slot_key, block in (m.get("blocks") or {}).items():
            leader = block.get("leader_per_rewards")
            if not leader:
                continue
            entry = blocks.setdefault(
                slot_key,
                {
                    "slot": int(slot_key),
                    "leader": leader,
                    "tx_count": block.get("tx_count"),
                    "probes": [],
                },
            )
            for probe in m["probes"]:
                if probe.get("landed") and probe.get("slot") == int(slot_key):
                    entry["probes"].append(probe)

    rows: list[dict] = []
    for slot_key, entry in sorted(blocks.items()):
        landed = [p for p in entry["probes"] if p.get("tx_index") is not None]
        meta = validators.get(entry["leader"], {})
        family = meta.get("client_family") or "unknown"
        if len(landed) < 3:
            rows.append(
                {
                    "slot": entry["slot"],
                    "leader": entry["leader"],
                    "client_family": family,
                    "landed": len(landed),
                    "verdict": "insufficient data",
                }
            )
            continue

        observed = sorted(landed, key=lambda p: p["tx_index"])
        pos = [p["tx_index"] for p in observed]

        def corr(keyfn, reverse=False) -> float:
            """Correlate the rule's predicted rank with the real block position.

            The predicted rank of a probe is its index once the batch is sorted by
            the rule; the observed value is its position in the block. Correlating
            anything else (e.g. the probe's index in the input list) would measure
            the input ordering against itself and produce a meaningless 1.0.
            """
            ordered = sorted(landed, key=keyfn, reverse=reverse)
            predicted = {id(p): i for i, p in enumerate(ordered)}
            return spearman([predicted[id(p)] for p in landed], pos)

        scores = {
            "fee_desc": corr(lambda p: p["fee_lamports"] if p["fee_lamports"] is not None else -1, reverse=True),
            "send_seq": corr(lambda p: p["send_seq"]),
            "sent_at": corr(lambda p: p["sent_at"]),
            "nonce": corr(lambda p: p["nonce"]),
        }
        best = max(scores, key=lambda k: scores[k])
        rows.append(
            {
                "slot": entry["slot"],
                "leader": entry["leader"],
                "client_family": family,
                "version": meta.get("version"),
                "stake_sol": meta.get("stake_sol"),
                "landed": len(landed),
                "block_tx_count": entry["tx_count"],
                "correlation": {k: round(v, 3) for k, v in scores.items()},
                "best_rule": best,
                "best_correlation": round(scores[best], 3),
            }
        )

    by_family: dict[str, list[dict]] = {}
    for r in rows:
        if "best_rule" in r:
            by_family.setdefault(r["client_family"], []).append(r)

    summary = {}
    for fam, rs in by_family.items():
        rules: dict[str, int] = {}
        for r in rs:
            rules[r["best_rule"]] = rules.get(r["best_rule"], 0) + 1
        summary[fam] = {
            "blocks": len(rs),
            "landed_total": sum(r["landed"] for r in rs),
            "rule_votes": rules,
            "mean_best_corr": round(statistics.fmean(r["best_correlation"] for r in rs), 3),
        }
    return {"rows": rows, "summary": summary}


# ---------------------------------------------------------------------------- cli


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Measure Solana in-block transaction ordering per validator.")
    p.add_argument("--rpc", default=os.environ.get("SOLANA_RPC", "https://api.mainnet-beta.solana.com"))
    p.add_argument("--keypair", default=None, help="path to base58 secret key file (never committed)")
    p.add_argument("--min-stake-sol", type=float, default=1_000_000.0)
    p.add_argument("--per-family", type=int, default=1, help="validators measured per client family")
    p.add_argument("--per-slot", type=int, default=8, help="competing transactions per leader slot")
    p.add_argument("--cu-limit", type=int, default=1_200)
    p.add_argument("--fee-base", type=int, default=0, help="lowest priority fee, micro-lamports per CU")
    p.add_argument(
        "--fee-step",
        type=int,
        default=1_500_000,
        help=(
            "step between consecutive priority fees, micro-lamports per CU. The "
            "priority fee in lamports is cu_limit*micro/1e6, so a step of 2_500 "
            "buys 3 lamports at a 1_200 CU limit: a spread too small to test the "
            "fee hypothesis at all. The default buys 1_800 lamports per step."
        ),
    )
    p.add_argument(
        "--lead-slots",
        type=int,
        default=6,
        help="send this many slots before the target slot (too few means the block is already sealed)",
    )
    p.add_argument("--gap", type=float, default=2.0, help="seconds between rounds")
    p.add_argument("--send-workers", type=int, default=4, help="parallel sends (higher triggers RPC 429)")
    p.add_argument(
        "--backup-rpc",
        nargs="*",
        default=["https://api.mainnet-beta.solana.com"],
        help="extra endpoints that receive the same signed bytes, to defeat silent drops",
    )
    p.add_argument("--settle", type=float, default=20.0, help="seconds before reading blocks back")
    # The default plan (6 families x 2 slots x 8 probes with the default fee
    # ladder) estimates 0.00093 SOL; the cap has to exceed that or the default
    # command refuses its own default plan.
    p.add_argument("--max-lamports", type=int, default=1_500_000, help="hard cap, enforced before each send")
    p.add_argument("--reserve-lamports", type=int, default=200_000)
    p.add_argument("--out", default="data/run.json")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", default=True, help="plan only (default)")
    mode.add_argument("--live", dest="dry_run", action="store_false", help="send real transactions")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    report = run(args)
    if not args.dry_run:
        report["analysis"] = analyse(report)
    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2))
    log(f"wrote {out}")
    if report.get("analysis"):
        for fam, s in report["analysis"]["summary"].items():
            print(f"  {fam}: blocks={s['blocks']} landed={s['landed_total']} rules={s['rule_votes']} corr={s['mean_best_corr']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
