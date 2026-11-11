# Roblox sales notifier

Checks the configured groups and sends new sales to their Discord webhooks.
The scheduled/manual workflow runs only on `main`, shares a single concurrency
group, and explicitly checks out the latest `main` after waiting in the queue.

## Duplicate-sale fix

On November 11, 2026, run [43098351669](https://github.com/fiverrdev44-collab/roblox-sales-notifier/actions/runs/43098351669)
posted a Dustbowl sale and saved its token in commit `10846bc`.
Queued run [43098807095](https://github.com/fiverrdev44-collab/roblox-sales-notifier/actions/runs/43098807095)
then checked out its older event commit `e797dfc` and posted that same sale again.
Concurrency alone did not prevent the stale checkout. The explicit `ref: main`
fixes that cause.

## Delivery policy

- Each new sale is recorded and pushed to GitHub **before** contacting Discord.
  If saving or pushing fails, delivery stops. Checkpoints never force-push or
  rebase a stale claim. Existing tokens are preserved and no longer evicted.
- Discord requests use `wait=true`. Only definitive rejections (such as 429 or
  403) allow a later retry. Exhausted rate limits fail the run without marking
  the sale sent or marking any unattempted sales seen.
- A timeout, server error, or interrupted runner can leave an uncertain outcome.
  Its token stays reserved under `delivery_issues` in `scripts/last_seen.json`.
  Later runs warn and skip that token. This prioritizes avoiding duplicate posts:
  a crash between reservation and sending can mean a notification is missing.
  Discord delivery and Git storage are not one atomic transaction, so this is
  at-most-once attempts, not a guarantee of exactly-once delivery.
- Review an unconfirmed sale against Discord before changing its reservation.
  If the message exists, retain the token and clear only its issue entry. If it
  is confirmed absent, remove that token and issue entry to permit a retry while
  it remains in the fetched transactions. Do not clear or reset the whole ledger.
- Transaction identity uses Roblox's purchase token, transaction ID, or the
  legacy fallback fingerprint. Distinct purchase tokens for the same buyer,
  item, and amount are separate sales. If Roblox changes every identifying
  field, the notifier cannot prove that two records represent one purchase.

All production runners must share this workflow and ledger. Local runs only
write local state unless `PERSIST_STATE_TO_GIT=1` is enabled. The checker still
reads the most recent 100 transactions per group; full historical backfill is
outside this fix. The ledger keeps all tokens it has recorded, but cannot
recover older tokens already evicted by the previous 500-token limit.

## Tests

```sh
python -m pip install requests
python -m unittest discover -s tests -v
```

Tests mock all Discord requests. Git integration tests use temporary local bare
repositories to verify remote reservations, stale runners, and push failures.
