#!/usr/bin/env python3
"""
Roblox host-region monitor (Python) — includes IPs in webhook payload when available.

- Set WEBHOOK_URL, ROBLOX_SOURCE_URL, POLL_INTERVAL_MS, STORAGE_FILE as before.
- Optional: set RESOLVE_HOSTNAMES=true to attempt DNS resolution for names that look like hostnames.
"""

import os
import json
import time
import datetime
import urllib.request
import urllib.error
import socket
from typing import List, Set, Any, Dict, Optional
from pathlib import Path

WEBHOOK_URL = os.getenv("WEBHOOK_URL") or "https://example.com/your_webhook_here"
ROBLOX_SOURCE_URL = os.getenv("ROBLOX_SOURCE_URL") or "https://example.com/roblox-regions.json"
POLL_INTERVAL_MS = int(os.getenv("POLL_INTERVAL_MS") or "300000")  # milliseconds
SCRIPT_DIR = Path(__file__).resolve().parent
STORAGE_FILE = os.getenv("STORAGE_FILE") or str(SCRIPT_DIR / "seen_regions.json")
USER_AGENT = "roblox-region-monitor/1.0"
RESOLVE_HOSTNAMES = os.getenv("RESOLVE_HOSTNAMES", "false").lower() in ("1", "true", "yes")

if not WEBHOOK_URL or "example.com" in WEBHOOK_URL:
    print("Warning: WEBHOOK_URL not configured. Edit file or set env WEBHOOK_URL to your webhook endpoint.")


def iso_now() -> str:
    return datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def is_array_of_strings(v: Any) -> bool:
    return isinstance(v, list) and all(isinstance(x, str) for x in v)


def is_ip_string(s: str) -> bool:
    try:
        socket.inet_aton(s)
        return True
    except Exception:
        return False


def parse_regions(body: Any) -> List[Dict[str, Optional[str]]]:
    """
    Return a list of dicts: { 'name': <str>, 'ip': <str or None> }.
    Accepts multiple input shapes:
      - ["Athens", "Istanbul"]
      - { "regions": ["Athens", ...] }
      - { "data": [ { "name": "Athens", "ip": "1.2.3.4" }, ... ] }
      - [ { "name": "Athens", "ip": "1.2.3.4" }, ... ]
      - Fallback: find any top-level array of strings or array of objects with name
    """
    results: List[Dict[str, Optional[str]]] = []

    # plain array of strings
    if is_array_of_strings(body):
        return [{"name": s.strip(), "ip": None} for s in body if s and s.strip()]

    # array of objects with name/ip
    if isinstance(body, list) and all(isinstance(i, dict) and isinstance(i.get("name"), str) for i in body):
        for i in body:
            name = i.get("name")
            ip = i.get("ip") or i.get("address") or None
            results.append({"name": name.strip(), "ip": str(ip).strip() if ip else None})
        return results

    if isinstance(body, dict):
        # { regions: [...] }
        if is_array_of_strings(body.get("regions")):
            return [{"name": s.strip(), "ip": None} for s in body["regions"] if s and s.strip()]

        # { data: [ { name, ip } ] }
        data = body.get("data")
        if isinstance(data, list) and all(isinstance(i, dict) and isinstance(i.get("name"), str) for i in data):
            for i in data:
                name = i.get("name")
                ip = i.get("ip") or i.get("address") or None
                results.append({"name": name.strip(), "ip": str(ip).strip() if ip else None})
            return results

        # Fallback: find any top-level array-of-strings or array-of-objects-with-name
        for v in body.values():
            if is_array_of_strings(v):
                return [{"name": s.strip(), "ip": None} for s in v if s and s.strip()]
            if isinstance(v, list) and all(isinstance(i, dict) and isinstance(i.get("name"), str) for i in v):
                for i in v:
                    name = i.get("name")
                    ip = i.get("ip") or i.get("address") or None
                    results.append({"name": name.strip(), "ip": str(ip).strip() if ip else None})
                return results

    raise ValueError("Unable to parse regions from response — update parse_regions() to match your source structure.")


def fetch_regions() -> List[Dict[str, Optional[str]]]:
    req = urllib.request.Request(ROBLOX_SOURCE_URL, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            content_type = (resp.getheader("Content-Type") or "").lower()
            raw = resp.read()
            if "application/json" in content_type or "text/json" in content_type:
                try:
                    body = json.loads(raw.decode("utf-8"))
                except Exception:
                    raise ValueError("Failed to decode JSON from source")
                items = parse_regions(body)
            else:
                text = raw.decode("utf-8", errors="replace")
                stripped = text.strip()
                if stripped.startswith("{") or stripped.startswith("["):
                    try:
                        body = json.loads(text)
                        items = parse_regions(body)
                    except Exception:
                        # treat as plain lines
                        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
                        items = [{"name": ln, "ip": None} for ln in lines]
                else:
                    # plain line-separated list
                    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
                    items = [{"name": ln, "ip": None} for ln in lines]
            # Optionally resolve hostnames to IPs if requested and ip missing
            if RESOLVE_HOSTNAMES:
                for it in items:
                    if not it.get("ip") and it.get("name"):
                        name = it["name"]
                        # Only attempt DNS if name looks like a hostname or not an IP
                        if "." in name or not is_ip_string(name):
                            try:
                                resolved = socket.gethostbyname(name)
                                it["ip"] = resolved
                            except Exception:
                                # leave ip as None if resolution fails
                                pass
            return items
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
        return set()
    return set()


def save_seen(seen: Set[str]) -> None:
    tmp = STORAGE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(sorted(list(seen)), f, indent=2)
    os.replace(tmp, STORAGE_FILE)


def make_seen_key(item: Dict[str, Optional[str]]) -> str:
    # Dedupe by "name|ip" — ip may be empty string
    name = (item.get("name") or "").strip()
    ip = (item.get("ip") or "").strip()
    return f"{name}|{ip}"


def post_webhook(new_items: List[Dict[str, Optional[str]]]) -> None:
    """
    Basic JSON payload with name and IP when available.
    Example content: "New Roblox host regions detected: Athens (1.2.3.4), Istanbul"
    """
    parts = []
    for it in new_items:
        name = it.get("name") or ""
        ip = it.get("ip")
        if ip:
            parts.append(f"{name} ({ip})")
        else:
            parts.append(f"{name}")
    body = {"content": f"New Roblox host regions detected: {', '.join(parts)}"}
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(WEBHOOK_URL, data=data,
                                 headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
                                 method="POST")
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
        # normalize: ensure name stripped, ip normalized (or None)
        normalized = []
        for r in regions:
            name = (r.get("name") or "").strip()
            ip = (r.get("ip") or "").strip() if r.get("ip") else None
            if name:
                normalized.append({"name": name, "ip": ip})
        # Find new items by seen key
        new_items = [it for it in normalized if make_seen_key(it) not in seen]
        if new_items:
            print(f"{iso_now()} - Found {len(new_items)} new region(s): {', '.join([it['name'] for it in new_items])}")
            post_webhook(new_items)
            for it in new_items:
                seen.add(make_seen_key(it))
            save_seen(seen)
        else:
            print(f"{iso_now()} - No new regions. Total known: {len(seen)}")
    except Exception as e:
        print(f"{iso_now()} - Error during check: {e}")


def main_loop():
    seen = load_seen()
    # Initial sync
    try:
        initial = fetch_regions()
        for r in initial:
            key = make_seen_key({"name": (r.get("name") or "").strip(), "ip": (r.get("ip") or "").strip() if r.get("ip") else None})
            if key:
                seen.add(key)
        save_seen(seen)
        print(f"{iso_now()} - Initial sync complete. Known regions: {len(seen)}")
    except Exception as e:
        print(f"Initial fetch failed: {e}")

    poll_seconds = POLL_INTERVAL_MS / 1000.0
    try:
        while True:
            time.sleep(poll_seconds)
            check_once(seen)
    except KeyboardInterrupt:
        print("Shutting down (KeyboardInterrupt).")


if __name__ == "__main__":
    # Run a delayed immediate check 5s after start (mirrors previous behavior)
    from threading import Timer
    seen = load_seen()
    Timer(5.0, lambda: check_once(seen)).start()
    try:
        main_loop()
    except Exception as e:
        print("Fatal error:", e)
        raise