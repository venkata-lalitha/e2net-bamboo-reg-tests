"""Root-cause rule engine for JUnit failures.

Each rule inspects a TestFailure and, if it matches, returns a Diagnosis.
Diagnoses may include an auto-fix: a (source_file, old_text, new_text) patch
that main.py will apply when fix.auto_apply is enabled AND confidence=="high".

Design intent: only a small, well-understood set of failure patterns are
auto-fixed. Everything else is still diagnosed (root cause + evidence) but
routed to "needs manual review" in the report, because blindly patching
NullPointerExceptions / infra errors / business-logic assertions without
understanding intent is unsafe.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Callable

from junit_parser import TestFailure


@dataclass
class Patch:
    file_path: str
    old_text: str
    new_text: str


@dataclass
class Diagnosis:
    rule_name: str
    root_cause: str
    confidence: str  # "high" | "medium" | "low"
    suggestion: str
    patch: Optional[Patch] = None


# --- Individual rules ------------------------------------------------------

_ASSERT_EQ_RE = re.compile(
    r"expected:\s*<?(?P<expected>[^>\n]+?)>?\s*but was:\s*<?(?P<actual>[^>\n]+?)>?\s*$",
    re.IGNORECASE,
)


def rule_stale_assert_equals(failure: TestFailure, source_file: Optional[str]) -> Optional[Diagnosis]:
    """assertEquals(expected, actual) failing where 'expected' is a hardcoded
    literal in the test source that looks stale vs. the actual value produced
    by (presumably updated) application code. Auto-fixable only when the
    expected literal appears exactly once in the test file (unambiguous).
    """
    m = _ASSERT_EQ_RE.search(failure.message)
    if not m or failure.kind != "failure" or not source_file:
        return None
    expected, actual = m.group("expected").strip(), m.group("actual").strip()
    if expected == actual:
        return None
    try:
        with open(source_file, "r", encoding="utf-8") as f:
            content = f.read()
    except OSError:
        return None

    literal_variants = [f'"{expected}"', expected]
    matches = [v for v in literal_variants if content.count(v) == 1]
    if not matches:
        return Diagnosis(
            rule_name="stale_assert_equals",
            root_cause=(
                f"Assertion mismatch in {failure.classname}#{failure.test_name}: "
                f"expected '{expected}' but got '{actual}'. Could not uniquely "
                f"locate the literal in source for a safe auto-fix."
            ),
            confidence="low",
            suggestion=(
                f"Manually verify whether '{actual}' is the new correct value and "
                f"update the assertion in {source_file}."
            ),
        )

    literal = matches[0]
    replacement = literal.replace(expected, actual)
    return Diagnosis(
        rule_name="stale_assert_equals",
        root_cause=(
            f"Test {failure.classname}#{failure.test_name} asserts a hardcoded "
            f"expected value '{expected}' that no longer matches actual output "
            f"'{actual}'. Likely a stale expectation left over from an "
            f"application behavior change."
        ),
        confidence="high",
        suggestion=f"Update expected literal {literal} -> {replacement} in {source_file}.",
        patch=Patch(file_path=source_file, old_text=literal, new_text=replacement),
    )


def rule_timeout(failure: TestFailure, source_file: Optional[str]) -> Optional[Diagnosis]:
    text = failure.message + "\n" + failure.stacktrace
    if not re.search(r"timeout|timed out|SocketTimeoutException", text, re.IGNORECASE):
        return None
    return Diagnosis(
        rule_name="timeout",
        root_cause=(
            f"{failure.classname}#{failure.test_name} timed out. This is often "
            f"caused by a slow/unresponsive dependency (DB, downstream service) "
            f"rather than a code defect, or a timeout value set too low for "
            f"current environment latency."
        ),
        confidence="medium",
        suggestion=(
            "Check that dependent services on the target host are up and "
            "responsive; if consistently slow, consider raising the test's "
            "timeout/connect-timeout configuration. Not auto-applied because "
            "the correct new value requires operator judgment."
        ),
    )


def rule_connection_refused(failure: TestFailure, source_file: Optional[str]) -> Optional[Diagnosis]:
    text = failure.message + "\n" + failure.stacktrace
    m = re.search(r"Connection refused.*?(?P<host>[\w.\-]+):(?P<port>\d+)", text)
    if not m and "ConnectException" not in text and "Connection refused" not in text:
        return None
    endpoint = f"{m.group('host')}:{m.group('port')}" if m else "the target endpoint"
    return Diagnosis(
        rule_name="connection_refused",
        root_cause=(
            f"{failure.classname}#{failure.test_name} failed with connection "
            f"refused to {endpoint}. This is an infrastructure/environment "
            f"issue (service down, wrong host/port, firewall) rather than an "
            f"application code bug."
        ),
        confidence="medium",
        suggestion=(
            f"Verify the service behind {endpoint} is running and reachable "
            f"from the test host, and that config/application.properties "
            f"points at the correct host/port for this environment."
        ),
    )


def rule_class_not_found(failure: TestFailure, source_file: Optional[str]) -> Optional[Diagnosis]:
    text = failure.message + "\n" + failure.stacktrace
    if not re.search(r"ClassNotFoundException|NoClassDefFoundError", text):
        return None
    return Diagnosis(
        rule_name="class_not_found",
        root_cause=(
            f"{failure.classname}#{failure.test_name} failed due to a missing "
            f"class on the classpath, typically a stale/incomplete build or a "
            f"missing dependency after a merge."
        ),
        confidence="medium",
        suggestion="Run a clean rebuild (e.g. mvn clean install / gradle clean build) on the remote host and re-run the plan.",
    )


def rule_null_pointer(failure: TestFailure, source_file: Optional[str]) -> Optional[Diagnosis]:
    text = failure.message + "\n" + failure.stacktrace
    if "NullPointerException" not in text:
        return None
    frame = re.search(rf"at {re.escape(failure.classname)}\.[\w$]+\((?P<loc>[^)]+)\)", text)
    location = frame.group("loc") if frame else "unknown location"
    return Diagnosis(
        rule_name="null_pointer",
        root_cause=(
            f"{failure.classname}#{failure.test_name} threw a NullPointerException "
            f"at {location}. Likely an unmocked/unset dependency, missing test "
            f"fixture data, or a legitimate null-handling gap in the code under test."
        ),
        confidence="low",
        suggestion=(
            f"Inspect {source_file or failure.classname} around {location}: confirm "
            f"whether the null is expected (add a null guard) or the test setup is "
            f"missing required stubbing/fixture data."
        ),
    )


RULES: list = [
    rule_stale_assert_equals,
    rule_timeout,
    rule_connection_refused,
    rule_class_not_found,
    rule_null_pointer,
]


def diagnose(failure: TestFailure, source_file: Optional[str]) -> Diagnosis:
    for rule in RULES:
        result = rule(failure, source_file)
        if result:
            return result
    return Diagnosis(
        rule_name="unknown",
        root_cause=(
            f"{failure.classname}#{failure.test_name} failed with an unrecognized "
            f"pattern: {failure.message[:200]}"
        ),
        confidence="low",
        suggestion="No matching rule. Review the full stack trace manually.",
    )
