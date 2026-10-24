#!/usr/bin/env python3
"""
Fully Automated GitHub Repository Analytics Dashboard
Repository: shivampimple29/Intelligent-Data-System-sem5

Premium, minimal, production-ready analytics engine.
Collects official GitHub traffic, community growth, and verified supporters;
maintains version-controlled historical JSON storage; generates theme-adaptive
SVG metric cards & compact trendlines; and safely updates README.md.

Zero external dependencies required (uses standard library only).
"""

from __future__ import annotations

import base64
import html
import json
import math
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Timezone: Indian Standard Time (IST, UTC+05:30)
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc

START_MARKER = "<!-- REPOSITORY_ANALYTICS_START -->"
END_MARKER = "<!-- REPOSITORY_ANALYTICS_END -->"


# -----------------------------------------------------------------------------
# Atomic File Utilities
# -----------------------------------------------------------------------------

def atomic_write_text(filepath: Path, content: str, encoding: str = "utf-8") -> None:
    """Safely write text to file using an atomic replacement."""
    filepath.parent.mkdir(parents=True, exist_ok=True)
    temp_path = filepath.with_suffix(f".tmp-{os.getpid()}-{int(time.time() * 1000)}")
    try:
        with open(temp_path, "w", encoding=encoding) as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, filepath)
    finally:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


def atomic_write_json(filepath: Path, data: Any, indent: int = 2) -> None:
    """Safely write JSON to file using atomic replacement."""
    content = json.dumps(data, indent=indent, ensure_ascii=False) + "\n"
    atomic_write_text(filepath, content)


def safe_load_json(filepath: Path, default_factory) -> Any:
    """Load JSON from file. If file does not exist or is corrupted, return default."""
    if not filepath.exists():
        return default_factory()
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        sys.stderr.write(f"[WARNING] Failed to parse JSON from {filepath}: {exc}\n")
        backup = filepath.with_name(f"{filepath.name}.corrupt-{int(time.time())}")
        try:
            filepath.rename(backup)
            sys.stderr.write(f"[INFO] Moved corrupted file to {backup}\n")
        except OSError:
            pass
        return default_factory()


# -----------------------------------------------------------------------------
# Date & Timezone Formatting
# -----------------------------------------------------------------------------

def parse_iso_datetime(iso_str: str) -> Optional[datetime]:
    """Parse ISO 8601 string into a UTC datetime object."""
    if not iso_str:
        return None
    cleaned = iso_str.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(cleaned)
    except ValueError:
        return None


def format_ist(dt: Optional[datetime], fmt: str = "%Y-%m-%d %H:%M:%S IST") -> str:
    """Convert a datetime to IST formatted string."""
    if dt is None:
        return "N/A"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(IST).strftime(fmt)


def get_current_timestamps() -> Tuple[str, str]:
    """Return current timestamp in (ISO UTC, IST formatted string)."""
    now_utc = datetime.now(UTC)
    now_ist_str = format_ist(now_utc)
    return now_utc.isoformat(), now_ist_str


def get_git_credential_token() -> Optional[str]:
    """Retrieve GitHub token from local Git Credential Manager as fallback."""
    try:
        proc = subprocess.run(
            ["git", "credential", "fill"],
            input="protocol=https\nhost=github.com\n",
            text=True,
            capture_output=True,
            timeout=5,
        )
        for line in proc.stdout.splitlines():
            if line.startswith("password="):
                val = line.split("=", 1)[1].strip()
                if val:
                    return val
    except Exception:
        pass
    return None


# -----------------------------------------------------------------------------
# GitHub REST API Client
# -----------------------------------------------------------------------------

class GitHubAPIClient:
    """Robust GitHub REST API client with bounded retries and exponential backoff."""

    def __init__(self, traffic_token: Optional[str] = None, fallback_token: Optional[str] = None):
        self.traffic_token = traffic_token or fallback_token
        self.general_token = fallback_token or traffic_token
        self.api_base = "https://api.github.com"
        self.user_agent = "Repository-AnalyticsBot/2.0"

    def request(
        self,
        endpoint: str,
        token: Optional[str] = None,
        accept: Optional[str] = None,
        max_retries: int = 3,
        backoff_sec: float = 2.0,
    ) -> Tuple[int, Optional[Any], Dict[str, str]]:
        url = endpoint if endpoint.startswith("http") else f"{self.api_base}/{endpoint.lstrip('/')}"
        headers = {
            "User-Agent": self.user_agent,
            "Accept": accept or "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"

        req = urllib.request.Request(url, headers=headers)

        for attempt in range(1, max_retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    status = resp.status
                    resp_headers = dict(resp.headers.items())
                    raw_body = resp.read().decode("utf-8")
                    data = json.loads(raw_body) if raw_body else None
                    return status, data, resp_headers
            except urllib.error.HTTPError as exc:
                status = exc.code
                resp_headers = dict(exc.headers.items()) if exc.headers else {}
                try:
                    err_body = exc.read().decode("utf-8")
                    err_json = json.loads(err_body)
                except Exception:
                    err_json = {"raw": str(exc)}

                if status in (401, 403, 404):
                    return status, err_json, resp_headers

                if status in (429, 500, 502, 503, 504) and attempt < max_retries:
                    retry_after = resp_headers.get("Retry-After")
                    sleep_time = float(retry_after) if retry_after else (backoff_sec * (2 ** (attempt - 1)))
                    sys.stderr.write(
                        f"[HTTP {status}] Retrying {endpoint} in {sleep_time:.1f}s (attempt {attempt}/{max_retries})...\n"
                    )
                    time.sleep(sleep_time)
                    continue

                return status, err_json, resp_headers

            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                if attempt < max_retries:
                    sleep_time = backoff_sec * (2 ** (attempt - 1))
                    sys.stderr.write(
                        f"[Network Error] {exc} for {endpoint}. Retrying in {sleep_time:.1f}s (attempt {attempt}/{max_retries})...\n"
                    )
                    time.sleep(sleep_time)
                    continue
                return 0, {"error": str(exc)}, {}

        return 0, {"error": "Max retries exceeded"}, {}


# -----------------------------------------------------------------------------
# Historical Traffic Storage & Processing
# -----------------------------------------------------------------------------

def initialize_traffic_history() -> Dict[str, Any]:
    """Default structure for traffic-history.json."""
    return {
        "schemaVersion": 1,
        "collectionStartedAt": None,
        "lastSuccessfulUpdate": None,
        "lastUpdateStatus": "uninitialized",
        "notes": (
            "Daily buckets follow GitHub API UTC 24h reporting periods. "
            "Summing daily uniques across multiple days does not equal distinct individuals across the period."
        ),
        "daily": {},
    }


def merge_traffic_data(
    history: Dict[str, Any],
    clones_payload: Optional[Dict[str, Any]],
    views_payload: Optional[Dict[str, Any]],
    collection_time_iso: str,
) -> bool:
    """Merge incoming traffic records into persistent history without overwriting historical dates."""
    if "daily" not in history or not isinstance(history["daily"], dict):
        history["daily"] = {}

    if not history.get("collectionStartedAt"):
        history["collectionStartedAt"] = collection_time_iso

    updated_any = False

    if clones_payload and isinstance(clones_payload.get("clones"), list):
        for item in clones_payload["clones"]:
            ts = item.get("timestamp")
            if not ts:
                continue
            date_key = ts[:10]
            if date_key not in history["daily"]:
                history["daily"][date_key] = {
                    "clones": 0,
                    "uniqueCloners": 0,
                    "views": 0,
                    "uniqueVisitors": 0,
                    "collectedAt": collection_time_iso,
                }
            history["daily"][date_key]["clones"] = int(item.get("count", 0))
            history["daily"][date_key]["uniqueCloners"] = int(item.get("uniques", 0))
            history["daily"][date_key]["collectedAt"] = collection_time_iso
            updated_any = True

    if views_payload and isinstance(views_payload.get("views"), list):
        for item in views_payload["views"]:
            ts = item.get("timestamp")
            if not ts:
                continue
            date_key = ts[:10]
            if date_key not in history["daily"]:
                history["daily"][date_key] = {
                    "clones": 0,
                    "uniqueCloners": 0,
                    "views": 0,
                    "uniqueVisitors": 0,
                    "collectedAt": collection_time_iso,
                }
            history["daily"][date_key]["views"] = int(item.get("count", 0))
            history["daily"][date_key]["uniqueVisitors"] = int(item.get("uniques", 0))
            history["daily"][date_key]["collectedAt"] = collection_time_iso
            updated_any = True

    if updated_any:
        history["lastSuccessfulUpdate"] = collection_time_iso
        history["lastUpdateStatus"] = "success"

    return updated_any


def compute_traffic_metrics(history: Dict[str, Any]) -> Dict[str, Any]:
    """Compute rolling 5-day metrics, comparisons, and lifetime cumulative totals."""
    daily = history.get("daily", {})
    sorted_dates = sorted(daily.keys())

    cumulative_clones = sum(d.get("clones", 0) for d in daily.values())
    cumulative_views = sum(d.get("views", 0) for d in daily.values())
    cumulative_unique_cloners = sum(d.get("uniqueCloners", 0) for d in daily.values())
    cumulative_unique_visitors = sum(d.get("uniqueVisitors", 0) for d in daily.values())

    current_5_dates = sorted_dates[-5:] if sorted_dates else []
    current_records: List[Dict[str, Any]] = []

    total_5d_clones = 0
    total_5d_views = 0
    sum_unique_cloners = 0
    sum_unique_visitors = 0

    for d in current_5_dates:
        rec = daily[d]
        clones = rec.get("clones", 0)
        u_cloners = rec.get("uniqueCloners", 0)
        views = rec.get("views", 0)
        u_visitors = rec.get("uniqueVisitors", 0)

        total_5d_clones += clones
        total_5d_views += views
        sum_unique_cloners += u_cloners
        sum_unique_visitors += u_visitors

        current_records.append({
            "date": d,
            "clones": clones,
            "uniqueCloners": u_cloners,
            "views": views,
            "uniqueVisitors": u_visitors,
            "status": "Recorded",
        })

    num_days = len(current_records)
    avg_unique_cloners = round(sum_unique_cloners / num_days, 1) if num_days > 0 else 0.0
    avg_unique_visitors = round(sum_unique_visitors / num_days, 1) if num_days > 0 else 0.0

    prev_5_dates = sorted_dates[-10:-5] if len(sorted_dates) >= 10 else []
    prev_5d_clones: Optional[int] = None
    prev_5d_views: Optional[int] = None
    clone_growth_pct: Optional[float] = None
    view_growth_pct: Optional[float] = None
    clone_diff: Optional[int] = None
    view_diff: Optional[int] = None

    if len(prev_5_dates) == 5:
        prev_5d_clones = sum(daily[d].get("clones", 0) for d in prev_5_dates)
        prev_5d_views = sum(daily[d].get("views", 0) for d in prev_5_dates)
        clone_diff = total_5d_clones - prev_5d_clones
        view_diff = total_5d_views - prev_5d_views

        if prev_5d_clones > 0:
            clone_growth_pct = round((clone_diff / prev_5d_clones) * 100, 1)
        if prev_5d_views > 0:
            view_growth_pct = round((view_diff / prev_5d_views) * 100, 1)

    return {
        "availableDaysCount": num_days,
        "isWindowComplete": (num_days >= 5),
        "windowStartDate": current_5_dates[0] if current_5_dates else None,
        "windowEndDate": current_5_dates[-1] if current_5_dates else None,
        "last5DaysRecords": current_records,
        "last5DaysTotalClones": total_5d_clones,
        "last5DaysTotalViews": total_5d_views,
        "last5DaysSumUniqueCloners": sum_unique_cloners,
        "last5DaysSumUniqueVisitors": sum_unique_visitors,
        "last5DaysAvgUniqueClonersPerDay": avg_unique_cloners,
        "last5DaysAvgUniqueVisitorsPerDay": avg_unique_visitors,
        "previous5DaysDates": prev_5_dates,
        "previous5DaysTotalClones": prev_5d_clones,
        "previous5DaysTotalViews": prev_5d_views,
        "cloneDifference": clone_diff,
        "viewDifference": view_diff,
        "cloneGrowthPercent": clone_growth_pct,
        "viewGrowthPercent": view_growth_pct,
        "recordedCumulativeClones": cumulative_clones,
        "recordedCumulativeViews": cumulative_views,
        "recordedCumulativeUniqueCloners": cumulative_unique_cloners,
        "recordedCumulativeUniqueVisitors": cumulative_unique_visitors,
        "collectionStartedAt": history.get("collectionStartedAt"),
        "lastSuccessfulUpdate": history.get("lastSuccessfulUpdate"),
    }


# -----------------------------------------------------------------------------
# Activity History Processing & Deduplication
# -----------------------------------------------------------------------------

def initialize_activity_history() -> Dict[str, Any]:
    return {
        "schemaVersion": 1,
        "lastUpdated": None,
        "events": [],
    }


def merge_activity_events(
    activity_store: Dict[str, Any],
    new_events: List[Dict[str, Any]],
    max_history: int = 50,
) -> List[Dict[str, Any]]:
    existing_events = activity_store.get("events", [])
    events_map: Dict[str, Dict[str, Any]] = {}

    for ev in existing_events:
        eid = ev.get("id")
        if eid:
            events_map[eid] = ev

    for ev in new_events:
        eid = ev.get("id")
        if eid:
            events_map[eid] = ev

    all_events = list(events_map.values())
    all_events.sort(key=lambda x: x.get("timestamp") or "", reverse=True)
    all_events = all_events[:max_history]

    activity_store["events"] = all_events
    activity_store["lastUpdated"] = datetime.now(UTC).isoformat()
    return all_events


# -----------------------------------------------------------------------------
# Premium Visualizations (Theme-Adaptive SVG)
# -----------------------------------------------------------------------------

def generate_metrics_card_svg(
    metrics: Dict[str, Any],
    forks_count: int,
    traffic_status: str,
    output_filepath: Path,
) -> None:
    """
    Generate an ultra-crisp, editorial SVG card displaying the 5 core metrics:
    1. Total Clones (recorded cumulative)
    2. Unique Cloners (5-day aggregate)
    3. Total Forks (current total)
    4. Total Views (recorded cumulative)
    5. Unique Visitors (5-day aggregate)
    """
    output_filepath.parent.mkdir(parents=True, exist_ok=True)

    has_traffic = (
        metrics.get("recordedCumulativeClones", 0) > 0
        or metrics.get("recordedCumulativeViews", 0) > 0
        or metrics.get("availableDaysCount", 0) > 0
    )

    # Values & subtitles (Lifetime Aggregation)
    if has_traffic:
        clones_val = f"{metrics.get('recordedCumulativeClones', 0):,}"
        clones_sub = "Recorded lifetime"
        clones_class = "metric-val"

        u_cloners_val = f"{metrics.get('recordedCumulativeUniqueCloners', 0):,}"
        u_cloners_sub = "Recorded lifetime"
        u_cloners_class = "metric-val"

        views_val = f"{metrics.get('recordedCumulativeViews', 0):,}"
        views_sub = "Recorded lifetime"
        views_class = "metric-val"

        u_visitors_val = f"{metrics.get('recordedCumulativeUniqueVisitors', 0):,}"
        u_visitors_sub = "Recorded lifetime"
        u_visitors_class = "metric-val"
    else:
        clones_val = "—"
        clones_sub = "Collecting data"
        clones_class = "metric-val-dim"

        u_cloners_val = "—"
        u_cloners_sub = "Collecting data"
        u_cloners_class = "metric-val-dim"

        views_val = "—"
        views_sub = "Collecting data"
        views_class = "metric-val-dim"

        u_visitors_val = "—"
        u_visitors_sub = "Collecting data"
        u_visitors_class = "metric-val-dim"

    forks_val = f"{forks_count:,}"
    forks_sub = "Public community"
    forks_class = "metric-val"

    columns = [
        ("TOTAL CLONES", clones_val, clones_sub, clones_class),
        ("UNIQUE CLONERS", u_cloners_val, u_cloners_sub, u_cloners_class),
        ("TOTAL FORKS", forks_val, forks_sub, forks_class),
        ("TOTAL VIEWS", views_val, views_sub, views_class),
        ("UNIQUE VISITORS", u_visitors_val, u_visitors_sub, u_visitors_class),
    ]

    cols_svg = []
    dividers_svg = []
    col_width = 820.0 / len(columns)

    for idx, (label, val, sub, vclass) in enumerate(columns):
        col_center = (idx * col_width) + (col_width / 2.0)
        cols_svg.append(f"""    <g>
      <text x="{col_center:.1f}" y="28" text-anchor="middle" class="metric-label">{label}</text>
      <text x="{col_center:.1f}" y="59" text-anchor="middle" class="{vclass}">{val}</text>
      <text x="{col_center:.1f}" y="79" text-anchor="middle" class="metric-sub">{sub}</text>
    </g>""")
        if idx > 0:
            div_x = idx * col_width
            dividers_svg.append(f'<line x1="{div_x:.1f}" y1="18" x2="{div_x:.1f}" y2="78" class="divider"/>')

    svg_content = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 820 96" width="100%" height="auto">
  <defs>
    <style>
      .card-bg {{ fill: #ffffff; stroke: #d0d7de; stroke-width: 1; }}
      .divider {{ stroke: #eaecef; stroke-width: 1; }}
      .metric-val {{ font: 700 22px -apple-system, BlinkMacSystemFont, "Segoe UI", Inter, Roboto, sans-serif; fill: #1f2328; }}
      .metric-val-dim {{ font: 600 20px -apple-system, BlinkMacSystemFont, "Segoe UI", Inter, Roboto, sans-serif; fill: #8c959f; }}
      .metric-label {{ font: 600 10px -apple-system, BlinkMacSystemFont, "Segoe UI", Inter, Roboto, sans-serif; fill: #656d76; letter-spacing: 0.5px; }}
      .metric-sub {{ font: 400 10px -apple-system, BlinkMacSystemFont, "Segoe UI", Inter, Roboto, sans-serif; fill: #8c959f; }}
      @media (prefers-color-scheme: dark) {{
        .card-bg {{ fill: #0d1117; stroke: #30363d; }}
        .divider {{ stroke: #21262d; }}
        .metric-val {{ fill: #f0f6fc; }}
        .metric-val-dim {{ fill: #6e7681; }}
        .metric-label {{ fill: #8b949e; }}
        .metric-sub {{ fill: #6e7681; }}
      }}
    </style>
  </defs>

  <rect width="820" height="96" rx="8" class="card-bg"/>
  {''.join(dividers_svg)}
  {''.join(cols_svg)}
</svg>
"""
    atomic_write_text(output_filepath, svg_content)


def generate_hall_title_svg(output_filepath: Path) -> None:
    """
    Generate the royal Golden Wings header for the Hall of Appreciation.
    Features majestic symmetrical golden plumage flanking royal typography.
    """
    output_filepath.parent.mkdir(parents=True, exist_ok=True)

    svg_content = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 820 120" width="100%" height="auto">
  <defs>
    <linearGradient id="gold-grad" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#fff2a8"/>
      <stop offset="25%" stop-color="#ffd700"/>
      <stop offset="60%" stop-color="#d48806"/>
      <stop offset="90%" stop-color="#8c5803"/>
      <stop offset="100%" stop-color="#ffd700"/>
    </linearGradient>
    <linearGradient id="gold-bright" x1="0%" y1="0%" x2="0%" y2="100%">
      <stop offset="0%" stop-color="#fff8d1"/>
      <stop offset="45%" stop-color="#ffd700"/>
      <stop offset="100%" stop-color="#b87304"/>
    </linearGradient>
    <linearGradient id="gold-dark" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#d48806"/>
      <stop offset="100%" stop-color="#3d2703"/>
    </linearGradient>
    <filter id="gold-shadow" x="-20%" y="-20%" width="140%" height="140%">
      <feDropShadow dx="0" dy="4" stdDeviation="4" flood-color="#000000" flood-opacity="0.6"/>
    </filter>

    <!-- Detailed Majestic Wing (Left Side) -->
    <g id="majestic-wing">
      <path d="M 0 52 C -18 38, -32 20, -36 -2 C -42 -26, -30 -44, -18 -54 C -22 -44, -22 -28, -14 -12 C -8 3, 2 18, 12 32 Z" fill="url(#gold-bright)" filter="url(#gold-shadow)"/>
      <path d="M -18 -54 C -30 -42, -42 -24, -36 -2 C -32 20, -18 38, 0 52 C -12 38, -24 20, -28 0 C -32 -20, -22 -38, -18 -54 Z" fill="url(#gold-dark)" opacity="0.4"/>
      <path d="M 5 56 C -20 48, -45 32, -62 8 C -74 -10, -78 -28, -74 -36 C -75 -25, -68 -8, -52 10 C -38 26, -18 42, 10 50 Z" fill="url(#gold-grad)" filter="url(#gold-shadow)"/>
      <path d="M 12 60 C -18 56, -55 42, -80 20 C -96 2, -102 -14, -100 -22 C -99 -12, -90 6, -72 24 C -54 40, -22 52, 14 56 Z" fill="url(#gold-bright)" filter="url(#gold-shadow)"/>
      <path d="M 16 66 C -16 64, -60 52, -90 32 C -112 16, -120 -2, -118 -10 C -116 0, -104 18, -84 34 C -62 48, -20 58, 16 62 Z" fill="url(#gold-grad)" filter="url(#gold-shadow)"/>
      <path d="M 18 72 C -14 70, -56 60, -84 44 C -106 30, -114 14, -112 6 C -110 14, -98 30, -78 44 C -56 56, -16 66, 18 68 Z" fill="url(#gold-bright)" filter="url(#gold-shadow)"/>
      <path d="M 18 78 C -12 76, -48 68, -74 54 C -94 42, -102 28, -100 20 C -98 26, -88 40, -68 52 C -48 62, -12 72, 18 74 Z" fill="url(#gold-grad)" filter="url(#gold-shadow)"/>
      <path d="M 16 84 C -8 82, -38 74, -60 62 C -78 52, -84 40, -82 34 C -80 38, -72 50, -54 60 C -36 68, -8 78, 16 80 Z" fill="url(#gold-bright)" filter="url(#gold-shadow)"/>
      <path d="M 12 90 C -6 88, -28 80, -44 70 C -58 60, -62 50, -60 46 C -58 50, -52 58, -38 66 C -24 74, -4 82, 12 85 Z" fill="url(#gold-grad)" filter="url(#gold-shadow)"/>
      <path d="M 18 50 C 6 52, -2 60, 0 70 C 2 78, 12 84, 20 86 C 14 80, 10 74, 12 66 C 14 58, 20 54, 24 50 Z" fill="url(#gold-dark)"/>
      <circle cx="8" cy="65" r="16" fill="url(#gold-bright)" opacity="0.6"/>
    </g>
    
    <style>
      .royal-title {
        font: 800 24px -apple-system, BlinkMacSystemFont, "Segoe UI", Inter, Roboto, sans-serif;
        fill: url(#gold-bright);
        letter-spacing: 4px;
        filter: drop-shadow(0px 2px 8px rgba(212, 136, 6, 0.5));
      }
      .royal-sub {
        font: 600 10px -apple-system, BlinkMacSystemFont, "Segoe UI", Inter, Roboto, sans-serif;
        fill: #8b949e;
        letter-spacing: 3px;
      }
    </style>
  </defs>

  <!-- Left Wing -->
  <g transform="translate(195, 45) scale(0.85)">
    <use href="#majestic-wing"/>
  </g>

  <!-- Right Wing (Mirrored) -->
  <g transform="translate(625, 45) scale(-0.85, 0.85)">
    <use href="#majestic-wing"/>
  </g>

  <!-- Center Crest & Royal Title -->
  <g>
    <path d="M 410 18 L 413 25 L 420 27 L 414 32 L 416 39 L 410 35 L 404 39 L 406 32 L 400 27 L 407 25 Z" fill="url(#gold-bright)"/>
    <text x="410" y="66" text-anchor="middle" class="royal-title">HALL OF APPRECIATION</text>
    <line x1="270" y1="80" x2="550" y2="80" stroke="url(#gold-grad)" stroke-width="1.5" stroke-linecap="round"/>
    <polygon points="410,76 415,80 410,84 405,80" fill="url(#gold-bright)"/>
    <circle cx="270" cy="80" r="2.5" fill="url(#gold-bright)"/>
    <circle cx="550" cy="80" r="2.5" fill="url(#gold-bright)"/>
    <text x="410" y="98" text-anchor="middle" class="royal-sub">HONORARY RECOGNITION WALL</text>
  </g>
</svg>
"""
    atomic_write_text(output_filepath, svg_content)


def generate_hall_banner_svg(supporters_count: int, output_filepath: Path) -> None:
    """
    Generate an animated SVG banner for the Hall of Appreciation.
    Features dual-circulating gold laser lines starting simultaneously from top-center,
    traveling clockwise and counter-clockwise in a continuous loop.
    Clean text with vertical pipe separator and no emojis or duplicate titles.
    """
    output_filepath.parent.mkdir(parents=True, exist_ok=True)

    if supporters_count > 0:
        announcement = "Star or fork this repository to join the wall of appreciation"
    else:
        announcement = "Be the first to star or fork this repository to claim your spot"

    svg_content = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 820 54" width="100%" height="auto">
  <defs>
    <linearGradient id="gold-tracer" x1="0%" y1="0%" x2="100%" y2="0%">
      <stop offset="0%" stop-color="#fff8d1"/>
      <stop offset="30%" stop-color="#ffd700"/>
      <stop offset="70%" stop-color="#ffb300"/>
      <stop offset="100%" stop-color="#fff8d1"/>
    </linearGradient>
    <filter id="gold-glow" x="-20%" y="-20%" width="140%" height="140%">
      <feGaussianBlur stdDeviation="2.5" result="blur"/>
      <feMerge>
        <feMergeNode in="blur"/>
        <feMergeNode in="SourceGraphic"/>
      </feMerge>
    </filter>
    <style>
      .banner-bg {{ fill: #0d1117; stroke: #2a2010; stroke-width: 1.5; }}
      .tracer-line {{
        stroke: url(#gold-tracer);
        stroke-width: 2.5;
        stroke-linecap: round;
        filter: url(#gold-glow);
        fill: none;
        stroke-dasharray: 160 701;
        animation: circuit 3.2s linear infinite;
      }}
      @keyframes circuit {{
        0% {{ stroke-dashoffset: 160; }}
        100% {{ stroke-dashoffset: -701; }}
      }}
      .banner-text {{
        font: 500 13px -apple-system, BlinkMacSystemFont, "Segoe UI", Inter, Roboto, sans-serif;
        fill: #e6edf3;
        letter-spacing: 0.3px;
      }}
      .pipe-sep {{
        font-weight: 700;
        fill: #ffd700;
      }}
      @media (prefers-color-scheme: light) {{
        .banner-bg {{ fill: #fcfaf6; stroke: #e0d0b0; }}
        .banner-text {{ fill: #1f2328; }}
      }}
    </style>
  </defs>

  <!-- Background Card -->
  <rect x="1" y="1" width="818" height="52" rx="8" class="banner-bg"/>

  <!-- Left Circulation (Counter-Clockwise from top-center 410,1 to bottom-center 410,53) -->
  <path d="M 410 1 L 9 1 A 8 8 0 0 0 1 9 L 1 45 A 8 8 0 0 0 9 53 L 410 53" class="tracer-line"/>

  <!-- Right Circulation (Clockwise from top-center 410,1 to bottom-center 410,53) -->
  <path d="M 410 1 L 811 1 A 8 8 0 0 1 819 9 L 819 45 A 8 8 0 0 1 811 53 L 410 53" class="tracer-line"/>

  <!-- Central Clean Announcement Text -->
  <text x="410" y="31" text-anchor="middle" class="banner-text">
    {announcement} <tspan class="pipe-sep"> | </tspan> Updates every day at 12:00 AM IST
  </text>
</svg>
"""
    atomic_write_text(output_filepath, svg_content)


def generate_supporter_card_svg(
    supporter: Dict[str, str],
    output_filepath: Path,
) -> None:
    """
    Generate a royal esports-style golden framed avatar card for a Hall of Famer.
    Includes the angular top crest, beveled gold border, bottom-left '2026' badge,
    embedded avatar image, username, and role badge.
    """
    output_filepath.parent.mkdir(parents=True, exist_ok=True)
    u_raw = str(supporter.get("username", ""))
    u_slug = re.sub(r"[^a-zA-Z0-9_-]", "_", u_raw)
    u = html.escape(u_raw)
    role = html.escape(str(supporter.get("role", "Community Fork")))
    year = html.escape(str(supporter.get("year", "2026")))
    avatar_url = supporter.get("avatarUrl", f"https://github.com/{u_raw}.png?size=96")

    # Fetch avatar bytes and convert to base64 for offline & camo resilience
    avatar_data_uri = avatar_url
    try:
        req = urllib.request.Request(
            avatar_url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            avatar_b64 = base64.b64encode(resp.read()).decode("utf-8")
            avatar_data_uri = f"data:image/png;base64,{avatar_b64}"
    except Exception:
        pass

    svg_content = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 240 280" width="240" height="280">
  <defs>
    <!-- Gold Metallic Gradients -->
    <linearGradient id="frame-gold-main" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#fff8d1"/>
      <stop offset="20%" stop-color="#ffd700"/>
      <stop offset="50%" stop-color="#e09900"/>
      <stop offset="80%" stop-color="#995e00"/>
      <stop offset="100%" stop-color="#ffd700"/>
    </linearGradient>
    <linearGradient id="frame-gold-light" x1="0%" y1="0%" x2="0%" y2="100%">
      <stop offset="0%" stop-color="#fffbe8"/>
      <stop offset="40%" stop-color="#ffd700"/>
      <stop offset="100%" stop-color="#b87304"/>
    </linearGradient>
    <linearGradient id="badge-gold" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#ffe680"/>
      <stop offset="50%" stop-color="#d48806"/>
      <stop offset="100%" stop-color="#ffd700"/>
    </linearGradient>

    <!-- Clip path for the square avatar inside the frame -->
    <clipPath id="avatar-clip-{u_slug}">
      <rect x="47" y="47" width="126" height="126" rx="4"/>
    </clipPath>

    <filter id="royal-shadow-{u_slug}" x="-20%" y="-20%" width="140%" height="140%">
      <feDropShadow dx="0" dy="6" stdDeviation="6" flood-color="#000000" flood-opacity="0.8"/>
    </filter>

    <style>
      .supporter-name {{
        font: 700 15px -apple-system, BlinkMacSystemFont, "Segoe UI", Inter, Roboto, sans-serif;
        fill: #f0f6fc;
        text-shadow: 0 2px 4px rgba(0,0,0,0.8);
      }}
      .role-pill-bg {{
        fill: #161b22;
        stroke: #ffd700;
        stroke-width: 1.2;
      }}
      .role-pill-text {{
        font: 600 11px -apple-system, BlinkMacSystemFont, "Segoe UI", Inter, Roboto, sans-serif;
        fill: #ffd700;
        letter-spacing: 0.5px;
      }}
      .year-text {{
        font: 800 16px "Times New Roman", Georgia, serif;
        fill: #ffdf7e;
        letter-spacing: 1px;
      }}
    </style>
  </defs>

  <!-- ==================== AVATAR (INSIDE FRAME) ==================== -->
  <g filter="url(#royal-shadow-{u_slug})">
    <rect x="47" y="47" width="126" height="126" rx="4" fill="#0d1117"/>
    <image href="{avatar_data_uri}" x="47" y="47" width="126" height="126" clip-path="url(#avatar-clip-{u_slug})" preserveAspectRatio="xMidYMid slice"/>
  </g>

  <!-- ==================== ROYAL GOLDEN FRAME ==================== -->
  <g filter="url(#royal-shadow-{u_slug})">
    <!-- Outer Main Gold Border with Chamfered Corners -->
    <path d="M 54 40 L 166 40 L 180 54 L 180 166 L 166 180 L 54 180 L 40 166 L 40 54 Z"
          fill="none" stroke="url(#frame-gold-main)" stroke-width="8" stroke-linejoin="miter"/>

    <!-- Inner Dark Groove -->
    <path d="M 52 42 L 168 42 L 178 52 L 178 168 L 168 178 L 52 178 L 42 168 L 42 52 Z"
          fill="none" stroke="#000000" stroke-width="2.5" stroke-linejoin="miter"/>

    <!-- Inner Gold Lip -->
    <rect x="45" y="45" width="130" height="130" fill="none" stroke="url(#frame-gold-light)" stroke-width="2"/>

    <!-- Top Angular Crest / Crown -->
    <path d="M 110 32 L 88 18 L 102 18 L 110 24 L 118 24 L 126 18 L 140 18 L 118 32 Z" fill="url(#frame-gold-main)"/>
    <path d="M 98 18 L 110 27 L 122 18 Z" fill="#0d1117"/>

    <!-- Top Left Winged Blade -->
    <path d="M 68 12 L 86 18 L 62 26 L 42 38 L 52 38 L 74 24 L 88 20 Z" fill="url(#frame-gold-light)"/>
    <!-- Top Right Winged Blade (Mirrored) -->
    <path d="M 160 12 L 142 18 L 166 26 L 186 38 L 176 38 L 154 24 L 140 20 Z" fill="url(#frame-gold-light)"/>

    <!-- Detached Shard Accents Under Outer Crest Wings -->
    <polygon points="56,36 68,30 64,36" fill="url(#frame-gold-main)"/>
    <polygon points="172,36 160,30 164,36" fill="url(#frame-gold-main)"/>

    <!-- Bottom Angular Notch -->
    <path d="M 94 178 L 110 188 L 126 178 L 138 178 L 110 194 L 82 178 Z" fill="url(#frame-gold-main)"/>
    <polygon points="98,179 110,186 122,179" fill="#0d1117"/>

    <!-- Bottom-Left Badge: 2026 -->
    <path d="M 28 144 L 68 144 L 72 148 L 72 184 L 68 188 L 28 188 L 24 184 L 24 148 Z"
          fill="#000000" stroke="url(#badge-gold)" stroke-width="4" stroke-linejoin="miter"/>
    
    <rect x="29" y="149" width="38" height="34" fill="#080808" stroke="url(#frame-gold-light)" stroke-width="1.2"/>

    <text x="48" y="172" text-anchor="middle" class="year-text">{year}</text>
  </g>

  <!-- ==================== SUPPORTER USERNAME & ROLE ==================== -->
  <g transform="translate(0, 204)">
    <text x="110" y="14" text-anchor="middle" class="supporter-name">@{u}</text>

    <rect x="35" y="24" width="150" height="24" rx="12" class="role-pill-bg"/>
    <text x="110" y="40" text-anchor="middle" class="role-pill-text">{role}</text>
  </g>
</svg>
"""
    atomic_write_text(output_filepath, svg_content)


def ensure_button_svgs(assets_dir: Path) -> None:
    """Ensure bespoke SVG action buttons exist in assets directory."""
    assets_dir.mkdir(parents=True, exist_ok=True)

    star_svg = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 160 36" width="160" height="36">
  <defs>
    <style>
      .btn-bg { fill: #161b22; stroke: #30363d; stroke-width: 1; transition: all 0.2s ease; }
      .btn-bg:hover { stroke: #e3b341; fill: #1c2128; }
      .btn-text { font: 600 13px -apple-system, BlinkMacSystemFont, "Segoe UI", Inter, Roboto, sans-serif; fill: #f0f6fc; }
      .star-icon { fill: #e3b341; stroke: #e3b341; stroke-width: 1; }
    </style>
  </defs>
  <rect width="158" height="34" x="1" y="1" rx="8" class="btn-bg"/>
  <g transform="translate(16, 9)">
    <polygon points="9,0.5 11.8,6.2 18,7.1 13.5,11.5 14.6,17.7 9,14.7 3.4,17.7 4.5,11.5 0,7.1 6.2,6.2" class="star-icon" transform="scale(0.9)"/>
  </g>
  <text x="44" y="22" class="btn-text">Star Repository</text>
</svg>
"""
    fork_svg = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 145 36" width="145" height="36">
  <defs>
    <style>
      .btn-bg { fill: #161b22; stroke: #30363d; stroke-width: 1; transition: all 0.2s ease; }
      .btn-bg:hover { stroke: #58a6ff; fill: #1c2128; }
      .btn-text { font: 600 13px -apple-system, BlinkMacSystemFont, "Segoe UI", Inter, Roboto, sans-serif; fill: #f0f6fc; }
      .fork-icon { stroke: #58a6ff; stroke-width: 1.8; stroke-linecap: round; stroke-linejoin: round; fill: none; }
    </style>
  </defs>
  <rect width="143" height="34" x="1" y="1" rx="8" class="btn-bg"/>
  <g transform="translate(16, 9)">
    <circle cx="9" cy="14" r="2.5" class="fork-icon"/>
    <circle cx="4" cy="4" r="2.5" class="fork-icon"/>
    <circle cx="14" cy="4" r="2.5" class="fork-icon"/>
    <path d="M4 6.5v1a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2v-1" class="fork-icon"/>
    <path d="M9 9.5v2" class="fork-icon"/>
  </g>
  <text x="44" y="22" class="btn-text">Fork Project</text>
</svg>
"""
    follow_svg = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 205 36" width="205" height="36">
  <defs>
    <style>
      .btn-bg { fill: #161b22; stroke: #30363d; stroke-width: 1; transition: all 0.2s ease; }
      .btn-bg:hover { stroke: #bc8cff; fill: #1c2128; }
      .btn-text { font: 600 13px -apple-system, BlinkMacSystemFont, "Segoe UI", Inter, Roboto, sans-serif; fill: #f0f6fc; }
      .user-icon { stroke: #bc8cff; stroke-width: 1.8; stroke-linecap: round; stroke-linejoin: round; fill: none; }
    </style>
  </defs>
  <rect width="203" height="34" x="1" y="1" rx="8" class="btn-bg"/>
  <g transform="translate(16, 9)">
    <path d="M13 16v-1.5a3 3 0 0 0-3-3H4a3 3 0 0 0-3 3V16" class="user-icon"/>
    <circle cx="7" cy="5" r="3" class="user-icon"/>
    <line x1="16" y1="7" x2="16" y2="13" class="user-icon"/>
    <line x1="13" y1="10" x2="19" y2="10" class="user-icon"/>
  </g>
  <text x="44" y="22" class="btn-text">Follow @shivampimple29</text>
</svg>
"""
    doubt_svg = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 145 36" width="145" height="36">
  <defs>
    <style>
      .btn-bg { fill: #161b22; stroke: #30363d; stroke-width: 1; transition: all 0.2s ease; }
      .btn-bg:hover { stroke: #3fb950; fill: #1c2128; }
      .btn-text { font: 600 13px -apple-system, BlinkMacSystemFont, "Segoe UI", Inter, Roboto, sans-serif; fill: #f0f6fc; }
      .doubt-icon { stroke: #3fb950; stroke-width: 1.8; stroke-linecap: round; stroke-linejoin: round; fill: none; }
    </style>
  </defs>
  <rect width="143" height="34" x="1" y="1" rx="8" class="btn-bg"/>
  <g transform="translate(16, 9)">
    <path d="M14 2H4a2 2 0 0 0-2 2v7a2 2 0 0 0 2 2h2v3l3.5-3H14a2 2 0 0 0 2-2V4a2 2 0 0 0-2-2z" class="doubt-icon"/>
    <path d="M7.5 6a1.5 1.5 0 0 1 2.8.6c0 1-1.3 1.2-1.3 2" class="doubt-icon"/>
    <circle cx="9" cy="11.2" r="0.8" fill="#3fb950"/>
  </g>
  <text x="42" y="22" class="btn-text">Ask a Doubt</text>
</svg>
"""
    atomic_write_text(assets_dir / "star.svg", star_svg)
    atomic_write_text(assets_dir / "fork.svg", fork_svg)
    atomic_write_text(assets_dir / "follow.svg", follow_svg)
    atomic_write_text(assets_dir / "doubt.svg", doubt_svg)


def generate_svg_chart(
    records: List[Dict[str, Any]],
    output_filepath: Path,
    width: int = 820,
    height: int = 125,
) -> None:
    """
    Generate a compact, refined, theme-adaptive 5-day traffic trend visualizer.
    Communicates the relationship between clones and page views with clean restrained styling.
    """
    output_filepath.parent.mkdir(parents=True, exist_ok=True)

    has_records = len(records) > 0
    max_val = max([r.get("clones", 0) for r in records] + [r.get("views", 0) for r in records] + [0]) if has_records else 0

    elements_svg = []

    if not has_records or max_val == 0:
        # Elegant minimal baseline when awaiting traffic data
        elements_svg.append(
            '<text x="410" y="66" text-anchor="middle" class="empty-text">5-day rolling traffic trendline will display here as daily activity occurs</text>'
        )
    else:
        y_max = max(5, int(math.ceil(max_val * 1.25)))
        plot_bottom = 86.0
        plot_top = 40.0
        avail_h = plot_bottom - plot_top

        n_days = len(records)
        slot_w = 720.0 / n_days
        bar_w = min(16.0, (slot_w - 24.0) / 2.0)

        for i, rec in enumerate(records):
            center_x = 50.0 + (i * slot_w) + (slot_w / 2.0)
            date_str = rec.get("date", "")
            try:
                dt_obj = datetime.strptime(date_str, "%Y-%m-%d")
                short_date = dt_obj.strftime("%b %d")
            except Exception:
                short_date = date_str

            clones = rec.get("clones", 0)
            views = rec.get("views", 0)

            v_h = (views / y_max) * avail_h if y_max > 0 else 0
            v_x = center_x - bar_w - 2
            v_y = plot_bottom - v_h

            c_h = (clones / y_max) * avail_h if y_max > 0 else 0
            c_x = center_x + 2
            c_y = plot_bottom - c_h

            # Views bar (Blue)
            elements_svg.append(
                f'<rect x="{v_x:.1f}" y="{v_y:.1f}" width="{bar_w:.1f}" height="{v_h:.1f}" rx="2" fill="#0969da" opacity="0.9">'
                f'<title>{date_str}: {views} views</title></rect>'
            )
            if views > 0:
                elements_svg.append(
                    f'<text x="{v_x + (bar_w/2):.1f}" y="{max(plot_top + 8, v_y - 3):.1f}" '
                    f'class="count-label" text-anchor="middle">{views}</text>'
                )

            # Clones bar (Purple)
            elements_svg.append(
                f'<rect x="{c_x:.1f}" y="{c_y:.1f}" width="{bar_w:.1f}" height="{c_h:.1f}" rx="2" fill="#8250df" opacity="0.9">'
                f'<title>{date_str}: {clones} clones</title></rect>'
            )
            if clones > 0:
                elements_svg.append(
                    f'<text x="{c_x + (bar_w/2):.1f}" y="{max(plot_top + 8, c_y - 3):.1f}" '
                    f'class="count-label" text-anchor="middle">{clones}</text>'
                )

            # Date label below baseline
            elements_svg.append(
                f'<text x="{center_x:.1f}" y="106" class="axis-label" text-anchor="middle">{short_date}</text>'
            )

    svg_content = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="100%" height="auto">
  <defs>
    <style>
      .card-bg {{ fill: #ffffff; stroke: #d0d7de; stroke-width: 1; }}
      .title-text {{ font: 600 11px -apple-system, BlinkMacSystemFont, "Segoe UI", Inter, Roboto, sans-serif; fill: #656d76; letter-spacing: 0.5px; }}
      .legend-text {{ font: 500 11px -apple-system, BlinkMacSystemFont, "Segoe UI", Inter, Roboto, sans-serif; fill: #656d76; }}
      .axis-label {{ font: 500 10px -apple-system, BlinkMacSystemFont, "Segoe UI", Inter, Roboto, sans-serif; fill: #8c959f; }}
      .count-label {{ font: 600 10px -apple-system, BlinkMacSystemFont, "Segoe UI", Inter, Roboto, sans-serif; fill: #1f2328; }}
      .empty-text {{ font: 400 11px -apple-system, BlinkMacSystemFont, "Segoe UI", Inter, Roboto, sans-serif; fill: #8c959f; }}
      .baseline {{ stroke: #eaecef; stroke-width: 1; }}
      @media (prefers-color-scheme: dark) {{
        .card-bg {{ fill: #0d1117; stroke: #30363d; }}
        .title-text {{ fill: #8b949e; }}
        .legend-text {{ fill: #8b949e; }}
        .axis-label {{ fill: #6e7681; }}
        .count-label {{ fill: #f0f6fc; }}
        .empty-text {{ fill: #6e7681; }}
        .baseline {{ stroke: #21262d; }}
      }}
    </style>
  </defs>

  <rect width="{width}" height="{height}" rx="8" class="card-bg"/>

  <text x="24" y="24" class="title-text">5-DAY TRAFFIC TREND</text>

  <g>
    <circle cx="710" cy="20" r="4" fill="#0969da"/>
    <text x="720" y="24" class="legend-text">Views</text>
    <circle cx="765" cy="20" r="4" fill="#8250df"/>
    <text x="775" y="24" class="legend-text">Clones</text>
  </g>

  <line x1="24" y1="88" x2="796" y2="88" class="baseline"/>

  <g>
    {''.join(elements_svg)}
  </g>
</svg>
"""
    atomic_write_text(output_filepath, svg_content)


# -----------------------------------------------------------------------------
# Shields.io Endpoint Badges Generation (Preserved for API consumers)
# -----------------------------------------------------------------------------

def generate_shields_badges(
    metrics: Dict[str, Any],
    shields_dir: Path,
) -> None:
    """Generate Shields.io endpoint badge JSON files for API consumers."""
    shields_dir.mkdir(parents=True, exist_ok=True)

    badges = {
        "total-clones.json": {
            "schemaVersion": 1,
            "label": "total clones",
            "message": str(metrics.get("recordedCumulativeClones", 0)),
            "color": "blue",
        },
        "daily-unique-cloners.json": {
            "schemaVersion": 1,
            "label": "unique cloners",
            "message": str(metrics.get("recordedCumulativeUniqueCloners", 0)),
            "color": "informational",
        },
        "total-views.json": {
            "schemaVersion": 1,
            "label": "total views",
            "message": str(metrics.get("recordedCumulativeViews", 0)),
            "color": "brightgreen",
        },
        "daily-unique-visitors.json": {
            "schemaVersion": 1,
            "label": "unique visitors",
            "message": str(metrics.get("recordedCumulativeUniqueVisitors", 0)),
            "color": "informational",
        },
        "cumulative-clones.json": {
            "schemaVersion": 1,
            "label": "recorded clones",
            "message": str(metrics.get("recordedCumulativeClones", 0)),
            "color": "blue",
        },
        "cumulative-views.json": {
            "schemaVersion": 1,
            "label": "recorded views",
            "message": str(metrics.get("recordedCumulativeViews", 0)),
            "color": "brightgreen",
        },
    }

    for filename, badge_data in badges.items():
        atomic_write_json(shields_dir / filename, badge_data)


# -----------------------------------------------------------------------------
# Markdown Dashboard Rendering & README Integration
# -----------------------------------------------------------------------------

def render_hall_of_appreciation(supporters: List[Dict[str, str]]) -> str:
    """
    Render the Hall of Appreciation recognition gallery.
    Features royal golden wings title, animated dual-circulating gold banner,
    and esports-style golden framed supporter cards.
    Displays verified public supporters with avatar, username, and role.
    Respects strict privacy: never associates anyone with cloning/viewing activity.
    """
    header_markup = (
        '<br><br>\n\n'
        '<p align="center">\n'
        '  <img src=".github/analytics/hall-title.svg" alt="Hall of Appreciation" width="620">\n'
        '</p>\n\n'
        '<p align="center">\n'
        '  <img src=".github/analytics/hall-banner.svg" alt="Hall of Appreciation Banner" width="100%">\n'
        '</p>'
    )
    if not supporters:
        return header_markup

    cells = []
    for s in supporters[:15]:
        u = s.get("username", "")
        p_url = s.get("profileUrl", f"https://github.com/{u}")

        cell = (
            f'    <td align="center" style="border: none; padding: 12px 20px;">\n'
            f'      <a href="{p_url}">\n'
            f'        <img src=".github/analytics/supporters/{u}.svg" width="220" alt="{u}" />\n'
            f'      </a>\n'
            f'    </td>'
        )
        cells.append(cell)

    table_markup = (
        '<table align="center" border="0" style="border: none; border-collapse: collapse;">\n'
        '  <tr>\n' + "\n".join(cells) + "\n  </tr>\n</table>"
    )
    return f"{header_markup}\n\n{table_markup}"


def render_dashboard_markdown(
    repo_name: str,
    default_branch: str,
    traffic_metrics: Dict[str, Any],
    forks_count: int,
    supporters: List[Dict[str, str]],
) -> str:
    """
    Build the refined, minimal public dashboard.
    Shows the 5 core lifetime metrics and the Hall of Appreciation recognition wall.
    """
    hall_markup = render_hall_of_appreciation(supporters)

    section = f"""{START_MARKER}
### Repository Insights

<p align="center">
  <img src=".github/analytics/metrics.svg" alt="Repository Metrics" width="100%">
</p>
{hall_markup}
{END_MARKER}"""

    return section


REPO_METADATA: Dict[str, Tuple[str, str]] = {
    "Ethical-Hacking-and-Forensics-sem5": (
        "Ethical Hacking and Forensics",
        "Practical solutions, lab experiments, and reference implementations for Semester 5 Ethical Hacking and Forensics.",
    ),
    "IoT-sem5": (
        "Internet of Things",
        "Practical solutions, lab experiments, and reference implementations for Semester 5 Internet of Things.",
    ),
    "Security-Lab-semV": (
        "Security Lab",
        "Practical solutions, lab experiments, and reference implementations for Semester 5 Security Lab.",
    ),
    "WebX": (
        "WebX",
        "Practical solutions, lab experiments, and reference implementations for Semester 5 WebX.",
    ),
    "Intelligent-Data-System-sem5": (
        "Intelligent Data Systems",
        "Practical solutions, lab experiments, and reference implementations for Semester 5 Intelligent Data Systems.",
    ),
}


def get_git_remote_repo_name(workspace_root: Path) -> Optional[str]:
    """Extract owner/repo from local git remote origin url if available."""
    try:
        res = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            cwd=str(workspace_root),
            capture_output=True,
            text=True,
            check=False,
        )
        url = res.stdout.strip()
        m = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?$", url)
        if m:
            return m.group(1)
    except Exception:
        pass
    return None


def update_readme_content(
    original_readme: str,
    new_analytics_section: str,
    repo_name: str = "shivampimple29/Intelligent-Data-System-sem5",
    repo_title: Optional[str] = None,
    repo_desc: Optional[str] = None,
) -> str:
    """
    Safely replace the analytics section between START_MARKER and END_MARKER.
    Removes any duplicated Copyright section completely (covered by LICENSE).
    Preserves and refines the interactive Support & Contribute section.
    """
    cleaned = original_readme

    # 1. Remove Copyright section completely if present
    copyright_pattern = re.compile(
        r"## Copyright\s+Copyright © \d{4}.*?(?=(###|<!--|$))",
        re.DOTALL | re.IGNORECASE,
    )
    cleaned = copyright_pattern.sub("", cleaned)

    cleaned = re.sub(
        r"## Copyright\s+.*?(?=(###|<!--|$))",
        "",
        cleaned,
        flags=re.DOTALL | re.IGNORECASE,
    )

    # 2. Refined interactive Support & Contribute section
    support_cleaned = (
        "\n\n---\n\n"
        "### Support & Contribute\n\n"
        "If this repository helps your studies, practicals, or reference work, support the project:\n\n"
        '<p align="left">\n'
        f'  <a href="https://github.com/{repo_name}/stargazers">\n'
        '    <img src=".github/analytics/assets/star.svg" alt="Star Repository" height="36" />\n'
        '  </a>\n'
        '  &nbsp;&nbsp;\n'
        f'  <a href="https://github.com/{repo_name}/fork">\n'
        '    <img src=".github/analytics/assets/fork.svg" alt="Fork Project" height="36" />\n'
        '  </a>\n'
        '  &nbsp;&nbsp;\n'
        '  <a href="https://github.com/shivampimple29">\n'
        '    <img src=".github/analytics/assets/follow.svg" alt="Follow @shivampimple29" height="36" />\n'
        '  </a>\n'
        '  &nbsp;&nbsp;\n'
        f'  <a href="https://github.com/{repo_name}/issues">\n'
        '    <img src=".github/analytics/assets/doubt.svg" alt="Ask a Doubt" height="36" />\n'
        '  </a>\n'
        '</p>\n\n'
        "```bash\n"
        "# Clone this repository\n"
        f"git clone https://github.com/{repo_name}.git\n"
        "```"
    )

    # Strip existing Support section and any preceding dividers
    cleaned = re.sub(
        r"(\n\s*---\s*)*\n*###\s*⭐?\s*Support.*",
        "",
        cleaned,
        flags=re.DOTALL | re.IGNORECASE,
    )
    cleaned = re.sub(r"(\n\s*---\s*)+\Z", "", cleaned.rstrip())

    # Resolve repo metadata
    repo_slug = repo_name.split("/")[-1] if "/" in repo_name else repo_name
    if not repo_title or not repo_desc:
        meta = REPO_METADATA.get(repo_slug)
        if meta:
            repo_title = repo_title or meta[0]
            repo_desc = repo_desc or meta[1]
        else:
            repo_title = repo_title or repo_slug.replace("-", " ").replace("_", " ").title()
            repo_desc = repo_desc or f"Practical solutions, lab experiments, and reference implementations for {repo_title}."

    # 3. Replace or insert analytics block
    marker_pattern = re.compile(f"{re.escape(START_MARKER)}.*?{re.escape(END_MARKER)}", re.DOTALL)
    if marker_pattern.search(cleaned):
        cleaned = marker_pattern.sub(new_analytics_section, cleaned)
    else:
        if cleaned.strip().startswith("#"):
            lines = cleaned.strip().split("\n")
            title = lines[0]
            rest = "\n".join(lines[1:]).strip()
            cleaned = f"{title}\n\n{new_analytics_section}\n\n{rest}"
        else:
            cleaned = f"# {repo_title}\n\n{repo_desc}\n\n{new_analytics_section}\n\n{cleaned}"

    # Ensure clean trailing support section
    cleaned = cleaned.strip()
    if "### Support & Contribute" not in cleaned and "### Support the Project" not in cleaned:
        cleaned = f"{cleaned}\n\n{support_cleaned}"

    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned) + "\n"
    return cleaned


# -----------------------------------------------------------------------------
# Main Analytics Collection Pipeline
# -----------------------------------------------------------------------------

def run_pipeline() -> int:
    """Execute the full analytics collection and dashboard generation pipeline."""
    workspace_root = Path(os.environ.get("GITHUB_WORKSPACE") or Path(__file__).resolve().parents[2])
    repo_name = (
        os.environ.get("GITHUB_REPOSITORY")
        or get_git_remote_repo_name(workspace_root)
        or "shivampimple29/Intelligent-Data-System-sem5"
    )
    default_branch = os.environ.get("GITHUB_REF_NAME") or "main"
    if default_branch.startswith("refs/heads/"):
        default_branch = default_branch.replace("refs/heads/", "")

    gh_traffic_token = os.environ.get("GH_TRAFFIC_STATS_TOKEN")
    github_token = os.environ.get("GITHUB_TOKEN")
    if not gh_traffic_token and not github_token:
        cred_token = get_git_credential_token()
        if cred_token:
            gh_traffic_token = cred_token
            github_token = cred_token

    client = GitHubAPIClient(traffic_token=gh_traffic_token, fallback_token=github_token)

    analytics_dir = workspace_root / ".github" / "analytics"
    assets_dir = analytics_dir / "assets"
    supporters_dir = analytics_dir / "supporters"
    shields_dir = analytics_dir / "shields"
    readme_path = workspace_root / "README.md"
    traffic_history_file = analytics_dir / "traffic-history.json"
    activity_history_file = analytics_dir / "activity-history.json"
    dashboard_file = analytics_dir / "dashboard.json"
    metrics_svg_file = analytics_dir / "metrics.svg"
    hall_title_svg_file = analytics_dir / "hall-title.svg"
    hall_banner_svg_file = analytics_dir / "hall-banner.svg"
    trend_svg_file = analytics_dir / "traffic-trend.svg"
    chart_file = analytics_dir / "traffic-chart.svg"  # Preserved for backward compatibility

    now_utc_iso, now_ist_str = get_current_timestamps()
    print(f"=== Starting Redesigned Analytics Pipeline ===")
    print(f"Repository: {repo_name} (Branch: {default_branch})")
    print(f"Timestamp:  {now_utc_iso} ({now_ist_str})")

    # 1. Fetch Repository Metadata
    print("\n[1/5] Fetching repository metadata...")
    status, repo_data, _ = client.request(f"repos/{repo_name}", token=client.general_token)
    if status != 200 or not repo_data:
        print(f"[ERROR] Failed to fetch repository metadata (Status {status}): {repo_data}")
        repo_data = {}

    forks_count = int(repo_data.get("forks_count", 0))

    # 2. Fetch Traffic Data (Clones & Views)
    print("[2/5] Fetching repository traffic data...")
    traffic_status = "success"
    clones_payload = None
    views_payload = None

    c_status, c_data, _ = client.request(f"repos/{repo_name}/traffic/clones?per=day", token=client.traffic_token)
    if c_status == 200:
        clones_payload = c_data
    elif c_status in (401, 403):
        print(f"[NOTICE] Traffic API requires GH_TRAFFIC_STATS_TOKEN (Status {c_status}). Preserving existing history.")
        traffic_status = "token_required"
    else:
        traffic_status = "error"

    v_status, v_data, _ = client.request(f"repos/{repo_name}/traffic/views?per=day", token=client.traffic_token)
    if v_status == 200:
        views_payload = v_data
    elif v_status in (401, 403):
        traffic_status = "token_required"
    else:
        traffic_status = "error"

    # 3. Update Traffic History
    print("[3/5] Processing traffic history...")
    traffic_history = safe_load_json(traffic_history_file, initialize_traffic_history)
    merge_traffic_data(traffic_history, clones_payload, views_payload, now_utc_iso)
    atomic_write_json(traffic_history_file, traffic_history)

    activity_history = safe_load_json(activity_history_file, initialize_activity_history)
    activity_history["lastUpdated"] = now_utc_iso
    atomic_write_json(activity_history_file, activity_history)

    traffic_metrics = compute_traffic_metrics(traffic_history)

    # 4. Fetch Verified Public Supporters for Hall of Appreciation
    print("[4/5] Identifying verified public supporters...")
    supporters_map: Dict[str, Dict[str, str]] = {}
    current_year = str(now_ist_str[:4]) if now_ist_str else "2026"

    # Public Forks
    f_status, forks_data, _ = client.request(f"repos/{repo_name}/forks?sort=newest&per_page=50", token=client.general_token)
    if f_status == 200 and isinstance(forks_data, list):
        for fork in forks_data:
            owner = fork.get("owner", {})
            u_login = owner.get("login")
            if u_login:
                created_at = fork.get("created_at") or ""
                fork_year = created_at[:4] if len(created_at) >= 4 and created_at[:4].isdigit() else current_year
                supporters_map[u_login] = {
                    "username": u_login,
                    "profileUrl": owner.get("html_url", f"https://github.com/{u_login}"),
                    "avatarUrl": f"https://github.com/{u_login}.png?size=96",
                    "role": "Community Fork",
                    "year": fork_year,
                }

    # Public Stargazers
    s_status, stars_data, _ = client.request(
        f"repos/{repo_name}/stargazers",
        token=client.general_token,
        accept="application/vnd.github.star+json",
    )
    if s_status == 200 and isinstance(stars_data, list):
        for star in stars_data:
            user = star.get("user", {})
            u_login = user.get("login")
            if u_login:
                starred_at = star.get("starred_at") or ""
                star_year = starred_at[:4] if len(starred_at) >= 4 and starred_at[:4].isdigit() else current_year
                if u_login in supporters_map:
                    supporters_map[u_login]["role"] = "Community Star & Fork"
                    existing_year = supporters_map[u_login].get("year", current_year)
                    if star_year < existing_year:
                        supporters_map[u_login]["year"] = star_year
                else:
                    supporters_map[u_login] = {
                        "username": u_login,
                        "profileUrl": user.get("html_url", f"https://github.com/{u_login}"),
                        "avatarUrl": f"https://github.com/{u_login}.png?size=96",
                        "role": "Community Star",
                        "year": star_year,
                    }

    supporters = list(supporters_map.values())

    # 5. Generate Visualizations, Snapshot & Update README
    print("[5/5] Generating premium visuals and updating README...")
    # Ensure Action Button SVGs
    ensure_button_svgs(assets_dir)

    # Generate Metrics Card, Golden Wings Header & Circulating Banner SVGs
    generate_metrics_card_svg(traffic_metrics, forks_count, traffic_status, metrics_svg_file)
    generate_hall_title_svg(hall_title_svg_file)
    generate_hall_banner_svg(len(supporters), hall_banner_svg_file)

    # Generate Golden Frame Supporter Cards
    supporters_dir.mkdir(parents=True, exist_ok=True)
    for s in supporters:
        u = s.get("username", "")
        if u:
            card_file = supporters_dir / f"{u}.svg"
            generate_supporter_card_svg(s, card_file)

    # Generate Traffic Trend SVGs
    generate_svg_chart(traffic_metrics.get("last5DaysRecords", []), trend_svg_file)
    generate_svg_chart(traffic_metrics.get("last5DaysRecords", []), chart_file)

    # Generate Badges (for API backwards compatibility)
    generate_shields_badges(traffic_metrics, shields_dir)

    # Update Dashboard JSON
    dashboard_snapshot = {
        "repository": repo_name,
        "defaultBranch": default_branch,
        "updatedAtUTC": now_utc_iso,
        "updatedAtIST": now_ist_str,
        "trafficStatus": traffic_status,
        "trafficMetrics": traffic_metrics,
        "forksCount": forks_count,
        "supporters": supporters,
    }
    atomic_write_json(dashboard_file, dashboard_snapshot)

    # Update README
    existing_readme = ""
    if readme_path.exists():
        with open(readme_path, "r", encoding="utf-8") as f:
            existing_readme = f.read()

    new_section = render_dashboard_markdown(
        repo_name=repo_name,
        default_branch=default_branch,
        traffic_metrics=traffic_metrics,
        forks_count=forks_count,
        supporters=supporters,
    )

    updated_readme = update_readme_content(existing_readme, new_section, repo_name=repo_name)
    atomic_write_text(readme_path, updated_readme)

    print("\n=== Analytics Pipeline Completed Successfully ===")
    print(f"Metrics: {forks_count} Forks | Clones: {traffic_metrics.get('recordedCumulativeClones', 0)} | Views: {traffic_metrics.get('recordedCumulativeViews', 0)}")
    print(f"Hall of Appreciation: {len(supporters)} verified supporter(s)")

    return 0


if __name__ == "__main__":
    sys.exit(run_pipeline())
