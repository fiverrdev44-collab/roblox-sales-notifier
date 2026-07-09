"""
Roblox Group Marketplace Sale -> Discord Notifier (single-run version)
------------------------------------------------------------------------
Same idea as the standalone version, but designed to run ONCE per
invocation (checks all groups, posts any new sales, then exits) instead
of looping forever. This is meant to be triggered on a schedule by
GitHub Actions (see .github/workflows/check-sales.yml), which is free
to run indefinitely.

State is stored in scripts/last_seen.json, which the GitHub Actions
workflow commits back to the repo after each run so it persists between
runs.

DEDUPLICATION NOTE:
Roblox assigns "id": 0 to transactions that are still "isPending": true
(a short holding period before a sale is finalized). Because of that, a
transaction's "id" is NOT a reliable unique identifier by itself -- a
pending sale can sit at id 0 for a while before getting a real id once
it settles. Tracking "id" as a single moving cursor incorrectly treated
every pending sale as "already seen," so this version instead tracks
the set of "purchaseToken" values already posted (a stable unique string
present on every transaction regardless of pending status).
"""

import json
import os
import sys
import time
import requests

# ---------------------------------------------------------------------------
# CONFIG - fill this in
# ---------------------------------------------------------------------------

GROUPS = {
    "Motion Works Emote Group": {
        "id": 181575178,
        "webhook": "https://discord.com/api/webhooks/1524376540610297926/oNWFWnYYeSA0fY5C8O_QvAKQPbJcfgRk5H4TcQSVVj9fbb0a3ng2aR5TPg7oCrbbQwwL",
        "image": "https://i.imgur.com/U5AgpzD.png",
        "gif": "https://i.imgur.com/GT2D0SG.gif",  # big gif shown below the embed fields
    },
    "Eroseris UGC Group": {
        "id": 376787388,
        "webhook": "https://discord.com/api/webhooks/1524376550055612588/xOUUzDz-1WdeyHJs-l1P4KVL8G-1-tbrxExz_zhtbVBXOcLWakbJU9vaDflgOLOOEVfK",
        "image": "https://i.imgur.com/m8dVGql.png",
        "gif": "https://i.imgur.com/GT2D0SG.gif",
    },
    "Ami Berloga Clothing Group": {
        "id": 470988244,
        "webhook": "https://discord.com/api/webhooks/1524376555076190219/iTrQ_5pUSuBlvPP4m-ZYNzMopoDDI6AaJG4lJpEI-3p7kLaixwALBp5f2rt6Pnx0N9AN",
        "image": "https://i.imgur.com/XG0Ikat.jpeg",
        "gif": "https://i.imgur.com/GT2D0SG.gif",
    },
}

STATE_FILE = os.path.join(os.path.dirname(__file__), "last_seen.json")
DEBUG = False
MAX_TOKENS_STORED_PER_GROUP = 500  # cap so the state file doesn't grow forever

# ---------------------------------------------------------------------------

ROBLOSECURITY = os.environ.get("ROBLOX_COOKIE")
if not ROBLOSECURITY:
    print("ERROR: ROBLOX_COOKIE environment variable is not set.")
    sys.exit(1)


def get_session() -> requests.Session:
    session = requests.Session()
    session.cookies[".ROBLOSECURITY"] = ROBLOSECURITY
    r = session.post("https://auth.roblox.com/v2/logout", timeout=15)
    token = r.headers.get("x-csrf-token")
    if token:
        session.headers.update({"x-csrf-token": token})
    return session


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            raw = json.load(f)
    else:
        raw = {}

    # Migrate old format (group_id -> single last-seen id number) into the
    # new format (group_id -> {"tokens": [...], "seeded": bool}) so old
    # state files don't crash this version.
    migrated = {}
    for group_id, value in raw.items():
        if isinstance(value, dict) and "tokens" in value:
            migrated[group_id] = value
        else:
            migrated[group_id] = {"tokens": [], "seeded": False}
    return migrated


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def fetch_transactions(session: requests.Session, group_id: int) -> list:
    url = f"https://economy.roblox.com/v2/groups/{group_id}/transactions"
    params = {"transactionType": "Sale", "limit": 100}
    r = session.get(url, params=params, timeout=15)
    r.raise_for_status()
    return r.json().get("data", [])


def parse_transaction(txn: dict) -> dict:
    details = txn.get("details", {}) or {}
    return {
        "token": txn.get("purchaseToken"),
        "item_name": details.get("name", "Unknown Item"),
        "item_type": details.get("type", "Item"),
        "revenue": (txn.get("currency", {}) or {}).get("amount", 0),
        "created": txn.get("created"),
        "is_pending": txn.get("isPending", False),
    }


def post_to_discord(webhook_url: str, group_name: str, sale: dict, image_url: str = None, gif_url: str = None) -> None:
    embed = {
        "title": f"💰 New Sale — {group_name}",
        "color": 0x57F287,
        "fields": [
            {"name": "Item", "value": sale["item_name"], "inline": True},
            {"name": "Type", "value": sale["item_type"], "inline": True},
            {"name": "Revenue", "value": f"{sale['revenue']} R$", "inline": True},
        ],
        "footer": {"text": "Roblox Marketplace Sale"},
        "timestamp": sale.get("created") or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if image_url:
        embed["thumbnail"] = {"url": image_url}
    if gif_url:
        embed["image"] = {"url": gif_url}  # renders large, below the fields
    resp = requests.post(webhook_url, json={"embeds": [embed]}, timeout=15)
    if resp.status_code >= 300:
        print(f"[discord] webhook error {resp.status_code}: {resp.text}")


def main() -> None:
    session = get_session()
    state = load_state()

    for group_name, cfg in GROUPS.items():
        group_id = str(cfg["id"])
        webhook = cfg["webhook"]
        image_url = cfg.get("image")
        gif_url = cfg.get("gif")
        group_state = state.setdefault(group_id, {"tokens": [], "seeded": False})
        seen_tokens = set(group_state["tokens"])

        try:
            raw_transactions = fetch_transactions(session, int(group_id))
        except requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else None
            if status == 429:
                print(f"[{group_name}] rate limited (429), skipping this run — will retry next cycle")
            else:
                print(f"[{group_name}] fetch failed: {e}")
            time.sleep(3)
            continue
        except Exception as e:
            print(f"[{group_name}] unexpected error: {e}")
            time.sleep(3)
            continue

        print(f"[{group_name}] fetched {len(raw_transactions)} transaction(s)")
        if DEBUG and raw_transactions:
            print(json.dumps(raw_transactions[0], indent=2))

        current_tokens = [t.get("purchaseToken") for t in raw_transactions if t.get("purchaseToken")]

        if not group_state["seeded"]:
            # First run ever for this group: don't spam Discord with the
            # entire existing sales history, just record what's currently
            # there as "already seen" and start alerting from the next run.
            print(f"[{group_name}] first run for this group — seeding {len(current_tokens)} existing transaction(s), no alerts sent")
            group_state["tokens"] = current_tokens[:MAX_TOKENS_STORED_PER_GROUP]
            group_state["seeded"] = True
            time.sleep(3)
            continue

        new_txns = [t for t in raw_transactions if t.get("purchaseToken") and t.get("purchaseToken") not in seen_tokens]

        # Post oldest-of-the-new first so Discord shows them in order.
        for txn in reversed(new_txns):
            sale = parse_transaction(txn)
            if sale["is_pending"]:
                # Still post it -- it's a real new sale -- but note it may
                # not have a final settled id/status yet.
                print(f"[{group_name}] new sale is pending settlement: {sale['item_name']}")
            post_to_discord(webhook, group_name, sale, image_url, gif_url)
            print(f"[{group_name}] posted sale: {sale['item_name']} ({sale['revenue']} R$)")

        # Update the stored token list: newest tokens from this fetch,
        # capped so the file doesn't grow forever.
        merged = current_tokens + [t for t in group_state["tokens"] if t not in current_tokens]
        group_state["tokens"] = merged[:MAX_TOKENS_STORED_PER_GROUP]

        time.sleep(3)

    save_state(state)
    print("Check complete.")


if __name__ == "__main__":
    main()
