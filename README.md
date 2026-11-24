# RainCover — parametric weather micro-insurance

A standalone **GenLayer Intelligent Contract** (Python, GenVM). It is a
reusable primitive — weather cover + consensus measurement +
deterministic settlement — not a demo app and not a hello-world
contract. There is no frontend in this repo: the contract *is* the
submission.

## The problem

Small farmers lose a season to weather they cannot control — a dry
January, a heatwave in April — and the insurance that should cover
them is slow, paper-based, and argued over claim by claim. Parametric
insurance fixes the argument: the payout is agreed in advance on a
measurable trigger ("rainfall below 10 mm in January"), so when the
trigger is hit the money should move without a claims adjuster. But a
normal smart contract cannot fix it either: it cannot fetch weather
data, cannot tell whether it rained in Kano in January, and cannot
turn a weather record into a payout decision.

## What this contract does

1. **An underwriter offers cover** (`create_cover`, payable): a place,
   a period, a peril (`drought`, `excess_rain`, or `heat`), a
   threshold, a payout, and a premium. The payout is locked in the
   contract. The contract stores the Open-Meteo archive URL it will
   settle from, built deterministically from the coordinates and the
   period.
2. **A farmer buys the cover** (`buy_cover`, payable with the premium):
   the premium is held in escrow in the contract. One policy per
   cover.
3. **Anyone calls `request_settlement`** once the period has ended.
   GenLayer's validators fetch the *real* weather data from the stored
   Open-Meteo URL, compute the measured value — total rainfall for
   drought / excess rain, hottest day for heat — and agree whether the
   trigger was hit. The verdict and measured value are stored
   on-chain. Up to 3 judgment attempts.
4. **`settle` pays out deterministically from the stored verdict** —
   no nondeterministic call at settlement time:
   - triggered → the payout goes to the **farmer**, the premium to the
     **underwriter**
   - not triggered → payout **plus** premium go to the **underwriter**
   - no verdict, or a `cannot_verify` verdict → settlement fails; the
     policy stays open for re-judgment or expiry
5. **Policies that never get settled still end.** If a policy sits
   `active` (no usable verdict) for more than 3 days past the period
   end, anyone may call `expire_policy`: the premium is refunded to
   the farmer and the payout is released to the underwriter. This is
   the deterministic escape for the consensus hole found while
   building DeliveryEscrow: a failed consensus writes nothing
   on-chain, so an attempt-based escape can never fire when consensus
   itself fails. The escape has to be time-based. See `CONSENSUS.md`.
6. **The underwriter can cancel an unsold cover** (`cancel_cover`)
   and get the locked payout back.

## Why it needs GenLayer consensus

| Question | Normal contract | RainCover |
|---|---|---|
| Fetch weather data? | No | Validators fetch the real Open-Meteo archive |
| Measure the trigger? | No | Validators compute total rain / hottest day |
| Agree on the measurement? | No | Validators agree within ±10% or ±2 units |
| Move the money? | Yes | Deterministic settlement on the stored verdict |

The AI never touches money directly: it only produces a verdict. The
money logic is pure integer arithmetic on that verdict, so settlement
is reproducible and auditable. In fact **no language model is used at
all** — the measurement is deterministic arithmetic on fetched data
(see `CONSENSUS.md` for why that is the right call here), so there is
nothing for an LLM to get wrong.

## Units

Measured values and thresholds are integers in **hundredths** of the
natural unit — millimetres of rain for drought / excess_rain, degrees
Celsius for heat. A threshold of `1000` means 10.00 mm; `4000` means
40.00 °C. Hundredths keep all measurement math in integers (u256),
with no floats anywhere near a payout. Coordinates are stored as
decimal strings with six places (`"12.000000"`, `"-33.868800"`) —
u256 cannot hold negative latitudes, and floats are never stored.

## State design

`contracts/rain_cover.py` — one contract class, `RainCover`.

- **Storage** (class-level annotations only): `owner`,
  `covers: TreeMap[u256, Cover]`, `cover_order: DynArray[u256]`,
  `policies: TreeMap[u256, Policy]`, `policy_order: DynArray[u256]`,
  `counters: TreeMap[str, u256]`, `total_paid_out_atto`,
  `total_premiums_held_atto`. No Python dict/list in storage; money is
  `u256` atto-GEN (1 GEN = 10¹⁸ atto), never floats; status is `str`,
  never Enum.
- **Cover record** (`@allow_storage @dataclass`): id, underwriter,
  location_name, latitude, longitude (decimal strings), peril,
  threshold (hundredths), period_start, period_end (Unix seconds),
  payout_amount, premium, data_url, status, created_at.
- **Policy record** (`@allow_storage @dataclass`): id, cover_id,
  farmer, premium, status, verdict (stored JSON), measured_value
  (hundredths), attempts, created_at.
- **Status machines:** cover `open → sold` (bought once) or
  `open → cancelled`; policy `active → judged → settled`, or
  `active → expired` (3-day escape). A policy with a stored verdict
  (`judged`) is deliberately **not** expirable — expiring it would
  destroy a consensus verdict and could steal a farmer's payout; it
  must be settled instead.
- **Access control** via `gl.message.sender_address` (an `Address`),
  raising `gl.vm.UserError` for business errors (wrong caller, wrong
  status, bad peril, out-of-range coordinates, zero amounts, wrong
  attached value, period not ended, attempts exhausted, not yet
  expirable).
- **Payouts** are direct transfers of the GEN locked by the payable
  `create_cover` / `buy_cover` calls. They go out through
  `_PayoutTarget.emit_transfer` on an `@gl.evm.contract_interface`
  (the faucet pattern: `emit_transfer` takes only `value=`, and the
  destination is the address passed to the constructor). No Arbiter
  and no setup call are needed — `settle`, `expire_policy`, and
  `cancel_cover` pay out as soon as they run. See `DEPLOY.md`.

## How consensus and equivalence work here

All web fetching happens inside a leader function passed to
`gl.vm.run_nondet_unsafe(leader_fn, validator_fn)`. The leader fetches
the stored Open-Meteo URL with `gl.nondet.web.render(url,
mode="text")` and computes the verdict by deterministic arithmetic
(`_compute_verdict`): sum the daily rain, or take the hottest day,
compare against the threshold. There is no `exec_prompt` call — the
measurement is not a judgment for a language model to make, it is
addition and a maximum, and an LLM doing arithmetic would only add
error.

The validator is **custom, not `strict_eq`**. It re-runs the leader's
work and compares the *structured* verdict. It receives the leader's
outcome as a `gl.vm.Result`: on `gl.vm.Return` the verdict JSON is on
`.calldata`; on any other Result the leader failed, and the validator
re-runs the leader's work and compares the failure under the error
taxonomy below:

- `triggered` must match exactly — the trigger is what moves money;
- `cannot_verify` must match exactly — if either side says the data
  cannot be verified, both must say so;
- `measured_value` must agree within **±10% relative OR ±2.00 units
  absolute, whichever is larger** (values are in hundredths, so the
  absolute floor is 200);
- reasoning text is never compared.

Leader-side failures are classified with an error taxonomy
(`[EXPECTED]` business logic, `[EXTERNAL]` bad data / 4xx,
`[TRANSIENT]` network / 5xx, `[VERDICT]` malformed verdict).
Transient failures agree with each other, expected/external failures
must match exactly, and verdict errors always disagree so the leader
is rotated. Full design, including why the tolerance is what it is and
why a measurement that straddles the threshold still fails consensus:
`CONSENSUS.md`.

## Worked example

**Cover** (underwriter locks 5 GEN, premium 1 GEN):

> Kano, Nigeria — drought, threshold 10.00 mm, period 1–31 January
> 2025. Data URL:
> `https://archive-api.open-meteo.com/v1/archive?latitude=12.000000&longitude=8.520000&start_date=2025-01-01&end_date=2025-01-31&daily=precipitation_sum,temperature_2m_max&timezone=UTC`

**The real data** (fetched from that URL while writing this — see
`demo/demo-policy.md`): it rained **0.00 mm** in Kano all month, every
one of the 31 days.

**Expected verdict:** `{"measured_value": 0, "triggered": true,
"cannot_verify": false}` — 0.00 < 10.00, so the drought trigger hit.
**Settlement:** the farmer receives the **5 GEN** payout and the
underwriter receives the **1 GEN** premium.

**A second verified case** (same file): Kano, April 2025, heat,
threshold 40.00 °C — the hottest day was **41.4 °C** (24 April), so
that trigger hit too.

## Files

| File | What it is |
|---|---|
| `contracts/rain_cover.py` | The contract (937 lines) |
| `tests/test_rain_cover.py` | 131 pytest tests (see below) |
| `tests/conftest.py` | Minimal fake `genlayer` module (stub) |
| `demo/demo-policy.md` | The Kano drought policy, with real verified data |
| `CONSENSUS.md` | Equivalence design in depth |
| `DEPLOY.md` | Studio → testnet → portal, step by step |
| `pyproject.toml` / `requirements.txt` | pytest only |

## Tests

The `genlayer` package is not installed in this environment (see
Linting below), so the contract is structured with module-level pure
helpers (`_format_coord`, `_date_str`, `_archive_url`,
`_compute_verdict`, `_parse_verdict`, `_verdicts_agree`,
`_handle_leader_error`, `_errors_agree`) and `tests/conftest.py`
stubs the `genlayer` module (a minimal fake `gl` with Contract,
TreeMap, DynArray, public decorators, message, nondet, eq_principle,
vm with `gl.vm.Return` / `gl.vm.UserError` Result types, and the
stdlib clock pinned to the transaction datetime) so the contract file
imports cleanly and the lifecycle runs end-to-end under the stub —
including the consensus flow (leader + validator agreement and
disagreement, including the tolerance boundaries and the trigger
straddle) and payout calls, which are recorded by patching
`_PayoutTarget.emit_transfer`.

```sh
pip install -r requirements.txt
python3 -m pytest tests/ -q
# 131 passed
```

Coverage: coordinate/date/URL formatting (including negative
coordinates and the 2024 leap day), verdict computation (all three
perils, trigger boundaries at exactly the threshold, fractional
rounding, every cannot_verify shape), verdict parsing (string
booleans, string numbers, cannot_verify normalization, malformed
input), agreement rules (±10% / ±2.00 tolerance boundaries,
triggered-exact, cannot_verify rules, reasoning ignored), error
taxonomy, creation guards (period order, zero amounts, bad peril,
out-of-range and NaN coordinates, empty location, wrong attached
value), buy rules (wrong premium, double buy, cancelled cover),
settlement paths (triggered / not triggered / cannot_verify / fetch
failure ×3 → exhausted / consensus disagreement / tolerance agree and
disagree / trigger straddle / validator fetch failure / before period
end), settlement tiers and the premium split, the 3-day
`expire_policy` (before / at / after the boundary, settled and judged
policies refused, after 3 failed attempts), cancel rules
(owner-only, sold, twice), pagination, stats, and four full
lifecycles (Kano January 2025 drought, Kano April 2025 heat,
excess-rain not triggered, expire after no consensus).

## Linting

`genvm-lint` is **not available in this environment** and the contract
could not be lint-checked here:

- `pip install genvm-lint` → no such package on PyPI.
- `pip install py-genlayer` → no such package on PyPI.
- `pip install genlayer` → installs version 0.0.1, which is an empty
  placeholder package ("A minimal empty PyPI package for genlayer")
  with no `gl` API at all.

The contract was written to follow the linted patterns from the
official write-contract guidance (single contract per file, storage
annotations, TreeMap/DynArray, no floats for money, all
nondeterminism inside `run_nondet_unsafe`, custom validator instead
of `strict_eq`), and it compiles cleanly with
`python3 -m py_compile`. The stub was grep-verified to model only
real APIs: the contract contains none of `gl.contract`,
`gl.UserError`, `gl.block`, or `sender_account`. **Before submitting,
run `genvm-lint check contracts/rain_cover.py`** from the official
GenLayer tooling (GenLayer Studio runs the same checks on deploy) and
fix anything it flags — in particular confirm the
transaction-datetime deadline clock (`datetime.now(timezone.utc)`),
the `datetime.fromtimestamp` date derivation, and the
`_PayoutTarget.emit_transfer` payout wiring against the target
network.

## Deploy

See `DEPLOY.md`: GenLayer Studio (studio.genlayer.com, 5 simulated
validators) → Bradbury testnet (faucet) → submit via the GenLayer
portal (portal.genlayer.foundation) under the Builders / standalone
contracts category.

## Honest limits

- **Settlement interface:** payouts use `_PayoutTarget.emit_transfer`
  (the faucet pattern, copied from GenLayer Studio's bundled
  `faucet.py`). `settle`, `expire_policy`, and `cancel_cover` pay out
  directly with no setup call — confirm the payout wiring in Studio
  testing before mainnet use.
- **Settlement is only as good as the archive.** The contract judges
  the weather at the stored Open-Meteo URL. Open-Meteo revises
  archive values occasionally, which is exactly why the validator
  tolerance exists — but a large revision after settlement cannot be
  undone, and settlement is final. A cover whose period is in the
  far past is settled on the archive as it stands at settlement time.
- **No buy time gate.** A cover for a past period can still be bought
  — that is how the demo runs in Studio — so a farmer who already
  knows it did not rain could buy drought cover after the fact. A
  production deployment should refuse `buy_cover` once the period
  has ended (or price the premium from the same data). This is a
  deliberate demo trade-off, not an oversight; it is called out in
  `buy_cover`'s docstring.
- **No appeal flow in the contract itself.** A settled verdict is
  final as far as this contract is concerned. Disagreement is handled
  by GenLayer's consensus: validators that disagree force re-execution
  / leader rotation. Appeal of a settled verdict is GenLayer's own
  mechanism, not something this contract re-implements.
- **Deadlines use the transaction datetime** —
  `int(datetime.now(timezone.utc).timestamp())` in Unix seconds.
  GenLayer exposes no block timestamp (no block number or hash in the
  transaction context); the stdlib clock is wired to the transaction
  datetime, so every validator sees the same value.
- **`get_stats` iterates all policies.** Fine for a primitive's
  expected scale; a deployment expecting thousands of policies should
  maintain running counters instead.
- **The returned dates are not checked against the period.** The
  contract trusts Open-Meteo to return the requested range; a
  hardening step would verify the returned `daily.time` list matches
  the period's dates before computing.
