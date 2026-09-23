#!/usr/bin/env python3
"""
Reddit commission-lead monitor.

How it works (no Reddit API key, no approval wait):
  - Reddit's public .rss feeds work without authentication (e.g.
    https://www.reddit.com/r/forhire/new/.rss). We poll a handful of
    subreddits' "new" feeds on a schedule (see the GitHub Actions
    workflow), check each new post's title+body against a keyword list,
    and post a match to a Discord webhook so you see it fast.
  - This never sends anything to Reddit or Discord users on your
    behalf beyond posting into YOUR OWN webhook — it is a monitor, not
    an outreach bot. You still send the actual message yourself.
  - "Seen" post IDs are stored in state.json so the same post never
    alerts twice; the workflow commits that file back to the repo
    after each run so state survives between scheduled runs.

Edit SUBREDDITS and KEYWORDS below to tune what counts as a lead.
"""

import json
import os
import sys
import time
import re
import urllib.request
import urllib.error
from datetime import datetime, timezone
from html import unescape
import xml.etree.ElementTree as ET

# ---- Configuration -----------------------------------------------------

SUBREDDITS = [
    "forhire",
    "HungryArtists",
    "artcommissions",
    "DesignJobs",
    "artstore",
    "Artists_forhire",
]

# Case-insensitive; a post matches if ANY of these phrases appears in its
# title or body. Only buyer-intent phrases — "commissions open" and
# similar were removed because real-world testing showed they mostly
# catch OTHER ARTISTS advertising themselves, not people looking to hire.
KEYWORDS = [
    "looking for an artist",
    "looking for artist",
    "looking for artists",
    "need an artist",
    "need artist",
    "want an artist",
    "seeking artist",
    "seeking an artist",
    "hiring an artist",
    "hiring artist",
    "commission an artist",
    "artist needed",
    "artist wanted",
    "need a logo",
    "need a banner",
    "need a commission",
    "need artwork",
    "need illustration",
    "looking for an illustrator",
    "looking for illustrator",
    "hire an illustrator",
    "any artists interested",
    "recruiting an artist",
    "in need of an artist",
    "budget for art",
    "willing to pay an artist",
]

STATE_FILE = "state.json"
USER_AGENT = "commission-lead-monitor:v1.0 (by /u/replace_with_your_username)"
REQUEST_DELAY_SECONDS = 10  # be polite to Reddit's unauthenticated endpoint —
                            # higher than usual since shared CI IPs get
                            # throttled harder than a normal connection

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

# ---- Helpers -------------------------------------------------------------

ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {"seen_ids": []}


def save_state(state):
    # keep the seen-list from growing forever
    state["seen_ids"] = state["seen_ids"][-2000:]
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def fetch_rss(subreddit, max_retries=4):
    """Fetch a subreddit's RSS feed, retrying on 429 (rate limit) with
    increasing backoff. GitHub Actions runners share IPs with a lot of
    other automated traffic, so Reddit throttles them more aggressively
    than it would a normal home connection — retrying with a delay
    usually gets through within a run or two."""
    url = f"https://www.reddit.com/r/{subreddit}/new/.rss"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})

    for attempt in range(1, max_retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < max_retries:
                retry_after = e.headers.get("Retry-After")
                wait = int(retry_after) if retry_after and retry_after.isdigit() else attempt * 8
                print(f"r/{subreddit}: 429, retrying in {wait}s (attempt {attempt}/{max_retries})")
                time.sleep(wait)
                continue
            raise


def strip_html(text):
    text = re.sub(r"<[^>]+>", " ", text or "")
    return unescape(text).strip()


def parse_entries(xml_bytes):
    root = ET.fromstring(xml_bytes)
    entries = []
    for entry in root.findall("atom:entry", ATOM_NS):
        entry_id = entry.findtext("atom:id", default="", namespaces=ATOM_NS)
        title = entry.findtext("atom:title", default="", namespaces=ATOM_NS)
        content = entry.findtext("atom:content", default="", namespaces=ATOM_NS)
        link_el = entry.find("atom:link", ATOM_NS)
        link = link_el.get("href") if link_el is not None else ""
        author = entry.findtext("atom:author/atom:name", default="", namespaces=ATOM_NS)
        published = entry.findtext("atom:published", default="", namespaces=ATOM_NS)
        entries.append({
            "id": entry_id,
            "title": strip_html(title),
            "body": strip_html(content),
            "link": link,
            "author": author,
            "published": published,
        })
    return entries


def post_age_label(published_str):
    """Human-readable 'X minutes/hours ago', or '' if unparseable —
    lets you triage freshness at a glance without doing math."""
    if not published_str:
        return ""
    try:
        posted = datetime.fromisoformat(published_str)
        if posted.tzinfo is None:
            posted = posted.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - posted
        minutes = int(delta.total_seconds() // 60)
        if minutes < 1:
            return "just now"
        if minutes < 60:
            return f"{minutes}m ago"
        hours = minutes // 60
        if hours < 24:
            return f"{hours}h ago"
        return f"{hours // 24}d ago"
    except Exception:
        return ""


def matches_keywords(entry):
    haystack = f"{entry['title']} {entry['body']}".lower()
    for kw in KEYWORDS:
        if kw.lower() in haystack:
            return kw
    return None


def send_discord_alert(subreddit, entry, matched_keyword):
    """Returns True if delivered successfully, False otherwise. Callers
    should NOT mark a post as seen unless this returns True — otherwise
    a Discord/network hiccup permanently loses that lead."""
    if not DISCORD_WEBHOOK_URL:
        print(f"[no webhook set] would alert: {entry['title']}")
        return False
    age = post_age_label(entry.get("published", ""))
    payload = {
        "embeds": [{
            "title": entry["title"][:250],
            "url": entry["link"],
            "description": entry["body"][:500],
            "color": 0xD9A755,
            "fields": [
                {"name": "Subreddit", "value": f"r/{subreddit}", "inline": True},
                {"name": "Matched", "value": matched_keyword, "inline": True},
                {"name": "Author", "value": entry["author"] or "unknown", "inline": True},
                {"name": "Posted", "value": age or "unknown", "inline": True},
            ],
        }]
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        DISCORD_WEBHOOK_URL, data=data,
        headers={
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,  # Discord's Cloudflare layer can
                                        # 403 requests with generic/default
                                        # User-Agents, especially from
                                        # shared CI IP ranges
        },
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=15)
        return True
    except Exception as e:
        print(f"Discord webhook failed: {e}", file=sys.stderr)
        return False


def main():
    state = load_state()
    seen = set(state["seen_ids"])
    new_seen = []
    pending_matches = []  # (subreddit, entry, keyword) — collected across
                            # all subreddits, then sorted by freshness
                            # before any Discord message is sent

    for i, sub in enumerate(SUBREDDITS):
        try:
            xml_bytes = fetch_rss(sub)
            entries = parse_entries(xml_bytes)
        except Exception as e:
            print(f"Failed to fetch r/{sub}: {e}", file=sys.stderr)
            continue

        for entry in entries:
            if entry["id"] in seen:
                continue
            kw = matches_keywords(entry)
            if kw:
                pending_matches.append((sub, entry, kw))
            else:
                new_seen.append(entry["id"])

        if i < len(SUBREDDITS) - 1:
            time.sleep(REQUEST_DELAY_SECONDS)

    # Oldest first, freshest last — so the most recent, most-likely-still-
    # open post ends up at the bottom of the Discord channel, the first
    # thing you see when you check it.
    def sort_key(item):
        published = item[1].get("published", "")
        try:
            return datetime.fromisoformat(published)
        except Exception:
            return datetime.min.replace(tzinfo=timezone.utc)

    pending_matches.sort(key=sort_key)

    total_matches = len(pending_matches)
    for sub, entry, kw in pending_matches:
        age = post_age_label(entry.get("published", ""))
        print(f"MATCH r/{sub}: {entry['title']} (kw: {kw}, posted {age})")
        delivered = send_discord_alert(sub, entry, kw)
        if delivered:
            new_seen.append(entry["id"])
            time.sleep(2)  # small gap between Discord posts so a burst
                            # of matches doesn't look like spam to
                            # Discord's abuse protection
        # if delivery failed, don't mark as seen — it'll be retried on
        # the next run instead of being lost

    state["seen_ids"] = list(seen) + new_seen
    save_state(state)
    print(f"Done. {total_matches} new match(es) this run.")


if __name__ == "__main__":
    main()
