"""Tests for RainCover's pure helpers and its lifecycle (stubbed GenVM).

Run: python3 -m pytest tests/ -q
"""
import json
import math

import pytest

from conftest import (
    FARMER,
    FARMER2,
    NOW,
    ONE_GEN,
    OWNER,
    PERIOD_END,
    PERIOD_START,
    UNDERWRITER,
    cannot_verify_verdict,
    drought_triggered_verdict,
    not_triggered_verdict,
    record_transfers,
    set_now,
    set_sender,
    stub_settlement,
    stub_settlement_sequence,
    weather_json,
    rain_cover,
)

m = rain_cover  # the contract module
UserError = rain_cover.gl.vm.UserError


def _make_cover(contract, gl, peril="drought", threshold=1000,
                start=PERIOD_START, end=PERIOD_END,
                payout=5 * ONE_GEN, premium=ONE_GEN, value=5 * ONE_GEN,
                sender=UNDERWRITER, lat="12.0", lon="8.52",
                location="Kano, Nigeria"):
    set_sender(gl, sender, value)
    cover_id = contract.create_cover(
        location, lat, lon, peril, threshold, start, end, payout, premium
    )["cover_id"]
    set_sender(gl, sender, 0)
    return cover_id


def _make_policy(contract, gl, cover_id, sender=FARMER, value=ONE_GEN):
    set_sender(gl, sender, value)
    policy_id = contract.buy_cover(cover_id)["policy_id"]
    set_sender(gl, sender, 0)
    return policy_id


class _FakeCover:
    """Minimal stand-in for a Cover: _compute_verdict only reads peril
    and threshold."""

    def __init__(self, peril, threshold):
        self.peril = peril
        self.threshold = threshold


# ---------------------------------------------------------------------------
# Pure helpers: coordinates, dates, URL
# ---------------------------------------------------------------------------

def test_format_coord_positive():
    assert m._format_coord(12000000) == "12.000000"
    assert m._format_coord(8520000) == "8.520000"


def test_format_coord_zero():
    assert m._format_coord(0) == "0.000000"


def test_format_coord_negative():
    assert m._format_coord(-33868800) == "-33.868800"
    assert m._format_coord(-1) == "-0.000001"


def test_format_coord_max():
    assert m._format_coord(180000000) == "180.000000"
    assert m._format_coord(-90000000) == "-90.000000"


def test_parse_coord_values():
    assert m._parse_coord("12.0", "latitude") == 12000000
    assert m._parse_coord("12.000000", "latitude") == 12000000
    assert m._parse_coord("8.52", "longitude") == 8520000
    assert m._parse_coord("-33.8688", "latitude") == -33868800
    assert m._parse_coord("  8.52  ", "longitude") == 8520000
    assert m._parse_coord("90", "latitude") == 90000000
    assert m._parse_coord("-180.000000", "longitude") == -180000000


def test_parse_coord_rounds_seventh_place():
    assert m._parse_coord("12.0000004", "latitude") == 12000000
    assert m._parse_coord("12.0000005", "latitude") == 12000001
    assert m._parse_coord("-33.8688005", "latitude") == -33868801


def test_parse_coord_rejects_non_numbers():
    for bad in ("abc", "nan", "inf", "1e2", "12.0.0", "", " ", "12,5"):
        with pytest.raises(UserError, match="latitude must be a number"):
            m._parse_coord(bad, "latitude")


def test_parse_coord_rejects_out_of_range():
    with pytest.raises(UserError, match="latitude must be between"):
        m._parse_coord("90.000001", "latitude")
    with pytest.raises(UserError, match="longitude must be between"):
        m._parse_coord("-180.000001", "longitude")


def test_date_str_known_dates():
    assert m._date_str(1735689600) == "2025-01-01"
    assert m._date_str(1738367999) == "2025-01-31"


def test_date_str_leap_day():
    assert m._date_str(1709164800) == "2024-02-29"


def test_archive_url_exact_kano_january():
    url = m._archive_url("12.000000", "8.520000", 1735689600, 1738367999)
    assert url == (
        "https://archive-api.open-meteo.com/v1/archive?latitude=12.000000"
        "&longitude=8.520000&start_date=2025-01-01&end_date=2025-01-31"
        "&daily=precipitation_sum,temperature_2m_max&timezone=UTC"
    )


def test_fmt_hundredths():
    assert m._fmt_hundredths(0) == "0.00"
    assert m._fmt_hundredths(5) == "0.05"
    assert m._fmt_hundredths(1000) == "10.00"
    assert m._fmt_hundredths(4140) == "41.40"
    assert m._fmt_hundredths(-250) == "-2.50"


# ---------------------------------------------------------------------------
# _compute_verdict: measurement and triggers
# ---------------------------------------------------------------------------

def test_compute_drought_triggered_zero_rain():
    verdict = m._compute_verdict(
        _FakeCover("drought", 1000), weather_json(precip=[0.0] * 31)
    )
    assert verdict["measured_value"] == 0
    assert verdict["triggered"] is True
    assert verdict["cannot_verify"] is False


def test_compute_drought_not_triggered():
    verdict = m._compute_verdict(
        _FakeCover("drought", 1000), weather_json(precip=[10.0, 10.0, 5.5])
    )
    assert verdict["measured_value"] == 2550
    assert verdict["triggered"] is False


def test_compute_drought_boundary_exact_threshold_not_triggered():
    verdict = m._compute_verdict(
        _FakeCover("drought", 1000), weather_json(precip=[4.0, 6.0])
    )
    assert verdict["measured_value"] == 1000
    assert verdict["triggered"] is False


def test_compute_drought_just_below_threshold_triggered():
    verdict = m._compute_verdict(
        _FakeCover("drought", 1000), weather_json(precip=[4.0, 5.99])
    )
    assert verdict["measured_value"] == 999
    assert verdict["triggered"] is True


def test_compute_excess_rain_triggered():
    verdict = m._compute_verdict(
        _FakeCover("excess_rain", 1000), weather_json(precip=[10.0, 10.0, 5.5])
    )
    assert verdict["measured_value"] == 2550
    assert verdict["triggered"] is True


def test_compute_excess_rain_not_triggered():
    verdict = m._compute_verdict(
        _FakeCover("excess_rain", 1000), weather_json(precip=[2.0, 3.0])
    )
    assert verdict["measured_value"] == 500
    assert verdict["triggered"] is False


def test_compute_excess_rain_boundary_exact_threshold_not_triggered():
    verdict = m._compute_verdict(
        _FakeCover("excess_rain", 1000), weather_json(precip=[4.0, 6.0])
    )
    assert verdict["measured_value"] == 1000
    assert verdict["triggered"] is False


def test_compute_heat_triggered():
    verdict = m._compute_verdict(
        _FakeCover("heat", 4000),
        weather_json(temps=[37.2, 38.4, 41.4, 39.0]),
    )
    assert verdict["measured_value"] == 4140
    assert verdict["triggered"] is True


def test_compute_heat_not_triggered():
    verdict = m._compute_verdict(
        _FakeCover("heat", 4000),
        weather_json(temps=[37.2, 38.4, 39.9]),
    )
    assert verdict["measured_value"] == 3990
    assert verdict["triggered"] is False


def test_compute_heat_boundary_exact_threshold_not_triggered():
    verdict = m._compute_verdict(
        _FakeCover("heat", 4000), weather_json(temps=[39.0, 40.0])
    )
    assert verdict["measured_value"] == 4000
    assert verdict["triggered"] is False


def test_compute_heat_uses_max_not_sum():
    verdict = m._compute_verdict(
        _FakeCover("heat", 4000),
        weather_json(temps=[30.0] * 30 + [41.0]),
    )
    assert verdict["measured_value"] == 4100
    assert verdict["triggered"] is True


def test_compute_fractional_rounding():
    verdict = m._compute_verdict(
        _FakeCover("drought", 1000), weather_json(precip=[1.234, 2.345])
    )
    assert verdict["measured_value"] == 358


def test_compute_reasoning_names_measured_and_threshold():
    verdict = m._compute_verdict(
        _FakeCover("drought", 1000), weather_json(precip=[0.0, 0.0])
    )
    assert "0.00 mm" in verdict["reasoning"]
    assert "10.00 mm" in verdict["reasoning"]
    assert "HIT" in verdict["reasoning"]


def test_compute_not_json_is_cannot_verify():
    verdict = m._compute_verdict(_FakeCover("drought", 1000), "not json at all")
    assert verdict["cannot_verify"] is True
    assert verdict["measured_value"] == 0
    assert verdict["triggered"] is False


def test_compute_non_object_is_cannot_verify():
    verdict = m._compute_verdict(_FakeCover("drought", 1000), "[1, 2, 3]")
    assert verdict["cannot_verify"] is True


def test_compute_no_daily_is_cannot_verify():
    verdict = m._compute_verdict(
        _FakeCover("drought", 1000), json.dumps({"error": True})
    )
    assert verdict["cannot_verify"] is True


def test_compute_missing_series_is_cannot_verify():
    verdict = m._compute_verdict(
        _FakeCover("drought", 1000), json.dumps({"daily": {"time": ["2027-01-01"]}})
    )
    assert verdict["cannot_verify"] is True


def test_compute_empty_series_is_cannot_verify():
    verdict = m._compute_verdict(
        _FakeCover("drought", 1000),
        json.dumps({"daily": {"precipitation_sum": [], "temperature_2m_max": []}}),
    )
    assert verdict["cannot_verify"] is True


def test_compute_null_value_is_cannot_verify():
    verdict = m._compute_verdict(
        _FakeCover("drought", 1000), weather_json(precip=[1.0, None, 2.0])
    )
    assert verdict["cannot_verify"] is True


def test_compute_non_numeric_value_is_cannot_verify():
    verdict = m._compute_verdict(
        _FakeCover("drought", 1000), weather_json(precip=[1.0, "abc", 2.0])
    )
    assert verdict["cannot_verify"] is True


def test_compute_bool_value_is_cannot_verify():
    verdict = m._compute_verdict(
        _FakeCover("drought", 1000), weather_json(precip=[1.0, True, 2.0])
    )
    assert verdict["cannot_verify"] is True


def test_compute_heat_ignores_precip_series():
    verdict = m._compute_verdict(
        _FakeCover("heat", 4000),
        json.dumps(
            {
                "daily": {
                    "precipitation_sum": [999.0, 999.0],
                    "temperature_2m_max": [30.0, 31.0],
                }
            }
        ),
    )
    assert verdict["measured_value"] == 3100
    assert verdict["triggered"] is False


def test_compute_drought_ignores_temp_series():
    verdict = m._compute_verdict(
        _FakeCover("drought", 1000),
        json.dumps(
            {
                "daily": {
                    "precipitation_sum": [0.0, 0.0],
                    "temperature_2m_max": [45.0, 46.0],
                }
            }
        ),
    )
    assert verdict["measured_value"] == 0
    assert verdict["triggered"] is True


# ---------------------------------------------------------------------------
# _parse_verdict
# ---------------------------------------------------------------------------

def test_parse_verdict_valid_dict():
    verdict = m._parse_verdict(
        {"measured_value": 0, "triggered": True, "cannot_verify": False,
         "reasoning": "dry"}
    )
    assert verdict == {
        "measured_value": 0,
        "triggered": True,
        "cannot_verify": False,
        "reasoning": "dry",
    }


def test_parse_verdict_json_string():
    verdict = m._parse_verdict(
        '{"measured_value": 2550, "triggered": false, '
        '"cannot_verify": false, "reasoning": "wet"}'
    )
    assert verdict["measured_value"] == 2550
    assert verdict["triggered"] is False


def test_parse_verdict_string_booleans():
    verdict = m._parse_verdict(
        {"measured_value": 100, "triggered": "true", "cannot_verify": "false"}
    )
    assert verdict["triggered"] is True
    assert verdict["cannot_verify"] is False


def test_parse_verdict_string_measured():
    verdict = m._parse_verdict(
        {"measured_value": "12.6", "triggered": False, "cannot_verify": False}
    )
    assert verdict["measured_value"] == 13


def test_parse_verdict_cannot_verify_normalizes():
    verdict = m._parse_verdict(
        {"measured_value": 500, "triggered": True, "cannot_verify": True,
         "reasoning": "no data"}
    )
    assert verdict["measured_value"] == 0
    assert verdict["triggered"] is False
    assert verdict["cannot_verify"] is True


def test_parse_verdict_missing_triggered():
    with pytest.raises(ValueError, match=r"\[VERDICT\]"):
        m._parse_verdict({"measured_value": 100, "cannot_verify": False})


def test_parse_verdict_missing_measured():
    with pytest.raises(ValueError, match=r"\[VERDICT\]"):
        m._parse_verdict({"triggered": True, "cannot_verify": False})


def test_parse_verdict_negative_measured():
    with pytest.raises(ValueError, match=r"\[VERDICT\]"):
        m._parse_verdict(
            {"measured_value": -5, "triggered": False, "cannot_verify": False}
        )


def test_parse_verdict_bool_measured():
    with pytest.raises(ValueError, match=r"\[VERDICT\]"):
        m._parse_verdict(
            {"measured_value": True, "triggered": False, "cannot_verify": False}
        )


def test_parse_verdict_non_numeric_measured():
    with pytest.raises(ValueError, match=r"\[VERDICT\]"):
        m._parse_verdict(
            {"measured_value": "wet", "triggered": False, "cannot_verify": False}
        )


def test_parse_verdict_not_json():
    with pytest.raises(ValueError, match=r"\[VERDICT\]"):
        m._parse_verdict("not json")


def test_parse_verdict_non_object():
    with pytest.raises(ValueError, match=r"\[VERDICT\]"):
        m._parse_verdict("[1, 2]")


def test_parse_verdict_bad_triggered_string():
    with pytest.raises(ValueError, match=r"\[VERDICT\]"):
        m._parse_verdict(
            {"measured_value": 0, "triggered": "maybe", "cannot_verify": False}
        )


# ---------------------------------------------------------------------------
# _verdicts_agree: tolerance rules
# ---------------------------------------------------------------------------

def _v(measured, triggered, cannot_verify=False, reasoning=""):
    return {
        "measured_value": measured,
        "triggered": triggered,
        "cannot_verify": cannot_verify,
        "reasoning": reasoning,
    }


def test_agree_exact_match():
    assert m._verdicts_agree(_v(0, True), _v(0, True)) is True


def test_agree_absolute_floor_200():
    assert m._verdicts_agree(_v(0, True), _v(200, True)) is True


def test_disagree_just_past_absolute_floor():
    assert m._verdicts_agree(_v(0, True), _v(201, True)) is False


def test_agree_ten_percent_boundary():
    assert m._verdicts_agree(_v(10000, False), _v(11000, False)) is True


def test_disagree_beyond_ten_percent():
    assert m._verdicts_agree(_v(10000, False), _v(12000, False)) is False


def test_agree_ten_percent_of_larger_value():
    assert m._verdicts_agree(_v(10000, False), _v(11100, False)) is True


def test_disagree_triggered_mismatch_even_within_tolerance():
    # 999 vs 1001 differ by 2 (well inside the +/-200 floor) but the
    # trigger outcome differs — and the trigger is what moves money.
    assert m._verdicts_agree(_v(999, True), _v(1001, False)) is False


def test_agree_cannot_verify_both():
    assert m._verdicts_agree(_v(0, False, True), _v(0, False, True)) is True


def test_disagree_cannot_verify_one_side():
    assert m._verdicts_agree(_v(0, False, True), _v(0, False, False)) is False
    assert m._verdicts_agree(_v(0, True, False), _v(0, False, True)) is False


def test_agree_reasoning_ignored():
    assert (
        m._verdicts_agree(_v(500, True, reasoning="dry month"),
                          _v(500, True, reasoning="no rain at all"))
        is True
    )


def test_agree_zero_vs_zero():
    assert m._verdicts_agree(_v(0, True), _v(0, True)) is True


# ---------------------------------------------------------------------------
# Error taxonomy
# ---------------------------------------------------------------------------

def test_handle_leader_error_passthrough():
    assert m._handle_leader_error("[TRANSIENT] slow") == "[TRANSIENT] slow"
    assert m._handle_leader_error("[EXTERNAL] 404") == "[EXTERNAL] 404"
    assert m._handle_leader_error("[EXPECTED] bad input") == "[EXPECTED] bad input"
    assert m._handle_leader_error("[VERDICT] bad verdict") == "[VERDICT] bad verdict"


def test_handle_leader_error_404_is_external():
    assert m._handle_leader_error("HTTP 404 Not Found").startswith("[EXTERNAL]")


def test_handle_leader_error_403_is_external():
    assert m._handle_leader_error("403 forbidden").startswith("[EXTERNAL]")


def test_handle_leader_error_timeout_is_transient():
    assert m._handle_leader_error("connection timed out").startswith("[TRANSIENT]")


def test_handle_leader_error_500_is_transient():
    assert m._handle_leader_error("HTTP 503 Service Unavailable").startswith("[TRANSIENT]")


def test_handle_leader_error_unknown_is_transient():
    assert m._handle_leader_error("something odd happened").startswith("[TRANSIENT]")


def test_errors_agree_transient_with_transient():
    assert m._errors_agree("connection timed out", "HTTP 500 boom") is True


def test_errors_agree_external_must_match():
    assert m._errors_agree("[EXTERNAL] 404", "[EXTERNAL] 404") is True
    assert m._errors_agree("[EXTERNAL] 404", "[EXTERNAL] 403") is False


def test_errors_agree_expected_must_match():
    assert m._errors_agree("[EXPECTED] a", "[EXPECTED] a") is True
    assert m._errors_agree("[EXPECTED] a", "[EXPECTED] b") is False


def test_errors_agree_verdict_always_disagrees():
    assert m._errors_agree("[VERDICT] bad", "[VERDICT] bad") is False
    assert m._errors_agree("[VERDICT] bad", "[TRANSIENT] slow") is False


def test_errors_agree_different_prefixes_disagree():
    assert m._errors_agree("[TRANSIENT] slow", "[EXTERNAL] 404") is False


# ---------------------------------------------------------------------------
# create_cover
# ---------------------------------------------------------------------------

def test_create_cover_happy_path(contract, gl):
    cover_id = _make_cover(contract, gl)
    cover = contract.get_cover(cover_id)
    assert cover["id"] == cover_id
    assert cover["underwriter"] == UNDERWRITER
    assert cover["location_name"] == "Kano, Nigeria"
    assert cover["latitude"] == "12.000000"
    assert cover["longitude"] == "8.520000"
    assert cover["peril"] == "drought"
    assert cover["threshold"] == 1000
    assert cover["period_start"] == PERIOD_START
    assert cover["period_end"] == PERIOD_END
    assert cover["payout_amount"] == 5 * ONE_GEN
    assert cover["premium"] == ONE_GEN
    assert cover["status"] == "open"
    assert cover["data_url"] == (
        "https://archive-api.open-meteo.com/v1/archive?latitude=12.000000"
        "&longitude=8.520000&start_date=2026-12-16&end_date=2027-01-14"
        "&daily=precipitation_sum,temperature_2m_max&timezone=UTC"
    )


def test_create_cover_negative_coordinates(contract, gl):
    cover_id = _make_cover(contract, gl, lat="-33.8688", lon="151.2093",
                           location="Sydney, Australia")
    cover = contract.get_cover(cover_id)
    assert cover["latitude"] == "-33.868800"
    assert cover["longitude"] == "151.209300"
    assert "latitude=-33.868800" in cover["data_url"]
    assert "longitude=151.209300" in cover["data_url"]


def test_create_cover_ids_increment(contract, gl):
    first = _make_cover(contract, gl)
    second = _make_cover(contract, gl)
    assert first == 1
    assert second == 2
    assert contract.get_stats()["total_covers"] == 2


def test_create_cover_end_not_after_start(contract, gl):
    with pytest.raises(UserError, match="period_end must be after"):
        _make_cover(contract, gl, start=PERIOD_END, end=PERIOD_START)
    with pytest.raises(UserError, match="period_end must be after"):
        _make_cover(contract, gl, start=PERIOD_START, end=PERIOD_START)


def test_create_cover_zero_payout(contract, gl):
    with pytest.raises(UserError, match="payout_amount must be greater"):
        _make_cover(contract, gl, payout=0, value=0)


def test_create_cover_zero_premium(contract, gl):
    with pytest.raises(UserError, match="premium must be greater"):
        _make_cover(contract, gl, premium=0)


def test_create_cover_zero_threshold(contract, gl):
    with pytest.raises(UserError, match="threshold must be greater"):
        _make_cover(contract, gl, threshold=0)


def test_create_cover_bad_peril(contract, gl):
    with pytest.raises(UserError, match="peril must be one of"):
        _make_cover(contract, gl, peril="flood")
    with pytest.raises(UserError, match="peril must be one of"):
        _make_cover(contract, gl, peril="")


def test_create_cover_each_peril_allowed(contract, gl):
    for peril in ("drought", "excess_rain", "heat"):
        cover_id = _make_cover(contract, gl, peril=peril)
        assert contract.get_cover(cover_id)["peril"] == peril


def test_create_cover_latitude_out_of_range(contract, gl):
    with pytest.raises(UserError, match="latitude must be between"):
        _make_cover(contract, gl, lat="91")
    with pytest.raises(UserError, match="latitude must be between"):
        _make_cover(contract, gl, lat="-91")


def test_create_cover_longitude_out_of_range(contract, gl):
    with pytest.raises(UserError, match="longitude must be between"):
        _make_cover(contract, gl, lon="181")
    with pytest.raises(UserError, match="longitude must be between"):
        _make_cover(contract, gl, lon="-181")


def test_create_cover_nan_latitude(contract, gl):
    with pytest.raises(UserError, match="latitude must be a number"):
        _make_cover(contract, gl, lat="nan")


def test_create_cover_nan_longitude(contract, gl):
    with pytest.raises(UserError, match="longitude must be a number"):
        _make_cover(contract, gl, lon="nan")


def test_create_cover_empty_location(contract, gl):
    with pytest.raises(UserError, match="location_name must not be empty"):
        _make_cover(contract, gl, location="")
    with pytest.raises(UserError, match="location_name must not be empty"):
        _make_cover(contract, gl, location="   ")


def test_create_cover_value_must_equal_payout(contract, gl):
    with pytest.raises(UserError, match="attached value must equal payout"):
        _make_cover(contract, gl, value=4 * ONE_GEN)
    with pytest.raises(UserError, match="attached value must equal payout"):
        _make_cover(contract, gl, value=6 * ONE_GEN)
    with pytest.raises(UserError, match="attached value must equal payout"):
        _make_cover(contract, gl, value=0)


def test_create_cover_latitude_boundary_allowed(contract, gl):
    for lat in ("-90", "90"):
        cover_id = _make_cover(contract, gl, lat=lat)
        assert contract.get_cover(cover_id)["latitude"] in ("-90.000000", "90.000000")
    for lon in ("-180", "180"):
        cover_id = _make_cover(contract, gl, lon=lon)
        assert contract.get_cover(cover_id)["longitude"] in ("-180.000000", "180.000000")


def test_get_cover_not_found(contract, gl):
    with pytest.raises(UserError, match="cover not found"):
        contract.get_cover(99)


# ---------------------------------------------------------------------------
# buy_cover
# ---------------------------------------------------------------------------

def test_buy_cover_happy_path(contract, gl, cover):
    policy_id = _make_policy(contract, gl, cover)
    policy = contract.get_policy(policy_id)
    assert policy["cover_id"] == cover
    assert policy["farmer"] == FARMER
    assert policy["premium"] == ONE_GEN
    assert policy["status"] == "active"
    assert policy["verdict"] == ""
    assert policy["measured_value"] == 0
    assert policy["attempts"] == 0
    assert contract.get_cover(cover)["status"] == "sold"
    assert contract.get_stats()["total_premiums_held_atto"] == ONE_GEN
    assert contract.get_stats()["active_policies"] == 1


def test_buy_cover_wrong_premium(contract, gl, cover):
    with pytest.raises(UserError, match="attached value must equal the premium"):
        _make_policy(contract, gl, cover, value=ONE_GEN // 2)
    with pytest.raises(UserError, match="attached value must equal the premium"):
        _make_policy(contract, gl, cover, value=2 * ONE_GEN)
    with pytest.raises(UserError, match="attached value must equal the premium"):
        _make_policy(contract, gl, cover, value=0)


def test_buy_cover_double_buy(contract, gl, cover):
    _make_policy(contract, gl, cover)
    with pytest.raises(UserError, match="cover is not open"):
        _make_policy(contract, gl, cover, sender=FARMER2)


def test_buy_cover_not_found(contract, gl):
    with pytest.raises(UserError, match="cover not found"):
        _make_policy(contract, gl, 99)


def test_buy_cover_policy_ids_increment(contract, gl):
    first_cover = _make_cover(contract, gl)
    second_cover = _make_cover(contract, gl)
    first = _make_policy(contract, gl, first_cover)
    second = _make_policy(contract, gl, second_cover, sender=FARMER2)
    assert first == 1
    assert second == 2
    assert contract.get_stats()["total_policies"] == 2
    assert contract.get_stats()["total_premiums_held_atto"] == 2 * ONE_GEN


# ---------------------------------------------------------------------------
# request_settlement
# ---------------------------------------------------------------------------

def test_request_settlement_before_period_end(contract, gl):
    cover_id = _make_cover(
        contract, gl, start=NOW + 86_400, end=NOW + 30 * 86_400
    )
    policy_id = _make_policy(contract, gl, cover_id)
    with pytest.raises(UserError, match="period has not ended"):
        contract.request_settlement(policy_id)


def test_request_settlement_triggered(contract, gl, policy):
    stub_settlement(gl, weather_json(precip=[0.0, 0.0, 0.0]))
    result = contract.request_settlement(policy)
    assert result["status"] == "judged"
    assert result["triggered"] is True
    assert result["measured_value"] == 0
    assert result["attempts"] == 1
    stored = contract.get_policy(policy)
    assert stored["status"] == "judged"
    assert stored["measured_value"] == 0
    verdict = json.loads(stored["verdict"])
    assert verdict["triggered"] is True
    assert verdict["cannot_verify"] is False


def test_request_settlement_not_triggered(contract, gl, policy):
    stub_settlement(gl, weather_json(precip=[10.0, 10.0, 10.0]))
    result = contract.request_settlement(policy)
    assert result["status"] == "judged"
    assert result["triggered"] is False
    assert result["measured_value"] == 3000


def test_request_settlement_cannot_verify_stays_active(contract, gl, policy):
    stub_settlement(gl, "this is not json")
    result = contract.request_settlement(policy)
    assert result["cannot_verify"] is True
    assert result["attempts"] == 1
    assert contract.get_policy(policy)["status"] == "active"


def test_request_settlement_fetch_failure_counts_attempt(contract, gl, policy):
    gl.nondet.web.render = staticmethod(
        lambda url, mode="text": (_ for _ in ()).throw(
            RuntimeError("connection timed out")
        )
    )
    result = contract.request_settlement(policy)
    assert result["attempts"] == 1
    assert "[TRANSIENT]" in result["error"]
    assert contract.get_policy(policy)["status"] == "active"


def test_request_settlement_three_failures_then_exhausted(contract, gl, policy):
    gl.nondet.web.render = staticmethod(
        lambda url, mode="text": (_ for _ in ()).throw(
            RuntimeError("connection timed out")
        )
    )
    for _ in range(3):
        result = contract.request_settlement(policy)
        assert result["status"] == "active"
    assert contract.get_policy(policy)["attempts"] == 3
    with pytest.raises(UserError, match="attempts exhausted"):
        contract.request_settlement(policy)


def test_request_settlement_disagreement_counts_attempt(contract, gl, policy):
    stub_settlement_sequence(
        gl,
        [
            weather_json(precip=[0.0, 0.0, 0.0]),
            weather_json(precip=[50.0, 50.0, 50.0]),
        ],
    )
    result = contract.request_settlement(policy)
    assert result["attempts"] == 1
    assert "consensus failed" in result["error"]
    assert contract.get_policy(policy)["status"] == "active"


def test_request_settlement_validator_fetch_fails(contract, gl, policy):
    calls = {"n": 0}
    good = weather_json(precip=[0.0, 0.0, 0.0])

    def _render(url, mode="text"):
        calls["n"] += 1
        if calls["n"] == 1:
            return good
        raise RuntimeError("connection timed out")

    gl.nondet.web.render = staticmethod(_render)
    result = contract.request_settlement(policy)
    assert result["attempts"] == 1
    assert contract.get_policy(policy)["status"] == "active"


def test_request_settlement_tolerance_agreement(contract, gl, policy):
    # Leader measures 10.00 mm, validator 11.00 mm: diff 100, inside the
    # +/-200 absolute floor, and both agree the trigger did not hit.
    stub_settlement_sequence(
        gl,
        [
            weather_json(precip=[3.0, 3.0, 4.0]),
            weather_json(precip=[3.5, 3.5, 4.0]),
        ],
    )
    result = contract.request_settlement(policy)
    assert result["status"] == "judged"
    assert result["triggered"] is False
    assert result["measured_value"] == 1000


def test_request_settlement_tolerance_disagreement(contract, gl, policy):
    # Leader 100.00 mm, validator 120.00 mm: diff 2000, beyond 10% of
    # the larger (1200), so consensus fails.
    stub_settlement_sequence(
        gl,
        [
            weather_json(precip=[33.33, 33.33, 33.34]),
            weather_json(precip=[40.0, 40.0, 40.0]),
        ],
    )
    result = contract.request_settlement(policy)
    assert result["attempts"] == 1
    assert contract.get_policy(policy)["status"] == "active"


def test_request_settlement_trigger_straddle_disagrees(contract, gl, policy):
    # 9.99 mm vs 10.01 mm differ by 2 (inside the tolerance) but the
    # trigger outcome differs — and the trigger moves money.
    stub_settlement_sequence(
        gl,
        [
            weather_json(precip=[3.33, 3.33, 3.33]),
            weather_json(precip=[3.34, 3.34, 3.33]),
        ],
    )
    result = contract.request_settlement(policy)
    assert result["attempts"] == 1
    assert contract.get_policy(policy)["status"] == "active"


def test_request_settlement_not_found(contract, gl):
    with pytest.raises(UserError, match="policy not found"):
        contract.request_settlement(99)


def test_request_settlement_twice_after_judged(contract, gl, policy):
    stub_settlement(gl, weather_json(precip=[0.0, 0.0, 0.0]))
    contract.request_settlement(policy)
    with pytest.raises(UserError, match="is 'judged'"):
        contract.request_settlement(policy)


# ---------------------------------------------------------------------------
# settle
# ---------------------------------------------------------------------------

def test_settle_triggered_pays_farmer_and_premium(contract, gl, policy, monkeypatch):
    calls = record_transfers(monkeypatch)
    stub_settlement(gl, weather_json(precip=[0.0, 0.0, 0.0]))
    contract.request_settlement(policy)
    result = contract.settle(policy)
    assert result["status"] == "settled"
    assert result["triggered"] is True
    assert result["paid_farmer_atto"] == 5 * ONE_GEN
    assert result["paid_underwriter_atto"] == ONE_GEN
    assert calls == [(FARMER, 5 * ONE_GEN), (UNDERWRITER, ONE_GEN)]
    assert contract.get_policy(policy)["status"] == "settled"
    stats = contract.get_stats()
    assert stats["total_paid_out_atto"] == 5 * ONE_GEN
    assert stats["total_premiums_held_atto"] == 0
    assert stats["active_policies"] == 0


def test_settle_not_triggered_pays_underwriter_everything(contract, gl, policy, monkeypatch):
    calls = record_transfers(monkeypatch)
    stub_settlement(gl, weather_json(precip=[10.0, 10.0, 10.0]))
    contract.request_settlement(policy)
    result = contract.settle(policy)
    assert result["status"] == "settled"
    assert result["triggered"] is False
    assert result["paid_farmer_atto"] == 0
    assert result["paid_underwriter_atto"] == 6 * ONE_GEN
    assert calls == [(UNDERWRITER, 6 * ONE_GEN)]
    assert contract.get_stats()["total_paid_out_atto"] == 0
    assert contract.get_stats()["total_premiums_held_atto"] == 0


def test_settle_without_verdict(contract, gl, policy):
    with pytest.raises(UserError, match="is 'active'"):
        contract.settle(policy)


def test_settle_cannot_verify_verdict(contract, gl, policy):
    stub_settlement(gl, "this is not json")
    contract.request_settlement(policy)
    with pytest.raises(UserError, match="is 'active'"):
        contract.settle(policy)


def test_settle_twice(contract, gl, policy):
    stub_settlement(gl, weather_json(precip=[0.0, 0.0, 0.0]))
    contract.request_settlement(policy)
    contract.settle(policy)
    with pytest.raises(UserError, match="is 'settled'"):
        contract.settle(policy)


def test_settle_by_anyone(contract, gl, policy):
    stub_settlement(gl, weather_json(precip=[0.0, 0.0, 0.0]))
    contract.request_settlement(policy)
    set_sender(gl, FARMER2)
    result = contract.settle(policy)
    assert result["status"] == "settled"
    assert result["paid_farmer_atto"] == 5 * ONE_GEN


def test_settle_not_found(contract, gl):
    with pytest.raises(UserError, match="policy not found"):
        contract.settle(99)


# ---------------------------------------------------------------------------
# expire_policy
# ---------------------------------------------------------------------------

def test_expire_before_window(contract, gl, policy):
    set_now(NOW + 172800)  # exactly period_end + 3 days: still too early
    with pytest.raises(UserError, match="not yet expirable"):
        contract.expire_policy(policy)


def test_expire_after_window(contract, gl, policy, monkeypatch):
    calls = record_transfers(monkeypatch)
    set_now(NOW + 172801)
    result = contract.expire_policy(policy)
    assert result["status"] == "expired"
    assert result["refunded_premium_atto"] == ONE_GEN
    assert result["released_payout_atto"] == 5 * ONE_GEN
    assert calls == [(FARMER, ONE_GEN), (UNDERWRITER, 5 * ONE_GEN)]
    assert contract.get_policy(policy)["status"] == "expired"
    assert contract.get_stats()["total_premiums_held_atto"] == 0
    assert contract.get_stats()["active_policies"] == 0


def test_expire_settled_policy(contract, gl, policy):
    stub_settlement(gl, weather_json(precip=[0.0, 0.0, 0.0]))
    contract.request_settlement(policy)
    contract.settle(policy)
    set_now(NOW + 172801)
    with pytest.raises(UserError, match="is 'settled'"):
        contract.expire_policy(policy)


def test_expire_judged_policy_refused(contract, gl, policy):
    # A stored verdict must be settled, not expired: expiring it would
    # destroy a consensus verdict and could steal a farmer's payout.
    stub_settlement(gl, weather_json(precip=[0.0, 0.0, 0.0]))
    contract.request_settlement(policy)
    set_now(NOW + 172801)
    with pytest.raises(UserError, match="is 'judged'"):
        contract.expire_policy(policy)


def test_expire_not_found(contract, gl):
    with pytest.raises(UserError, match="policy not found"):
        contract.expire_policy(99)


def test_expire_after_three_failed_attempts(contract, gl, policy, monkeypatch):
    calls = record_transfers(monkeypatch)
    gl.nondet.web.render = staticmethod(
        lambda url, mode="text": (_ for _ in ()).throw(
            RuntimeError("connection timed out")
        )
    )
    for _ in range(3):
        contract.request_settlement(policy)
    set_now(NOW + 172801)
    result = contract.expire_policy(policy)
    assert result["status"] == "expired"
    assert calls == [(FARMER, ONE_GEN), (UNDERWRITER, 5 * ONE_GEN)]


# ---------------------------------------------------------------------------
# cancel_cover
# ---------------------------------------------------------------------------

def test_cancel_cover_happy_path(contract, gl, cover, monkeypatch):
    calls = record_transfers(monkeypatch)
    result = contract.cancel_cover(cover)
    assert result["status"] == "cancelled"
    assert result["refunded_atto"] == 5 * ONE_GEN
    assert calls == [(UNDERWRITER, 5 * ONE_GEN)]
    assert contract.get_cover(cover)["status"] == "cancelled"


def test_cancel_cover_not_underwriter(contract, gl, cover):
    set_sender(gl, FARMER)
    with pytest.raises(UserError, match="only the underwriter"):
        contract.cancel_cover(cover)


def test_cancel_cover_sold(contract, gl, cover):
    _make_policy(contract, gl, cover)
    set_sender(gl, UNDERWRITER)
    with pytest.raises(UserError, match="cover is not open"):
        contract.cancel_cover(cover)


def test_cancel_cover_twice(contract, gl, cover):
    contract.cancel_cover(cover)
    with pytest.raises(UserError, match="cover is not open"):
        contract.cancel_cover(cover)


def test_buy_cancelled_cover(contract, gl, cover):
    contract.cancel_cover(cover)
    with pytest.raises(UserError, match="cover is not open"):
        _make_policy(contract, gl, cover)


def test_cancel_cover_not_found(contract, gl):
    with pytest.raises(UserError, match="cover not found"):
        contract.cancel_cover(99)


# ---------------------------------------------------------------------------
# Views and stats
# ---------------------------------------------------------------------------

def test_get_policy_not_found(contract, gl):
    with pytest.raises(UserError, match="policy not found"):
        contract.get_policy(99)


def test_list_covers_pagination(contract, gl):
    ids = [_make_cover(contract, gl) for _ in range(3)]
    page = contract.list_covers(0, 2)
    assert page["total"] == 3
    assert [c["id"] for c in page["covers"]] == ids[:2]
    page = contract.list_covers(2, 10)
    assert [c["id"] for c in page["covers"]] == ids[2:]
    page = contract.list_covers(5, 10)
    assert page["covers"] == []


def test_list_covers_limit_capped_at_100(contract, gl):
    _make_cover(contract, gl)
    page = contract.list_covers(0, 200)
    assert len(page["covers"]) == 1
    assert page["total"] == 1


def test_list_policies_pagination(contract, gl):
    first = _make_cover(contract, gl)
    second = _make_cover(contract, gl)
    p1 = _make_policy(contract, gl, first)
    p2 = _make_policy(contract, gl, second, sender=FARMER2)
    page = contract.list_policies(0, 1)
    assert page["total"] == 2
    assert [p["id"] for p in page["policies"]] == [p1]
    page = contract.list_policies(1, 1)
    assert [p["id"] for p in page["policies"]] == [p2]


def test_get_stats_counts(contract, gl, cover, policy):
    stats = contract.get_stats()
    assert stats["total_covers"] == 1
    assert stats["total_policies"] == 1
    assert stats["active_policies"] == 1
    assert stats["total_paid_out_atto"] == 0
    assert stats["total_premiums_held_atto"] == ONE_GEN


def test_get_stats_judged_counts_as_active(contract, gl, policy):
    stub_settlement(gl, weather_json(precip=[0.0, 0.0, 0.0]))
    contract.request_settlement(policy)
    assert contract.get_stats()["active_policies"] == 1


def test_get_stats_empty(contract, gl):
    stats = contract.get_stats()
    assert stats == {
        "total_covers": 0,
        "total_policies": 0,
        "active_policies": 0,
        "total_paid_out_atto": 0,
        "total_premiums_held_atto": 0,
    }


# ---------------------------------------------------------------------------
# Full lifecycles
# ---------------------------------------------------------------------------

def test_lifecycle_kano_january_2025_drought(contract, gl, monkeypatch):
    """The demo policy: Kano, January 2025, 0.00 mm of rain all month.
    Threshold 10.00 mm -> triggered -> farmer is paid."""
    calls = record_transfers(monkeypatch)
    cover_id = _make_cover(
        contract, gl,
        location="Kano, Nigeria", lat="12.0", lon="8.52",
        peril="drought", threshold=1000,
        start=1735689600, end=1738367999,
    )
    cover = contract.get_cover(cover_id)
    assert cover["data_url"] == (
        "https://archive-api.open-meteo.com/v1/archive?latitude=12.000000"
        "&longitude=8.520000&start_date=2025-01-01&end_date=2025-01-31"
        "&daily=precipitation_sum,temperature_2m_max&timezone=UTC"
    )
    policy_id = _make_policy(contract, gl, cover_id)
    stub_settlement(gl, weather_json(precip=[0.0] * 31))
    result = contract.request_settlement(policy_id)
    assert result["triggered"] is True
    assert result["measured_value"] == 0
    settled = contract.settle(policy_id)
    assert settled["paid_farmer_atto"] == 5 * ONE_GEN
    assert settled["paid_underwriter_atto"] == ONE_GEN
    assert calls == [(FARMER, 5 * ONE_GEN), (UNDERWRITER, ONE_GEN)]


def test_lifecycle_kano_april_2025_heat(contract, gl, monkeypatch):
    """Second verified case: Kano, April 2025, hottest day 41.4 C.
    Threshold 40.00 C -> triggered -> farmer is paid."""
    calls = record_transfers(monkeypatch)
    april_temps = [
        37.2, 36.6, 37.8, 38.2, 38.4, 39.2, 40.6, 40.8, 38.8, 39.3,
        38.4, 37.7, 37.2, 37.9, 39.2, 39.7, 39.3, 39.8, 39.7, 39.7,
        39.9, 40.5, 40.3, 41.4, 40.4, 39.1, 40.5, 40.1, 39.7, 41.0,
    ]
    cover_id = _make_cover(
        contract, gl,
        location="Kano, Nigeria", lat="12.0", lon="8.52",
        peril="heat", threshold=4000,
        start=1743465600, end=1746057599,
    )
    policy_id = _make_policy(contract, gl, cover_id)
    stub_settlement(gl, weather_json(precip=[0.0] * 30, temps=april_temps))
    result = contract.request_settlement(policy_id)
    assert result["triggered"] is True
    assert result["measured_value"] == 4140
    settled = contract.settle(policy_id)
    assert settled["paid_farmer_atto"] == 5 * ONE_GEN
    assert calls == [(FARMER, 5 * ONE_GEN), (UNDERWRITER, ONE_GEN)]


def test_lifecycle_excess_rain_not_triggered(contract, gl, monkeypatch):
    """Wet-season rain below the excess threshold: no payout to the
    farmer; the underwriter keeps payout plus premium."""
    calls = record_transfers(monkeypatch)
    cover_id = _make_cover(
        contract, gl, peril="excess_rain", threshold=1000,
    )
    policy_id = _make_policy(contract, gl, cover_id)
    stub_settlement(gl, weather_json(precip=[2.0, 3.0]))
    result = contract.request_settlement(policy_id)
    assert result["triggered"] is False
    assert result["measured_value"] == 500
    settled = contract.settle(policy_id)
    assert settled["paid_farmer_atto"] == 0
    assert settled["paid_underwriter_atto"] == 6 * ONE_GEN
    assert calls == [(UNDERWRITER, 6 * ONE_GEN)]


def test_lifecycle_expire_after_no_consensus(contract, gl, policy, monkeypatch):
    """No consensus ever reached (fetch keeps failing): after 3
    attempts the policy is still active, then the 3-day window passes
    and expire_policy releases the funds deterministically."""
    calls = record_transfers(monkeypatch)
    gl.nondet.web.render = staticmethod(
        lambda url, mode="text": (_ for _ in ()).throw(
            RuntimeError("connection timed out")
        )
    )
    for _ in range(3):
        contract.request_settlement(policy)
    assert contract.get_policy(policy)["status"] == "active"
    set_now(NOW + 172801)
    result = contract.expire_policy(policy)
    assert result["status"] == "expired"
    assert calls == [(FARMER, ONE_GEN), (UNDERWRITER, 5 * ONE_GEN)]
