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
DAYS = int(os.getenv("GRAPH_DAYS", "90"))
EXCLUDED_REPOS = {
    item.strip()
    for item in os.getenv("GRAPH_EXCLUDED_REPOS", "BJKJKZHOU/BJKJKZHOU").split(",")
    if item.strip()
}
OUTPUT = Path(os.getenv("GRAPH_OUTPUT", "assets/commit-graph.svg"))
WATCHLIST_SIZE = int(os.getenv("GRAPH_WATCHLIST_SIZE", "4"))
BASELINE_DAYS = int(os.getenv("GRAPH_BASELINE_DAYS", "56"))

GRAPHQL_URL = "https://api.github.com/graphql"
QUERY = r"""
query($login: String!, $from: DateTime!, $to: DateTime!) {
  user(login: $login) {
    commitContributions: contributionsCollection(from: $from, to: $to) {
      commitContributionsByRepository(maxRepositories: 100) {
        repository { nameWithOwner }
        contributions(first: 100) {
          nodes { occurredAt commitCount }
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
    payload = json.dumps({
        "query": QUERY,
        "variables": {
            "login": USERNAME,
            "from": iso_utc(start_local),
            "to": iso_utc(end_local),
        },
    }).encode("utf-8")
    request = urllib.request.Request(
        GRAPHQL_URL,
        data=payload,
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Content-Type": "application/json",
            "User-Agent": "BJKJKZHOU-profile-commit-market",
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
    return {day: sum(repo_counts.get(day, 0) for repo_counts in repos.values()) for day in days}


def nice_axis_bounds(values):
    if not values:
        return 0.0, 100.0
    lo = min(values)
    hi = max(values)
    if math.isclose(lo, hi):
        pad = max(abs(lo) * 0.15, 10.0)
    else:
        pad = (hi - lo) * 0.15
    lo = max(0.0, lo - pad)
    hi += pad
    step = 25.0
    lo = math.floor(lo / step) * step
    hi = math.ceil(hi / step) * step
    if hi <= lo:
        hi = lo + step
    return lo, hi


def activity_index_series(counts, all_days):
    """Log-compressed 7D activity relative to the preceding 56D weekly baseline.

    Index 100 means recent 7D activity equals the preceding baseline week.
    A 2x ratio maps to 150, 4x to 200, 8x to 250; 0.5x maps to 50.
    A small pseudocount keeps zero/near-zero periods finite without hard clipping
    normal burst activity at an arbitrary ceiling.
    """
    values = {}
    for index, day in enumerate(all_days):
        recent_start = max(0, index - 6)
        recent = all_days[recent_start:index + 1]
        recent_total = sum(counts.get(item, 0) for item in recent)

        baseline_end = recent_start
        baseline_start = max(0, baseline_end - BASELINE_DAYS)
        baseline = all_days[baseline_start:baseline_end]
        baseline_total = sum(counts.get(item, 0) for item in baseline)

        if len(baseline) < 14:
            values[day] = None
            continue

        baseline_weekly = baseline_total * 7.0 / len(baseline)
        ratio = (recent_total + 1.0) / (baseline_weekly + 1.0)
        values[day] = max(0.0, 100.0 + 50.0 * math.log2(ratio))

    return values


def weekly_candles(chart_days, index_by_day, commit_counts):
    weeks = defaultdict(list)
    for day in chart_days:
        weeks[day - timedelta(days=day.weekday())].append(day)

    candles = []
    for week_start in sorted(weeks):
        days = weeks[week_start]
        points = [(day, index_by_day.get(day)) for day in days if index_by_day.get(day) is not None]
        if not points:
            continue
        values = [value for _, value in points]
        candles.append({
            "start": week_start,
            "end": days[-1],
            "open": values[0],
            "high": max(values),
            "low": min(values),
            "close": values[-1],
            "volume": sum(commit_counts.get(day, 0) for day in days),
        })
    return candles


def moving_average(values, period):
    result = []
    for index in range(len(values)):
        if index + 1 < period:
            result.append(None)
            continue
        window = values[index - period + 1:index + 1]
        result.append(sum(window) / period)
    return result


def activity_change(values):
    recent = values[-7:]
    previous = values[-14:-7]
    recent_avg = sum(recent) / len(recent) if recent else 0.0
    previous_avg = sum(previous) / len(previous) if previous else 0.0
    if previous_avg == 0:
        return 0.0 if recent_avg == 0 else None
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


def make_polyline(values, x0, y0, width, height):
    if not values:
        return ""
    ymax = max(max(values, default=0), 1)
    def x_pos(index):
        return x0 + width / 2 if len(values) == 1 else x0 + index * width / (len(values) - 1)
    def y_pos(value):
        return y0 + height - (value / ymax) * height
    return " ".join(f"{x_pos(i):.1f},{y_pos(v):.1f}" for i, v in enumerate(values))


def make_svg(chart_days, all_days, repos):
    width = 1280
    left = 82
    right = 62
    price_top = 124
    price_height = 218
    volume_top = 357
    volume_height = 54
    plot_width = width - left - right

    recent_31 = chart_days[-31:]
    recent_7 = chart_days[-7:]
    all_counts = total_commit_counts(repos, all_days)
    chart_counts = {day: all_counts.get(day, 0) for day in chart_days}
    index_by_day = activity_index_series(all_counts, all_days)
    candles = weekly_candles(chart_days, index_by_day, chart_counts)

    latest_index = next((index_by_day.get(day) for day in reversed(chart_days) if index_by_day.get(day) is not None), 100.0)
    seven_days_ago = chart_days[-8] if len(chart_days) >= 8 else chart_days[0]
    previous_index = index_by_day.get(seven_days_ago)
    index_change = ((latest_index - previous_index) / previous_index * 100.0) if previous_index and previous_index > 0 else None

    ranked_repos = sorted(
        repos.items(),
        key=lambda item: sum(item[1].get(day, 0) for day in recent_31),
        reverse=True,
    )
    ranked_repos = [item for item in ranked_repos if sum(item[1].get(day, 0) for day in recent_31) > 0]
    watchlist = ranked_repos[:WATCHLIST_SIZE]

    recent_total = sum(chart_counts.get(day, 0) for day in recent_7)
    active_days = sum(1 for day in recent_7 if chart_counts.get(day, 0) > 0)
    active_repos = sum(1 for _, repo_counts in repos.items() if any(repo_counts.get(day, 0) > 0 for day in recent_7))
    top_repo_total = max((sum(repo_counts.get(day, 0) for day in recent_7) for repo_counts in repos.values()), default=0)
    focus = (100.0 * top_repo_total / recent_total) if recent_total else 0.0

    row_height = 34
    watch_header_y = 462
    watch_top = 480
    footer_pad = 34
    height = max(535, watch_top + max(len(watchlist), 1) * row_height + footer_pad)

    candle_values = []
    for candle in candles:
        candle_values.extend([candle["low"], candle["high"]])
    ymin, ymax = nice_axis_bounds(candle_values or [latest_index])

    def y_price(value):
        return price_top + price_height - (value - ymin) / (ymax - ymin) * price_height

    def candle_x(index):
        if len(candles) == 1:
            return left + plot_width / 2
        slot = plot_width / len(candles)
        return left + slot * (index + 0.5)

    slot = plot_width / max(len(candles), 1)
    body_width = min(24.0, slot * 0.42)
    max_volume = max((item["volume"] for item in candles), default=1)
    last_updated = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d %H:%M %Z")
    excluded = ", ".join(sorted(EXCLUDED_REPOS))

    svg = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-labelledby="title desc">',
        '<title id="title">Zhou Heng&apos;s Development Market</title>',
        f'<desc id="desc">{len(chart_days)} day development activity index rendered as weekly candlesticks with moving averages, latest index line, commit volume and repository watchlist.</desc>',
        '<rect width="100%" height="100%" fill="#0d1117" rx="8"/>',
        '<g font-family="-apple-system,BlinkMacSystemFont,Segoe UI,Helvetica,Arial,sans-serif">',
        '<text x="82" y="42" fill="#f0f6fc" font-size="23" font-weight="650">Zhou Heng&apos;s Development Market</text>',
        f'<text x="82" y="70" fill="#8b949e" font-size="13">{len(chart_days)}D · weekly candles · activity index</text>',
        f'<text x="82" y="101" fill="#f0f6fc" font-size="25" font-weight="650">{latest_index:.1f}</text>',
        f'<text x="158" y="101" fill="{change_color(index_change)}" font-size="14" font-weight="600">{escape(change_text(index_change))}</text>',
        f'<text x="1218" y="42" fill="#8b949e" font-size="12" text-anchor="end">7D {recent_total} commits · {active_days}/7 active days</text>',
        f'<text x="1218" y="66" fill="#8b949e" font-size="12" text-anchor="end">Breadth {active_repos} · Focus {focus:.0f}%</text>',
    ]

    for i in range(4):
        value = ymin + (ymax - ymin) * i / 3
        y = y_price(value)
        svg.append(f'<line x1="{left}" y1="{y:.1f}" x2="{width-right}" y2="{y:.1f}" stroke="#21262d" stroke-width="1"/>')
        svg.append(f'<text x="{width-right+12}" y="{y+4:.1f}" fill="#8b949e" font-size="11">{value:.0f}</text>')

    close_values = [candle["close"] for candle in candles]
    ma2 = moving_average(close_values, 2)
    ma4 = moving_average(close_values, 4)

    for values, color in ((ma2, "#d29922"), (ma4, "#58a6ff")):
        segments = []
        for index, value in enumerate(values):
            if value is None:
                continue
            segments.append(f"{candle_x(index):.1f},{y_price(value):.1f}")
        if len(segments) >= 2:
            svg.append(
                f'<polyline points="{" ".join(segments)}" fill="none" stroke="{color}" stroke-width="1.8" '
                f'stroke-opacity="0.85" stroke-linecap="round" stroke-linejoin="round"/>'
            )

    svg.append('<text x="82" y="116" fill="#d29922" font-size="10">MA2</text>')
    svg.append('<text x="116" y="116" fill="#58a6ff" font-size="10">MA4</text>')

    latest_close = candles[-1]["close"] if candles else latest_index
    latest_y = y_price(latest_close)
    latest_up = candles[-1]["close"] >= candles[-1]["open"] if candles else True
    latest_color = "#3fb950" if latest_up else "#f85149"
    svg.append(
        f'<line x1="{left}" y1="{latest_y:.1f}" x2="{width-right}" y2="{latest_y:.1f}" '
        f'stroke="{latest_color}" stroke-width="1" stroke-dasharray="5 5" stroke-opacity="0.55"/>'
    )
    label_width = 48
    label_x = width - right + 8
    label_y = max(price_top + 2, min(latest_y - 10, price_top + price_height - 22))
    svg.append(f'<rect x="{label_x}" y="{label_y:.1f}" width="{label_width}" height="20" rx="3" fill="{latest_color}"/>')
    svg.append(
        f'<text x="{label_x + label_width/2:.1f}" y="{label_y + 14:.1f}" fill="#ffffff" font-size="10" '
        f'text-anchor="middle">{latest_close:.1f}</text>'
    )

    month_seen = set()
    for index, candle in enumerate(candles):
        x = candle_x(index)
        up = candle["close"] >= candle["open"]
        color = "#3fb950" if up else "#f85149"
        y_open = y_price(candle["open"])
        y_close = y_price(candle["close"])
        y_high = y_price(candle["high"])
        y_low = y_price(candle["low"])

        svg.append(f'<line x1="{x:.1f}" y1="{y_high:.1f}" x2="{x:.1f}" y2="{y_low:.1f}" stroke="{color}" stroke-width="2"/>')
        body_top = min(y_open, y_close)
        body_height = max(abs(y_close - y_open), 3.0)
        svg.append(
            f'<rect x="{x-body_width/2:.1f}" y="{body_top:.1f}" width="{body_width:.1f}" height="{body_height:.1f}" '
            f'fill="{color}" rx="1"><title>{candle["start"].isoformat()} · O {candle["open"]:.1f} H {candle["high"]:.1f} '
            f'L {candle["low"]:.1f} C {candle["close"]:.1f} · {candle["volume"]} commits</title></rect>'
        )

        volume_h = volume_height * candle["volume"] / max_volume if max_volume else 0
        svg.append(
            f'<rect x="{x-body_width/2:.1f}" y="{volume_top+volume_height-volume_h:.1f}" width="{body_width:.1f}" '
            f'height="{max(volume_h, 1):.1f}" fill="{color}" opacity="0.55"/>'
        )

        month_key = candle["start"].strftime("%Y-%m")
        if index == 0 or month_key not in month_seen:
            month_seen.add(month_key)
            svg.append(
                f'<text x="{x:.1f}" y="{volume_top+volume_height+22}" fill="#8b949e" font-size="11" text-anchor="middle">'
                f'{candle["start"].strftime("%b %d")}</text>'
            )

    svg.append(f'<text x="{left}" y="{volume_top-8}" fill="#6e7681" font-size="10">VOLUME</text>')

    watch_y = watch_header_y
    svg.append(f'<text x="82" y="{watch_y}" fill="#8b949e" font-size="12" font-weight="600">WATCHLIST</text>')
    svg.append(f'<text x="735" y="{watch_y}" fill="#8b949e" font-size="11" text-anchor="end">31D COMMITS</text>')
    svg.append(f'<text x="865" y="{watch_y}" fill="#8b949e" font-size="11" text-anchor="end">7D CHANGE</text>')

    spark_x = 925
    spark_width = 285
    spark_height = 22

    for row, (repo, repo_counts) in enumerate(watchlist):
        y = watch_top + row * row_height
        repo_values = [repo_counts.get(day, 0) for day in recent_31]
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
    svg.extend([
        f'<text x="82" y="{footer_y}" fill="#6e7681" font-size="10">Index 100 = recent 7D matches prior 56D weekly baseline · log scale · Excludes {escape(excluded)}</text>',
        f'<text x="1218" y="{footer_y}" fill="#6e7681" font-size="10" text-anchor="end">Updated {escape(last_updated)}</text>',
        '</g>',
        '</svg>',
        '',
    ])
    return "\n".join(svg)


def main():
    end_day = datetime.now(LOCAL_TZ).date()
    chart_start = end_day - timedelta(days=DAYS - 1)
    data_start = chart_start - timedelta(days=BASELINE_DAYS + 7)

    all_days = [data_start + timedelta(days=index) for index in range((end_day - data_start).days + 1)]
    chart_days = [chart_start + timedelta(days=index) for index in range(DAYS)]

    groups = query_contributions(data_start, end_day)
    repos = repo_commit_counts(groups, data_start, end_day)

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(make_svg(chart_days, all_days, repos), encoding="utf-8")

    chart_counts = total_commit_counts(repos, chart_days)
    print(
        f"Generated {OUTPUT}: {sum(chart_counts.values())} commits across "
        f"{len(repos)} repositories and {DAYS} chart days"
    )


if __name__ == "__main__":
    main()
