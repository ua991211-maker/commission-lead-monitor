#!/usr/bin/env python3
"""
Commission lead monitor (free version, no Reddit API needed).

Sources:
  1. Bluesky public search (no login, no key)
  2. Google Alerts RSS feeds (you create the alerts; URLs go in a GitHub secret)

Matches are sent to YOUR private Discord channel via webhook. The bot never
posts, DMs or comments anywhere. You reply yourself.
"""

import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from html import unescape

# ---- CONFIG ---------------------------------------------------------------

BLUESKY_QUERIES = [
    '"looking for an artist"',
    '"need an artist"',
    '"commission an artist"',
    '"hiring an artist"',
    '"looking for an illustrator"',
    '"need a logo"',
    '"need a banner"',
    '"looking for artist"',
]
BLUESKY_LIMIT = 25
MAX_AGE_HOURS = 72  # ignore anything older than this

# Google Alerts RSS URLs, one per line or comma-separated, in a GitHub secret.
GOOGLE_ALERT_FEEDS = [
    u.strip() for u in re.split(r"[\n,]+", os.environ.get("GOOGLE_ALERT_FEEDS", "")) if u.strip()
]

KEYWORDS = [
    "looking for an artist", "looking for artist", "looking for artists",
    "need an artist", "need artist", "want an artist",
    "seeking artist", "seeking an artist", "hiring an artist", "hiring artist",
    "commission an artist", "artist needed", "artist wanted",
    "need a logo", "need a banner", "need a commission", "need artwork",
    "need illustration", "looking for an illustrator", "looking for illustrator",
    "hire an illustrator", "any artists interested", "recruiting an artist",
    "in need of an artist", "budget for art", "willing to pay an artist",
]

# Posts from artists advertising themselves. Skipped.
NEGATIVE = [
    "commissions open", "commissions are open", "open for commissions",
    "taking commissions", "my commissions", "commission slots", "slots open",
]

SUBREDDIT_WEIGHT = {
    "forhire": 15, "artcommissions": 15, "Artists_forhire": 15,
    "HungryArtists": 10, "INAT": 10,
    "gamedev": 5, "tattoos": 5, "logodesign": 5,
}

PORTFOLIO_LINK = "https://discord.gg/TPKqDNSku"

REPLY_TEMPLATES = {
    "tattoo": "Hi! I saw your post about tattoo art. I'm an illustrator and would love to help. Portfolio: {link}",
    "logo": "Hi! I saw you need a logo. I design clean, custom logos. Portfolio: {link}",
    "banner": "Hi! I saw you need a banner. I make custom banners and channel art. Portfolio: {link}",
    "character": "Hi! I saw you're looking for character art. I draw OCs and characters. Portfolio: {link}",
    "default": "Hi! I saw your post and I'd love to help with your art. Portfolio: {link}",
}

USER_AGENT = "commission-lead-monitor:v2.0 (personal lead alerts)"
BROWSER_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
REQUEST_DELAY_SECONDS = 2
STATE_FILE = "state.json"
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

ATOM = {"a": "http://www.w3.org/2005/Atom"}

# ---- STATE ----------------------------------------------------------------


def load_state():
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
            data.setdefault("seen_ids", [])
            return data
    except (FileNotFoundError, json.JSONDecodeError):
        return {"seen_ids": []}


def save_state(state):
    state["seen_ids"] = state["seen_ids"][-3000:]
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ---- HTTP -----------------------------------------------------------------


def http_get(url, ua=USER_AGENT, max_retries=3):
    for attempt in range(max_retries + 1):
        req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept": "*/*"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503) and attempt < max_retries:
                wait = e.headers.get("Retry-After")
                time.sleep(min(int(wait) if wait and wait.isdigit() else 5 * (attempt + 1), 60))
                continue
            raise
        except urllib.error.URLError:
            if attempt < max_retries:
                time.sleep(3 * (attempt + 1))
                continue
            raise


# ---- HELPERS --------------------------------------------------------------


def strip_html(text):
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", text or ""))).strip()


def parse_time(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def minutes_old(entry):
    t = entry.get("published")
    if not t:
        return None
    return max(0, int((datetime.now(timezone.utc) - t).total_seconds() // 60))


def post_age_label(entry):
    m = minutes_old(entry)
    if m is None:
        return "unknown"
    if m < 60:
        return f"{m}m ago"
    if m < 1440:
        return f"{m // 60}h {m % 60}m ago"
    return f"{m // 1440}d ago"


def matches_keywords(entry):
    text = f"{entry['title']} {entry['body']}".lower()
    if any(n in text for n in NEGATIVE):
        return None
    for kw in KEYWORDS:
        if kw in text:
            return kw
    return None


def detect_budget(entry):
    text = f"{entry['title']} {entry['body']}"
    vals = []
    for m in re.finditer(r"\$\s?(\d{1,3}(?:,\d{3})*(?:\.\d+)?)", text):
        try:
            vals.append(float(m.group(1).replace(",", "")))
        except ValueError:
            pass
    return max(vals) if vals else None


def detect_category(entry):
    t = f"{entry['title']} {entry['body']}".lower()
    if "tattoo" in t:
        return "tattoo"
    if "logo" in t:
        return "logo"
    if "banner" in t:
        return "banner"
    if any(w in t for w in ("character", " oc ", "ocs", "vtuber", "avatar", "pfp")):
        return "character"
    return "default"


def score_match(entry):
    m = minutes_old(entry)
    score = max(0, 500 - (m if m is not None else 500))
    b = detect_budget(entry)
    if b:
        score += min(b / 2, 1000)
    score += SUBREDDIT_WEIGHT.get(entry.get("subreddit", ""), 0)
    return score


# ---- SOURCES --------------------------------------------------------------


def fetch_bluesky(query):
    url = ("https://public.api.bsky.app/xrpc/app.bsky.feed.searchPosts?"
           + urllib.parse.urlencode({"q": query, "sort": "latest", "limit": BLUESKY_LIMIT}))
    data = json.loads(http_get(url))
    return parse_bluesky(data)


def parse_bluesky(data):
    out = []
    for p in data.get("posts", []):
        uri = p.get("uri", "")
        rec = p.get("record", {})
        handle = p.get("author", {}).get("handle", "")
        rkey = uri.rsplit("/", 1)[-1]
        text = rec.get("text", "")
        out.append({
            "id": uri,
            "title": text[:120].replace("\n", " "),
            "body": text,
            "link": f"https://bsky.app/profile/{handle}/post/{rkey}",
            "author": f"@{handle}",
            "published": parse_time(rec.get("createdAt") or p.get("indexedAt")),
            "subreddit": "",
            "source": "Bluesky",
        })
    return out


def real_url(link):
    """Google Alerts wraps links as google.com/url?...&url=REAL&..."""
    try:
        q = urllib.parse.parse_qs(urllib.parse.urlparse(link).query)
        return q.get("url", [link])[0]
    except Exception:
        return link


def parse_google_alert(xml_bytes):
    root = ET.fromstring(xml_bytes)
    out = []
    for e in root.findall("a:entry", ATOM):
        link_el = e.find("a:link", ATOM)
        link = real_url(link_el.get("href")) if link_el is not None else ""
        m = re.search(r"reddit\.com/r/([^/]+)/", link)
        sub = m.group(1) if m else ""
        title = strip_html(e.findtext("a:title", default="", namespaces=ATOM))
        out.append({
            "id": e.findtext("a:id", default=link, namespaces=ATOM) or link,
            "title": title,
            "body": strip_html(e.findtext("a:content", default="", namespaces=ATOM)),
            "link": link,
            "author": e.findtext("a:author/a:name", default="", namespaces=ATOM) or "unknown",
            "published": parse_time(e.findtext("a:published", default="", namespaces=ATOM)
                                    or e.findtext("a:updated", default="", namespaces=ATOM)),
            "subreddit": sub,
            "source": f"Google Alerts (r/{sub})" if sub else "Google Alerts",
        })
    return out


# ---- DISCORD --------------------------------------------------------------


def send_discord_alert(entry, kw):
    if not DISCORD_WEBHOOK_URL:
        print(f"[no webhook] would alert: {entry['title']}")
        return False
    cat = detect_category(entry)
    fields = [
        {"name": "Source", "value": entry["source"], "inline": True},
        {"name": "Matched", "value": kw, "inline": True},
        {"name": "Posted", "value": post_age_label(entry), "inline": True},
        {"name": "Author", "value": entry["author"] or "unknown", "inline": True},
    ]
    b = detect_budget(entry)
    if b:
        fields.append({"name": "Budget mentioned", "value": f"${b:,.0f}", "inline": True})
    fields.append({"name": "Suggested reply",
                   "value": REPLY_TEMPLATES[cat].format(link=PORTFOLIO_LINK)[:1000], "inline": False})
    payload = {"embeds": [{
        "title": (entry["title"] or "New lead")[:250],
        "url": entry["link"],
        "description": entry["body"][:500],
        "color": 0xD9A755,
        "fields": fields,
    }]}
    req = urllib.request.Request(
        DISCORD_WEBHOOK_URL, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT}, method="POST")
    try:
        urllib.request.urlopen(req, timeout=15)
        return True
    except Exception as e:
        print(f"Discord failed: {e}", file=sys.stderr)
        return False


# ---- MAIN -----------------------------------------------------------------


def gather():
    entries = []
    for q in BLUESKY_QUERIES:
        try:
            got = fetch_bluesky(q)
            print(f"Bluesky {q}: {len(got)} posts")
            entries += got
        except Exception as e:
            print(f"Bluesky {q} failed: {e}", file=sys.stderr)
        time.sleep(REQUEST_DELAY_SECONDS)
    for i, url in enumerate(GOOGLE_ALERT_FEEDS, 1):
        try:
            got = parse_google_alert(http_get(url, ua=BROWSER_UA))
            print(f"Google Alert feed {i}: {len(got)} items")
            entries += got
        except Exception as e:
            print(f"Google Alert feed {i} failed: {e}", file=sys.stderr)
        time.sleep(REQUEST_DELAY_SECONDS)
    if not GOOGLE_ALERT_FEEDS:
        print("No GOOGLE_ALERT_FEEDS secret set; using Bluesky only.")
    return entries


def main():
    state = load_state()
    seen = set(state["seen_ids"])
    pending, queued = [], set()
    for e in gather():
        if e["id"] in seen or e["id"] in queued:
            continue
        age = minutes_old(e)
        if age is not None and age > MAX_AGE_HOURS * 60:
            seen.add(e["id"])  # too old, don't re-check
            state["seen_ids"].append(e["id"])
            continue
        kw = matches_keywords(e)
        if kw:
            queued.add(e["id"])
            pending.append((score_match(e), e, kw))
        else:
            state["seen_ids"].append(e["id"])  # non-match, never needs re-check
    pending.sort(key=lambda x: x[0])  # best lead lands last (bottom of Discord)
    sent = 0
    for _, e, kw in pending:
        print(f"MATCH [{e['source']}] {e['title']} (kw: {kw})")
        if send_discord_alert(e, kw):
            state["seen_ids"].append(e["id"])
            sent += 1
        time.sleep(2)
    save_state(state)
    print(f"Done. {sent}/{len(pending)} lead(s) delivered.")


if __name__ == "__main__":
    main()
