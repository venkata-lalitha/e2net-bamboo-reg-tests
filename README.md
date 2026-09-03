# Bamboo Regression Root-Cause & Auto-Fix Agent

Connects to Bamboo, SSHes into the E2net runtime hosts to execute regression
plans, parses the resulting JUnit XML, diagnoses the root cause of each
failure, and auto-applies safe, high-confidence code fixes to your local
checkouts of `e2net-platform`, `e2net-commons`, and `e2net-test-service`
(working tree only — nothing is committed or pushed).

## 1. Install

```powershell
cd C:\git\bamboo-reg-agent
pip install -r requirements.txt
```

## 2. Fill in `config.yaml`

Every `TODO` in `config.yaml` must be replaced before the agent will run:

- `bamboo.url` — e.g. `https://bamboo.mycompany.com/rest/api/latest`
- `bamboo.plans` — the Bamboo plan keys for your E2net regression suites
- `bamboo.token_env` — name of an env var holding your Bamboo Personal Access
  Token (don't put the token in the file). Set it before running:
  ```powershell
  $env:BAMBOO_TOKEN = "<your PAT>"
  ```
- `ssh.hosts` — already set to `dev10091`, `dev10092`
- `ssh.auth_method` — `key`, `password`, or `agent` (passwordless/agent-based
  SSH already configured). If `password`, set:
  ```powershell
  $env:SSH_PASSWORD = "<password>"
  ```
- `test_execution.remote_command` — the actual command to run a regression
  plan on the host, e.g.
  `"cd /opt/e2net && ./run_regression.sh --plan {plan_key}"`
- `test_execution.remote_results_dir` — where that command writes JUnit XML
  on the remote host, e.g. `/opt/e2net/test-results/{plan_key}`
- `deploy.host_containers` — which docker container(s) run on each SSH host
  (already set to `dev10091: [e2net]`, `dev10092: [e2netio, e2na, scenario]`)
- `repo.paths[].container_base_path` — for each repo
  (`e2net-platform`, `e2net-commons`, `e2net-test-service`), the path inside
  the containers where that repo's source lives, e.g. `/opt/e2net`

## 3. Run

```powershell
cd C:\git\bamboo-reg-agent
python src\main.py --config config.yaml
```

Useful flags:
- `--plan TODO-PLAN-KEY-1` — run only one plan (repeatable)
- `--dry-run` — diagnose only, never touch files in the E2net repo

## Run automatically on Bamboo build completion (webhook)

Instead of running `src/main.py` by hand, you can have Bamboo trigger the
agent automatically when a plan's build finishes:

1. Set `webhook.enabled: true` in `config.yaml` and pick a shared secret:
   ```powershell
   $env:WEBHOOK_TOKEN = "<random shared secret>"
   ```
2. Start the listener (keep it running, e.g. as a scheduled task/service):
   ```powershell
   python src\webhook_listener.py --config config.yaml
   ```
3. On each plan in `bamboo.plans`, configure a Bamboo webhook notification
   (e.g. the "Webhook Notifications for Bamboo" plugin, or an equivalent
   post-build task) to POST to `http://<this-machine>:8787/bamboo-webhook`
   with header `X-Webhook-Token: <the same WEBHOOK_TOKEN value>`.

The listener only acts on plan keys listed in `bamboo.plans`, only reacts to
failed builds when `bamboo.only_if_failed: true`, rejects requests without a
matching `X-Webhook-Token`, and skips a plan if it's already being processed
from a previous trigger. Keep `webhook.host` bound to localhost/private
network — this endpoint can trigger code changes, so it must never be
exposed unauthenticated to the internet.

## Extract Jira Work Summary

To pin what you worked from July 1 through today, set Jira credentials in the
environment and run the Jira summary script:

```powershell
$env:JIRA_URL = "https://jira.example.com"
$env:JIRA_USERNAME = "<your username or email>"
$env:JIRA_API_TOKEN = "<your api token or password>"
python src\jira_work_summary.py --from 2026-07-01 --to 2026-09-02 --output results\jira_work_summary.md
```

For bearer-token Jira instances, set `JIRA_TOKEN` instead of
`JIRA_USERNAME`/`JIRA_API_TOKEN`. The script queries issues assigned to,
reported by, or worklogged by the current Jira user and writes a Markdown
summary grouped by month.

## What it does

1. **Check Bamboo** (if `bamboo.only_if_failed: true`) — skips plans whose
   latest build isn't failed.
2. **Fetch failing tests directly from Bamboo's own test-result API** —
   the remote test framework's log output isn't machine-parseable JUnit
   XML, so Bamboo's own aggregation (which already knows pass/fail per
   test) is the source of truth instead of SSHing in and parsing raw logs.
3. **Locate the matching source file** across the configured `repo.paths`
   checkouts by fully-qualified classname.
4. **Diagnose root cause** using rule-based pattern matching
   (`src/fix_rules.py`):
   - Stale hardcoded assertion values → **auto-fixed** when the literal is
     unambiguous in the source file.
   - Timeouts, connection-refused, missing-class errors → diagnosed with a
     clear root cause and remediation suggestion, but **not** auto-patched
     (these are usually environment/infra issues, not code bugs).
   - NullPointerException → located and explained, flagged for manual review
     (auto-patching NPEs blindly is unsafe).
   - Anything unmatched → included in the report as "needs manual review"
     with the full stack trace.
5. **Apply fixes** to the matching local repo's working tree (creates a
   `.bak` backup of each file it touches).
6. **Deploy fixes** (when `deploy.enabled: true`, the default) — every
   auto-applied fix is uploaded to each SSH host and `docker cp`'d into
   every container configured for that host in `deploy.host_containers`, at
   the path built from that file's repo `container_base_path`, then a
   fresh SSH run of the plan is triggered to exercise the fix (its output
   isn't parsed - check Bamboo's next build to confirm the fix worked).
7. **Writes a report** to `results/root_cause_report.md` summarizing every
   plan, failure, root cause, and whether a fix was applied.

## Extending the rule engine

Add new pattern-matching functions to `src/fix_rules.py` following the
existing signature `(failure: TestFailure, source_file: str | None) ->
Diagnosis | None` and register them in the `RULES` list. Only return a
`patch=` when you're confident the fix is unambiguous and safe to apply
automatically — everything else should just explain the root cause.

## Safety notes

- The agent never commits, pushes, or touches git history — it only edits
  files in the working tree of the repos configured under `repo.paths`.
- Every auto-applied patch has a `.bak` backup created next to it (unless
  `fix.create_backup: false`).
- Credentials are never stored in `config.yaml` — only environment variable
  *names* are configured there.
