"""HTTP listener for Bamboo post-build webhook notifications.

Lets Bamboo trigger this agent automatically when a plan's build finishes,
instead of requiring someone to run `python src/main.py` by hand.

Configure a Bamboo webhook notification (e.g. the "Webhook Notifications for
Bamboo" plugin) on each plan in config.yaml to POST to this listener's
/bamboo-webhook path on build completion, with header
`X-Webhook-Token: <value of the WEBHOOK_TOKEN env var>`.

Usage:
    $env:WEBHOOK_TOKEN = "<shared secret>"
    python src/webhook_listener.py --config config.yaml
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(__file__))

from config import load_config, AppConfig
from main import build_clients, process_plan, write_report

# Field names vary across Bamboo webhook plugins/versions; try each in order.
_PLAN_KEY_FIELDS = ("planKey", "plan_key", "buildPlanKey")
_BUILD_NUMBER_FIELDS = ("buildNumber", "build_number")
_BUILD_STATE_FIELDS = ("buildState", "build_state", "status")

_plan_locks_guard = threading.Lock()
_plan_locks: dict = {}


def _lock_for_plan(plan_key: str) -> threading.Lock:
    with _plan_locks_guard:
        return _plan_locks.setdefault(plan_key, threading.Lock())


def _extract(payload: dict, fields: tuple, default=None):
    for f in fields:
        if f in payload and payload[f] not in (None, ""):
            return payload[f]
    return default


def _process_async(cfg: AppConfig, plan_key: str, build_number: str) -> None:
    lock = _lock_for_plan(plan_key)
    if not lock.acquire(blocking=False):
        print(f"[webhook] {plan_key} is already being processed, skipping duplicate trigger.")
        return
    try:
        bamboo, ssh = build_clients(cfg)
        entries = process_plan(cfg, bamboo, ssh, plan_key, dry_run=False, build_number=build_number)
        write_report([entries], cfg.report_path)
    except Exception as e:
        print(f"[webhook] Error processing {plan_key}: {e}")
    finally:
        lock.release()


def _make_handler(cfg: AppConfig):
    class WebhookHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            print(f"[webhook] {self.address_string()} - {fmt % args}")

        def _respond(self, code: int, body: str):
            self.send_response(code)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))

        def do_POST(self):
            if self.path.rstrip("/") != "/bamboo-webhook":
                self._respond(404, "not found")
                return

            provided_token = self.headers.get("X-Webhook-Token", "")
            if not cfg.webhook.token or provided_token != cfg.webhook.token:
                self._respond(401, "unauthorized")
                return

            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b""
            try:
                payload = json.loads(raw or b"{}")
            except json.JSONDecodeError:
                self._respond(400, "invalid JSON")
                return

            plan_key = _extract(payload, _PLAN_KEY_FIELDS)
            build_number = str(_extract(payload, _BUILD_NUMBER_FIELDS, "latest"))
            build_state = str(_extract(payload, _BUILD_STATE_FIELDS, "")).upper()

            if not plan_key or plan_key not in cfg.bamboo.plans:
                self._respond(202, f"ignored (plan {plan_key!r} not configured)")
                return
            if cfg.bamboo.only_if_failed and build_state and build_state not in ("FAILED", "ERROR"):
                self._respond(202, f"ignored (build state {build_state!r} not failed)")
                return

            self._respond(202, f"accepted: processing {plan_key} build {build_number}")
            threading.Thread(
                target=_process_async,
                args=(cfg, plan_key, build_number),
                daemon=True,
            ).start()

    return WebhookHandler


def main():
    parser = argparse.ArgumentParser(description="Bamboo webhook listener for the regression agent")
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()

    cfg = load_config(args.config)
    if not cfg.webhook.enabled:
        raise SystemExit("webhook.enabled is false in config.yaml; nothing to do.")

    server = ThreadingHTTPServer((cfg.webhook.host, cfg.webhook.port), _make_handler(cfg))
    print(f"Listening for Bamboo webhooks on http://{cfg.webhook.host}:{cfg.webhook.port}/bamboo-webhook")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
