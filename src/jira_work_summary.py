"""Extract a date-bounded Jira work summary for the current user.

Usage:
    python src/jira_work_summary.py --from 2026-07-01 --to 2026-09-02

Credentials are read from environment variables. Set JIRA_URL and either
JIRA_USERNAME plus JIRA_API_TOKEN/JIRA_PASSWORD, or JIRA_TOKEN for bearer auth.
"""
from __future__ import annotations

import argparse
import os
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Iterable

import requests


@dataclass(frozen=True)
class Activity:
    when: datetime
    issue_key: str
    kind: str
    text: str


class JiraClient:
    def __init__(self, base_url: str, api_version: str, username: str | None, secret: str | None, bearer_token: str | None):
        self.base_url = base_url.rstrip("/")
        self.api_base = f"{self.base_url}/rest/api/{api_version}"
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})
        if bearer_token:
            self.session.headers.update({"Authorization": f"Bearer {bearer_token}"})
        elif username and secret:
            self.session.auth = (username, secret)
        else:
            raise SystemExit(
                "Set JIRA_TOKEN for bearer auth, or set JIRA_USERNAME and "
                "JIRA_API_TOKEN/JIRA_PASSWORD for basic auth."
            )

    def search_issues(self, jql: str, fields: list[str], max_issues: int | None = None) -> list[dict[str, Any]]:
        issues: list[dict[str, Any]] = []
        start_at = 0
        page_size = 100
        while True:
            if max_issues is not None:
                page_size = min(page_size, max_issues - len(issues))
                if page_size <= 0:
                    break
            response = self.session.get(
                f"{self.api_base}/search",
                params={
                    "jql": jql,
                    "fields": ",".join(fields),
                    "startAt": start_at,
                    "maxResults": page_size,
                },
                timeout=60,
            )
            response.raise_for_status()
            payload = response.json()
            page = payload.get("issues", [])
            issues.extend(page)
            total = payload.get("total", len(issues))
            if not page or len(issues) >= total or (max_issues is not None and len(issues) >= max_issues):
                break
            start_at += len(page)
        return issues

    def issue_worklogs(self, issue_key: str) -> list[dict[str, Any]]:
        return self._paged_issue_collection(issue_key, "worklog", "worklogs")

    def issue_changelog(self, issue_key: str) -> list[dict[str, Any]]:
        return self._paged_issue_collection(issue_key, "changelog", "values")

    def _paged_issue_collection(self, issue_key: str, collection: str, item_key: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        start_at = 0
        while True:
            response = self.session.get(
                f"{self.api_base}/issue/{issue_key}/{collection}",
                params={"startAt": start_at, "maxResults": 100},
                timeout=60,
            )
            response.raise_for_status()
            payload = response.json()
            page = payload.get(item_key, [])
            items.extend(page)
            total = payload.get("total", len(items))
            if not page or len(items) >= total:
                break
            start_at += len(page)
        return items


def default_start() -> date:
    today = date.today()
    return date(today.year, 7, 1)


def parse_day(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def parse_jira_datetime(value: str) -> datetime:
    return datetime.strptime(value.replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S.%f%z").replace(tzinfo=None)


def in_range(moment: datetime, start: date, end: date) -> bool:
    return datetime.combine(start, time.min) <= moment <= datetime.combine(end, time.max)


def author_values(author: dict[str, Any] | None) -> set[str]:
    if not author:
        return set()
    values = set()
    for key in ("accountId", "name", "key", "emailAddress", "displayName"):
        value = author.get(key)
        if value:
            values.add(str(value).casefold())
    return values


def author_matches(author: dict[str, Any] | None, identities: set[str]) -> bool:
    if not identities:
        return True
    return bool(author_values(author) & identities)


def field_name(issue: dict[str, Any], field: str, fallback: str = "Unspecified") -> str:
    value = issue.get("fields", {}).get(field)
    if isinstance(value, dict):
        return str(value.get("name") or value.get("displayName") or fallback)
    if value:
        return str(value)
    return fallback


def worklog_text(worklog: dict[str, Any]) -> str:
    seconds = int(worklog.get("timeSpentSeconds") or 0)
    hours = seconds / 3600
    comment = worklog.get("comment")
    if isinstance(comment, str) and comment.strip():
        return f"Logged {hours:.2f}h - {comment.strip()}"
    return f"Logged {hours:.2f}h"


def changelog_text(history: dict[str, Any]) -> str:
    changes = []
    for item in history.get("items", []):
        field = item.get("field") or "field"
        before = item.get("fromString") or "empty"
        after = item.get("toString") or "empty"
        changes.append(f"{field}: {before} -> {after}")
    return "; ".join(changes) or "Updated issue"


def build_jql(start: date, end: date) -> str:
    start_text = start.isoformat()
    end_text = end.isoformat()
    return (
        "(assignee = currentUser() OR reporter = currentUser() OR worklogAuthor = currentUser()) "
        f'AND (updated >= "{start_text}" OR worklogDate >= "{start_text}") '
        f'AND (updated <= "{end_text}" OR worklogDate <= "{end_text}") '
        "ORDER BY updated DESC"
    )


def collect_activities(client: JiraClient, issues: Iterable[dict[str, Any]], identities: set[str], start: date, end: date) -> list[Activity]:
    activities: list[Activity] = []
    for issue in issues:
        issue_key = issue["key"]
        for worklog in client.issue_worklogs(issue_key):
            started = parse_jira_datetime(worklog["started"])
            if in_range(started, start, end) and author_matches(worklog.get("author"), identities):
                activities.append(Activity(started, issue_key, "worklog", worklog_text(worklog)))

        for history in client.issue_changelog(issue_key):
            created = parse_jira_datetime(history["created"])
            if in_range(created, start, end) and author_matches(history.get("author"), identities):
                activities.append(Activity(created, issue_key, "change", changelog_text(history)))
    return sorted(activities, key=lambda activity: activity.when, reverse=True)


def issue_line(issue: dict[str, Any]) -> str:
    fields = issue.get("fields", {})
    summary = fields.get("summary", "No summary")
    status = field_name(issue, "status")
    issue_type = field_name(issue, "issuetype")
    updated = fields.get("updated", "unknown")
    return f"- **{issue['key']}** [{issue_type}] {summary} - {status} (updated {updated})"


def render_report(issues: list[dict[str, Any]], activities: list[Activity], start: date, end: date, jql: str) -> str:
    by_month: dict[str, list[Activity]] = defaultdict(list)
    for activity in activities:
        by_month[activity.when.strftime("%Y-%m")].append(activity)

    lines = [
        "# Jira Work Summary",
        f"Generated: {datetime.now().isoformat(timespec='seconds')}",
        f"Range: {start.isoformat()} to {end.isoformat()}",
        "",
        "## Pinned Summary",
        f"- Jira issues found: {len(issues)}",
        f"- Dated activities found: {len(activities)}",
        f"- Worklog entries: {sum(1 for activity in activities if activity.kind == 'worklog')}",
        f"- Issue changes: {sum(1 for activity in activities if activity.kind == 'change')}",
        "",
        "## Issues",
    ]
    lines.extend(issue_line(issue) for issue in issues)
    if not issues:
        lines.append("No issues matched the query.")

    lines.extend(["", "## Activity By Month"])
    if not activities:
        lines.append("No dated worklogs or issue changes were returned for the matched issues.")
    for month in sorted(by_month.keys(), reverse=True):
        lines.append(f"### {month}")
        for activity in by_month[month]:
            lines.append(
                f"- {activity.when.strftime('%Y-%m-%d %H:%M')} - **{activity.issue_key}** "
                f"({activity.kind}): {activity.text}"
            )

    lines.extend(["", "## Query", "```jql", jql, "```", ""])
    return "\n".join(lines)


def identity_set(values: Iterable[str | None]) -> set[str]:
    return {value.casefold() for value in values if value}


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract a Jira work summary for July-to-today or a custom date range.")
    parser.add_argument("--url", default=os.environ.get("JIRA_URL"), help="Jira base URL. Defaults to JIRA_URL.")
    parser.add_argument("--api-version", default=os.environ.get("JIRA_API_VERSION", "2"), choices=("2", "3"))
    parser.add_argument("--from", dest="start", type=parse_day, default=default_start(), help="Start date, YYYY-MM-DD.")
    parser.add_argument("--to", dest="end", type=parse_day, default=date.today(), help="End date, YYYY-MM-DD.")
    parser.add_argument("--jql", help="Override the default current-user JQL query.")
    parser.add_argument("--max-issues", type=int, help="Optional cap for the number of issues to fetch.")
    parser.add_argument(
        "--output",
        default="results/jira_work_summary.md",
        help="Markdown output path. Defaults to results/jira_work_summary.md.",
    )
    args = parser.parse_args()

    if not args.url:
        raise SystemExit("Set JIRA_URL or pass --url, for example https://jira.example.com")
    if args.start > args.end:
        raise SystemExit("--from must be on or before --to")

    username = os.environ.get("JIRA_USERNAME")
    secret = os.environ.get("JIRA_API_TOKEN") or os.environ.get("JIRA_PASSWORD")
    bearer_token = os.environ.get("JIRA_TOKEN")
    identities = identity_set(
        [
            os.environ.get("JIRA_ACCOUNT_ID"),
            os.environ.get("JIRA_USERNAME"),
            os.environ.get("JIRA_EMAIL"),
            os.environ.get("JIRA_DISPLAY_NAME"),
        ]
    )

    client = JiraClient(args.url, args.api_version, username, secret, bearer_token)
    jql = args.jql or build_jql(args.start, args.end)
    issues = client.search_issues(
        jql,
        fields=["summary", "status", "issuetype", "updated", "assignee", "reporter"],
        max_issues=args.max_issues,
    )
    activities = collect_activities(client, issues, identities, args.start, args.end)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(render_report(issues, activities, args.start, args.end, jql), encoding="utf-8")
    print(f"Wrote {output_path} with {len(issues)} issue(s) and {len(activities)} dated activity item(s).")


if __name__ == "__main__":
    main()