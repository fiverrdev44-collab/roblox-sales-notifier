"""
Roblox sales notifier that posts sales from configured groups to Discord webhooks.
Uses a durable transaction ledger and at-most-once delivery attempts.
Ambiguous Discord outcomes are retained for review, never automatically resent.
"""

import json
import os
import subprocess
import sys
import time
import uuid
import requests

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
GROUPS = {
    "Motion Works Emote Group": {
        "id": 181575178,
        "webhook": os.environ.get("WEBHOOK_MOTION_WORKS", "").strip(),
        "gif": "https://i.imgur.com/7HcNbSN.png",
    },
    "Eroseris UGC Group": {
        "id": 376787388,
        "webhook": os.environ.get("WEBHOOK_EROSERIS", "").strip(),
        "gif": "https://i.imgur.com/2hPH2Hl.png",
    },
    "Ami Berloga Clothing Group": {
        "id": 470988244,
        "webhook": os.environ.get("WEBHOOK_AMI_BERLOGA", "").strip(),
        "gif": "https://i.imgur.com/D62FF8H.jpeg",
    },
    "/Background Check": {
        "id": 852118640,
        "webhook": os.environ.get("WEBHOOK_BACKGROUND_CHECK", "").strip(),
        "gif": "https://i.imgur.com/0XCzCSl.png",
    },
}

STATE_FILE = os.path.join(os.path.dirname(__file__), "last_seen.json")
DEBUG = False
ALERT_COOLDOWN_SECONDS = 6 * 3600  # Alert Discord at most once every 6 hours if cookie expired

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
    # Missing/corrupt history must stop delivery, not silently reset deduplication.
    with open(STATE_FILE) as f:
        raw = json.load(f)
    if not isinstance(raw, dict):
        raise ValueError("Sale ledger must be a JSON object")
    for group_id, value in raw.items():
        if group_id == "_meta":
            continue
        if (not isinstance(value, dict)
                or not isinstance(value.get("tokens"), list)
                or not all(isinstance(token, str) and token for token in value["tokens"])
                or not isinstance(value.get("seeded"), bool)
                or not isinstance(value.get("delivery_issues", {}), dict)):
            raise ValueError(f"Invalid sale ledger for group {group_id}")
    return raw

def save_state(state: dict) -> None:
    temp_file = STATE_FILE + ".tmp"
    with open(temp_file, "w") as f:
        json.dump(state, f, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp_file, STATE_FILE)


def persist_state(state: dict) -> None:
    """Checkpoint before sending; a rejected push must abort without rebasing."""
    save_state(state)
    if (os.environ.get("PERSIST_STATE_TO_GIT") != "1"
            and os.environ.get("GITHUB_ACTIONS") != "true"):
        return
    repo = os.path.dirname(os.path.dirname(os.path.abspath(STATE_FILE)))
    path = os.path.relpath(STATE_FILE, repo)

    def git(*args, check=True):
        return subprocess.run(["git", *args], cwd=repo, check=check, timeout=45)

    git("add", "--", path)
    diff = git("diff", "--cached", "--quiet", "--", path, check=False)
    if diff.returncode not in (0, 1):
        raise RuntimeError("Could not inspect staged sale ledger")
    if diff.returncode == 1:
        git("commit", "-m", "Checkpoint sale delivery ledger", "--", path)
    # Always confirm the remote checkpoint, including after a prior failed push.
    # Never rebase a stale claim and then send: another run may own that sale.
    git("push", "origin", "HEAD:refs/heads/main")


class DiscordRejected(RuntimeError):
    """Discord definitively rejected the request; a later retry is safe."""


class DiscordUncertain(RuntimeError):
    """Discord may have accepted the request; automatic retry risks a duplicate."""

def get_unique_transaction_token(txn: dict) -> str:
    """
    Prefer Roblox's purchase token, then transaction ID, then legacy fingerprint.
    1. Primary: purchaseToken (Roblox UUID for marketplace purchases)
    2. Fallback: unique transaction ID
    3. Final fallback: composite fingerprint
    """
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
        "color": 0xED4245,  # Red
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
        persist_state(state)

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

def post_to_discord(webhook_url: str, group_name: str, sale: dict, image_url: str = None, gif_url: str = None) -> None:
    if sale.get("buyer_id"):
        buyer_value = f"[{sale['buyer']}](https://www.roblox.com/users/{sale['buyer_id']}/profile)"
    else:
        buyer_value = sale["buyer"]

    fields = [
        {"name": "Item", "value": sale["item_name"], "inline": True},
        {"name": "Type", "value": sale["item_type"], "inline": True},
        {"name": "Revenue", "value": f"{sale['revenue']} R$", "inline": True},
        {"name": "Buyer", "value": buyer_value, "inline": True},
    ]

    embed = {
        "title": f"💰 New Sale — {group_name}",
        "color": 0x57F287,
        "fields": fields,
        "footer": {"text": "Roblox Marketplace Sale"},
        "timestamp": sale.get("created") or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if image_url:
        embed["thumbnail"] = {"url": image_url}
    if gif_url:
        embed["image"] = {"url": gif_url}

    payload = {"embeds": [embed]}

    max_retries = 3
    for attempt in range(max_retries):
        try:
            resp = requests.post(webhook_url, params={"wait": "true"}, json=payload,
                                 timeout=15, allow_redirects=False)
        except requests.RequestException as exc:
            # Do not log exceptions containing a webhook URL/token.
            raise DiscordUncertain(f"Discord transport error: {type(exc).__name__}") from None
        if resp.status_code == 429:
            if attempt == max_retries - 1:
                raise DiscordRejected("Discord rate limit exhausted; retry next run")
            try:
                retry_after = max(0.0, float(resp.json().get("retry_after", 1.5)))
            except (ValueError, TypeError, AttributeError):
                raise DiscordRejected("Invalid Discord rate-limit response") from None
            if not 0 <= retry_after <= 30:
                raise DiscordRejected("Discord requested a long rate-limit delay")
            print(f"[discord] rate limited, waiting {retry_after}s (attempt {attempt + 1}/{max_retries})")
            time.sleep(retry_after)
            continue
        if 200 <= resp.status_code < 300:
            return
        if 400 <= resp.status_code < 500 and resp.status_code != 408:
            raise DiscordRejected(f"Discord rejected webhook: HTTP {resp.status_code}")
        raise DiscordUncertain(f"Unconfirmed Discord response: HTTP {resp.status_code}")


def process_transactions(state: dict, group_name: str, cfg: dict, transactions: list) -> int:
    group_id = str(cfg["id"])
    group = state.setdefault(group_id, {"tokens": [], "seeded": False})
    seen = set(group["tokens"])
    issues = group.setdefault("delivery_issues", {})
    if not group["seeded"]:
        group["tokens"] = list(dict.fromkeys(get_unique_transaction_token(t) for t in transactions))
        group["seeded"] = True
        persist_state(state)
        print(f"[{group_name}] initial seed — recorded existing transactions without posting")
        return 0

    if issues:
        print(f"::warning::Group {group_id} has {len(issues)} unconfirmed delivery attempt(s); not retrying")
    posted = 0
    for txn in reversed(transactions):
        sale = parse_transaction(txn)
        token = sale["token"]
        if token in seen:
            continue
        # Reserve durably BEFORE the external side effect. A killed runner or
        # an ambiguous HTTP outcome leaves this token blocked on the next run.
        group["tokens"].insert(0, token)
        seen.add(token)
        issues[token] = {
            "status": "unconfirmed",
            "created": sale.get("created"),
            # Distinct claims must produce distinct commits even in the same
            # second, so two stale runners cannot both push an identical claim.
            "attempt_id": uuid.uuid4().hex,
        }
        persist_state(state)
        try:
            post_to_discord(cfg["webhook"], group_name, sale, cfg.get("image"), cfg.get("gif"))
        except DiscordRejected:
            # A definitive rejection did not create a message. Release for retry.
            group["tokens"].remove(token)
            issues.pop(token)
            persist_state(state)
            raise
        except DiscordUncertain:
            print(f"::error::Group {group_id}: delivery unconfirmed; reserved sale will not be resent")
            raise
        issues.pop(token)
        persist_state(state)
        posted += 1
        print(f"[{group_name}] posted sale: {sale['item_name']} ({sale['revenue']} R$)")
        time.sleep(0.5)
    return posted

def main() -> None:
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
            # Fail the step so GitHub Actions reports the real error
            sys.exit(1)
    except Exception as e:
        print(f"[auth] Warning during auth precheck: {e}")

    total_posted = 0
    had_auth_error = False

    for group_name, cfg in GROUPS.items():
        group_id = str(cfg["id"])
        webhook = cfg["webhook"]
        if not webhook:
            print(f"[{group_name}] webhook secret is not set, skipping Discord alert.")
            continue
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

        total_posted += process_transactions(state, group_name, cfg, raw_transactions)
        time.sleep(3)

    if had_auth_error:
        check_and_alert_auth_failure(state, "One or more group transaction requests returned 401 Unauthorized.")
        sys.exit(1)

    print(f"Check complete. Total new sales posted this run: {total_posted}")

if __name__ == "__main__":
    main()

