"""Pytest fixtures: a minimal fake `genlayer` module so the contract file
imports and runs without GenVM installed.

The stub mirrors the parts of the real API the contract uses:
- `gl` namespace: public.write / public.write.payable / public.view,
  evm.contract_interface, nondet.web.render, nondet.exec_prompt,
  vm.run_nondet_unsafe (with gl.vm.Return / gl.vm.UserError Result
  types), eq_principle, message, Contract.
- Top-level types: u256, Address, TreeMap, DynArray, allow_storage.

There is deliberately NO `gl.block`: GenLayer exposes no block number,
hash or timestamp (docs: Transaction Context). Time comes from the
stdlib clock, which GenVM wires to the transaction datetime — the stub
pins the contract module's `datetime.now(timezone.utc)` to the test's
NOW via _FixedDateTime (see set_now). `datetime.fromtimestamp` is also
modeled (delegated to the real datetime), because the contract derives
the Open-Meteo calendar dates from the cover period timestamps.

`run_nondet_unsafe` here models the real flow in miniature: run the
leader, wrap its outcome in a gl.vm.Result (Return with the value on
.calldata, or gl.vm.UserError carrying the leader's message), hand that
Result to the validator, and raise if the validator disagrees. If the
leader errored and the validator agrees it failed, the leader's error
is re-raised, as errors flow from nondet to deterministic code. Tests
override `gl.nondet.web.render` with canned weather JSON, and patch
`_PayoutTarget.emit_transfer` to record payouts. RainCover uses no
language model: the leader computes the verdict by deterministic
arithmetic on the fetched data, so exec_prompt is never called.
"""
import sys
import types
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Build the fake genlayer module
# ---------------------------------------------------------------------------

class _Deco:
    """A no-op decorator that is also usable as `.payable`.

    Plain functions stored as class attributes would get `self` bound when
    accessed through an instance; this object avoids that.
    """

    def __call__(self, fn):
        return fn

    @property
    def payable(self):
        return self


class _InterfaceDeco:
    """Decorator for @gl.evm.contract_interface classes.

    In real GenVM, an interface class is instantiated with the payout
    destination's address: ``_PayoutTarget(recipient).emit_transfer(value=v)``.
    The stub emulates that by giving the class an address-taking constructor.
    """

    def __call__(self, cls):
        class Wrapped(cls):
            def __init__(self, address=None):
                self._address = address

            def emit_transfer(self, value):
                # Default no-op: real GenVM provides this on the
                # interface. Tests that care about payouts override it
                # via record_transfers(); tests that don't simply let
                # the transfer succeed silently.
                return None

        Wrapped.__name__ = cls.__name__
        Wrapped.__qualname__ = cls.__qualname__
        return Wrapped


class _Public:
    write = _Deco()
    view = _Deco()


class _Evm:
    contract_interface = _InterfaceDeco()


class _Web:
    @staticmethod
    def render(url, mode="text"):
        raise RuntimeError("nondet web.render not stubbed for this test")


class _Nondet:
    web = _Web()

    @staticmethod
    def exec_prompt(prompt, response_format="json"):
        raise RuntimeError("nondet exec_prompt not stubbed for this test")


class _Vm:
    class Return:
        """A successful leader outcome; the value is on .calldata."""

        def __init__(self, calldata):
            self.calldata = calldata

    class UserError(Exception):
        """A leader application error, delivered to the validator as a
        gl.vm.Result carrying the leader's message on .message."""

        def __init__(self, message):
            super().__init__(message)
            self.message = message

    @staticmethod
    def run_nondet_unsafe(leader_fn, validator_fn):
        try:
            result = leader_fn()
        except Exception as e:
            leaders_res = _Vm.UserError(str(e))
            if not validator_fn(leaders_res):
                raise RuntimeError("consensus failed: validator disagreed with leader")
            raise  # validators agreed the leader failed — propagate its error
        leaders_res = _Vm.Return(result)
        if not validator_fn(leaders_res):
            raise RuntimeError("consensus failed: validator disagreed with leader")
        return result


class _EqPrinciple:
    @staticmethod
    def strict_eq(a, b):
        return a == b

    @staticmethod
    def custom_eq(a, b, fn):
        return fn(a, b)


class _Message:
    sender_address = "0x0000000000000000000000000000000000000000"
    value = 0


class _Gl:
    public = _Public()
    evm = _Evm()
    nondet = _Nondet()
    vm = _Vm()
    eq_principle = _EqPrinciple()
    message = _Message()

    class Contract:
        pass


class TreeMap(dict):
    def __class_getitem__(cls, params):
        return cls


class DynArray(list):
    def __class_getitem__(cls, params):
        return cls


genlayer = types.ModuleType("genlayer")
genlayer.gl = _Gl()
genlayer.u256 = int
genlayer.Address = str
genlayer.TreeMap = TreeMap
genlayer.DynArray = DynArray
genlayer.allow_storage = _Deco()
genlayer.__all__ = ["gl", "u256", "Address", "TreeMap", "DynArray", "allow_storage"]
sys.modules["genlayer"] = genlayer

# ---------------------------------------------------------------------------
# Import the contract file by path (no package needed)
# ---------------------------------------------------------------------------
CONTRACT_PATH = Path(__file__).resolve().parent.parent / "contracts" / "rain_cover.py"

import importlib.util

_spec = importlib.util.spec_from_file_location("rain_cover", CONTRACT_PATH)
rain_cover = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rain_cover)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

OWNER = "0x1111111111111111111111111111111111111111"
UNDERWRITER = "0x2222222222222222222222222222222222222222"
FARMER = "0x3333333333333333333333333333333333333333"
FARMER2 = "0x4444444444444444444444444444444444444444"
NOW = 1_800_000_000  # fixed "now" for deterministic deadline tests
# NOW is 2027-01-15T08:00:00Z. The demo cover below uses a 30-day period
# ending yesterday, so settlement can run immediately.
PERIOD_START = NOW - 30 * 86_400
PERIOD_END = NOW - 86_400

ONE_GEN = 10**18

from datetime import datetime as _RealDateTime, timezone as _RealTimezone


class _FixedDateTime:
    """Stand-in for the contract module's `datetime` name.

    Real GenVM wires the stdlib clock to the transaction datetime, so
    `datetime.now(timezone.utc)` returns the same value on every
    validator (docs: Transaction Context). The stub pins it to NOW;
    set_now() moves it, which is how tests simulate time passing.
    `fromtimestamp` is delegated to the real datetime: the contract
    derives calendar dates from period timestamps with it.
    """

    _now = _RealDateTime.fromtimestamp(NOW, tz=_RealTimezone.utc)

    @classmethod
    def now(cls, tz=None):
        return cls._now

    @classmethod
    def fromtimestamp(cls, ts, tz=None):
        return _RealDateTime.fromtimestamp(ts, tz=tz or _RealTimezone.utc)

    @classmethod
    def set_now(cls, ts):
        cls._now = _RealDateTime.fromtimestamp(ts, tz=_RealTimezone.utc)


rain_cover.datetime = _FixedDateTime


def set_now(ts):
    """Move the stub's pinned transaction datetime to Unix seconds `ts`."""
    _FixedDateTime.set_now(ts)


@pytest.fixture(autouse=True)
def _reset_nondet(gl):
    """Every test starts with the nondet calls un-stubbed (raising), so a
    test that forgets to stub a verdict fails loudly instead of silently
    reusing the previous test's canned response."""
    gl.nondet.web.render = staticmethod(
        lambda url, mode="text": (_ for _ in ()).throw(
            RuntimeError("nondet web.render not stubbed for this test")
        )
    )
    gl.nondet.exec_prompt = staticmethod(
        lambda prompt, response_format="json": (_ for _ in ()).throw(
            RuntimeError("nondet exec_prompt not stubbed for this test")
        )
    )
    yield


@pytest.fixture
def gl():
    return genlayer.gl


@pytest.fixture
def contract(gl):
    """A freshly deployed contract with sender=owner and a fixed clock.

    Storage maps are created by hand here: real GenVM auto-creates them,
    but the stub needs them to exist before __init__ runs.
    """
    gl.message.sender_address = OWNER
    gl.message.value = 0
    set_now(NOW)
    c = object.__new__(rain_cover.RainCover)
    c.covers = {}
    c.cover_order = []
    c.policies = {}
    c.policy_order = []
    c.counters = {}
    c.__init__(OWNER)
    return c


@pytest.fixture
def cover(contract, gl):
    """An open drought cover from UNDERWRITER: Kano, Nigeria, 30-day
    period ending yesterday, payout 5 GEN, premium 1 GEN, threshold
    10.00 mm (1000 hundredths)."""
    gl.message.sender_address = UNDERWRITER
    gl.message.value = 5 * ONE_GEN
    cover_id = contract.create_cover(
        "Kano, Nigeria",
        12.0,
        8.52,
        "drought",
        1000,
        PERIOD_START,
        PERIOD_END,
        5 * ONE_GEN,
        1 * ONE_GEN,
    )["cover_id"]
    gl.message.value = 0
    return cover_id


@pytest.fixture
def policy(contract, gl, cover):
    """An active policy: FARMER bought `cover` for its 1 GEN premium."""
    gl.message.sender_address = FARMER
    gl.message.value = 1 * ONE_GEN
    policy_id = contract.buy_cover(cover)["policy_id"]
    gl.message.value = 0
    return policy_id


def set_sender(gl, account, value=0):
    gl.message.sender_address = account
    gl.message.value = value


def weather_json(precip=None, temps=None, days=3):
    """A canned Open-Meteo archive response as a JSON string.

    precip: list of daily precipitation_sum (mm); temps: list of daily
    temperature_2m_max (C). Defaults: 3 dry days, 30.0 C each.
    """
    import json as _json

    if precip is None:
        precip = [0.0] * days
    if temps is None:
        temps = [30.0] * days
    dates = ["2027-01-%02d" % (i + 1) for i in range(len(precip))]
    return _json.dumps(
        {
            "daily": {
                "time": dates,
                "precipitation_sum": precip,
                "temperature_2m_max": temps,
            }
        }
    )


def stub_settlement(gl, fetched_json):
    """Make the stub's web.render return a fixed weather response for
    everyone (leader and validator fetch the same data, so they agree).
    """
    gl.nondet.web.render = staticmethod(lambda url, mode="text": fetched_json)


def stub_settlement_sequence(gl, responses):
    """Like stub_settlement, but web.render returns the queued responses
    in order (the last one repeats once the queue is exhausted) — so a
    test can make the leader and the validator fetch different data
    and disagree."""
    queue = list(responses)

    def _next(url, mode="text"):
        if len(queue) > 1:
            return queue.pop(0)
        if queue:
            return queue[0]
        raise RuntimeError("no more canned responses")

    gl.nondet.web.render = staticmethod(_next)


def record_transfers(monkeypatch):
    """Patch _PayoutTarget.emit_transfer to record (destination, value) calls.

    emit_transfer takes only value=; the destination is the address the
    interface was constructed with, stored on the instance as _address.
    """
    calls = []
    monkeypatch.setattr(
        rain_cover._PayoutTarget,
        "emit_transfer",
        lambda self, value: calls.append((self._address, value)),
        raising=False,
    )
    return calls


def drought_triggered_verdict(measured=0):
    """A drought verdict that triggered (measured < 1000 hundredths)."""
    return (
        '{"measured_value": %d, "triggered": true, "cannot_verify": false, '
        '"reasoning": "drought: sum = 0.00 mm; threshold 10.00 mm"}' % measured
    )


def not_triggered_verdict(measured=2500):
    """A verdict that did not trigger (measured 25.00 mm > 10.00 mm)."""
    return (
        '{"measured_value": %d, "triggered": false, "cannot_verify": false, '
        '"reasoning": "drought: sum = 25.00 mm; threshold 10.00 mm"}' % measured
    )


def cannot_verify_verdict():
    return (
        '{"measured_value": 0, "triggered": false, "cannot_verify": true, '
        '"reasoning": "weather data has no daily series"}'
    )
