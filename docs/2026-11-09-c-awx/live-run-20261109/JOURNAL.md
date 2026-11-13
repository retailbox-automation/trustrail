# TrustRail live sandbox run for the video · 09.11.2026

Live sandbox run. Base `https://api-demo.airwallex.com/api/v1`, test money only. One line per Airwallex call: `ts (UTC) · type · id · amount · status`. Source: `calls.jsonl`, written by the adapter itself (sanitized); amounts of card limits and the step headings come from the run ledger.

- 2026-11-09T20:29:23Z · PREFLIGHT · synthetic, no network · - · PASS (base=api-demo.airwallex.com; other base refused=True; AIRWALLEX_* env: none; keys: sandbox keys from the operator's private secret store (that loader is not in the public repository); ledger docs/2026-11-09-c-awx/runtime/live-video-20261109T202922Z.sqlite3 new=True 0600=True git-ignored=True dataset=sandbox-live; inbox extractor deterministic=True)

## 01 setup and invoice intake (2026-11-09T20:29:23Z)

- 2026-11-09T20:29:24Z · POST authentication/login · - · - · HTTP 201
- 2026-11-09T20:29:26Z · GET account · acct_MASKED · - · HTTP 200 ACTIVE
- 2026-11-09T20:29:27Z · GET balances/current · - · - · HTTP 200
- 2026-11-09T20:29:28Z · POST beneficiaries/validate · - · - · HTTP 200 OK
- 2026-11-09T20:29:29Z · POST beneficiaries/validate · - · - · HTTP 200 OK
- 2026-11-09T20:29:30Z · POST beneficiaries/validate · - · - · HTTP 200 OK

## 02 plans (2026-11-09T20:29:32Z)

- 2026-11-09T20:29:34Z · POST authentication/login · - · - · HTTP 201
- 2026-11-09T20:29:34Z · GET account · acct_MASKED · - · HTTP 200 ACTIVE
- 2026-11-09T20:29:36Z · GET balances/current · - · - · HTTP 200
- 2026-11-09T20:29:37Z · POST fx/quotes/create · 87fea024-8dfc-3c64-848d-7afc66c3fd4b · buy GBP 1,800.00 · sell USD 2,429.13 · HTTP 200
- 2026-11-09T20:29:38Z · GET balances/current · - · - · HTTP 200
- 2026-11-09T20:29:40Z · POST authentication/login · - · - · HTTP 201
- 2026-11-09T20:29:41Z · GET account · acct_MASKED · - · HTTP 200 ACTIVE
- 2026-11-09T20:29:42Z · GET balances/current · - · - · HTTP 200
- 2026-11-09T20:29:44Z · POST authentication/login · - · - · HTTP 201
- 2026-11-09T20:29:45Z · GET account · acct_MASKED · - · HTTP 200 ACTIVE
- 2026-11-09T20:29:46Z · GET balances/current · - · - · HTTP 200
- 2026-11-09T20:29:48Z · POST authentication/login · - · - · HTTP 201
- 2026-11-09T20:29:49Z · GET account · acct_MASKED · - · HTTP 200 ACTIVE
- 2026-11-09T20:29:50Z · GET balances/current · - · - · HTTP 200
- 2026-11-09T20:29:51Z · POST fx/quotes/create · 20de5231-c253-363d-955e-4d27f8ff074b · buy CNY 50,058.00 · sell USD 7,581.76 · HTTP 200
- 2026-11-09T20:29:52Z · GET balances/current · - · - · HTTP 200

## 03 operator approvals (2026-11-09T20:29:55Z)

- (no Airwallex call)

## 04 pay with card: Foshan single-use card (2026-11-09T20:29:57Z)

- 2026-11-09T20:29:58Z · POST authentication/login · - · - · HTTP 201
- 2026-11-09T20:30:00Z · GET account · acct_MASKED · - · HTTP 200 ACTIVE
- 2026-11-09T20:30:01Z · GET issuing/cardholders · - · - · HTTP 200 1 items, 1 READY
- 2026-11-09T20:30:02Z · POST authentication/login · - · - · HTTP 201
- 2026-11-09T20:30:03Z · GET account · acct_MASKED · - · HTTP 200 ACTIVE
- 2026-11-09T20:30:04Z · GET balances/current · - · - · HTTP 200
- 2026-11-09T20:30:06Z · GET balances/current · - · - · HTTP 200
- 2026-11-09T20:30:07Z · POST issuing/cards/create · 703ffce7-a588-4be1-930a-ce25ba5f7b1e · limit CNY 50,058.00 · HTTP 202 ACTIVE
- 2026-11-09T20:30:08Z · GET issuing/cards/{id} · 703ffce7-a588-4be1-930a-ce25ba5f7b1e · limit CNY 50,058.00 · HTTP 200 ACTIVE

## 05 card checks: three sandbox authorizations (2026-11-09T20:30:10Z)

- 2026-11-09T20:30:12Z · POST authentication/login · - · - · HTTP 201
- 2026-11-09T20:30:13Z · GET account · acct_MASKED · - · HTTP 200 ACTIVE
- 2026-11-09T20:30:19Z · POST simulation/issuing/create · 01a1c201-21dc-7000-a1ec-70c873fc5aa5 · CNY 50,058.01 · HTTP 201 DECLINED LIMIT_EXCEEDED
- 2026-11-09T20:30:21Z · POST simulation/issuing/create · 01a1c201-2e05-7000-8d8f-244261fd0199 · USD 10.00 · HTTP 201 DECLINED CURRENCY_NOT_ALLOWED
- 2026-11-09T20:30:25Z · POST simulation/issuing/create · 01a1c201-38a7-7000-9467-e8493d5c0066 · CNY 48,600.00 · HTTP 201 APPROVED
- 2026-11-09T20:30:26Z · GET issuing/cards/{id} · 703ffce7-a588-4be1-930a-ce25ba5f7b1e · limit CNY 50,058.00 · HTTP 200 ACTIVE
- 2026-11-09T20:30:28Z · POST authentication/login · - · - · HTTP 201
- 2026-11-09T20:30:29Z · GET account · acct_MASKED · - · HTTP 200 ACTIVE
- 2026-11-09T20:30:30Z · GET issuing/cards/{id} · 703ffce7-a588-4be1-930a-ce25ba5f7b1e · limit CNY 50,058.00 · HTTP 200 CLOSED
- 2026-11-09T20:30:31Z · GET issuing/cards/{id} · 703ffce7-a588-4be1-930a-ce25ba5f7b1e · limit CNY 50,058.00 · HTTP 200 CLOSED

## 06 trusted supplier: Atelier Brandt transfer, then SENT (2026-11-09T20:30:34Z)

- 2026-11-09T20:30:35Z · POST authentication/login · - · - · HTTP 201
- 2026-11-09T20:30:36Z · GET account · acct_MASKED · - · HTTP 200 ACTIVE
- 2026-11-09T20:30:37Z · GET balances/current · - · - · HTTP 200
- 2026-11-09T20:30:39Z · POST transfers/create · 2b97bd88-181d-4ba0-bb9d-5a5f4d740667 · EUR 12,400.00 · HTTP 201 SCHEDULED
- 2026-11-09T20:30:41Z · POST authentication/login · - · - · HTTP 201
- 2026-11-09T20:30:42Z · GET account · acct_MASKED · - · HTTP 200 ACTIVE
- 2026-11-09T20:30:43Z · GET transfers · 2b97bd88-181d-4ba0-bb9d-5a5f4d740667 · - · HTTP 200 PROCESSING
- 2026-11-09T20:30:45Z · POST simulation/transfers/{id}/transition · 2b97bd88-181d-4ba0-bb9d-5a5f4d740667 · EUR 12,400.00 · HTTP 200 SENT
- 2026-11-09T20:30:45Z · GET transfers · 2b97bd88-181d-4ba0-bb9d-5a5f4d740667 · - · HTTP 200 SENT

## 07 verified supplier: Marlow 30 % deposit funded from USD (2026-11-09T20:30:48Z)

- 2026-11-09T20:30:50Z · POST authentication/login · - · - · HTTP 201
- 2026-11-09T20:30:51Z · GET account · acct_MASKED · - · HTTP 200 ACTIVE
- 2026-11-09T20:30:52Z · GET balances/current · - · - · HTTP 200
- 2026-11-09T20:30:53Z · GET balances/current · - · - · HTTP 200
- 2026-11-09T20:30:55Z · POST fx/quotes/create · 9a6412c4-9051-39ef-bdc3-f44646197110 · buy GBP 1,800.00 · sell USD 2,429.13 · HTTP 200
- 2026-11-09T20:30:56Z · POST fx/conversions/create · 09bb087d-3c03-4af2-b136-4c27d1390ac0 · buy GBP 1,800.00 · sell USD · HTTP 201 SETTLED
- 2026-11-09T20:30:57Z · GET balances/current · - · - · HTTP 200
- 2026-11-09T20:30:58Z · POST transfers/create · c020e2d7-83c5-4199-8461-b906f04d8989 · GBP 1,800.00 · HTTP 201 SCHEDULED

## 08 Pennine: bank-details email from a lookalike domain stops the approved transfer (2026-11-09T20:31:01Z)

- (no Airwallex call)

## 09 balances and audit (2026-11-09T20:31:04Z)

- 2026-11-09T20:31:06Z · POST authentication/login · - · - · HTTP 201
- 2026-11-09T20:31:07Z · GET account · acct_MASKED · - · HTTP 200 ACTIVE
- 2026-11-09T20:31:08Z · GET balances/current · - · - · HTTP 200

## Caps

| kind | used (call log) | used (guard) | allowed for this run |
|---|---|---|---|
| card | 1 | 1 | 2 |
| transfer | 2 | 2 | 2 |
| conversion | 1 | 1 | 1 |
| simulation | 4 | 4 | 4 |
| card_update | 0 | 0 | not a money call; only to freeze a card left active |

Total Airwallex calls: 61.

## Outcome

COMPLETED

## post-run re-render (round 5, 2026-11-09T20:52:39Z)

One fresh balances read for the re-rendered frames; nothing moves money. Source: `calls-rerender.jsonl` (written by the adapter). Transport allowed only `GET /balances/current` after login.

- 2026-11-09T20:52:42Z · POST authentication/login · - · - · HTTP 201
- 2026-11-09T20:52:43Z · GET account · acct_MASKED · - · HTTP 200 ACTIVE
- 2026-11-09T20:52:44Z · GET balances/current · - · - · HTTP 200 (recorded in the ledger: USD 9,990,192.10 · EUR 9,987,594.00 · GBP 10,000,000.00 · CNY 10,000,000.00)

Local ledger rows, no Airwallex call: audit #47 `CORRECTION_OF_35:CARD_ALREADY_CLOSED` (20:52:39Z). It corrects #35 `HUMAN_CARD_FROZEN`: the card was already CLOSED before the freeze action and no update call was made (calls at 20:30:30Z and 20:30:31Z above). Row #35 is unchanged. Caps unchanged: no card, transfer, conversion or simulation call.
