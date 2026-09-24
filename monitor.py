#!/usr/bin/env python3
"""
Reddit commission-lead monitor.

How it works (no Reddit API key, no approval wait):
  - Reddit's public .rss feeds work without authentication — both
    per-subreddit feeds (e.g. reddit.com/r/forhire/new/.rss) AND
    sitewide search (reddit.com/search.rss?q=...), which catches
    buyer-intent posts in subreddits we didn't think to list.
  - This never sends anything to Reddit or Discord users on your
    behalf beyond posting into YOUR OWN webhook — it is a monitor, not
    an outreach bot. You still send the actual message yourself.
  - "Seen" post IDs are stored in state.json so the same post never
    alerts twice; the workflow commits that file back to the repo
    after each run so state survives between scheduled runs.
  - Matches are scored (freshness + mentioned budget + subreddit
    quality) and sent lowest-score-first, so the single best lead
    lands last — at the bottom of Discord, the first thing you see.

Edit the CONFIG section below to tune everything.
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from html import unescape

# ==== CONFIG ==============================================================

SUBREDDITS = [
    "forhire",
    "HungryArtists",
    "artcommissions",
    "DesignJobs",
    "artstore",
    "Artists_forhire",
    "tattoos",       # people asking for tattoo design help
    "gamedev",       # indie devs needing art
    "INAT",          # "I Need A Team" — game dev collab/hire board
    "logodesign",
]

# Sitewide searches — cast a wider net beyond the subreddits above.
# Kept short: each one is an extra fetch, and broad queries return more
# noise, so only the strongest buyer-intent phrases are searched this way.
SEARCH_QUERIES = [
    "looking for an artist",
    "need an artist",
    "hiring an artist",
]

# Case-insensitive; a post matches if ANY of these phrases appears in its
# title or body. Buyer-intent only — seller phrases like "commissions
# open" were deliberately excluded (they mostly catch competing artists
# advertising themselves, not people looking to hire, per real testing).
KEYWORDS = [
    "looking for an artist", "looking for artist", "looking for artists",
    "need an artist", "need artist", "want an artist",
    "seeking artist", "seeking an artist",
    "hiring an artist", "hiring artist", "commission an artist",
    "artist needed", "artist wanted",
    "need a logo", "need a banner", "need a commission",
    "need artwork", "need illustration",
    "looking for an illustrator", "looking for illustrator",
    "hire an illustrator",
    "any artists interested", "recruiting an artist",
    "in need of an artist", "budget for art", "willing to pay an artist",
]

# Subreddits you trust most for genuine buyer intent get a priority boost.
# Anything not listed defaults to 0 — tune these as you see what converts.
SUBREDDIT_WEIGHT = {
    "forhire": 15,
    "artcommissions": 15,
    "Artists_forhire": 15,
    "HungryArtists": 10,
    "INAT": 10,
    "gamedev": 5,
    "tattoos": 5,
    "logodesign": 5,
    "DesignJobs": 0,
    "artstore": 0,
}

# Suggested opening line per category — shown in the alert so replying is
# copy/personalize/send instead of composing from scratch. NEVER sent
# automatically; you still send it yourself. Replace the [link] with your
# actual portfolio link.
PORTFOLIO_LINK = "https://discord.gg/TPKqDNSku"
REPLY_TEMPLATES = {
    "tattoo": f"Hey! I do tattoo-ready line art — happy to help design yours. Some relevant work: {PORTFOLIO_LINK}. Want me to sketch a rough concept first?",
    "logo": f"Hi! I do logo/brand design — glad to help. A few examples: {PORTFOLIO_LINK}. What's your budget and timeline looking like?",
    "banner": f"Hey! I make banners/channel art like this — examples here: {PORTFOLIO_LINK}. Happy to start with a quick draft.",
    "character": f"Hi! I'd love to help bring your character/OC to life — character work here: {PORTFOLIO_LINK}. Any reference images you can share?",
    "default": f"Hey! Saw your post — I do work like this, examples here: {PORTFOLIO_LINK}. Happy to chat details!",
}

STATE_FILE = "state.json"
USER_AGENT = "commission-lead-monitor:v1.0 (by /u/replace_with_your_username)"
REQUEST_DELAY_SECONDS = 10  # be polite to Reddit's unauthenticated endpoint —
                            # higher than usual since shared CI IPs get
                            # throttled harder than a normal connection

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

# ==== Helpers ==============================================================

ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}
SUBREDDIT_FROM_LINK_RE = re.compile(r"reddit\.com/r/([^/]+)/", re.IGNORECASE)
BUDGET_RE = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})*(?:\.\d+)?)")


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {"seen_ids": []}


def save_state(state):
    state["seen_ids"] = state["seen_ids"][-3000:]  # cap growth
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def http_get_with_retry(url, max_retries=4):
    """Shared fetch helper with 429 backoff, used for both subreddit
    feeds and sitewide search."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(1, max_retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < max_retries:
                retry_after = e.headers.get("Retry-After")
                wait = int(retry_after) if retry_after and retry_after.isdigit() else attempt * 8
                print(f"{url}: 429, retrying in {wait}s (attempt {attempt}/{max_retries})")
                time.sleep(wait)
                continue
            raise


def fetch_subreddit_rss(subreddit):
    return http_get_with_retry(f"https://www.reddit.com/r/{subreddit}/new/.rss")


def fetch_search_rss(query):
    q = urllib.parse.quote(query)
    return http_get_with_retry(f"https://www.reddit.com/search.rss?q={q}&sort=new")


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

        m = SUBREDDIT_FROM_LINK_RE.search(link)
        subreddit = m.group(1) if m else "unknown"

        entries.append({
            "id": entry_id,
            "title": strip_html(title),
            "body": strip_html(content),
            "link": link,
            "author": author,
            "published": published,
            "subreddit": subreddit,
        })
    return entries


def post_age_label(published_str):
    """Human-readable 'X minutes/hours ago', or '' if unparseable."""
    if not published_str:
        return ""
    try:
        posted = datetime.fromisoformat(published_str)
        if posted.tzinfo is None:
            posted = posted.replace(tzinfo=timezone.utc)
        minutes = int((datetime.now(timezone.utc) - posted).total_seconds() // 60)
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


def minutes_old(published_str):
    if not published_str:
        return 99999
    try:
        posted = datetime.fromisoformat(published_str)
        if posted.tzinfo is None:
            posted = posted.replace(tzinfo=timezone.utc)
        return max(0, int((datetime.now(timezone.utc) - posted).total_seconds() // 60))
    except Exception:
        return 99999


def matches_keywords(entry):
    haystack = f"{entry['title']} {entry['body']}".lower()
    for kw in KEYWORDS:
        if kw.lower() in haystack:
            return kw
    return None


def detect_budget(entry):
    """Returns the largest $ amount mentioned, or None."""
    haystack = f"{entry['title']} {entry['body']}"
    amounts = [float(a.replace(",", "")) for a in BUDGET_RE.findall(haystack)]
    return max(amounts) if amounts else None


def detect_category(entry):
    text = f"{entry['title']} {entry['body']}".lower()
    if "tattoo" in text:
        return "tattoo"
    if "logo" in text:
        return "logo"
    if "banner" in text:
        return "banner"
    if " oc " in text or "character" in text or "oc!" in text or "oc " in text:
        return "character"
    return "default"


def score_match(entry, subreddit):
    """Higher score = better lead. Sorting sends lowest-scored first,
    so the best lead ends up last — at the bottom of Discord."""
    score = 0.0
    score += max(0, 500 - minutes_old(entry.get("published", "")))  # freshness, decays over ~8h
    budget = detect_budget(entry)
    if budget:
        score += min(budget, 1000) / 2
    score += SUBREDDIT_WEIGHT.get(subreddit, 0)
    return score


def send_discord_alert(entry, matched_keyword):
    """Returns True if delivered successfully, False otherwise. Callers
    should NOT mark a post as seen unless this returns True — otherwise
    a Discord/network hiccup permanently loses that lead."""
    if not DISCORD_WEBHOOK_URL:
        print(f"[no webhook set] would alert: {entry['title']}")
        return False

    age = post_age_label(entry.get("published", ""))
    budget = detect_budget(entry)
    category = detect_category(entry)
    reply = REPLY_TEMPLATES.get(category, REPLY_TEMPLATES["default"])

    fields = [
        {"name": "Subreddit", "value": f"r/{entry['subreddit']}", "inline": True},
        {"name": "Matched", "value": matched_keyword, "inline": True},
        {"name": "Posted", "value": age or "unknown", "inline": True},
        {"name": "Author", "value": entry["author"] or "unknown", "inline": True},
    ]
    if budget:
        fields.append({"name": "Budget mentioned", "value": f"${budget:,.0f}", "inline": True})
    fields.append({"name": "Suggested reply", "value": reply[:1000], "inline": False})

    payload = {
        "embeds": [{
            "title": entry["title"][:250],
            "url": entry["link"],
            "description": entry["body"][:500],
            "color": 0xD9A755,
            "fields": fields,
        }]
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        DISCORD_WEBHOOK_URL, data=data,
        headers={
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,  # Discord's Cloudflare layer can 403
                                        # requests with generic/default
                                        # User-Agents from shared CI IPs
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
    pending = []  # (entry, keyword) collected from all sources, scored
                    # and sorted before anything is sent

    sources = [("subreddit", s) for s in SUBREDDITS] + [("search", q) for q in SEARCH_QUERIES]

    for i, (kind, value) in enumerate(sources):
        try:
            if kind == "subreddit":
                xml_bytes = fetch_subreddit_rss(value)
            else:
                xml_bytes = fetch_search_rss(value)
            entries = parse_entries(xml_bytes)
        except Exception as e:
            print(f"Failed to fetch {kind}={value}: {e}", file=sys.stderr)
            continue

        for entry in entries:
            if entry["id"] in seen:
                continue
            kw = matches_keywords(entry)
            if kw:
                pending.append((entry, kw))
            else:
                new_seen.append(entry["id"])

        if i < len(sources) - 1:
            time.sleep(REQUEST_DELAY_SECONDS)

    # de-dupe: sitewide search and subreddit feeds can both surface the
    # same post — keep one copy per post id
    dedup = {}
    for entry, kw in pending:
        dedup[entry["id"]] = (entry, kw)
    pending = list(dedup.values())

    # lowest score first, highest score last — best lead ends up at the
    # bottom of Discord, the first thing you see
    pending.sort(key=lambda item: score_match(item[0], item[0]["subreddit"]))

    total_matches = len(pending)
    for entry, kw in pending:
        score = score_match(entry, entry["subreddit"])
        age = post_age_label(entry.get("published", ""))
        print(f"MATCH r/{entry['subreddit']}: {entry['title']} (kw: {kw}, posted {age}, score {score:.0f})")
        delivered = send_discord_alert(entry, kw)
        if delivered:
            new_seen.append(entry["id"])
            time.sleep(2)  # small gap between Discord posts so a burst of
                            # matches doesn't look like spam to Discord's
                            # abuse protection
        # if delivery failed, don't mark as seen — retried next run

    state["seen_ids"] = list(seen) + new_seen
    save_state(state)
    print(f"Done. {total_matches} new match(es) this run.")


if __name__ == "__main__":
    main()
