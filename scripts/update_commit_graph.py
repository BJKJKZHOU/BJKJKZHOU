#!/usr/bin/env python3

import json
import math
import os
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, time, timedelta, timezone
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

USERNAME = os.getenv("GITHUB_GRAPH_USER", "BJKJKZHOU")
TOKEN = os.environ["PROFILE_TOKEN"]
LOCAL_TZ = ZoneInfo(os.getenv("GRAPH_TIMEZONE", "Asia/Shanghai"))
DAYS = int(os.getenv("GRAPH_DAYS", "31"))
EXCLUDED_REPOS = {
    item.strip()
    for item in os.getenv("GRAPH_EXCLUDED_REPOS", "BJKJKZHOU/BJKJKZHOU").split(",")
    if item.strip()
}
OUTPUT = Path(os.getenv("GRAPH_OUTPUT", "assets/commit-graph.svg"))

GRAPHQL_URL = "https://api.github.com/graphql"
QUERY = r"""
query($login: String!, $from: DateTime!, $to: DateTime!) {
  user(login: $login) {
    commitContributions: contributionsCollection(from: $from, to: $to) {
      commitContributionsByRepository(maxRepositories: 100) {
        repository {
          nameWithOwner
        }
        contributions(first: 100) {
          nodes {
            occurredAt
            commitCount
          }
        }
      }
    }
  }
}
"""


def iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def query_contributions(start_day, end_day):
    start_local = datetime.combine(start_day, time.min, tzinfo=LOCAL_TZ)
    end_local = datetime.combine(end_day + timedelta(days=1), time.min, tzinfo=LOCAL_TZ)

    payload = json.dumps(
        {
            "query": QUERY,
            "variables": {
                "login": USERNAME,
                "from": iso_utc(start_local),
                "to": iso_utc(end_local),
            },
        }
    ).encode("utf-8")

    request = urllib.request.Request(
        GRAPHQL_URL,
        data=payload,
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Content-Type": "application/json",
            "User-Agent": "BJKJKZHOU-profile-commit-graph",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GitHub GraphQL HTTP {exc.code}: {detail}") from exc

    if result.get("errors"):
        raise RuntimeError(f"GitHub GraphQL error: {result['errors']}")

    user = result.get("data", {}).get("user")
    if user is None:
        raise RuntimeError(f"GitHub user not found: {USERNAME}")

    return user["commitContributions"]["commitContributionsByRepository"]


def daily_commit_counts(groups, start_day, end_day):
    counts = defaultdict(int)

    for group in groups:
        repo = group["repository"]["nameWithOwner"]
        if repo in EXCLUDED_REPOS:
            continue

        for node in group["contributions"]["nodes"]:
            occurred = datetime.fromisoformat(node["occurredAt"].replace("Z", "+00:00"))
            day = occurred.astimezone(LOCAL_TZ).date()
            if start_day <= day <= end_day:
                counts[day] += int(node["commitCount"])

    return counts


def nice_axis_max(value: int) -> int:
    if value <= 1:
        return 1

    exponent = 10 ** math.floor(math.log10(value))
    scaled = value / exponent
    if scaled <= 2:
        step = 2
    elif scaled <= 5:
        step = 5
    else:
        step = 10
    return int(step * exponent)


def make_svg(days, counts):
    width = 1280
    height = 450
    left = 95
    right = 55
    top = 90
    bottom = 78
    plot_width = width - left - right
    plot_height = height - top - bottom

    values = [counts.get(day, 0) for day in days]
    ymax = nice_axis_max(max(values, default=0))

    def x_pos(index):
        if len(days) == 1:
            return left + plot_width / 2
        return left + index * plot_width / (len(days) - 1)

    def y_pos(value):
        return top + plot_height - (value / ymax) * plot_height

    points = " ".join(
        f"{x_pos(index):.1f},{y_pos(value):.1f}"
        for index, value in enumerate(values)
    )

    total = sum(values)
    peak = max(values, default=0)
    last_updated = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d %H:%M %Z")

    svg = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-labelledby="title desc">',
        '<title id="title">Zhou Heng&apos;s Commit Graph</title>',
        f'<desc id="desc">Daily Git commit counts for the last {len(days)} days. Total {total}, peak {peak}.</desc>',
        '<rect width="100%" height="100%" fill="#0d1117" rx="8"/>',
        '<g font-family="-apple-system,BlinkMacSystemFont,Segoe UI,Helvetica,Arial,sans-serif">',
        '<text x="640" y="40" fill="#61dafb" font-size="22" font-weight="600" text-anchor="middle">Zhou Heng&apos;s Commit Graph</text>',
        f'<text x="640" y="66" fill="#8b949e" font-size="13" text-anchor="middle">Last {len(days)} days · {total} commits · excludes {escape(", ".join(sorted(EXCLUDED_REPOS)))}</text>',
    ]

    grid_levels = [0, ymax / 2, ymax]
    for level in grid_levels:
        y = y_pos(level)
        label = str(int(level)) if float(level).is_integer() else f"{level:.1f}"
        svg.append(f'<line x1="{left}" y1="{y:.1f}" x2="{width-right}" y2="{y:.1f}" stroke="#24404b" stroke-width="1" stroke-dasharray="2 3"/>')
        svg.append(f'<text x="{left-12}" y="{y+5:.1f}" fill="#61dafb" font-size="12" text-anchor="end">{label}</text>')

    for index, day in enumerate(days):
        x = x_pos(index)
        svg.append(f'<line x1="{x:.1f}" y1="{top}" x2="{x:.1f}" y2="{top+plot_height}" stroke="#1c3440" stroke-width="1" stroke-dasharray="2 3"/>')
        svg.append(f'<text x="{x:.1f}" y="{top+plot_height+25}" fill="#61dafb" font-size="11" text-anchor="middle">{day.day}</text>')

    svg.extend(
        [
            f'<polyline points="{points}" fill="none" stroke="#61dafb" stroke-width="4" stroke-linecap="round" stroke-linejoin="round"/>',
        ]
    )

    for index, value in enumerate(values):
        x = x_pos(index)
        y = y_pos(value)
        svg.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4.5" fill="#f0f6fc"><title>{days[index].isoformat()}: {value} commits</title></circle>')

    svg.extend(
        [
            f'<text x="{left + plot_width/2:.1f}" y="{height-22}" fill="#61dafb" font-size="12" text-anchor="middle">Days</text>',
            f'<text x="20" y="{top + plot_height/2:.1f}" fill="#61dafb" font-size="12" text-anchor="middle" transform="rotate(-90 20 {top + plot_height/2:.1f})">Commits</text>',
            f'<text x="{width-right}" y="{height-22}" fill="#8b949e" font-size="10" text-anchor="end">Updated {escape(last_updated)}</text>',
            '</g>',
            '</svg>',
            '',
        ]
    )

    return "\n".join(svg)


def main():
    end_day = datetime.now(LOCAL_TZ).date()
    start_day = end_day - timedelta(days=DAYS - 1)
    days = [start_day + timedelta(days=index) for index in range(DAYS)]

    groups = query_contributions(start_day, end_day)
    counts = daily_commit_counts(groups, start_day, end_day)

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(make_svg(days, counts), encoding="utf-8")

    print(f"Generated {OUTPUT}: {sum(counts.values())} commits across {DAYS} days")


if __name__ == "__main__":
    main()
