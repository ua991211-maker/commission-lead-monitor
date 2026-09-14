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
from html import unescape
import xml.etree.ElementTree as ET

# ---- Configuration -----------------------------------------------------

SUBREDDITS = [
    "forhire",
    "HungryArtists",
    "artcommissions",
    "DesignJobs",
]

# Case-insensitive; a post matches if ANY of these phrases appears in its
# title or body. Keep this list specific — broad words like "art" alone
# will flood you with noise.
KEYWORDS = [
    "looking for an artist",
    "looking for artist",
    "need an artist",
    "commission an artist",
    "hiring an artist",
    "need a logo",
    "need a banner",
    "need artwork",
    "commission open" ,
    "taking commissions",  # note: this one catches *artists* advertising,
                            # not buyers — remove it if you only want buyers
]

STATE_FILE = "state.json"
USER_AGENT = "commission-lead-monitor:v1.0 (by /u/replace_with_your_username)"
REQUEST_DELAY_SECONDS = 10  # be polite to Reddit's unauthenticated endpoint —
                            # higher than usual since shared CI IPs get
                            # throttled harder than a normal connection

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

#
