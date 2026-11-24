# Demo policy — Kano, Nigeria, January 2025 (drought)

One real example with a past period, so settlement can run immediately
in Studio. Every number below was fetched from the Open-Meteo archive
API while writing this file — nothing here is invented.

## The policy

| Field | Value |
|---|---|
| Location | Kano, Nigeria |
| Latitude / longitude | 12.0 / 8.52 |
| Peril | `drought` |
| Threshold | `1000` (= 10.00 mm) |
| Period | 1–31 January 2025 |
| period_start | `1735689600` (2025-01-01T00:00:00Z) |
| period_end | `1738367999` (2025-01-31T23:59:59Z) |
| Payout | 5 GEN |
| Premium | 1 GEN |

## The data URL the contract settles from

```
https://archive-api.open-meteo.com/v1/archive?latitude=12.000000&longitude=8.520000&start_date=2025-01-01&end_date=2025-01-31&daily=precipitation_sum,temperature_2m_max&timezone=UTC
```

## The verified measurement

Fetched from that URL (Open-Meteo archive, queried 2026-11-23):

- `precipitation_sum` for all 31 days: **0.00 mm every day**
- Total rainfall for the period: **0.00 mm**
- `measured_value`: **0** (hundredths)
- Highest daily maximum temperature in the same response: 34.7 °C
  (20 January) — not used for a drought cover

## The expected verdict

```json
{"measured_value": 0, "triggered": true, "cannot_verify": false}
```

0.00 mm < 10.00 mm, so the drought trigger **hit**. Settlement pays
the **5 GEN** payout to the farmer and the **1 GEN** premium to the
underwriter.

## Second verified case — Kano, April 2025 (heat)

Same place, a heat cover, for a second run-through:

| Field | Value |
|---|---|
| Peril | `heat` |
| Threshold | `4000` (= 40.00 °C) |
| Period | 1–30 April 2025 |
| period_start | `1743465600` (2025-04-01T00:00:00Z) |
| period_end | `1746057599` (2025-04-30T23:59:59Z) |

Data URL:

```
https://archive-api.open-meteo.com/v1/archive?latitude=12.000000&longitude=8.520000&start_date=2025-04-01&end_date=2025-04-30&daily=precipitation_sum,temperature_2m_max&timezone=UTC
```

Verified measurement: daily maximum temperatures for the 30 days,
highest **41.4 °C on 24 April 2025** (other hot days: 41.0 °C on
30 April, 40.8 °C on 8 April, 40.6 °C on 7 April). `measured_value`
**4140**, `triggered` **true** — 41.40 °C > 40.00 °C. Settlement pays
the farmer the payout and the underwriter the premium, same as above.

## A not-triggered case, from the same data

The January response also settles an `excess_rain` cover with
threshold 10.00 mm: total rain 0.00 mm is not above 10.00 mm, so
`triggered` is **false** and the underwriter receives payout plus
premium (6 GEN on the demo amounts).
