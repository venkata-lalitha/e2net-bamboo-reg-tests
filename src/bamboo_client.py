"""Thin client for the Bamboo REST API - used to check plan/build status
and (optionally) pull down JUnit test result summaries already known to Bamboo.
"""
from __future__ import annotations

import requests


class BambooClient:
    def __init__(self, base_url: str, auth_method: str = "token", token: str = None,
                 username: str = None, password: str = None):
        self.base_url = base_url
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})
        if auth_method == "basic":
            self.session.auth = (username, password)
        else:
            self.session.headers.update({"Authorization": f"Bearer {token}"})

    def latest_build_status(self, plan_key: str) -> dict:
        """Returns the latest build result summary for a plan.
        Endpoint: /result/{planKey}-latest.json
        """
        return self.build_status(plan_key)

    def build_status(self, plan_key: str, build_number: str | int = "latest") -> dict:
        """Returns the build result summary for a plan at a specific build
        number, or the latest build if build_number is "latest"/omitted.
        Endpoint: /result/{planKey}-{buildNumber}.json
        """
        url = f"{self.base_url}/result/{plan_key}-{build_number}.json"
        resp = self.session.get(url, params={"expand": "state"}, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def is_failed(self, plan_key: str, build_number: str | int = "latest") -> bool:
        try:
            data = self.build_status(plan_key, build_number)
        except requests.RequestException as e:
            raise RuntimeError(
                f"Failed to query Bamboo for plan {plan_key} (build {build_number}): {e}"
            ) from e
        state = data.get("buildState") or data.get("state")
        return str(state).upper() in ("FAILED", "ERROR")

    def failing_tests(self, plan_key: str, build_number: str | int = "latest") -> list:
        """Returns list of failing test dicts (className, methodName, errors)
        from Bamboo's own test result aggregation, expand=testResults.failedTests.
        Chain-level plans (e.g. "PROJ-PLAN") often don't roll up their jobs'
        test results directly, so if the chain build itself reports no
        failing tests, this also expands the chain's stages/jobs (e.g.
        "PROJ-PLAN-JOB") and checks each job build for failures too.
        """
        tests = self._failing_tests_for_key(plan_key, build_number)
        if tests:
            return tests

        job_tests = []
        for job_key, job_build_number in self._job_build_keys(plan_key, build_number):
            job_tests.extend(self._failing_tests_for_key(job_key, job_build_number))
        return job_tests

    def _failing_tests_for_key(self, result_key: str, build_number: str | int) -> list:
        url = f"{self.base_url}/result/{result_key}-{build_number}.json"
        try:
            resp = self.session.get(
                url,
                params={"expand": "testResults.failedTests.testResult.errors"},
                timeout=30,
            )
            resp.raise_for_status()
        except requests.RequestException as e:
            raise RuntimeError(f"Failed to query Bamboo test results for {result_key}-{build_number}: {e}") from e
        data = resp.json()
        tests = (
            data.get("testResults", {})
            .get("failedTests", {})
            .get("testResult", [])
        )
        # Bamboo/XML-to-JSON can collapse a single result to a dict instead
        # of a one-item list.
        if isinstance(tests, dict):
            tests = [tests]
        return tests

    def _job_build_keys(self, plan_key: str, build_number: str | int) -> list:
        """Returns (jobPlanKey, jobBuildNumber) pairs for every job build
        that ran as part of a chain build, via expand=stages.stage.results.result.
        Best-effort/tolerant of Bamboo API shape differences; returns [] if
        this build has no stages/jobs (e.g. it's already a job-level key).
        """
        url = f"{self.base_url}/result/{plan_key}-{build_number}.json"
        try:
            resp = self.session.get(url, params={"expand": "stages.stage.results.result"}, timeout=30)
            resp.raise_for_status()
        except requests.RequestException as e:
            raise RuntimeError(f"Failed to query Bamboo stages for {plan_key}-{build_number}: {e}") from e
        data = resp.json()

        stages = data.get("stages", {}).get("stage", [])
        if isinstance(stages, dict):
            stages = [stages]

        keys = []
        for stage in stages:
            results = stage.get("results", {}).get("result", [])
            if isinstance(results, dict):
                results = [results]
            for r in results:
                build_result_key = r.get("buildResultKey") or r.get("key")
                if not build_result_key:
                    continue
                job_key, _, job_build_number = build_result_key.rpartition("-")
                if job_key and job_build_number.isdigit():
                    keys.append((job_key, job_build_number))
        return keys


    def changed_files(self, plan_key: str, build_number: str | int = "latest") -> list:
        """Returns the list of file paths touched by the commit(s) included
        in this build, via expand=changes.change.files. Best-effort: the
        exact shape of "changes" varies across Bamboo versions, so this
        tolerates missing/renamed fields and returns [] if nothing is found.
        Used to correlate a failing test's source file with a recent code
        change, as supporting evidence for root-cause diagnosis.
        """
        url = f"{self.base_url}/result/{plan_key}-{build_number}.json"
        resp = self.session.get(
            url,
            params={"expand": "changes.change.files"},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()

        changes = data.get("changes", {}).get("change", [])
        if isinstance(changes, dict):
            changes = [changes]

        paths = []
        for change in changes:
            files = change.get("files", {}).get("file", [])
            if isinstance(files, dict):
                files = [files]
            for f in files:
                name = f.get("name") if isinstance(f, dict) else f
                if name:
                    paths.append(name)
        return paths

    def queue_build(self, plan_key: str) -> str:
        """Queues a fresh build of plan_key so Bamboo re-runs the suite and
        records a new official result (used to verify a deployed fix, since
        an ad-hoc SSH run doesn't update Bamboo's own test-result history).
        Endpoint: POST /queue/{planKey}. Returns the queued build's result
        key (e.g. "PROJ-PLAN-124").
        """
        url = f"{self.base_url}/queue/{plan_key}"
        try:
            resp = self.session.post(url, timeout=30)
            resp.raise_for_status()
        except requests.RequestException as e:
            raise RuntimeError(f"Failed to queue Bamboo build for plan {plan_key}: {e}") from e
        data = resp.json()
        return data.get("buildResultKey") or data.get("resultKey") or f"{plan_key}-queued"

