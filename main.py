#!/usr/bin/env python3
"""
Roblox host-region monitor (Python)

- Configure WEBHOOK_URL (env or edit the hardcoded fallback below)
- Configure ROBLOX_SOURCE_URL (env or fallback below)
- Runs continuously and polls every POLL_INTERVAL_MS (default 5 minutes)

No external deps (uses urllib from stdlib). Python 3.8+ recommended.
"""

import os
import json
import time
import datetime
import urllib.request
import urllib.error
from typing import List, Set, Any
from pathlib import Path

WEBHOOK_URL = os.getenv("WEBHOOK_URL") or "https://example.com/your_webhook_here"
ROBLOX_SOURCE_URL = os.getenv("ROBLOX_SOURCE_URL") or "https://example.com/roblox-regions.json"
POLL_INTERVAL_MS = int(os.getenv("POLL_INTERVAL_MS") or "300000")  # milliseconds
SCRIPT_DIR = Path(__file__).resolve().parent
STORAGE_FILE = os.getenv("STORAGE_FILE") or str(SCRIPT_DIR / "seen_regions.json")
USER_AGENT = "roblox-region-monitor/1.0"

if not WEBHOOK_URL or "example.com" in WEBHOOK_URL:
    print("Warning: WEBHOOK_URL not configured. Edit file or set env WEBHOOK_URL to your webhook endpoint.")


def iso_now() -> str:
    return datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def is_array_of_strings(v: Any) -> bool:
    return isinstance(v, list) and all(isinstance(x, str) for x in v)


def parse_regions(body: Any) -> List[str]:
    """
    Parse JSON result into a list of region strings.
    Mirrors the behavior in the Node version:
      - If body is an array of strings -> return it
      - If body.regions is an array -> return it
      - If body.data is an array of objects with .name -> return mapped names
      - Fallback: find any top-level array-of-strings value and return it
      - Otherwise raise
    """
    if is_array_of_strings(body):
        return body

    if isinstance(body, dict):
        if is_array_of_strings(body.get("regions")):
            return body["regions"]

        data = body.get("data")
        if isinstance(data, list) and all(isinstance(i, dict) and isinstance(i.get("name"), str) for i in data):
            return [i["name"] for i in data]

        # Fallback: find any top-level array-of-strings value
        for v in body.values():
            if is_array_of_strings(v):
                return v

    raise ValueError("Unable to parse regions from response — update parse_regions() to match your source structure.")


def fetch_regions() -> List[str]:
    """
    Fetch the source and return a list of region names (strings).
    If response is JSON, parse it. Otherwise treat as plain text and split lines.
    """
    req = urllib.request.Request(
        ROBLOX_SOURCE_URL,
        headers={"User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            content_type = (resp.getheader("Content-Type") or "").lower()
            raw = resp.read()
            # Try JSON if content-type indicates JSON
            if "application/json" in content_type or "text/json" in content_type:
                try:
                    body = json.loads(raw.decode("utf-8"))
                except Exception:
                    raise ValueError("Failed to decode JSON from source")
                return parse_regions(body)
            else:
                # Try to decode as text and if it looks like JSON, try JSON parse anyway
                text = raw.decode("utf-8", errors="replace")
                stripped = text.strip()
                if stripped.startswith("{") or stripped.startswith("["):
                    # attempt JSON parse
                    try:
                        body = json.loads(text)
                        return parse_regions(body)
                    except Exception:
                        pass
                # Otherwise treat as plain line-separated list
                lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
                if lines:
                    return lines
                raise ValueError("Source returned no usable content.")
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="ignore")
        except Exception:
            pass
        raise RuntimeError(f"Source fetch failed: {e.code} {e.reason} {body}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"Source fetch failed: {e}")


def load_seen() -> Set[str]:
    try:
        with open(STORAGE_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
            if isinstance(raw, list):
                return set(raw)
    except FileNotFoundError:
        return set()
    except Exception:
        # If parsing fails, return empty set
        return set()
    return set()


def save_seen(seen: Set[str]) -> None:
    tmp = STORAGE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(sorted(list(seen)), f, indent=2)
    os.replace(tmp, STORAGE_FILE)


def post_webhook(new_regions: List[str]) -> None:
    """
    Basic JSON payload — works with generic webhooks and Discord incoming webhooks (simple message)
    """
    body = {"content": f"New Roblox host regions detected: {', '.join(new_regions)}"}
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        WEBHOOK_URL,
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            code = resp.getcode()
            if code < 200 or code >= 300:
                text = resp.read().decode("utf-8", errors="ignore")
                raise RuntimeError(f"Webhook POST failed: {code} {text}")
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="ignore")
        except Exception:
            pass
        raise RuntimeError(f"Webhook POST failed: {e.code} {e.reason} {body}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"Webhook POST failed: {e}")


def check_once(seen: Set[str]) -> None:
    try:
        regions = fetch_regions()
        normalized = [str(r).strip() for r in regions if str(r).strip()]
        new_regions = [r for r in normalized if r not in seen]
        if new_regions:
            print(f"{iso_now()} - Found {len(new_regions)} new region(s): {', '.join(new_regions)}")
            try:
                post_webhook(new_regions)
            except Exception as e:
                print(f"{iso_now()} - Error posting webhook: {e}")
                # Do not return early; still add to seen to avoid repeated alerts if webhook is broken?
                # To match Node behavior, only mark as seen AFTER successful post. We'll follow Node and only add if post succeeds.
                return
            for r in new_regions:
                seen.add(r)
            save_seen(seen)
        else:
            print(f"{iso_now()} - No new regions. Total known: {len(seen)}")
    except Exception as e:
        print(f"{iso_now()} - Error during check: {e}")


def main_loop():
    seen = load_seen()

    # Initial sync: fetch and save so the first run doesn't spam; change if you prefer
    try:
        initial = fetch_regions()
        for r in (str(x).strip() for x in initial if str(x).strip()):
            seen.add(r)
        save_seen(seen)
        print(f"{iso_now()} - Initial sync complete. Known regions: {len(seen)}")
    except Exception as e:
        print(f"Initial fetch failed: {e}")

    # Periodic poll
    poll_seconds = POLL_INTERVAL_MS / 1000.0
    try:
        # Run periodic loop forever
        while True:
            # Sleep until next check (we schedule checks at fixed interval)
            time.sleep(poll_seconds)
            check_once(seen)
    except KeyboardInterrupt:
        print("Shutting down (KeyboardInterrupt).")


if __name__ == "__main__":
    # Also run a delayed immediate check after startup (to mirror setTimeout(..., 5000))
    from threading import Timer

    seen = load_seen()
    # If you want the very first check to run 5s after start, do it here:
    Timer(5.0, lambda: check_once(seen)).start()
    try:
        main_loop()
    except Exception as e:
        print("Fatal error:", e)
        raise