"""
Roblox Group Marketplace Sale -> Discord Notifier (single-run version)
------------------------------------------------------------------------
Same idea as the standalone version, but designed to run ONCE per
invocation (checks all groups, posts any new sales, then exits) instead
of looping forever. This is meant to be triggered on a schedule by
GitHub Actions (see .github/workflows/check-sales.yml), which is free
to run indefinitely.

State (the last transaction ID seen per group) is stored in
scripts/last_seen.json, which the GitHub Actions workflow commits back
to the repo after each run so it persists between runs.
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
    "Motion Works Inc": {
        "id": 181575178,
        "webhook": "https://discord.com/api/webhooks/1524376540610297926/oNWFWnYYeSA0fY5C8O_QvAKQPbJcfgRk5H4TcQSVVj9fbb0a3ng2aR5TPg7oCrbbQwwL",
        "image": "https://i.imgur.com/U5AgpzD.png",  # put your imgur (or any) image URL here
    },
    "EroserisUGC": {
        "id": 376787388,
        "webhook": "https://discord.com/api/webhooks/1524376550055612588/xOUUzDz-1WdeyHJs-l1P4KVL8G-1-tbrxExz_zhtbVBXOcLWakbJU9vaDflgOLOOEVfK",
        "image": "https://i.imgur.com/m8dVGql.png",
    },
    "Ami Berloga": {
        "id": 470988244,
        "webhook": "https://discord.com/api/webhooks/1524376555076190219/iTrQ_5pUSuBlvPP4m-ZYNzMopoDDI6AaJG4lJpEI-3p7kLaixwALBp5f2rt6Pnx0N9AN",
        "image": "https://i.imgur.com/XG0Ikat.jpeg",
    },
}

STATE_FILE = os.path.join(os.path.dirname(__file__), "last_seen.json")
DEBUG = False

# ---------------------------------------------------------------------------

ROBLOSECURITY = os.environ.get("ROBLOX_COOKIE")
if not ROBLOSECURITY:
    print("ERROR: ROBLOX_COOKIE environment variable is not set.")
    sys.exit(1)


def get_session() -> requests.Session:
    session = requests.Session()
    session.cookies[".ROBLOSECURITY"] = ROBLOSECURITY
    r = session.post("https://auth.roblox.com/v2/logout")
    token = r.headers.get("x-csrf-token")
    if token:
        session.headers.update({"x-csrf-token": token})
    return session


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


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
        "id": txn.get("id"),
        "item_name": details.get("name", "Unknown Item"),
        "item_type": details.get("type", "Item"),
        "revenue": (txn.get("currency", {}) or {}).get("amount", 0),
        "created": txn.get("created"),
    }


def post_to_discord(webhook_url: str, group_name: str, sale: dict, image_url: str = None) -> None:
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
        last_seen_id = state.get(group_id)

       # Remove the extra 'try:' line here
        try:
            raw_transactions = fetch_transactions(session, int(group_id))
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 429:
                print(f"[{group_name}] rate limited (429), skipping this run — will retry next cycle")
            else:
                print(f"[{group_name}] fetch failed: {e}")
            continue
        except Exception as e:
            print(f"[{group_name}] unexpected error: {e}")
            continue

        if DEBUG and raw_transactions:
            print(json.dumps(raw_transactions[0], indent=2))

        new_raw = []
        for txn in raw_transactions:
            if txn.get("id") == last_seen_id:
                break
            new_raw.append(txn)

        for txn in reversed(new_raw):
            sale = parse_transaction(txn)
            post_to_discord(webhook, group_name, sale, image_url)
            print(f"[{group_name}] posted sale: {sale['item_name']} ({sale['revenue']} R$)")

if raw_transactions:
            state[group_id] = raw_transactions[0].get("id")
        
        time.sleep(3) # This needs to be indented to align with line 152

    save_state(state)
    print("Check complete.")


if __name__ == "__main__":
    main()
