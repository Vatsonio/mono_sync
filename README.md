# mono_sync — Monobank → Firefly III

Keeps a Firefly III asset account fully in sync with a Monobank **black** card: one-time import of the available history, then automatic polling for new transactions. Runs as a small container (designed for a Raspberry Pi 4 + Portainer + Firefly III). No inbound ports, no webhooks.

## How it works

1. On start it reads `client-info` from the Monobank personal API, finds the `black` account(s), and creates a matching asset account in Firefly III (`Monobank Black`, plus `Monobank Black USD`/`EUR`/… if the card has extra currency accounts).
2. **Backfill** (once): walks 30-day windows backwards to `BACKFILL_FLOOR_DATE`, ~1 Monobank request per minute (Monobank caps statement calls at 1/60 s). On a few years of history this takes roughly half an hour; it is resumable.
3. **Incremental** (every `POLL_INTERVAL_MINUTES`): fetches new statement items (with a 24 h overlap to catch late items and `hold` → settled changes) and upserts them into Firefly. Hourly, it compares the Monobank balance with Firefly's computed balance and logs a warning on mismatch.
4. Idempotency: every Firefly transaction carries `external_id = Monobank transaction id`; a SQLite file on the `/data` volume tracks what has been synced.

All payments map to a single shared Firefly expense/revenue account named **Monobank** (the merchant/counterparty name goes in the transaction description). MCC codes go into the transaction notes plus a `monobank` tag — use Firefly's own rules to categorise. Set `MCC_CATEGORIES=true` to also apply a small built-in MCC→category map.

Firefly III calls that fail (timeouts, 5xx) are retried with backoff; a transaction that still can't be synced is recorded and retried on every later cycle, so transient Firefly downtime never loses data or crash-loops the container. If Firefly is slow on your hardware (a Raspberry Pi with the database on an SD card is the classic case), raise `FIREFLY_TIMEOUT_SECONDS` (default 60) and/or set `FIREFLY_APPLY_RULES=false` for the heavy backfill — that skips Firefly's rule engine on every imported transaction; afterwards you can "Apply rules" to all transactions in the Firefly UI and turn it back on.

## Prerequisites

- **Monobank token:** open <https://api.monobank.ua/>, scan the QR with the Monobank app, copy the token. (Personal tokens do not expire unless you revoke them in the app.)
- **Firefly III Personal Access Token:** Firefly III → Options → Profile → OAuth → *Create new token*.
- **`FIREFLY_URL`:** the internal address of the Firefly III container on its Docker network, e.g. `http://app:8080`. Find the container name with `docker ps`.
- **`FIREFLY_NETWORK`:** the Docker network the Firefly III stack is attached to. Find it with `docker network ls` (often `<stackname>_default`).

## Deploy with Portainer

1. In Portainer: **Stacks → Add stack**, paste the contents of [`docker-compose.yml`](docker-compose.yml).
2. In the **Environment variables** section add: `MONOBANK_TOKEN`, `FIREFLY_TOKEN`, `FIREFLY_URL`, `FIREFLY_NETWORK`, and optionally `FIREFLY_TIMEOUT_SECONDS`, `FIREFLY_APPLY_RULES`, `POLL_INTERVAL_MINUTES`, `BACKFILL`, `BACKFILL_FLOOR_DATE`, `MCC_CATEGORIES`, `TZ`, `LOG_LEVEL` (see [`.env.example`](.env.example)).
3. **Deploy**. Watch the container logs in Portainer — you should see account setup, then `backfill window …` lines, then `entering incremental loop`.

During backfill the asset account's balance in Firefly will look wrong (it climbs from 0 as old transactions are added); it self-corrects when backfill finishes and the opening balance is set.

## Updating

Push to `main` → GitHub Actions rebuilds and publishes `ghcr.io/vatsonio/mono_sync:latest` → in Portainer open the stack and **Pull and redeploy**. State on the `/data` volume is preserved, so backfill is not repeated.

## Development

```bash
pip install -r requirements.txt pytest
pytest -q
```

`tzdata` (in `requirements.txt`) is required for `zoneinfo` to work on machines without an IANA tz database (Windows, slim containers).
