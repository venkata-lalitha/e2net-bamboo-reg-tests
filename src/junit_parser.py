"""Parse JUnit XML result files into a normalized list of failure records."""
from __future__ import annotations

import glob
import os
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Optional


@dataclass
class TestFailure:
    classname: str
    test_name: str
    kind: str  # "failure" or "error"
    message: str
    stacktrace: str
    source_file: str  # local XML file this came from


def parse_dir(results_dir: str) -> list:
    """Parses every *.xml file under results_dir (recursively) for JUnit
    <testsuite>/<testcase> elements containing <failure> or <error>.
    """
    failures = []
    for xml_path in glob.glob(os.path.join(results_dir, "**", "*.xml"), recursive=True):
        failures.extend(parse_file(xml_path))
    return failures


def parse_file(xml_path: str) -> list:
    failures = []
    try:
        tree = ET.parse(xml_path)
    except ET.ParseError:
        return failures
    root = tree.getroot()

    testsuites = [root] if root.tag == "testsuite" else root.findall(".//testsuite")
    for suite in testsuites:
        for case in suite.findall("testcase"):
            classname = case.get("classname", "")
            test_name = case.get("name", "")
            for tag in ("failure", "error"):
                node = case.find(tag)
                if node is not None:
                    failures.append(
                        TestFailure(
                            classname=classname,
                            test_name=test_name,
                            kind=tag,
                            message=node.get("message", "") or "",
                            stacktrace=(node.text or "").strip(),
                            source_file=xml_path,
                        )
                    )
    return failures
