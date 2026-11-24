# Consensus and equivalence design

This note explains how RainCover reaches agreement on a measurement —
"how much did it rain, and did the trigger hit?" — and what happens
when validators disagree. It is the design behind
`contracts/rain_cover.py`; function names below refer to that file.

## Why no language model

Most GenLayer contracts in this family use an LLM to judge something
subjective (is this delivery good? is this flaw real?). RainCover
deliberately does not. The question here is not subjective: total the
daily rainfall, or find the hottest day, and compare against a
threshold. That is addition and a maximum. A language model doing
arithmetic would only add error — it can miscount, round wrong, or
hallucinate a value — and every one of those errors would be a
consensus failure or, worse, a wrong payout.

So the leader (`_leader_settlement`) does deterministic arithmetic on
the fetched data (`_compute_verdict`): parse the Open-Meteo JSON, sum
the rain or take the max temperature, compare with the threshold, and
build the verdict. The consensus is over the *fetched data*, not over
wording: two honest validators fetch the same archive URL and should
compute the same number. The tolerance below exists because the
archive itself can move slightly between fetches (see "Why this
tolerance"), not because the computation is fuzzy.

The verdict still carries a `reasoning` string — a deterministic,
human-readable summary of the computation ("drought: sum of 31 daily
precipitation values from Open-Meteo = 0.00 mm; threshold 10.00 mm;
0.00 mm < 10.00 mm, trigger HIT."). It is evidence for humans, never
compared, and never part of the consensus value.

## The leader / validator split

`request_settlement` runs one call:

```python
gl.vm.run_nondet_unsafe(leader_fn, validator_fn)
```

- **Leader** (`_leader_settlement`): fetches the stored Open-Meteo
  archive URL with `gl.nondet.web.render(url, mode="text")`, parses
  the JSON and computes the verdict with `_compute_verdict`, and
  returns the normalized verdict as a sorted JSON string. A fetch
  failure raises a `ValueError` carrying an error-taxonomy prefix; an
  empty response is `[EXTERNAL]`. Fetched content that does not parse
  into a usable daily series is *not* an error — it is a
  `cannot_verify` verdict (see below).
- **Validator** (`_validate_settlement`): receives the leader's
  outcome as a `gl.vm.Result` — on `gl.vm.Return` it reads the
  leader's verdict from `.calldata`; on any other Result (the leader
  failed) it re-runs the leader's work and compares the failure under
  the error taxonomy. On a verdict it re-runs the leader's work
  itself, parses both verdicts, and compares them with
  `_verdicts_agree`. It returns a boolean — agree or disagree. It
  never trusts the leader's text.

All web fetches happen inside these two functions. Nothing outside
`run_nondet_unsafe` is nondeterministic, and `settle` makes no
nondeterministic call at all: it settles from the stored,
already-agreed verdict.

## The comparison rules (`_verdicts_agree`)

The verdict is a small structured object:

```json
{"measured_value": 0, "triggered": true, "cannot_verify": false, "reasoning": "..."}
```

`measured_value` is in hundredths of the unit (mm of rain, °C).
Agreement requires:

1. **`cannot_verify` must match.** If either side says the data
   cannot be verified, both must say so. A verdict of "I can't tell"
   from one validator and "it rained 0.00 mm" from another is a real
   disagreement, not a wording difference.
2. **`triggered` must match exactly.** There is no tolerance on the
   trigger outcome — it is what moves money.
3. **`measured_value` must agree within ±10% relative OR ±2.00 units
   absolute, whichever is larger.** The values are in hundredths, so
   the absolute floor is 200.
4. **Reasoning is never compared.** It is a deterministic summary for
   humans, not part of the consensus value.

## Why this tolerance: ±10% or ±2.00, whichever is larger

The tolerance exists because the archive itself moves. Open-Meteo's
archive is built from observation and reanalysis data, and values for
a given day can be revised slightly after the fact — a station report
arrives late, a reanalysis run is updated. Two honest validators
fetching the same URL hours apart can therefore see slightly
different numbers for the same days, and the leader and the validator
in one consensus round are different machines fetching at different
moments. Comparing raw numbers with `strict_eq` would fail consensus
on honest work.

The two parts of the tolerance cover the two regimes:

- **The ±2.00 absolute floor is for small values.** Dry-season rain
  is the whole point of a drought cover, and dry-season rain is near
  zero. If the rule were relative-only, a leader measuring 0.00 mm
  and a validator measuring 0.50 mm would disagree (infinite
  relative difference on a zero base) even though half a millimetre
  of rain is meaningless for a 10.00 mm threshold. The floor says:
  below 20.00 units, any two measurements within 2.00 of each other
  agree. Examples: 0 vs 200 hundredths (0.00 vs 2.00 mm) agree;
  0 vs 201 disagree.
- **The ±10% relative band is for large values.** Revisions scale
  with the amount: a 150.00 mm wet-season total can be revised by
  several millimetres, while a 2.00 mm floor would be far too tight.
  At 100.00 mm the band is ±10.00 mm; at 150.00 mm it is ±15.00 mm.
  Examples: 10000 vs 11000 hundredths (100.00 vs 110.00 mm) agree;
  10000 vs 12000 disagree.

The "whichever is larger" rule means the tolerance never shrinks
below 2.00 units and grows with the measurement. The boundary is
tested on both sides (200 agrees, 201 does not; 10% agrees, beyond
10% does not).

## The trigger straddle — a deliberate disagreement

There is one case the tolerance deliberately does *not* save: a
measurement that sits within tolerance of the threshold but on
different sides of it. Leader measures 9.99 mm, validator measures
10.01 mm, threshold 10.00 mm: the measurements agree (they differ by
0.02, far inside the floor) but `triggered` differs — one says pay
the farmer, the other says pay the underwriter. `_verdicts_agree`
returns false, because `triggered` must match exactly.

That is the honest behaviour. When the measurement is that close to
the threshold, the validators genuinely cannot agree on the payout,
and no tolerance rule should paper over it — a tolerance that
decided the payout by itself would be the contract, not the
validators, choosing who gets paid. The policy stays `active`, the
attempt is counted, and the call can be retried (a later fetch may
land clearly on one side) or the policy expires after the 3-day
window. A trigger set far from any plausible measurement — like the
demo's 10.00 mm against a real 0.00 mm — never hits this case.

## Defensive parsing (`_parse_verdict`, `_compute_verdict`)

Fetched data is hostile input until proven otherwise. `_compute_verdict`:

- treats content that is not JSON, not an object, or has no `daily`
  series as `cannot_verify` — every validator seeing the same broken
  data agrees on that, instead of failing consensus on it;
- treats a missing or empty series, a `null` day, a boolean, or a
  non-numeric value as `cannot_verify` for the same reason;
- ignores the series it does not need (heat ignores rain, drought
  ignores temperature), so an unrelated series being broken cannot
  block a settlement.

`_parse_verdict` (used by the validator and by `settle`):

- requires a JSON object (a list, a bare string, or invalid JSON is
  rejected);
- accepts `triggered` and `cannot_verify` as booleans or as
  "true"/"false"/"yes"/"no";
- accepts `measured_value` as an int, float, or numeric string, and
  rejects booleans, non-numeric strings, and negatives;
- normalizes `cannot_verify: true` to `measured_value: 0,
  triggered: false`, so a failed verification can never smuggle a
  payout score with it.

Every rejection raises `ValueError("[VERDICT] ...")` — see the error
taxonomy below.

## Error taxonomy (`_handle_leader_error`, `_errors_agree`)

The leader's work can fail in three different ways, and the
validators must agree on *what kind* of failure it was — not on the
exact error text, which varies between runs and networks. There is no
`[LLM_ERROR]` here: no language model is used, so a malformed verdict
is `[VERDICT]` instead.

| Prefix | Meaning | Agreement rule |
|---|---|---|
| `[EXPECTED]` | Business-logic failure | Exact string match |
| `[EXTERNAL]` | Bad data / HTTP 4xx (404, 403, …) | Exact string match |
| `[TRANSIENT]` | Network failure / HTTP 5xx / timeout | Agree if both are transient, wording ignored |
| `[VERDICT]` | Malformed verdict | **Always disagree** — forces leader rotation |

Classification (`_handle_leader_error`): an already-prefixed error
passes through unchanged. Otherwise the message is inspected — 4xx /
"not found" / "forbidden" become `[EXTERNAL]`; 5xx / timeout /
connection problems become `[TRANSIENT]`; anything unrecognized is
treated as `[TRANSIENT]` (retryable) rather than failing the policy
permanently.

Why verdict errors always disagree: a malformed verdict is a property
of the leader's run, not of the world. If validators agreed "the
leader's output was garbage", a bad leader could stall every
settlement. Forcing disagreement rotates the leader, and the next
leader's output is judged fresh.

## What happens on each outcome

| Situation | On-chain result |
|---|---|
| Validators agree on a verdict | Verdict + measured value stored, status `judged`; `settle` can now run |
| Verdict is `cannot_verify` | Attempt counted; status stays `active`; nobody paid; re-judgment or expiry |
| Leader fails, validators agree it is transient/external | Attempt counted; status stays `active`; nobody paid; the caller may retry |
| Validators disagree (trigger differs, measured too far apart, verdict error) | **No consensus.** The settlement does not finalize; the policy stays `active` with its attempt count unchanged on a live network (in the stubbed tests a disagreement surfaces as a failed `run_nondet_unsafe` call, which `request_settlement` records as an attempt — confirm the exact behaviour in Studio testing), and the call can be retried |
| 3 failed attempts | `request_settlement` refuses further judgments; the policy stays `active` until expiry |
| 3 days past period end with no verdict | `expire_policy` (anyone) refunds the premium to the farmer and releases the payout to the underwriter |

In every failure path the money stays locked. The contract fails
closed: there is no path from a failed or unverifiable settlement to
a payout.

## The UNDETERMINED hole — and the expire_policy escape

This design exists because of a hole found while building
DeliveryEscrow (a freelance-delivery escrow on the same consensus
pattern): when validators cannot reach consensus at all, the judgment
comes back UNDETERMINED — "Failed to reach consensus" — and **nothing
is written on-chain**. The attempt is not counted, the status does not
change, and the escrow simply stays open forever. Any escape that
depends on counting failed attempts can therefore never fire when
consensus itself is the thing failing.

RainCover fixes that with a second, independent escape that does not
depend on consensus at all: `expire_policy` is a purely deterministic
method. Anyone may call it once a policy has sat `active` (no usable
verdict stored) for more than 3 days past the cover period end
(259200 seconds). The only input is the transaction datetime
(`int(datetime.now(timezone.utc).timestamp())`), which every validator
sees identically, so it cannot fail consensus — there is no
nondeterministic work in it. The premium is refunded to the farmer
and the payout released to the underwriter. The 3-day window is
deliberately generous: it gives callers time to retry settlement, and
it only fires on policies nobody has managed to settle at all.

One deliberate restriction: a policy that already has a verdict
(`judged`) is **not** expirable. Expiring it would destroy a
consensus verdict and could steal a farmer's payout — a judged policy
must be settled, not expired. The escape is for the no-verdict hole
only.

## What this design deliberately does not do

- **No appeal inside the contract.** A settled verdict is final as far
  as this contract is concerned. Re-judgment of a *settled* policy is
  not possible — that would let a loser re-roll until they win. Appeal
  of a consensus outcome is GenLayer's own mechanism (validator
  rotation, and ultimately the network's dispute process), not
  something a single contract should re-implement.
- **No buy time gate.** A cover for a past period can still be bought
  — that is how the demo runs in Studio — so a farmer who already
  knows it did not rain could buy drought cover after the fact. A
  production deployment should refuse `buy_cover` once the period has
  ended. This is a deliberate demo trade-off, called out in
  `buy_cover`'s docstring and the README.
- **No check that the returned dates match the period.** The contract
  trusts Open-Meteo to return the requested range. A hardening step
  would verify the returned `daily.time` list against the period's
  dates before computing.
- **No reputation or staking for underwriters.** The underwriter's
  payout is locked at cover creation, so a triggered policy always
  pays — the trust question is whether the underwriter's cover terms
  are honest, which is a market problem, not a contract one.

## Open questions (confirm in Studio testing)

1. **Payout interface.** Payouts use `_PayoutTarget.emit_transfer`
   (the faucet pattern from Studio's bundled `faucet.py`); confirm
   the payout wiring in Studio testing (see README).

2. **Consensus-failure behaviour.** Confirm that a genuine
   UNDETERMINED settlement leaves the policy `active` with attempts
   unchanged (as observed in DeliveryEscrow), and that
   `expire_policy` then releases the funds as designed.

3. **Archive revision window.** Confirm how much the Open-Meteo
   archive values for a settled period move between fetches in
   practice — the ±10% / ±2.00 tolerance was chosen from the
   revision behaviour of reanalysis data; live observation may
   suggest a wider or narrower band.

The following were open questions in earlier projects and are now
resolved against the official docs (docs.genlayer.com):

- **Error propagation shape / validator argument (resolved).** The
  validator receives a `gl.vm.Result`, not the raw verdict — see
  "Error Patterns for Consensus" in the Non-determinism / Equivalence
  Principle docs: `validator_fn(leaders_res: gl.vm.Result) -> bool`.
  On `gl.vm.Return` the leader's value is on `.calldata`; on
  `gl.vm.UserError` / `gl.vm.VMError` the leader failed and its message
  is on `.message`. `_validate_settlement` follows that pattern
  exactly.
- **Time source (resolved).** There is no block timestamp in GenLayer
  — the Transaction Context docs state "No block number, no block
  hash." Deadlines and the 3-day expiry use
  `int(datetime.now(timezone.utc).timestamp())` (Unix seconds, the
  docs' own form for "Arithmetic, expiries, deltas"): the stdlib
  clock is wired to the transaction datetime, so every validator sees
  the same value.
