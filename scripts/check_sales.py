"""
Roblox sales notifier that posts sales from 3 groups to Discord webhooks.
Uses authentic digital sales receipt bill format with dual Creator signatures.
Guarantees zero duplicates, encrypted webhook secrets, and automated cookie failure alerts.
"""

import json
import os
import sys
import time
import requests

# ---------------------------------------------------------------------------
# CONFIG - 3 Groups with custom brand headers, types, and small side thumbnails
# ---------------------------------------------------------------------------
GROUPS = {
    "Motion Works Emote Group": {
        "id": 181575178,
        "display_name": "MOTION WORKS INC.",
        "default_type": "EMOTE",
        "webhook": os.environ.get("WEBHOOK_MOTION_WORKS", "").strip(),
        "image": "https://i.imgur.com/7HcNbSN.png",
    },
    "Eroseris UGC Group": {
        "id": 376787388,
        "display_name": "EROSERIS UGC",
        "default_type": "UGC ASSET",
        "webhook": os.environ.get("WEBHOOK_EROSERIS", "").strip(),
        "image": "https://i.imgur.com/2hPH2Hl.png",
    },
    "Ami Berloga Clothing Group": {
        "id": 470988244,
        "display_name": "AMI BERLOGA",
        "default_type": "CLOTHING",
        "webhook": os.environ.get("WEBHOOK_AMI_BERLOGA", "").strip(),
        "image": "https://i.imgur.com/D62FF8H.jpeg",
    },
}

STATE_FILE = os.path.join(os.path.dirname(__file__), "last_seen.json")
DEBUG = False
MAX_TOKENS_STORED_PER_GROUP = 500
ALERT_COOLDOWN_SECONDS = 6 * 3600

# ---------------------------------------------------------------------------
raw_cookie = os.environ.get("ROBLOX_COOKIE", "").strip()
if (raw_cookie.startswith('"') and raw_cookie.endswith('"')) or (raw_cookie.startswith("'") and raw_cookie.endswith("'")):
    raw_cookie = raw_cookie[1:-1].strip()

def get_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": "https://www.roblox.com",
        "Referer": "https://www.roblox.com/",
    })
    session.cookies[".ROBLOSECURITY"] = raw_cookie
    try:
        r = session.post("https://auth.roblox.com/v2/logout", timeout=15)
        token = r.headers.get("x-csrf-token")
        if token:
            session.headers.update({"x-csrf-token": token})
    except Exception as e:
        print(f"[warning] CSRF fetch error: {e}")
    return session

def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                raw = json.load(f)
        except Exception as e:
            print(f"[warning] Failed to load state file: {e}")
            raw = {}
    else:
        raw = {}

    migrated = {}
    for group_id, value in raw.items():
        if group_id == "_meta":
            migrated["_meta"] = value
        elif isinstance(value, dict) and "tokens" in value:
            migrated[group_id] = value
        else:
            migrated[group_id] = {"tokens": [], "seeded": False}
    return migrated

def save_state(state: dict) -> None:
    try:
        temp_file = STATE_FILE + ".tmp"
        with open(temp_file, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(temp_file, STATE_FILE)
    except Exception as e:
        print(f"[error] Failed to atomically save state file: {e}")

def get_unique_transaction_token(txn: dict) -> str:
    p_token = txn.get("purchaseToken")
    if p_token and str(p_token).strip():
        return str(p_token).strip()

    txn_id = txn.get("id")
    if txn_id:
        return f"txn_{txn_id}"

    created = txn.get("created", "")
    buyer_id = (txn.get("agent", {}) or {}).get("id", "")
    revenue = (txn.get("currency", {}) or {}).get("amount", "")
    item_name = (txn.get("details", {}) or {}).get("name", "")
    return f"fp_{buyer_id}_{item_name}_{revenue}_{created}"

def send_discord_system_alert(title: str, description: str, fields: list = None) -> None:
    embed = {
        "title": title,
        "description": description,
        "color": 0xED4245,
        "fields": fields or [],
        "footer": {"text": "Roblox Sales Notifier Monitor"},
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    payload = {"embeds": [embed]}
    for name, cfg in GROUPS.items():
        webhook = cfg.get("webhook")
        if not webhook:
            continue
        try:
            r = requests.post(webhook, json=payload, timeout=10)
            if r.status_code < 300:
                print(f"[alert] Sent system notification to {name}")
        except Exception as e:
            print(f"[alert] Failed sending alert to {name}: {e}")

def check_and_alert_auth_failure(state: dict, error_msg: str) -> None:
    meta = state.setdefault("_meta", {})
    now = time.time()
    last_alert = meta.get("last_auth_alert", 0)

    if now - last_alert > ALERT_COOLDOWN_SECONDS:
        print("[auth] Sending cookie expiration alert to Discord...")
        send_discord_system_alert(
            title="🚨 Roblox Sales Notifier — ROBLOX_COOKIE Expired",
            description=(
                "**The Roblox session cookie stored in GitHub repository secrets is invalid or expired.**\n\n"
                "Sales cannot be checked until a new `.ROBLOSECURITY` cookie is added to GitHub secrets."
            ),
            fields=[
                {
                    "name": "Error Details",
                    "value": f"```{error_msg[:400]}```",
                    "inline": False
                },
                {
                    "name": "How to update",
                    "value": (
                        "1. Open Incognito browser, log in to Roblox.\n"
                        "2. Inspect -> Cookies -> Copy `.ROBLOSECURITY` (do not log out!).\n"
                        "3. Update `ROBLOX_COOKIE` at:\n"
                        "https://github.com/fiverrdev44-collab/roblox-sales-notifier/settings/secrets/actions"
                    ),
                    "inline": False
                }
            ]
        )
        meta["last_auth_alert"] = now
        save_state(state)

def fetch_transactions(session: requests.Session, group_id: int) -> list:
    url = f"https://economy.roblox.com/v2/groups/{group_id}/transactions"
    params = {"transactionType": "Sale", "limit": 100}
    r = session.get(url, params=params, timeout=15)
    r.raise_for_status()
    return r.json().get("data", [])

def parse_transaction(txn: dict) -> dict:
    details = txn.get("details", {}) or {}
    token = get_unique_transaction_token(txn)
    return {
        "token": token,
        "item_name": details.get("name", "Unknown Item"),
        "item_type": details.get("type", "Item"),
        "revenue": (txn.get("currency", {}) or {}).get("amount", 0),
        "buyer": (txn.get("agent", {}) or {}).get("name", "Unknown"),
        "buyer_id": (txn.get("agent", {}) or {}).get("id"),
        "created": txn.get("created"),
        "is_pending": txn.get("isPending", False),
    }

def format_receipt_description(group_name: str, group_cfg: dict, sale: dict) -> str:
    """Formats an authentic monospaced thermal receipt bill."""
    display_title = group_cfg.get("display_name", group_name.upper())
    item_name = sale.get("item_name", "Unknown Item")
    item_type = group_cfg.get("default_type") or str(sale.get("item_type", "ITEM")).upper()
    revenue = sale.get("revenue", 0)
    buyer = sale.get("buyer", "Unknown")
    token = str(sale.get("token", ""))
    short_txn = token[-8:].upper() if len(token) >= 8 else "00000000"
    
    created = sale.get("created", "")
    date_str = created.replace("T", " ")[:19] + " UTC" if created else time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    status_str = "PENDING SETTLEMENT" if sale.get("is_pending") else "COMPLETED"

    w = 46
    header_line = f"║{display_title.center(w)}║"

    receipt = (
        "```text\n"
        "╔══════════════════════════════════════════════╗\n"
        "║               OFFICIAL RECEIPT               ║\n"
        f"{header_line}\n"
        "╠══════════════════════════════════════════════╣\n"
        f"  RECEIPT NO : #TXN-{short_txn}\n"
        f"  DATE/TIME  : {date_str}\n"
        f"  BUYER      : @{buyer}\n"
        "  PAYMENT    : ROBLOX MARKETPLACE SALE\n"
        "------------------------------------------------\n"
        f"  ITEM NAME  : {item_name}\n"
        f"  ITEM TYPE  : {item_type}\n"
        "------------------------------------------------\n"
        f"  TOTAL NET REVENUE CREDITED : {revenue} R$\n"
        f"  SETTLEMENT : {status_str}\n"
        "════════════════════════════════════════════════\n"
        "  OFFICIALLY RECEIVED & VERIFIED:\n"
        "  ✍️ Gashphul (Creator)   ✍️ MasterSai (Creator)\n"
        "╚══════════════════════════════════════════════╝\n"
        "```"
    )
    return receipt

def post_to_discord(webhook_url: str, group_name: str, group_cfg: dict, sale: dict) -> None:
    receipt_text = format_receipt_description(group_name, group_cfg, sale)
    image_url = group_cfg.get("image")
    display_title = group_cfg.get("display_name", group_name)

    if sale.get("buyer_id"):
        buyer_val = f"[{sale['buyer']}](https://www.roblox.com/users/{sale['buyer_id']}/profile)"
    else:
        buyer_val = sale["buyer"]

    fields = [
        {"name": "Customer", "value": buyer_val, "inline": True},
        {"name": "Net Robux", "value": f"**{sale['revenue']} R$**", "inline": True},
        {"name": "Type", "value": group_cfg.get("default_type") or sale.get("item_type", "Item"), "inline": True},
    ]

    embed = {
        "title": f"🧾 Sales Receipt — {display_title}",
        "description": receipt_text,
        "color": 0x57F287,
        "fields": fields,
        "footer": {"text": "Roblox Marketplace Sale • Verified Digital Receipt"},
        "timestamp": sale.get("created") or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    # Small side embed thumbnail (big bottom image omitted)
    if image_url:
        embed["thumbnail"] = {"url": image_url}

    payload = {"embeds": [embed]}

    max_retries = 3
    for attempt in range(max_retries):
        resp = requests.post(webhook_url, json=payload, timeout=15)
        if resp.status_code == 429:
            retry_after = resp.json().get("retry_after", 1.5)
            print(f"[discord] rate limited, waiting {retry_after}s (attempt {attempt + 1}/{max_retries})")
            time.sleep(retry_after)
            continue
        if resp.status_code >= 300:
            print(f"[discord] webhook error {resp.status_code}: {resp.text}")
        break

def send_test_check() -> None:
    print("[test] Sending test receipt to all configured group Discord webhooks...")
    sample_sales = {
        "Motion Works Emote Group": {
            "token": "test-receipt-001",
            "item_name": "⭐Smooth 360 flip Rizz",
            "item_type": "Emote",
            "revenue": 16,
            "buyer": "luigi56",
            "buyer_id": 10762793687,
            "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "is_pending": False,
        },
        "Eroseris UGC Group": {
            "token": "test-receipt-002",
            "item_name": "MJ Float Aura Floating Emote🔥",
            "item_type": "UGC Asset",
            "revenue": 16,
            "buyer": "Astro",
            "buyer_id": 10762793687,
            "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "is_pending": False,
        },
        "Ami Berloga Clothing Group": {
            "token": "test-receipt-003",
            "item_name": "Ami Berloga Autumn Streetwear",
            "item_type": "Clothing",
            "revenue": 5,
            "buyer": "BerlogaFan",
            "buyer_id": 10762793687,
            "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "is_pending": False,
        },
    }

    for name, cfg in GROUPS.items():
        webhook = cfg.get("webhook", "").strip()
        if not webhook:
            print(f"[test] Webhook for {name} not found, skipping.")
            continue
        sale = sample_sales.get(name)
        post_to_discord(webhook, name, cfg, sale)
        print(f"[test] Posted official test receipt to {name}!")
        time.sleep(0.5)

def main() -> None:
    if os.environ.get("TEST_RECEIPT") in ("true", "1", "True") or "--test" in sys.argv:
        send_test_check()
        print("[test] Test receipts completed.")
        return

    state = load_state()

    if not raw_cookie:
        err = "ERROR: ROBLOX_COOKIE environment variable is empty or not set."
        print(err)
        check_and_alert_auth_failure(state, err)
        sys.exit(1)

    session = get_session()

    # Pre-flight check: Test Roblox authentication
    auth_check_url = "https://users.roblox.com/v1/users/authenticated"
    try:
        auth_res = session.get(auth_check_url, timeout=15)
        if auth_res.status_code == 200:
            user_info = auth_res.json()
            print(f"[auth] Successfully authenticated as Roblox user: {user_info.get('name')} (ID: {user_info.get('id')})")
        else:
            err_msg = f"Roblox authentication check failed ({auth_res.status_code}): {auth_res.text}"
            print(f"[auth] {err_msg}")
            check_and_alert_auth_failure(state, err_msg)
            sys.exit(1)
    except Exception as e:
        print(f"[auth] Warning during auth precheck: {e}")

    total_posted = 0
    had_auth_error = False

    for group_name, cfg in GROUPS.items():
        group_id = str(cfg["id"])
        webhook = cfg.get("webhook", "").strip()
        if not webhook:
            print(f"[{group_name}] webhook secret is not set, skipping Discord alert.")
            continue

        group_state = state.setdefault(group_id, {"tokens": [], "seeded": False})
        seen_tokens = set(group_state["tokens"])

        try:
            raw_transactions = fetch_transactions(session, int(group_id))
        except requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else None
            if status == 401:
                print(f"[{group_name}] Unauthorized (401) - Cookie expired or lacking permission for group {group_id}")
                had_auth_error = True
            elif status == 429:
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

        current_tokens = [get_unique_transaction_token(t) for t in raw_transactions]

        if not group_state["seeded"]:
            print(f"[{group_name}] initial seed — recording {len(current_tokens)} existing transactions, zero duplicates sent.")
            group_state["tokens"] = current_tokens[:MAX_TOKENS_STORED_PER_GROUP]
            group_state["seeded"] = True
            save_state(state)
            time.sleep(3)
            continue

        new_txns = [t for t in raw_transactions if get_unique_transaction_token(t) not in seen_tokens]

        for txn in reversed(new_txns):
            sale = parse_transaction(txn)
            token = sale["token"]

            if token in seen_tokens or token in group_state["tokens"]:
                continue

            if sale["is_pending"]:
                print(f"[{group_name}] new sale pending settlement: {sale['item_name']}")

            post_to_discord(webhook, group_name, cfg, sale)
            print(f"[{group_name}] posted receipt: {sale['item_name']} ({sale['revenue']} R$)")
            total_posted += 1

            group_state["tokens"].insert(0, token)
            seen_tokens.add(token)
            group_state["tokens"] = group_state["tokens"][:MAX_TOKENS_STORED_PER_GROUP]
            save_state(state)
            time.sleep(0.5)

        merged = current_tokens + [t for t in group_state["tokens"] if t not in current_tokens]
        group_state["tokens"] = merged[:MAX_TOKENS_STORED_PER_GROUP]
        save_state(state)
        time.sleep(3)

    if had_auth_error:
        check_and_alert_auth_failure(state, "One or more group transaction requests returned 401 Unauthorized.")
        sys.exit(1)

    print(f"Check complete. Total new sales posted this run: {total_posted}")

if __name__ == "__main__":
    main()
