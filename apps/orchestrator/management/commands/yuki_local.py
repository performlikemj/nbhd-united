"""Console-only, secret-safe helper for the isolated Yuki stack."""

import json
import os
import re
import secrets
import stat
import sys
import time
import uuid
from datetime import date
from pathlib import Path

import httpx
from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.integrations.models import Integration, SautaiMealPlanJob
from apps.orchestrator.local_test import local_root
from apps.tenants.models import Tenant

BASE = "http://127.0.0.1:18080"
STATE = Path(settings.BASE_DIR) / "deploy/local-test/.state"
CONFIRM = "Yes, please go ahead."


class Failure(Exception):
    def __init__(self, reason, **details):
        self.payload = {"reason": reason, **details}


def private_json(path, payload):
    """Exclusive creation: never replace existing credentials or evidence."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(payload, stream, ensure_ascii=False)
        stream.write("\n")


def stdin_line(limit):
    value = sys.stdin.readline(limit + 2).rstrip("\r\n")
    if not value.strip() or len(value) > limit or "\r" in value or "\n" in value:
        raise Failure("invalid_stdin")
    return value


def object_response(response, statuses, reason):
    if response.status_code not in statuses:
        raise Failure(reason)
    try:
        body = response.json()
    except ValueError:
        raise Failure("invalid_response") from None
    if not isinstance(body, dict):
        raise Failure("invalid_response")
    return body


def meal_count(plan):
    if not isinstance(plan, dict):
        return None
    if isinstance(plan.get("meals"), list):
        return len(plan["meals"])
    days = plan.get("days")
    if isinstance(days, list) and all(isinstance(d, dict) and isinstance(d.get("meals"), list) for d in days):
        return sum(len(d["meals"]) for d in days)
    return None


def asks_confirmation(reply):
    return bool(
        re.search(
            r"\b(confirm|confirmation|shall I|should I|may I|can I|go ahead|proceed|look correct|look good)\b|確認",
            reply,
            re.IGNORECASE,
        )
    )


class Command(BaseCommand):
    help = "Yuki's local account, console link, chat plan and Tonight proof"
    requires_system_checks = []

    def add_arguments(self, parser):
        subcommands = parser.add_subparsers(dest="action", required=True)
        subcommands.add_parser("signup")
        subcommands.add_parser("link")
        subcommands.add_parser("chat-plan").add_argument("--week")
        subcommands.add_parser("tonight")

    def handle(self, *args, **options):
        action = options["action"]
        failure = {
            "signup": {"account": "failed"},
            "link": {"linked": False},
            "chat-plan": {"proof": "chat-plan", "status": "failed", "week": options.get("week")},
            "tonight": {"proof": "tonight", "status": "failed"},
        }[action]
        try:
            self.tid = os.environ.get("NBHD_TENANT_ID", "")
            try:
                if action == "signup":
                    uuid.UUID(self.tid)
                    root = local_root(self.tid, require_tenant=False)
                    if Tenant.objects.exclude(id=self.tid).exists():
                        raise Failure("local_stack_required")
                else:
                    root = local_root(self.tid)
            except Exception:
                raise Failure("local_stack_required") from None
            if root is None:
                raise Failure("local_stack_required")
            with httpx.Client(base_url=BASE, trust_env=False, follow_redirects=False, timeout=30) as client:
                self.client = client
                if action == "signup":
                    result = self.signup()
                else:
                    self.login(self.account())
                    self.tenant_gate()
                    if action == "link":
                        result = self.link()
                    elif action == "chat-plan":
                        result = self.chat_plan(options.get("week"))
                    else:
                        result = self.tonight()
        except Exception as exc:
            details = exc.payload if isinstance(exc, Failure) else {"reason": "local_request_failed"}
            self.stdout.write(json.dumps({**failure, **details}))
            raise SystemExit(1) from None
        self.stdout.write(json.dumps(result, ensure_ascii=False))

    def account(self):
        path = STATE / "yuki-account.json"
        # Refuse symlinks and insecure credentials rather than silently copying
        # or repairing an existing operator-owned file.
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except OSError:
            raise Failure("account_unavailable") from None
        with os.fdopen(fd) as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
                raise Failure("account_permissions")
            try:
                account = json.load(stream)
            except ValueError:
                raise Failure("account_invalid") from None
        if (
            not isinstance(account, dict)
            or set(account) != {"email", "password"}
            or not all(isinstance(value, str) and value for value in account.values())
        ):
            raise Failure("account_invalid")
        return account

    def login(self, account):
        response = self.client.post("/api/v1/auth/login/", json=account)
        body = object_response(response, {200}, "login_failed")
        access = body.get("access")
        if not isinstance(access, str) or not access:
            raise Failure("login_failed")
        self.client.headers["Authorization"] = "Bearer " + access

    def signup(self):
        path = STATE / "yuki-account.json"
        if path.exists() or path.is_symlink():
            account = self.account()
            try:
                self.login(account)
            except Failure as exc:
                if exc.payload["reason"] != "login_failed":
                    raise
            else:
                return {"account": "exists"}
        else:
            account = {"email": "yuki@example.com", "password": secrets.token_urlsafe(24)}
            private_json(path, account)
        # A prior interrupted signup can reuse its saved credentials; no file
        # is overwritten even when the normal API rejects this attempt.
        object_response(self.client.post("/api/v1/auth/signup/", json=account), {201}, "signup_failed")
        self.login(account)
        return {"account": "created"}

    def tenant_gate(self):
        tenant = object_response(self.client.get("/api/v1/tenants/me/"), {200}, "tenant_unavailable")
        if (
            tenant.get("id") != self.tid
            or tenant.get("is_synthetic") is not True
            or tenant.get("is_eval_sink") is not False
        ):
            raise Failure("tenant_gate_failed")

    def link(self):
        key = stdin_line(8192).strip()
        body = object_response(
            self.client.post("/api/v1/integrations/sautai/link/", json={"connect_key": key}), {200}, "link_failed"
        )
        if body.get("status") != "connected":
            raise Failure("link_not_connected")
        row = Integration.objects.filter(tenant_id=self.tid, provider=Integration.Provider.SAUTAI).first()
        if not row or type(row.sautai_user_id) is not int or row.sautai_user_id <= 0 or not row.linked_at:
            raise Failure("link_not_persisted")
        return {"linked": True, "sautai_user_id": row.sautai_user_id, "linked_at": row.linked_at.isoformat()}

    def chat_turn(self, thread, text, transcript):
        message_id = str(uuid.uuid4())
        transcript.append({"role": "yuki", "text": text, "timestamp": timezone.now().isoformat()})
        object_response(
            self.client.post(
                "/api/v1/chat/messages/", json={"client_msg_id": message_id, "thread_id": thread, "text": text}
            ),
            {200, 201, 202},
            "chat_admission_failed",
        )
        deadline = time.monotonic() + 900
        while time.monotonic() < deadline:
            body = object_response(
                self.client.get(f"/api/v1/chat/messages/{message_id}/", timeout=min(30, deadline - time.monotonic())),
                {200},
                "chat_poll_failed",
            )
            if body.get("status") == "ready":
                reply = body.get("reply_text")
                if isinstance(reply, str):
                    transcript.append(
                        {"role": "assistant", "reply_text": reply, "timestamp": timezone.now().isoformat()}
                    )
                if (
                    body.get("source") != "tenant"
                    or body.get("error")
                    or not isinstance(reply, str)
                    or not reply.strip()
                ):
                    raise Failure("reply_gate_failed")
                return reply
            if body.get("status") in {"failed", "error"}:
                raise Failure("chat_failed")
            time.sleep(min(1.5, max(0, deadline - time.monotonic())))
        raise Failure("chat_timeout")

    def chat_plan(self, value):
        try:
            week = date.fromisoformat(value)
        except (ValueError, TypeError):
            raise Failure("invalid_week") from None
        if week.isoformat() != value or week.weekday() != 0:
            raise Failure("week_must_be_monday")
        text = stdin_line(400)
        jobs = SautaiMealPlanJob.objects.filter(tenant_id=self.tid)
        started = timezone.now()
        thread = object_response(
            self.client.post("/api/v1/chat/threads/", json={"title": f"Yuki weekly plan {value}"}),
            {201},
            "thread_failed",
        )
        if thread.get("is_main") is not False or not thread.get("id"):
            raise Failure("refuse_main_thread")
        transcript = []
        path = STATE / "proof" / f"chat-plan-{value}-{started.strftime('%Y%m%dT%H%M%S%fZ')}.json"
        confirms = 0

        def find_job():
            # Detect wrong-week jobs even when a previous correct-week job exists.
            wrong = jobs.filter(created_at__gte=started).exclude(week_start=week).order_by("-created_at").first()
            if wrong:
                raise Failure("week_mismatch", week=value, job_week=str(wrong.week_start), job_id=str(wrong.id))
            job = jobs.filter(week_start=week).order_by("-created_at").first()
            if job and job.status == "failed":
                raise Failure("job_failed", job_id=str(job.id))
            return job

        try:
            reply = self.chat_turn(thread["id"], text, transcript)
            for _ in range(2):
                job = find_job()
                if job is not None and not asks_confirmation(reply):
                    break
                reply = self.chat_turn(thread["id"], CONFIRM, transcript)
                confirms += 1
            deadline = time.monotonic() + 1800
            while time.monotonic() < deadline:
                job = find_job()
                if job and job.status == "ready":
                    if job.week_start != week:
                        raise Failure("week_mismatch", week=value, job_week=str(job.week_start))
                    if job.addressed_by != "linked_id" or not job.result or job.error:
                        raise Failure("job_result_invalid")
                    result_week = job.result.get("week_start") if isinstance(job.result, dict) else None
                    if result_week is not None and result_week != value:
                        raise Failure("week_mismatch", week=value, job_week=str(result_week))
                    return {
                        "proof": "chat-plan",
                        "status": "ready",
                        "week": value,
                        "turns": 1 + confirms,
                        "confirm_turns": confirms,
                        "job_id": str(job.id),
                        "addressed_by": job.addressed_by,
                        "meal_count": meal_count(job.result),
                        "transcript": str(path),
                        "reply_excerpts": [
                            item["reply_text"][:160] for item in transcript if item["role"] == "assistant"
                        ],
                    }
                time.sleep(min(2, max(0, deadline - time.monotonic())))
            raise Failure("job_timeout")
        except Failure as exc:
            exc.payload["transcript"] = str(path)
            raise
        finally:
            private_json(path, {"week": value, "thread_id": thread["id"], "messages": transcript})

    def tonight(self):
        link = object_response(self.client.get("/api/v1/integrations/sautai/link/"), {200}, "link_read_failed")
        body = object_response(self.client.get("/api/v1/fuel/meals/today/"), {200}, "tonight_read_failed")
        meals = body.get("meals")
        if (
            type(link.get("linked")) is not bool
            or body.get("linked") is not link["linked"]
            or not isinstance(meals, list)
            or not all(isinstance(meal, dict) and isinstance(meal.get("name"), str) for meal in meals)
        ):
            raise Failure("tonight_payload_invalid")
        week = body.get("week_start")
        try:
            valid_week = date.fromisoformat(week)
        except (ValueError, TypeError):
            raise Failure("tonight_week_missing") from None
        if valid_week.weekday() != 0:
            raise Failure("tonight_week_invalid")
        result = {
            "linked": link["linked"],
            "meals_today": len(meals),
            "meal_names": [m["name"] for m in meals],
            "week_start": week,
        }
        if not meals:
            reason = body.get("empty_reason")
            result["empty_reason"] = (
                reason if reason in {"not_linked", "no_meal_today", "plan_unavailable"} else "unknown"
            )
        return result
