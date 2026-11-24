# Deploying RainCover

No wallet, account, or deployment has been created for you — these are
the steps to run yourself. Nothing here costs real money until mainnet,
and the first two stages are free.

## 0. What you need

- The repo in this folder (`contracts/rain_cover.py` and the docs).
- A browser. No local toolchain is needed for Studio; the testnet steps
  use the GenLayer CLI if you prefer it to Studio's UI.
- A GenLayer-compatible wallet (the Studio / portal will prompt you).
  Never paste a private key or seed phrase anywhere except your own
  wallet.
- The demo uses a **past period** (January 2025), so settlement can
  run immediately — no waiting for a period to end. The weather data
  comes from the Open-Meteo archive API, which validators can fetch
  directly; nothing needs hosting.

## 1. Lint first (before you deploy anywhere)

```sh
genvm-lint check contracts/rain_cover.py
```

> Note: `genvm-lint` could not be installed in the environment this
> project was written in (`pip install genvm-lint` and `pip install
> py-genlayer` find no such packages; the `genlayer` package on PyPI is
> an empty placeholder). Install it from GenLayer's official tooling
> instructions and run the check before deploying. Fix anything it
> flags — in particular confirm the transaction-datetime deadline
> clock (`datetime.now(timezone.utc)`), the
> `datetime.fromtimestamp` date derivation, and the
> `_PayoutTarget.emit_transfer` payout wiring (see step 2).

Also run the unit tests once more, so you know the logic you are
deploying is the logic that passed:

```sh
pip install -r requirements.txt
python3 -m pytest tests/ -q
# 131 passed
```

## 2. GenLayer Studio — deploy and play with 5 simulated validators

1. Open **https://studio.genlayer.com** and connect your wallet.
2. Create a new contract and paste in `contracts/rain_cover.py`
   (one contract per file — this file is the whole contract).
   When Studio asks for the constructor argument, enter your own wallet
   address as `owner_address` (a hex string — the constructor takes it
   as `str` and converts it to `Address` inside, because reading the
   sender inside `__init__` breaks deployment on Studio).
3. Deploy to **studionet**. Studio deploys with 5 simulated validators,
   so `request_settlement` actually runs consensus.
4. **No payout setup needed.** Payouts are direct transfers of the
   GEN locked by `create_cover` / `buy_cover`, through
   `_PayoutTarget.emit_transfer` (the faucet pattern) — there is no
   Arbiter address to confirm and no `set_arbiter` call. `settle`,
   `expire_policy`, and `cancel_cover` pay out as soon as they run.
5. Walk the full flow on studionet (amounts are play money). Use two
   Studio accounts: the underwriter (account A) and the farmer
   (account B).
   - **Create:** from account A, call `create_cover` attaching
     5 GEN (the `value` field):
     - location_name: `Kano, Nigeria`
     - latitude: `"12.000000"`, longitude: `"8.520000"` (decimal
       strings — GenVM rejects float parameters)
     - peril: `drought`
     - threshold: `1000` (10.00 mm, in hundredths)
     - period_start: `1735689600` (2025-01-01T00:00:00Z)
     - period_end: `1738367999` (2025-01-31T23:59:59Z)
     - payout_amount: 5 GEN, premium: 1 GEN (in atto:
       5000000000000000000, 1000000000000000000)
     - Check the returned `data_url` matches
       `demo/demo-policy.md` — the contract builds it from the
       coordinates and period.
   - **Buy:** from account B, call `buy_cover` attaching 1 GEN (the
     `value` field). The policy becomes `active`; the premium is held.
   - **Settle:** call `request_settlement` (any account may call it).
     Expected verdict: `measured_value: 0, triggered: true` — it
     rained 0.00 mm in Kano all month. The policy should become
     `judged`.
   - **Pay:** call `settle` and check account B (the farmer) receives
     the **5 GEN** payout and account A receives the **1 GEN**
     premium.
   - **Try the not-triggered path too:** create a second cover with
     peril `excess_rain`, threshold `1000`, same period, and settle
     it. Expected: measured 0.00 mm, triggered false, and account A
     receives payout **plus** premium (6 GEN).
   - **Try the heat path:** create a cover with peril `heat`,
     threshold `4000` (40.00 °C), period 1–30 April 2025
     (`1743465600` to `1746057599`). Expected: measured 4140
     (41.4 °C on 24 April), triggered true.
   - **Try the failure paths too:** a policy left `active` for 3
     days past the period end followed by `expire_policy` (premium
     refunded to the farmer, payout released to the underwriter), and
     a `cancel_cover` on an unsold cover (payout returned).
6. Note the deployed contract address and the transaction hashes — you
   will need them for the portal submission.

## 3. Bradbury testnet

1. In Studio, switch the network to **Bradbury** (GenLayer testnet).
2. Get testnet GEN from the faucet (linked in Studio / the GenLayer
   docs). Testnet funds have no real value.
3. Redeploy the contract and repeat the full flow from step 2.5 (no
   payout setup call is needed on Bradbury either).
4. Record: contract address, a `create_cover` transaction, a
   `request_settlement` transaction showing a stored verdict, and a
   `settle` transaction. These are your evidence for the portal.

## 4. Submit to the GenLayer portal

1. Open **https://portal.genlayer.foundation** and sign in.
2. Submit under the **Builders / standalone contracts** category with:
   - **Link:** this repository (or a public GitHub copy of it), and
     the deployed contract address(es) from Studio and Bradbury.
   - **Title:** "RainCover — parametric weather micro-insurance".
   - **Description:** the README's first two sections (problem + what
     the contract does), plus the worked example and its expected
     outcome.
   - **Evidence:** the transaction hashes from step 3.4 and a note
     that the 131 unit tests pass (`python3 -m pytest tests/ -q`).
   - **Docs:** point reviewers at `README.md` (purpose, state design,
     worked example) and `CONSENSUS.md` (equivalence design, including
     the tolerance choice, the trigger straddle, the UNDETERMINED hole
     and the `expire_policy` escape).
3. Keep the repository public and do not change the contract after
   submitting — reviewers check the submitted code against the deployed
   address.

## 5. Mainnet (only when you are ready)

- Confirm the payout wiring (`_PayoutTarget.emit_transfer`) against
  the official GenLayer docs for mainnet, and redeploy.
- Re-run `genvm-lint` against the exact file you deploy.
- Decide the buy time gate before mainnet: as written, a cover for a
  past period can still be bought (that is how the demo runs). Either
  add the gate in `buy_cover` or price premiums from the same archive
  data — do not ship the demo behaviour with real money.
- Start with small payouts: settlement is deterministic, but the
  trigger quality depends on the live validators and the archive data
  they can fetch.
