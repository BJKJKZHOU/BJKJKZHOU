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
WATCHLIST_SIZE = int(os.getenv("GRAPH_WATCHLIST_SIZE", "4"))

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


def repo_commit_counts(groups, start_day, end_day):
    repos = defaultdict(lambda: defaultdict(int))

    for group in groups:
        repo = group["repository"]["nameWithOwner"]
        if repo in EXCLUDED_REPOS:
            continue

        for node in group["contributions"]["nodes"]:
            occurred = datetime.fromisoformat(node["occurredAt"].replace("Z", "+00:00"))
            day = occurred.astimezone(LOCAL_TZ).date()
            if start_day <= day <= end_day:
                repos[repo][day] += int(node["commitCount"])

    return repos


def total_commit_counts(repos, days):
    return {
        day: sum(repo_counts.get(day, 0) for repo_counts in repos.values())
        for day in days
    }


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


def activity_change(values):
    recent = values[-7:]
    previous = values[-14:-7]
    recent_avg = sum(recent) / len(recent) if recent else 0.0
    previous_avg = sum(previous) / len(previous) if previous else 0.0

    if previous_avg == 0:
        if recent_avg == 0:
            return 0.0
        return None

    return (recent_avg - previous_avg) / previous_avg * 100.0


def change_text(change):
    if change is None:
        return "NEW"
    if abs(change) < 0.05:
        return "0.0%"
    return f"{change:+.1f}%"


def change_color(change):
    if change is None or change > 0.05:
        return "#3fb950"
    if change < -0.05:
        return "#f85149"
    return "#8b949e"


def make_polyline(values, x0, y0, width, height, ymax=None):
    if not values:
        return ""
    if ymax is None:
        ymax = max(values, default=0)
    ymax = max(ymax, 1)

    def x_pos(index):
        if len(values) == 1:
            return x0 + width / 2
        return x0 + index * width / (len(values) - 1)

    def y_pos(value):
        return y0 + height - (value / ymax) * height

    return " ".join(
        f"{x_pos(index):.1f},{y_pos(value):.1f}"
        for index, value in enumerate(values)
    )


def make_svg(days, repos):
    width = 1280
    height = 610
    left = 82
    right = 62
    top = 118
    plot_height = 270
    plot_width = width - left - right

    counts = total_commit_counts(repos, days)
    values = [counts.get(day, 0) for day in days]
    ymax = nice_axis_max(max(values, default=0))
    total = sum(values)
    overall_change = activity_change(values)

    ranked_repos = sorted(
        repos.items(),
        key=lambda item: sum(item[1].get(day, 0) for day in days),
        reverse=True,
    )
    watchlist = ranked_repos[:WATCHLIST_SIZE]

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
    area_points = (
        f"{x_pos(0):.1f},{top + plot_height:.1f} "
        + points
        + f" {x_pos(len(days)-1):.1f},{top + plot_height:.1f}"
    )

    last_updated = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d %H:%M %Z")

    svg = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-labelledby="title desc">',
        '<title id="title">Zhou Heng&apos;s Commit Market</title>',
        f'<desc id="desc">Daily Git commit activity for the last {len(days)} days with repository watchlist.</desc>',
        '<defs>',
        '<linearGradient id="area" x1="0" y1="0" x2="0" y2="1">',
        '<stop offset="0%" stop-color="#3fb950" stop-opacity="0.28"/>',
        '<stop offset="100%" stop-color="#3fb950" stop-opacity="0.02"/>',
        '</linearGradient>',
        '</defs>',
        '<rect width="100%" height="100%" fill="#0d1117" rx="8"/>',
        '<g font-family="-apple-system,BlinkMacSystemFont,Segoe UI,Helvetica,Arial,sans-serif">',
        '<text x="82" y="42" fill="#f0f6fc" font-size="23" font-weight="650">Zhou Heng&apos;s Commit Market</text>',
        f'<text x="82" y="72" fill="#8b949e" font-size="13">{len(days)}D · total activity</text>',
        f'<text x="82" y="102" fill="#f0f6fc" font-size="24" font-weight="650">{total}</text>',
        f'<text x="142" y="102" fill="{change_color(overall_change)}" font-size="14" font-weight="600">{escape(change_text(overall_change))}</text>',
        '<text x="1218" y="42" fill="#8b949e" font-size="12" text-anchor="end">7D avg vs previous 7D</text>',
    ]

    grid_levels = [0, ymax / 2, ymax]
    for level in grid_levels:
        y = y_pos(level)
        label = str(int(level)) if float(level).is_integer() else f"{level:.1f}"
        svg.append(f'<line x1="{left}" y1="{y:.1f}" x2="{width-right}" y2="{y:.1f}" stroke="#21262d" stroke-width="1"/>')
        svg.append(f'<text x="{width-right+12}" y="{y+4:.1f}" fill="#8b949e" font-size="11">{label}</text>')

    tick_indices = sorted(set([0, 7, 14, 21, 28, len(days) - 1]))
    for index in tick_indices:
        if index < 0 or index >= len(days):
            continue
        x = x_pos(index)
        day = days[index]
        label = day.strftime("%b %d")
        svg.append(f'<text x="{x:.1f}" y="{top+plot_height+24}" fill="#8b949e" font-size="11" text-anchor="middle">{label}</text>')

    svg.extend(
        [
            f'<polygon points="{area_points}" fill="url(#area)"/>',
            f'<polyline points="{points}" fill="none" stroke="#3fb950" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/>',
        ]
    )

    if values:
        last_x = x_pos(len(values) - 1)
        last_y = y_pos(values[-1])
        svg.append(f'<circle cx="{last_x:.1f}" cy="{last_y:.1f}" r="4" fill="#3fb950" stroke="#0d1117" stroke-width="2"/>')
        svg.append(f'<rect x="{last_x-16:.1f}" y="{last_y-29:.1f}" width="32" height="19" rx="4" fill="#238636"/>')
        svg.append(f'<text x="{last_x:.1f}" y="{last_y-15:.1f}" fill="#ffffff" font-size="11" text-anchor="middle">{values[-1]}</text>')

    watch_top = 442
    svg.append('<text x="82" y="428" fill="#8b949e" font-size="12" font-weight="600">WATCHLIST</text>')
    svg.append('<text x="735" y="428" fill="#8b949e" font-size="11" text-anchor="end">31D COMMITS</text>')
    svg.append('<text x="865" y="428" fill="#8b949e" font-size="11" text-anchor="end">7D CHANGE</text>')

    row_height = 34
    spark_x = 925
    spark_width = 285
    spark_height = 22

    for row, (repo, repo_counts) in enumerate(watchlist):
        y = watch_top + row * row_height
        repo_values = [repo_counts.get(day, 0) for day in days]
        repo_total = sum(repo_values)
        repo_change = activity_change(repo_values)
        short_name = repo.split("/", 1)[-1]
        spark_points = make_polyline(repo_values, spark_x, y - 16, spark_width, spark_height)
        spark_color = change_color(repo_change)

        if row:
            svg.append(f'<line x1="82" y1="{y-24}" x2="1218" y2="{y-24}" stroke="#21262d" stroke-width="1"/>')
        svg.append(f'<text x="82" y="{y}" fill="#f0f6fc" font-size="14" font-weight="600">{escape(short_name)}</text>')
        svg.append(f'<text x="735" y="{y}" fill="#c9d1d9" font-size="13" text-anchor="end">{repo_total}</text>')
        svg.append(f'<text x="865" y="{y}" fill="{spark_color}" font-size="13" font-weight="600" text-anchor="end">{escape(change_text(repo_change))}</text>')
        if spark_points:
            svg.append(f'<polyline points="{spark_points}" fill="none" stroke="{spark_color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>')

    footer_y = height - 18
    excluded = ", ".join(sorted(EXCLUDED_REPOS))
    svg.extend(
        [
            f'<text x="82" y="{footer_y}" fill="#6e7681" font-size="10">Excludes {escape(excluded)}</text>',
            f'<text x="1218" y="{footer_y}" fill="#6e7681" font-size="10" text-anchor="end">Updated {escape(last_updated)}</text>',
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
    repos = repo_commit_counts(groups, start_day, end_day)
    counts = total_commit_counts(repos, days)

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(make_svg(days, repos), encoding="utf-8")

    print(
        f"Generated {OUTPUT}: {sum(counts.values())} commits across "
        f"{len(repos)} repositories and {DAYS} days"
    )


if __name__ == "__main__":
    main()
