#!/usr/bin/env python3
"""
Comprehensive Unit & Integration Test Suite for Analytics Dashboard
Tests all 17 requirements specified in the project specification,
including the redesigned visual layout, Hall of Appreciation, and copyright removal.
"""

import json
import os
import shutil
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))

from update_analytics import (
    GitHubAPIClient,
    START_MARKER,
    END_MARKER,
    atomic_write_json,
    atomic_write_text,
    safe_load_json,
    initialize_traffic_history,
    merge_traffic_data,
    compute_traffic_metrics,
    initialize_activity_history,
    merge_activity_events,
    generate_metrics_card_svg,
    generate_hall_title_svg,
    generate_hall_banner_svg,
    generate_supporter_card_svg,
    ensure_button_svgs,
    generate_svg_chart,
    generate_shields_badges,
    render_hall_of_appreciation,
    render_dashboard_markdown,
    update_readme_content,
    parse_iso_datetime,
    format_ist,
    IST,
    UTC,
)


class TestAnalyticsDashboard(unittest.TestCase):

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.test_path = Path(self.test_dir)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_01_first_run_no_history(self):
        """1. A first run with no historical data."""
        history = initialize_traffic_history()
        self.assertIsNone(history["collectionStartedAt"])
        self.assertEqual(history["daily"], {})

        clones = {"count": 5, "uniques": 2, "clones": [{"timestamp": "2026-10-20T00:00:00Z", "count": 5, "uniques": 2}]}
        views = {"count": 20, "uniques": 8, "views": [{"timestamp": "2026-10-20T00:00:00Z", "count": 20, "uniques": 8}]}

        updated = merge_traffic_data(history, clones, views, "2026-10-20T12:00:00Z")
        self.assertTrue(updated)
        self.assertEqual(history["collectionStartedAt"], "2026-10-20T12:00:00Z")
        self.assertIn("2026-10-20", history["daily"])
        self.assertEqual(history["daily"]["2026-10-20"]["clones"], 5)
        self.assertEqual(history["daily"]["2026-10-20"]["views"], 20)

    def test_02_normal_daily_update(self):
        """2. A normal daily update."""
        history = initialize_traffic_history()
        merge_traffic_data(history, {"clones": [{"timestamp": "2026-10-19T00:00:00Z", "count": 4, "uniques": 2}]}, None, "2026-10-19T10:00:00Z")
        self.assertEqual(len(history["daily"]), 1)

        merge_traffic_data(history, {"clones": [{"timestamp": "2026-10-20T00:00:00Z", "count": 7, "uniques": 3}]}, None, "2026-10-20T10:00:00Z")
        self.assertEqual(len(history["daily"]), 2)
        self.assertEqual(history["daily"]["2026-10-19"]["clones"], 4)
        self.assertEqual(history["daily"]["2026-10-20"]["clones"], 7)

    def test_03_reprocessing_same_date_no_double_counting(self):
        """3. Reprocessing the same date without double counting."""
        history = initialize_traffic_history()
        payload = {"clones": [{"timestamp": "2026-10-20T00:00:00Z", "count": 10, "uniques": 4}]}

        merge_traffic_data(history, payload, None, "2026-10-20T10:00:00Z")
        self.assertEqual(history["daily"]["2026-10-20"]["clones"], 10)

        merge_traffic_data(history, payload, None, "2026-10-20T15:00:00Z")
        self.assertEqual(history["daily"]["2026-10-20"]["clones"], 10)
        self.assertEqual(len(history["daily"]), 1)

    def test_04_updates_to_earlier_dates_from_github(self):
        """4. Updates to earlier dates returned by GitHub (revision within reporting window)."""
        history = initialize_traffic_history()
        merge_traffic_data(history, {"clones": [{"timestamp": "2026-10-18T00:00:00Z", "count": 5, "uniques": 2}]}, None, "2026-10-18T10:00:00Z")

        merge_traffic_data(history, {"clones": [{"timestamp": "2026-10-18T00:00:00Z", "count": 9, "uniques": 4}]}, None, "2026-10-19T10:00:00Z")
        self.assertEqual(history["daily"]["2026-10-18"]["clones"], 9)
        self.assertEqual(history["daily"]["2026-10-18"]["uniqueCloners"], 4)

    def test_05_missing_traffic_day(self):
        """5. A missing traffic day handled without fabricating data."""
        history = initialize_traffic_history()
        history["daily"] = {
            "2026-10-16": {"clones": 2, "uniqueCloners": 1, "views": 5, "uniqueVisitors": 3},
            "2026-10-18": {"clones": 4, "uniqueCloners": 2, "views": 10, "uniqueVisitors": 5},
        }
        metrics = compute_traffic_metrics(history)
        self.assertEqual(metrics["availableDaysCount"], 2)
        self.assertFalse(metrics["isWindowComplete"])
        dates = [r["date"] for r in metrics["last5DaysRecords"]]
        self.assertEqual(dates, ["2026-10-16", "2026-10-18"])

    def test_06_temporary_api_failure_and_retries(self):
        """6. Bounded retries for temporary API failures."""
        client = GitHubAPIClient()
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.headers = {}
        mock_response.read.return_value = b'{"ok": true}'
        mock_response.__enter__.return_value = mock_response
        mock_response.__exit__.return_value = None

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_err = urllib.error.HTTPError("http://api", 500, "Server Error", {}, None)
            mock_err.read = MagicMock(return_value=b'{"error": "temp"}')
            mock_urlopen.side_effect = [mock_err, mock_response]

            with patch("time.sleep", return_value=None):
                status, data, _ = client.request("test-endpoint", max_retries=2, backoff_sec=0.01)
                self.assertEqual(status, 200)
                self.assertEqual(data, {"ok": True})
                self.assertEqual(mock_urlopen.call_count, 2)

    def test_07_invalid_json_file(self):
        """7. Handling an invalid/corrupted JSON file safely."""
        corrupt_file = self.test_path / "corrupt.json"
        corrupt_file.write_text("{ unclosed json content ...", encoding="utf-8")

        data = safe_load_json(corrupt_file, initialize_traffic_history)
        self.assertEqual(data["schemaVersion"], 1)
        backups = list(self.test_path.glob("corrupt.json.corrupt-*"))
        self.assertEqual(len(backups), 1)

    def test_08_partial_endpoint_failure(self):
        """8. Partial failure: traffic endpoint fails (e.g. 403), history is preserved."""
        history = initialize_traffic_history()
        history["daily"] = {
            "2026-10-19": {"clones": 15, "uniqueCloners": 5, "views": 40, "uniqueVisitors": 12}
        }
        views_payload = {"views": [{"timestamp": "2026-10-20T00:00:00Z", "count": 25, "uniques": 8}]}
        updated = merge_traffic_data(history, None, views_payload, "2026-10-20T12:00:00Z")

        self.assertTrue(updated)
        self.assertEqual(history["daily"]["2026-10-19"]["clones"], 15)
        self.assertEqual(history["daily"]["2026-10-20"]["views"], 25)

    def test_09_five_day_aggregation(self):
        """9. 5-day aggregation arithmetic."""
        history = initialize_traffic_history()
        for i in range(1, 6):
            d_str = f"2026-10-0{i}"
            history["daily"][d_str] = {
                "clones": i * 2,          # 2, 4, 6, 8, 10 -> sum = 30
                "uniqueCloners": i,       # 1, 2, 3, 4, 5  -> sum = 15
                "views": i * 10,          # 10, 20, 30, 40, 50 -> sum = 150
                "uniqueVisitors": i * 2,  # 2, 4, 6, 8, 10 -> sum = 30
            }
        metrics = compute_traffic_metrics(history)
        self.assertEqual(metrics["last5DaysTotalClones"], 30)
        self.assertEqual(metrics["last5DaysTotalViews"], 150)
        self.assertEqual(metrics["last5DaysSumUniqueCloners"], 15)
        self.assertEqual(metrics["last5DaysSumUniqueVisitors"], 30)
        self.assertTrue(metrics["isWindowComplete"])

    def test_10_previous_five_day_comparison(self):
        """10. Previous 5-day comparison and growth calculation."""
        history = initialize_traffic_history()
        for i in range(1, 6):
            history["daily"][f"2026-10-0{i}"] = {"clones": 2, "uniqueCloners": 1, "views": 10, "uniqueVisitors": 5}
        for i in range(6, 11):
            history["daily"][f"2026-10-{i:02d}"] = {"clones": 4, "uniqueCloners": 2, "views": 20, "uniqueVisitors": 10}

        metrics = compute_traffic_metrics(history)
        self.assertEqual(metrics["last5DaysTotalClones"], 20)
        self.assertEqual(metrics["previous5DaysTotalClones"], 10)
        self.assertEqual(metrics["cloneGrowthPercent"], 100.0)

        self.assertEqual(metrics["last5DaysTotalViews"], 100)
        self.assertEqual(metrics["previous5DaysTotalViews"], 50)
        self.assertEqual(metrics["viewGrowthPercent"], 100.0)

    def test_11_cumulative_recorded_totals(self):
        """11. Cumulative recorded totals across full historical record."""
        history = initialize_traffic_history()
        for i in range(1, 21):
            history["daily"][f"2026-09-{i:02d}"] = {"clones": 3, "uniqueCloners": 1, "views": 12, "uniqueVisitors": 4}

        metrics = compute_traffic_metrics(history)
        self.assertEqual(metrics["recordedCumulativeClones"], 60)
        self.assertEqual(metrics["recordedCumulativeViews"], 240)
        self.assertEqual(metrics["recordedCumulativeUniqueCloners"], 20)
        self.assertEqual(metrics["recordedCumulativeUniqueVisitors"], 80)

    def test_12_activity_deduplication(self):
        """12. Activity event deduplication using stable IDs."""
        store = initialize_activity_history()
        events_batch_1 = [
            {"id": "fork-101", "action": "Fork", "actor": "alice", "timestamp": "2026-10-20T10:00:00Z"},
            {"id": "commit-aaa", "action": "Commit", "actor": "bob", "timestamp": "2026-10-20T11:00:00Z"},
        ]
        merge_activity_events(store, events_batch_1)
        self.assertEqual(len(store["events"]), 2)

        events_batch_2 = [
            {"id": "fork-101", "action": "Fork", "actor": "alice", "timestamp": "2026-10-20T10:00:00Z"},
            {"id": "star-charlie", "action": "Star", "actor": "charlie", "timestamp": "2026-10-20T12:00:00Z"},
        ]
        merged = merge_activity_events(store, events_batch_2)
        self.assertEqual(len(merged), 3)
        ids = [e["id"] for e in merged]
        self.assertEqual(ids, ["star-charlie", "commit-aaa", "fork-101"])

    def test_13_date_formatting_and_ist(self):
        """13. Correct date parsing and IST conversion."""
        utc_ts = "2026-10-20T18:00:00Z"
        dt = parse_iso_datetime(utc_ts)
        self.assertIsNotNone(dt)
        ist_str = format_ist(dt)
        self.assertEqual(ist_str, "2026-10-20 23:30:00 IST")

    def test_14_readme_marker_replacement_and_copyright_removal(self):
        """14. README marker replacement removes duplicate Copyright section and preserves Support."""
        original = (
            "# Intelligent Data Systems\n\n"
            "## Copyright\n\n"
            "Copyright © 2026 Shivam S. Pimple. All rights reserved.\n\n"
            "This repository is created for educational and reference purposes.\n"
            "You may view the contents for learning and reference, but please do not\n"
            "claim, copy, redistribute, or present this work as your own.\n\n"
            "### ⭐ Support the Project\n\n"
            "If you find this repository useful, please consider **starring ⭐ the repository**\n"
            "and **following me on GitHub** for more projects and updates.\n\n"
            "Your support is appreciated!\n"
        )
        new_section = f"{START_MARKER}\n### Repository Insights\n{END_MARKER}"

        updated = update_readme_content(original, new_section)
        # Verify Copyright is COMPLETELY removed
        self.assertNotIn("## Copyright", updated)
        self.assertNotIn("All rights reserved", updated)
        self.assertNotIn("educational and reference purposes", updated)

        # Verify new analytics section is present
        self.assertIn("### Repository Insights", updated)

        # Verify Support section is preserved in clean editorial style
        self.assertIn("### Support & Contribute", updated)
        self.assertIn("git clone", updated)

    def test_15_valid_generated_json_and_svg(self):
        """15. Valid generated JSON files and theme-adaptive SVGs."""
        metrics_file = self.test_path / "metrics.svg"
        trend_file = self.test_path / "traffic-trend.svg"

        metrics = {
            "recordedCumulativeClones": 42,
            "recordedCumulativeViews": 180,
            "last5DaysSumUniqueCloners": 10,
            "last5DaysSumUniqueVisitors": 45,
            "availableDaysCount": 5,
        }
        generate_metrics_card_svg(metrics, forks_count=1, traffic_status="success", output_filepath=metrics_file)
        self.assertTrue(metrics_file.exists())
        m_content = metrics_file.read_text(encoding="utf-8")
        self.assertTrue(m_content.startswith("<svg"))
        self.assertTrue(m_content.endswith("</svg>\n"))
        self.assertIn("TOTAL CLONES", m_content)
        self.assertIn("42", m_content)
        self.assertIn("TOTAL FORKS", m_content)
        self.assertIn("prefers-color-scheme: dark", m_content)

        records = [
            {"date": "2026-10-18", "clones": 3, "uniqueCloners": 2, "views": 15, "uniqueVisitors": 6},
            {"date": "2026-10-19", "clones": 5, "uniqueCloners": 3, "views": 25, "uniqueVisitors": 10},
        ]
        generate_svg_chart(records, trend_file)
        self.assertTrue(trend_file.exists())
        t_content = trend_file.read_text(encoding="utf-8")
        self.assertTrue(t_content.startswith("<svg"))
        self.assertTrue(t_content.endswith("</svg>\n"))
        self.assertIn("5-DAY TRAFFIC TREND", t_content)

    def test_16_git_changes_filter(self):
        """16. Verify workflow targets only .github/analytics/ and README.md."""
        workflow_file = Path(__file__).resolve().parents[1] / "workflows" / "repository-analytics.yml"
        self.assertTrue(workflow_file.exists())
        wf_text = workflow_file.read_text(encoding="utf-8")
        self.assertIn("git add .github/analytics/ README.md", wf_text)
        self.assertIn("[skip ci]", wf_text)

    def test_17_hall_of_appreciation_and_clean_layout(self):
        """17. Hall of Appreciation renders verified public supporters without activity dumps."""
        supporters = [
            {
                "username": "abhi220705",
                "profileUrl": "https://github.com/abhi220705",
                "avatarUrl": "https://github.com/abhi220705.png?size=96",
                "role": "Community Fork",
            }
        ]
        markdown = render_dashboard_markdown(
            repo_name="shivampimple29/Intelligent-Data-System-sem5",
            default_branch="main",
            traffic_metrics={"availableDaysCount": 0},
            forks_count=1,
            supporters=supporters,
        )

        self.assertIn("### Repository Insights", markdown)
        self.assertIn(".github/analytics/metrics.svg", markdown)
        self.assertNotIn(".github/analytics/traffic-trend.svg", markdown)
        self.assertIn(".github/analytics/hall-title.svg", markdown)
        self.assertIn(".github/analytics/hall-banner.svg", markdown)
        self.assertIn(".github/analytics/supporters/abhi220705.svg", markdown)

        # Ensure NO maintainer activity dump or commit messages in public dashboard
        self.assertNotIn("Commit 📦", markdown)
        self.assertNotIn("chore(analytics)", markdown)
        self.assertNotIn("Traffic API Authentication Setup", markdown)

    def test_18_hall_banner_and_buttons(self):
        """18. Verify hall banner SVG generation and bespoke action buttons."""
        banner_file = self.test_path / "hall-banner.svg"
        generate_hall_banner_svg(1, banner_file)
        self.assertTrue(banner_file.exists())
        b_content = banner_file.read_text(encoding="utf-8")
        self.assertIn("tracer-line", b_content)
        self.assertIn("Updates every day at 12:00 AM UTC", b_content)
        self.assertIn("pipe-sep", b_content)

        assets_dir = self.test_path / "assets"
        ensure_button_svgs(assets_dir)
        self.assertTrue((assets_dir / "star.svg").exists())
        self.assertTrue((assets_dir / "fork.svg").exists())
        self.assertTrue((assets_dir / "follow.svg").exists())
        self.assertTrue((assets_dir / "doubt.svg").exists())

        readme_text = update_readme_content("# Test Readme", "<!-- REPOSITORY_ANALYTICS_START -->\n<!-- REPOSITORY_ANALYTICS_END -->")
        self.assertIn(".github/analytics/assets/star.svg", readme_text)
        self.assertIn(".github/analytics/assets/fork.svg", readme_text)
        self.assertIn(".github/analytics/assets/follow.svg", readme_text)
        self.assertIn(".github/analytics/assets/doubt.svg", readme_text)
        self.assertIn("/issues", readme_text)
        self.assertNotIn("img.shields.io/badge/⭐_Star_Repository", readme_text)

    def test_19_hall_title_and_supporter_card(self):
        """19. Verify royal golden wings title and esports frame supporter card with dynamic year."""
        title_file = self.test_path / "hall-title.svg"
        generate_hall_title_svg(title_file)
        self.assertTrue(title_file.exists())
        t_content = title_file.read_text(encoding="utf-8")
        self.assertIn("HALL OF APPRECIATION", t_content)
        self.assertIn("majestic-wing", t_content)

        supporter_2026 = {
            "username": "abhi220705",
            "profileUrl": "https://github.com/abhi220705",
            "avatarUrl": "https://github.com/abhi220705.png?size=96",
            "role": "Community Fork",
            "year": "2026",
        }
        card_file_2026 = self.test_path / "abhi220705.svg"
        generate_supporter_card_svg(supporter_2026, card_file_2026)
        self.assertTrue(card_file_2026.exists())
        c_content_2026 = card_file_2026.read_text(encoding="utf-8")
        self.assertIn("2026", c_content_2026)
        self.assertIn("@abhi220705", c_content_2026)
        self.assertIn("Community Fork", c_content_2026)

        # Verify dynamic future year (e.g., 2027 supporter)
        supporter_2027 = {
            "username": "futureUser",
            "profileUrl": "https://github.com/futureUser",
            "avatarUrl": "https://github.com/futureUser.png?size=96",
            "role": "Community Star",
            "year": "2027",
        }
        card_file_2027 = self.test_path / "futureUser.svg"
        generate_supporter_card_svg(supporter_2027, card_file_2027)
        self.assertTrue(card_file_2027.exists())
        c_content_2027 = card_file_2027.read_text(encoding="utf-8")
        self.assertIn("2027", c_content_2027)
        self.assertIn("@futureUser", c_content_2027)


if __name__ == "__main__":
    unittest.main()
