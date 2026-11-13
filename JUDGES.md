# Judging TrustRail in about three minutes

TrustRail pays overseas suppliers by trust. A new supplier gets an exact-amount, single-use Airwallex card. A verified supplier gets a local transfer at a quoted FX rate. A bank-details email stops the unsent transfer in the formats we detect (README lists them); anything else is recorded, and payment stays on the account on file. A person then confirms new details after a phone call, or rejects them and keeps the details on file.

Python 3.11 or newer, standard library only. The full suite passes on 3.11.17, 3.12.15 and 3.14.8.

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install .
```

## 1. Offline replay: no keys, no network (about one minute)

```sh
trustrail demo --replay docs
```

This prints every recorded sandbox step and a summary of the real Airwallex sandbox objects from our runs on 5 and 9 November 2026: cards, declines (`LIMIT_EXCEEDED`, `CURRENCY_NOT_ALLOWED`, `CARD_INACTIVE`), the FX quote and conversion, and the EUR and GBP transfers. Every replayed step and object carries `"replayed": true`, and so does every audit row in the web replay. The command reads every `.jsonl` evidence file under the directory you give it. Nothing is generated: every ID comes from the sanitized evidence files under `docs/`.

To see the same replay in the console:

```sh
trustrail web --replay docs      # open http://127.0.0.1:8765/ ; read-only, so no token is needed; every write is refused
```

The same console with an offline demo world (no keys, marked "demo"):

```sh
trustrail demo --seed ./demo.sqlite3 && trustrail --db ./demo.sqlite3 web   # open the printed link (it carries the token)
```

## 2. Red-team: 96 hostile supplier emails (about one minute)

```sh
python3 -m unittest tests.test_redteam -v
```

These cover lookalike domains, Reply-To and display-name spoofing (including a "CFO" asking to release a hold), "same bank, new account, re-send it today", amount tampering, instructions to the AI inside the email, Unicode/IBAN confusables, and 60 emails from three adversarial review panels. Each runs as written and through a model that obeys the email; 46 also run through a model that returns attacker-chosen fields. The test fails if any email causes a money movement, an approval or a "paid" status. Known misses (README): one as written (an amount written in words), two with the obeying model.

## 3. With your own Airwallex sandbox key (optional)

Our keys are never in this repository. Use yours through environment variables:

```sh
export AIRWALLEX_CLIENT_ID=...  AIRWALLEX_API_KEY=...  AIRWALLEX_ACCOUNT_ID=acct_...
trustrail --db ./judge.sqlite3 web
```

The console prints a link `http://127.0.0.1:PORT/#t=<token>`; open it. The token travels only in the URL fragment, the page keeps it in memory, and the page itself never contains it. Every POST and `GET /api/state` need the header `X-TrustRail-Token`, except in replay, which refuses every write and so serves its recorded state without a token. The endpoints are listed in README under "Local API". The code only talks to `https://api-demo.airwallex.com/api/v1`; any other base URL, including production, is rejected before any request is sent. Known limit: the environment-key path is covered by unit tests only. Our live runs used our own sandbox account, so a run with a judge's key has not been observed yet. A card payment needs an existing READY cardholder in your account (`cardholder_id` in the execute request).

Full test suite: `python3 -m unittest discover -v` (149 tests, about 16 seconds).
