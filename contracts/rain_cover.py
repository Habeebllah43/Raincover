# v0.2.16
# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }

# Always put above lines as first in the contract file.
# (GenLayer Studio / GenVM require this runner header; without it schema
# loading fails with "invalid contract absent_runner_comment".)
"""
RainCover — parametric weather micro-insurance.

A standalone GenLayer Intelligent Contract primitive: an underwriter
locks a payout in the contract for a place, a period and a weather
peril (drought, excess rain, or heat). A farmer buys the cover by
paying a premium, which is held in escrow. After the period ends,
GenLayer's validators fetch the *real* weather data for that place and
period from the Open-Meteo archive API, compute the measured value
(total rainfall, or the hottest day), and agree whether the trigger
was hit. Settlement is deterministic from the agreed verdict — the
payout goes to the farmer and the premium to the underwriter when the
trigger hit, and everything goes to the underwriter when it did not.

Why this needs GenLayer (and why a normal smart contract cannot do
it): a normal contract cannot fetch weather data, cannot tell whether
it rained in Kano in January, and cannot turn a weather record into a
payout decision. Here the validators fetch the same public archive
and agree on the measured value, and Optimistic Democracy turns their
agreement into an on-chain verdict the contract can settle from. The
money logic itself — who gets the payout, who gets the premium — is
fully deterministic and runs on the stored verdict only; the AI never
moves money. In fact no language model is used at all: the measurement
is arithmetic on fetched data (see CONSENSUS.md for why), so there is
nothing for an LLM to get wrong.

Units: measured values and thresholds are integers in *hundredths* of
the natural unit — millimetres of rain for drought / excess_rain,
degrees Celsius for heat. A threshold of 1000 means 10.00 mm; a
threshold of 4000 means 40.00 C. Hundredths keep all money and
measurement math in integers (u256), with no floats anywhere near a
payout. Coordinates are stored as decimal strings with six places
("12.000000") — u256 cannot hold negative latitudes, and floats are
never stored.

Perils and triggers (measured value vs threshold):
    drought      -> triggered when measured < threshold (too little rain)
    excess_rain  -> triggered when measured > threshold (too much rain)
    heat         -> triggered when measured > threshold (too hot)
Measured value:
    drought / excess_rain -> sum of daily precipitation_sum (mm)
    heat                  -> max of daily temperature_2m_max (C)

Policy status machine:
    active -> judged -> settled    (verdict stored, then settled)
    active -> expired              (no verdict after period_end + 3 days)

Money is u256 atto-GEN (1 GEN = 10**18 atto). No floats are ever used
for money. Fail closed: if the weather data cannot be fetched or
cannot be verified, nobody is paid — the policy stays open for
re-judgment, and after 3 days past the period end anyone may expire
it, refunding the premium to the farmer and releasing the payout to
the underwriter.

Payouts are direct transfers of the GEN locked by the payable
create_cover / buy_cover calls. They go out through the
faucet-pattern EVM interface below (_PayoutTarget): the
@gl.evm.contract_interface decorator provides emit_transfer, which
takes only value=, and the destination is the address passed to the
constructor. No Arbiter or setup call is needed.
"""
import json
from dataclasses import dataclass
from datetime import datetime, timezone

from genlayer import *

# ---------------------------------------------------------------------------
# Error taxonomy for consensus (see CONSENSUS.md for the full design)
#
# The leader's nondeterministic work can fail in three different ways,
# and the validators must agree on *what kind* of failure it was — not
# on the exact wording of an error message, which differs between runs.
# There is deliberately no [LLM_ERROR] here: this contract uses no
# language model (the measurement is deterministic arithmetic on fetched
# data), so a malformed verdict is classified [VERDICT] instead.
# ---------------------------------------------------------------------------
ERROR_EXPECTED = "[EXPECTED]"      # business-logic failure, exact match
ERROR_EXTERNAL = "[EXTERNAL]"      # bad data / 4xx, exact match
ERROR_TRANSIENT = "[TRANSIENT]"     # network / 5xx, agree if both transient
ERROR_VERDICT = "[VERDICT]"        # malformed verdict, always disagree

MAX_SETTLEMENT_ATTEMPTS = 3
EXPIRE_AFTER_SECONDS = 259200      # 3 days past period_end: a policy with
                                   # no verdict becomes expirable (see
                                   # expire_policy / CONSENSUS.md)
MEASURED_TOLERANCE_PERCENT = 10    # validators must agree within +/-10%
MEASURED_TOLERANCE_ABSOLUTE = 200  # ... or +/-2.00 units, whichever is
                                   # larger (values are in hundredths)
PERILS = ("drought", "excess_rain", "heat")


# ---------------------------------------------------------------------------
# Pure helpers (module level, so they are unit-testable without GenVM)
# ---------------------------------------------------------------------------
def _format_coord(micro):
    """Format integer microdegrees as a decimal string with six places.

    Coordinates are stored as strings ("12.000000", "-33.868800") so
    negative latitudes survive (u256 cannot hold them) and no float is
    ever stored. Open-Meteo accepts six-place decimals.
    """
    sign = "-" if micro < 0 else ""
    magnitude = -micro if micro < 0 else micro
    return "%s%d.%06d" % (sign, magnitude // 1000000, magnitude % 1000000)


def _date_str(ts):
    """Calendar date (YYYY-MM-DD, UTC) of a Unix timestamp."""
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return "%04d-%02d-%02d" % (dt.year, dt.month, dt.day)


def _archive_url(latitude_str, longitude_str, period_start, period_end):
    """The Open-Meteo archive URL a cover settles from.

    Built deterministically from the cover's coordinates and period, so
    every validator fetches the same data: daily precipitation_sum and
    temperature_2m_max for the period's calendar dates, UTC.
    """
    return (
        "https://archive-api.open-meteo.com/v1/archive?latitude=%s"
        "&longitude=%s&start_date=%s&end_date=%s"
        "&daily=precipitation_sum,temperature_2m_max&timezone=UTC"
        % (
            latitude_str,
            longitude_str,
            _date_str(period_start),
            _date_str(period_end),
        )
    )


def _fmt_hundredths(value):
    """Format an integer hundredths value as "12.34" (no floats)."""
    if value < 0:
        return "-%d.%02d" % ((-value) // 100, (-value) % 100)
    return "%d.%02d" % (value // 100, value % 100)


def _cannot_verify_verdict(reasoning):
    """A verdict that the weather cannot be verified.

    Normalized so a failed verification can never smuggle a payout
    with it: measured 0, triggered false.
    """
    return {
        "measured_value": 0,
        "triggered": False,
        "cannot_verify": True,
        "reasoning": reasoning,
    }


def _compute_verdict(cover, text):
    """Compute the settlement verdict from fetched weather JSON.

    Deterministic arithmetic, no language model: parse the Open-Meteo
    daily series, sum the rain (drought / excess_rain) or take the
    hottest day (heat), compare against the threshold. Any fetched
    content that does not parse into a usable daily series is a
    cannot_verify verdict — every validator seeing the same broken
    data agrees on that, instead of failing consensus on it.
    """
    try:
        data = json.loads(text)
    except Exception:
        return _cannot_verify_verdict("weather data is not valid JSON")
    if not isinstance(data, dict):
        return _cannot_verify_verdict("weather data is not a JSON object")
    daily = data.get("daily")
    if not isinstance(daily, dict):
        return _cannot_verify_verdict("weather data has no daily series")
    if cover.peril in ("drought", "excess_rain"):
        series = daily.get("precipitation_sum")
        label = "precipitation"
        unit = "mm"
    else:
        series = daily.get("temperature_2m_max")
        label = "temperature"
        unit = "C"
    if not isinstance(series, list) or len(series) == 0:
        return _cannot_verify_verdict(
            "weather data has no %s series" % label
        )
    values = []
    for value in series:
        if isinstance(value, bool) or value is None:
            return _cannot_verify_verdict(
                "weather data has a missing daily %s value" % label
            )
        try:
            num = float(value)
        except Exception:
            return _cannot_verify_verdict(
                "weather data has a non-numeric daily %s value" % label
            )
        if num != num:
            return _cannot_verify_verdict(
                "weather data has a non-numeric daily %s value" % label
            )
        values.append(num)
    if cover.peril == "heat":
        measured = max(values)
        operation = "max"
    else:
        measured = sum(values)
        operation = "sum"
    measured_hundredths = int(round(measured * 100))
    if cover.peril == "drought":
        triggered = measured_hundredths < cover.threshold
        comparison = "<"
    else:
        triggered = measured_hundredths > cover.threshold
        comparison = ">"
    reasoning = (
        "%s: %s of %d daily %s values from Open-Meteo = %s %s; "
        "threshold %s %s; %s %s %s %s, trigger %s."
        % (
            cover.peril,
            operation,
            len(values),
            label,
            _fmt_hundredths(measured_hundredths),
            unit,
            _fmt_hundredths(cover.threshold),
            unit,
            _fmt_hundredths(measured_hundredths),
            unit,
            comparison,
            _fmt_hundredths(cover.threshold),
            "HIT" if triggered else "not hit",
        )
    )
    return {
        "measured_value": measured_hundredths,
        "triggered": triggered,
        "cannot_verify": False,
        "reasoning": reasoning,
    }


def _parse_verdict(raw):
    """Parse and validate a settlement verdict into a normalized dict.

    Accepts a JSON string or an already-parsed dict. The verdict must
    have measured_value (an integer number of hundredths, >= 0),
    triggered (bool), cannot_verify (bool, default false) and reasoning
    (string, optional). A verdict of cannot_verify=True is normalized
    to measured_value 0, triggered False, so it can never smuggle a
    payout. Anything malformed raises ValueError(ERROR_VERDICT ...),
    which the consensus error taxonomy treats as "always disagree" so
    the leader is rotated instead of a bad verdict being settled on.
    """
    if isinstance(raw, dict):
        data = raw
    else:
        try:
            data = json.loads(raw)
        except Exception:
            raise ValueError(ERROR_VERDICT + " verdict is not valid JSON")
    if not isinstance(data, dict):
        raise ValueError(ERROR_VERDICT + " verdict is not a JSON object")

    cannot_verify = data.get("cannot_verify", False)
    if isinstance(cannot_verify, str):
        cannot_verify = cannot_verify.strip().lower() in ("true", "yes", "1")
    cannot_verify = bool(cannot_verify)
    reasoning = data.get("reasoning", "")
    if not isinstance(reasoning, str):
        reasoning = str(reasoning)
    if cannot_verify:
        return {
            "measured_value": 0,
            "triggered": False,
            "cannot_verify": True,
            "reasoning": reasoning,
        }

    if "triggered" not in data:
        raise ValueError(ERROR_VERDICT + " verdict is missing 'triggered'")
    triggered = data["triggered"]
    if isinstance(triggered, str):
        lowered = triggered.strip().lower()
        if lowered in ("true", "yes", "1"):
            triggered = True
        elif lowered in ("false", "no", "0"):
            triggered = False
        else:
            raise ValueError(ERROR_VERDICT + " 'triggered' is not a boolean")
    elif not isinstance(triggered, bool):
        raise ValueError(ERROR_VERDICT + " 'triggered' is not a boolean")

    if "measured_value" not in data:
        raise ValueError(ERROR_VERDICT + " verdict is missing 'measured_value'")
    measured = data["measured_value"]
    if isinstance(measured, bool):
        raise ValueError(ERROR_VERDICT + " measured_value must be a number, got a boolean")
    if isinstance(measured, (int, float)):
        measured_hundredths = int(round(float(measured)))
    elif isinstance(measured, str):
        try:
            measured_hundredths = int(round(float(measured.strip())))
        except ValueError:
            raise ValueError(ERROR_VERDICT + " measured_value is not numeric: %r" % (measured,))
    else:
        raise ValueError(ERROR_VERDICT + " measured_value has an unsupported type")
    if measured_hundredths < 0:
        raise ValueError(ERROR_VERDICT + " measured_value must not be negative")

    return {
        "measured_value": measured_hundredths,
        "triggered": triggered,
        "cannot_verify": False,
        "reasoning": reasoning,
    }


def _verdicts_agree(a, b):
    """Equivalence check for two structured settlement verdicts.

    - If either side says cannot_verify, both must agree on it.
    - Otherwise `triggered` must match exactly: the trigger outcome
      is what moves money, so there is no tolerance on it. (A
      measurement that straddles the threshold within the measured
      tolerance still disagrees on `triggered` — see CONSENSUS.md.)
    - `measured_value` must agree within +/-10% relative OR +/-2.00
      units absolute, whichever is larger. The values are in
      hundredths, so the absolute floor is 200.
    Reasoning text is never compared.
    """
    if a.get("cannot_verify") or b.get("cannot_verify"):
        return bool(a.get("cannot_verify")) == bool(b.get("cannot_verify"))
    if a.get("triggered") != b.get("triggered"):
        return False
    ma = int(a.get("measured_value", 0))
    mb = int(b.get("measured_value", 0))
    allowed = max(
        (max(ma, mb) * MEASURED_TOLERANCE_PERCENT) // 100,
        MEASURED_TOLERANCE_ABSOLUTE,
    )
    return abs(ma - mb) <= allowed


def _handle_leader_error(err):
    """Normalize a leader-side failure to one of the error-taxonomy strings.

    Already-prefixed errors pass through unchanged. HTTP 4xx / "not
    found" / "forbidden" become [EXTERNAL]; network and 5xx failures
    become [TRANSIENT]; anything unrecognized is treated as
    [TRANSIENT] (retryable) rather than failing the policy permanently.
    """
    text = err if isinstance(err, str) else str(err)
    for prefix in (ERROR_EXPECTED, ERROR_EXTERNAL, ERROR_TRANSIENT, ERROR_VERDICT):
        if text.startswith(prefix):
            return text
    lowered = text.lower()
    if any(code in lowered for code in ("404", "403", "400", "401", "410", "not found", "forbidden", "gone")):
        return ERROR_EXTERNAL + " " + text
    if any(code in lowered for code in ("500", "502", "503", "504", "timeout", "timed out", "connection", "network", "temporarily", "unreachable")):
        return ERROR_TRANSIENT + " " + text
    return ERROR_TRANSIENT + " " + text


def _error_prefix(normalized):
    """Return the "[...]" prefix token of a normalized error string."""
    if "]" in normalized:
        return normalized[: normalized.index("]") + 1]
    return normalized


def _errors_agree(a, b):
    """Do two leader-side failures agree under the error taxonomy?

    - [VERDICT] always disagrees (forces leader rotation).
    - [TRANSIENT] agrees with [TRANSIENT], whatever the wording.
    - [EXPECTED] / [EXTERNAL] must match exactly.
    """
    na = _handle_leader_error(a)
    nb = _handle_leader_error(b)
    pa = _error_prefix(na)
    pb = _error_prefix(nb)
    if pa == ERROR_VERDICT or pb == ERROR_VERDICT:
        return False
    if pa != pb:
        return False
    if pa == ERROR_TRANSIENT:
        return True
    return na == nb


def _validate_create_cover(location_name, latitude, longitude, peril, threshold, period_start, period_end, payout_amount, premium):
    """Business-rule checks for create_cover. Raises gl.vm.UserError."""
    if not location_name or not location_name.strip():
        raise gl.vm.UserError("location_name must not be empty")
    if latitude != latitude:
        raise gl.vm.UserError("latitude must be a number")
    if longitude != longitude:
        raise gl.vm.UserError("longitude must be a number")
    if latitude < -90 or latitude > 90:
        raise gl.vm.UserError("latitude must be between -90 and 90")
    if longitude < -180 or longitude > 180:
        raise gl.vm.UserError("longitude must be between -180 and 180")
    if peril not in PERILS:
        raise gl.vm.UserError("peril must be one of drought, excess_rain, heat")
    if threshold <= 0:
        raise gl.vm.UserError("threshold must be greater than zero")
    if period_end <= period_start:
        raise gl.vm.UserError("period_end must be after period_start")
    if payout_amount <= 0:
        raise gl.vm.UserError("payout_amount must be greater than zero")
    if premium <= 0:
        raise gl.vm.UserError("premium must be greater than zero")


def _require_status(record, *allowed):
    if record.status not in allowed:
        raise gl.vm.UserError(
            "record %d is '%s', expected one of %s"
            % (record.id, record.status, ", ".join(allowed))
        )


# ---------------------------------------------------------------------------
# Storage records
# ---------------------------------------------------------------------------
@allow_storage
@dataclass
class Cover:
    id: u256
    underwriter: Address
    location_name: str
    latitude: str
    longitude: str
    peril: str
    threshold: u256
    period_start: u256
    period_end: u256
    payout_amount: u256
    premium: u256
    data_url: str
    status: str
    created_at: u256


@allow_storage
@dataclass
class Policy:
    id: u256
    cover_id: u256
    farmer: Address
    premium: u256
    status: str
    verdict: str
    measured_value: u256
    attempts: u256
    created_at: u256


# ---------------------------------------------------------------------------
# Payout interface (faucet pattern)
#
# Payouts are direct transfers of the GEN locked by the payable
# create_cover / buy_cover calls. The @gl.evm.contract_interface
# decorator provides emit_transfer: it takes only value=, and the
# destination is the address passed to the constructor. No methods are
# declared here — the nested View and Write classes are required by the
# decorator, and declaring emit_transfer (or any method) breaks schema
# generation in GenLayer Studio ("'IEscrowArbiter' has no attribute
# 'View'"). This contract never holds private keys and never pays out
# without a stored, consensus-reached verdict.
# ---------------------------------------------------------------------------
@gl.evm.contract_interface
class _PayoutTarget:
    class View:
        pass

    class Write:
        pass


# ---------------------------------------------------------------------------
# The contract
# ---------------------------------------------------------------------------
class RainCover(gl.Contract):
    owner: Address
    covers: TreeMap[u256, Cover]
    cover_order: DynArray[u256]
    policies: TreeMap[u256, Policy]
    policy_order: DynArray[u256]
    counters: TreeMap[str, u256]
    total_paid_out_atto: u256
    total_premiums_held_atto: u256

    def __init__(self, owner_address: str):
        # The owner is passed in as a constructor argument on purpose:
        # reading the sender inside __init__ breaks deployment on
        # GenLayer Studio. When deploying, pass your own wallet address
        # as a hex string — GenLayer constructors never take Address
        # parameters, so it is converted to Address here.
        self.owner = Address(owner_address)
        self.counters["cover_count"] = 0
        self.counters["policy_count"] = 0
        self.total_paid_out_atto = 0
        self.total_premiums_held_atto = 0

    # -- internal -----------------------------------------------------------
    def _now(self) -> u256:
        # Transaction datetime, in Unix seconds. GenLayer exposes NO block
        # timestamp (docs: Transaction Context — "No block number, no block
        # hash"). Instead the stdlib clock is wired to the transaction
        # datetime, so datetime.now(timezone.utc) returns the same value
        # on every validator, and int(...timestamp()) is the documented
        # form for arithmetic / expiries. Deadlines are u256 Unix seconds,
        # so both sides of every deadline comparison are in seconds.
        return int(datetime.now(timezone.utc).timestamp())

    def _get_cover(self, cover_id: u256) -> Cover:
        if cover_id <= 0 or cover_id > self.counters["cover_count"]:
            raise gl.vm.UserError("cover not found")
        return self.covers[cover_id]

    def _get_policy(self, policy_id: u256) -> Policy:
        if policy_id <= 0 or policy_id > self.counters["policy_count"]:
            raise gl.vm.UserError("policy not found")
        return self.policies[policy_id]

    def _pay(self, to: Address, amount: u256) -> None:
        if amount <= 0:
            return
        _PayoutTarget(to).emit_transfer(value=amount)

    def _leader_settlement(self, cover: Cover) -> str:
        """Fetch the weather and compute the verdict. Runs on leader AND
        validators.

        Returns the normalized verdict as a sorted JSON string. Raises
        ValueError with an error-taxonomy prefix on any failure, so the
        validator side can classify and compare the failure. The
        computation itself is deterministic arithmetic on the fetched
        data — no language model is involved (see CONSENSUS.md).
        """
        try:
            text = gl.nondet.web.render(cover.data_url, mode="text")
        except Exception as e:
            raise ValueError(_handle_leader_error(e))
        if not text or not text.strip():
            raise ValueError(ERROR_EXTERNAL + " weather data fetched but was empty")
        verdict = _compute_verdict(cover, text)
        return json.dumps(verdict, sort_keys=True)

    def _validate_settlement(self, cover: Cover, leaders_res) -> bool:
        """Custom equivalence validator for the settlement (see
        CONSENSUS.md).

        The validator receives a gl.vm.Result, not the raw verdict
        (docs: Non-determinism / Equivalence Principle, "Error Patterns
        for Consensus" — validator_fn(leaders_res: gl.vm.Result) ->
        bool):
        - gl.vm.Return: the leader succeeded; its verdict is on
          .calldata.
        - anything else (gl.vm.UserError / gl.vm.VMError): the leader
          failed; re-run the leader's work and compare the failure
          against the leader's message under the error taxonomy.
        On success it re-runs the leader's work and compares the
        STRUCTURED verdict — never the raw text, which two honest
        validators may word differently.
        """
        if not isinstance(leaders_res, gl.vm.Return):
            leader_msg = leaders_res.message if hasattr(leaders_res, "message") else ""
            try:
                self._leader_settlement(cover)
            except Exception as e:
                return _errors_agree(leader_msg, e)
            return False
        try:
            mine = self._leader_settlement(cover)
        except Exception:
            return False
        try:
            theirs = _parse_verdict(leaders_res.calldata)
            mine_v = _parse_verdict(mine)
        except Exception:
            return False
        return _verdicts_agree(theirs, mine_v)

    def _run_settlement(self, cover: Cover) -> str:
        """Run the leader/validator consensus for one settlement."""
        def leader_fn():
            return self._leader_settlement(cover)

        def validator_fn(leaders_res):
            return self._validate_settlement(cover, leaders_res)

        return gl.vm.run_nondet_unsafe(leader_fn, validator_fn)

    # -- cover lifecycle ----------------------------------------------------
    @gl.public.write.payable
    def create_cover(
        self,
        location_name: str,
        latitude: float,
        longitude: float,
        peril: str,
        threshold: u256,
        period_start: u256,
        period_end: u256,
        payout_amount: u256,
        premium: u256,
    ) -> dict:
        """Offer weather cover. The attached GEN is the payout, locked
        in the contract; the caller becomes the underwriter. The
        threshold is in hundredths of the peril's unit (mm of rain, or
        degrees C): 1000 means 10.00 mm, 4000 means 40.00 C. The
        contract stores the Open-Meteo archive URL it will settle
        from, built deterministically from the coordinates and period.
        The period may be in the past (for a demo) or the future."""
        underwriter = gl.message.sender_address
        value = gl.message.value
        _validate_create_cover(
            location_name,
            latitude,
            longitude,
            peril,
            threshold,
            period_start,
            period_end,
            payout_amount,
            premium,
        )
        if value != payout_amount:
            raise gl.vm.UserError("attached value must equal payout_amount")
        latitude_str = _format_coord(int(round(latitude * 1000000)))
        longitude_str = _format_coord(int(round(longitude * 1000000)))
        cover_id = self.counters["cover_count"] + 1
        self.counters["cover_count"] = cover_id
        cover = Cover(
            id=cover_id,
            underwriter=underwriter,
            location_name=location_name,
            latitude=latitude_str,
            longitude=longitude_str,
            peril=peril,
            threshold=threshold,
            period_start=period_start,
            period_end=period_end,
            payout_amount=payout_amount,
            premium=premium,
            data_url=_archive_url(latitude_str, longitude_str, period_start, period_end),
            status="open",
            created_at=self._now(),
        )
        self.covers[cover_id] = cover
        self.cover_order.append(cover_id)
        return {"cover_id": cover_id, "status": "open", "data_url": cover.data_url}

    @gl.public.write.payable
    def buy_cover(self, cover_id: u256) -> dict:
        """Buy an open cover: the caller becomes the farmer, the premium
        is held in escrow in the contract. One policy per cover — a
        cover is bought once. No time gate is placed on buying: a cover
        for a past period can still be bought (that is how the demo in
        demo/demo-policy.md runs in Studio), so a production deployment
        that wants to forbid buying after the period ends should add
        that check here. See README's honest limits."""
        cover = self._get_cover(cover_id)
        if cover.status != "open":
            raise gl.vm.UserError("cover is not open")
        value = gl.message.value
        if value != cover.premium:
            raise gl.vm.UserError("attached value must equal the premium")
        farmer = gl.message.sender_address
        policy_id = self.counters["policy_count"] + 1
        self.counters["policy_count"] = policy_id
        policy = Policy(
            id=policy_id,
            cover_id=cover_id,
            farmer=farmer,
            premium=cover.premium,
            status="active",
            verdict="",
            measured_value=0,
            attempts=0,
            created_at=self._now(),
        )
        self.policies[policy_id] = policy
        self.policy_order.append(policy_id)
        cover.status = "sold"
        self.covers[cover_id] = cover
        self.total_premiums_held_atto = self.total_premiums_held_atto + cover.premium
        return {"policy_id": policy_id, "status": "active"}

    @gl.public.write
    def request_settlement(self, policy_id: u256) -> dict:
        """Anyone may call once the cover period has ended. Runs
        validator consensus on the weather data.

        On a reached verdict the policy becomes "judged" with the
        verdict and measured value stored (or stays "active" on a
        cannot_verify verdict, for re-judgment or expiry). On a
        leader-side failure the validators agree is transient/external,
        the attempt is counted and the policy stays "active". After 3
        attempts no more judgments are run; the policy can still be
        expired once the 3-day window passes. A genuine consensus
        failure (validators disagree) finalizes nothing on-chain, so
        the attempt is not consumed there — the policy simply stays
        "active" and the call can be retried. See CONSENSUS.md.
        """
        policy = self._get_policy(policy_id)
        _require_status(policy, "active")
        cover = self._get_cover(policy.cover_id)
        if self._now() < cover.period_end:
            raise gl.vm.UserError("cover period has not ended yet")
        if policy.attempts >= MAX_SETTLEMENT_ATTEMPTS:
            raise gl.vm.UserError("settlement attempts exhausted")
        try:
            verdict_json = self._run_settlement(cover)
        except Exception as e:
            normalized = _handle_leader_error(e)
            policy.attempts = policy.attempts + 1
            self.policies[policy_id] = policy
            return {
                "policy_id": policy_id,
                "status": policy.status,
                "attempts": policy.attempts,
                "error": normalized,
            }
        verdict = _parse_verdict(verdict_json)
        policy.verdict = json.dumps(verdict, sort_keys=True)
        policy.measured_value = verdict["measured_value"]
        policy.attempts = policy.attempts + 1
        if verdict["cannot_verify"]:
            self.policies[policy_id] = policy
            return {
                "policy_id": policy_id,
                "status": policy.status,
                "attempts": policy.attempts,
                "cannot_verify": True,
            }
        policy.status = "judged"
        self.policies[policy_id] = policy
        return {
            "policy_id": policy_id,
            "status": "judged",
            "triggered": verdict["triggered"],
            "measured_value": verdict["measured_value"],
            "attempts": policy.attempts,
        }

    @gl.public.write
    def settle(self, policy_id: u256) -> dict:
        """Settle deterministically from the stored verdict. Once only.

        No nondeterministic call happens here: the verdict was already
        agreed by consensus in request_settlement, so settlement is
        pure arithmetic on the stored verdict. Triggered: the payout
        goes to the farmer and the premium to the underwriter. Not
        triggered: payout plus premium go to the underwriter. A policy
        with no verdict, or a cannot_verify verdict, cannot settle —
        it stays open for re-judgment or expiry.
        """
        policy = self._get_policy(policy_id)
        _require_status(policy, "judged")
        verdict = _parse_verdict(policy.verdict)
        if verdict["cannot_verify"]:
            raise gl.vm.UserError("policy has no verifiable verdict; settle is not possible")
        cover = self._get_cover(policy.cover_id)
        if verdict["triggered"]:
            self._pay(policy.farmer, cover.payout_amount)
            self._pay(cover.underwriter, policy.premium)
            self.total_paid_out_atto = self.total_paid_out_atto + cover.payout_amount
            paid_farmer = cover.payout_amount
            paid_underwriter = policy.premium
        else:
            self._pay(cover.underwriter, cover.payout_amount + policy.premium)
            paid_farmer = 0
            paid_underwriter = cover.payout_amount + policy.premium
        self.total_premiums_held_atto = self.total_premiums_held_atto - policy.premium
        policy.status = "settled"
        self.policies[policy_id] = policy
        return {
            "policy_id": policy_id,
            "status": "settled",
            "triggered": verdict["triggered"],
            "paid_farmer_atto": paid_farmer,
            "paid_underwriter_atto": paid_underwriter,
        }

    @gl.public.write
    def expire_policy(self, policy_id: u256) -> dict:
        """Deterministic escape for a policy that never gets settled.

        Anyone may call it once the policy has sat "active" (no usable
        verdict stored) for more than 3 days past the cover period end
        (259200 seconds). This is the fix for the hole found in
        DeliveryEscrow: a failed consensus writes nothing on-chain, so
        an attempt-based escape can never fire when consensus itself
        fails — the escape has to be time-based, using the transaction
        datetime, which every validator sees identically. The premium
        is refunded to the farmer and the payout is released to the
        underwriter. A policy that already has a verdict ("judged") is
        deliberately NOT expirable: expiring it would destroy a
        consensus verdict and could steal a farmer's payout — it must
        be settled instead. See CONSENSUS.md.
        """
        policy = self._get_policy(policy_id)
        _require_status(policy, "active")
        cover = self._get_cover(policy.cover_id)
        if self._now() <= cover.period_end + EXPIRE_AFTER_SECONDS:
            raise gl.vm.UserError(
                "policy is not yet expirable; 3 days must pass after the cover period ends"
            )
        self._pay(policy.farmer, policy.premium)
        self._pay(cover.underwriter, cover.payout_amount)
        self.total_premiums_held_atto = self.total_premiums_held_atto - policy.premium
        policy.status = "expired"
        self.policies[policy_id] = policy
        return {
            "policy_id": policy_id,
            "status": "expired",
            "refunded_premium_atto": policy.premium,
            "released_payout_atto": cover.payout_amount,
        }

    @gl.public.write
    def cancel_cover(self, cover_id: u256) -> dict:
        """Underwriter-only cancel of an "open" (never bought) cover.
        The locked payout is returned to the underwriter."""
        cover = self._get_cover(cover_id)
        if gl.message.sender_address != cover.underwriter:
            raise gl.vm.UserError("only the underwriter can cancel the cover")
        if cover.status != "open":
            raise gl.vm.UserError("cover is not open")
        self._pay(cover.underwriter, cover.payout_amount)
        cover.status = "cancelled"
        self.covers[cover_id] = cover
        return {"cover_id": cover_id, "status": "cancelled", "refunded_atto": cover.payout_amount}

    # -- views --------------------------------------------------------------
    @gl.public.view
    def get_cover(self, cover_id: u256) -> dict:
        return _cover_to_dict(self._get_cover(cover_id))

    @gl.public.view
    def list_covers(self, offset: u256, limit: u256) -> dict:
        """Paginated list (max 100 per call), oldest first."""
        total = self.counters["cover_count"]
        if limit > 100:
            limit = 100
        end = offset + limit
        if end > total:
            end = total
        out = []
        i = offset + 1
        while i <= end:
            out.append(_cover_to_dict(self.covers[i]))
            i = i + 1
        return {"total": total, "offset": offset, "covers": out}

    @gl.public.view
    def get_policy(self, policy_id: u256) -> dict:
        return _policy_to_dict(self._get_policy(policy_id))

    @gl.public.view
    def list_policies(self, offset: u256, limit: u256) -> dict:
        """Paginated list (max 100 per call), oldest first."""
        total = self.counters["policy_count"]
        if limit > 100:
            limit = 100
        end = offset + limit
        if end > total:
            end = total
        out = []
        i = offset + 1
        while i <= end:
            out.append(_policy_to_dict(self.policies[i]))
            i = i + 1
        return {"total": total, "offset": offset, "policies": out}

    @gl.public.view
    def get_stats(self) -> dict:
        """Covers, policies still unsettled (active or judged), GEN paid
        out to farmers on triggered settlements, and premiums currently
        held in escrow."""
        active = 0
        for policy_id in self.policy_order:
            policy = self.policies[policy_id]
            if policy.status in ("active", "judged"):
                active = active + 1
        return {
            "total_covers": self.counters["cover_count"],
            "total_policies": self.counters["policy_count"],
            "active_policies": active,
            "total_paid_out_atto": self.total_paid_out_atto,
            "total_premiums_held_atto": self.total_premiums_held_atto,
        }


def _cover_to_dict(cover: Cover) -> dict:
    """Public-safe serialization of a cover."""
    return {
        "id": cover.id,
        "underwriter": cover.underwriter,
        "location_name": cover.location_name,
        "latitude": cover.latitude,
        "longitude": cover.longitude,
        "peril": cover.peril,
        "threshold": cover.threshold,
        "period_start": cover.period_start,
        "period_end": cover.period_end,
        "payout_amount": cover.payout_amount,
        "premium": cover.premium,
        "data_url": cover.data_url,
        "status": cover.status,
        "created_at": cover.created_at,
    }


def _policy_to_dict(policy: Policy) -> dict:
    """Public-safe serialization of a policy."""
    return {
        "id": policy.id,
        "cover_id": policy.cover_id,
        "farmer": policy.farmer,
        "premium": policy.premium,
        "status": policy.status,
        "verdict": policy.verdict,
        "measured_value": policy.measured_value,
        "attempts": policy.attempts,
        "created_at": policy.created_at,
    }
