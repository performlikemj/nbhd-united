"""Tests for the Phase 0.5 channel-decommission sunset broadcast command.

Covers: targeting query (tg / line / both linked; excludes synthetic +
unlinked), dry-run sends nothing, --execute fans out to every linked channel +
email, service-notice opt-out (email skipped, channel message still sent),
Reply-To + List-Unsubscribe headers and the unsubscribe footer, per-tenant
failure isolation, --cutover-date gate, copy regressions from MJ's review,
and the App Store URL drift guard against the frontend badge. All network is
mocked; email is captured by Django's locmem backend (``mail.outbox``).
"""

from __future__ import annotations

import secrets
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.core import mail
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from apps.router.management.commands import send_channel_sunset_notice as cmd
from apps.tenants.models import Tenant, User

_CMD = "send_channel_sunset_notice"
_SUPPORT = "support@test.example"


def _linked_tenant(
    email,
    *,
    chat_id=None,
    line_uid=None,
    synthetic=False,
    display_name="Friend",
    service_opt_out=False,
    **kw,
):
    # line_user_id is UNIQUE + null=True: the "no LINE" sentinel is NULL, not
    # "" (multiple "" rows would collide on the unique index — matching prod,
    # where unlinked users carry NULL).
    user = User.objects.create_user(
        username=f"{email}-{secrets.token_hex(3)}",
        email=email,
        telegram_chat_id=chat_id,
        line_user_id=line_uid,
        display_name=display_name,
        service_email_opt_out=service_opt_out,
    )
    return Tenant.objects.create(user=user, is_synthetic=synthetic, **kw)


class TargetingTest(TestCase):
    def test_includes_tg_line_and_both_excludes_synthetic_and_unlinked(self):
        tg = _linked_tenant("tg@e.com", chat_id=111)
        line = _linked_tenant("line@e.com", line_uid="Uline")
        both = _linked_tenant("both@e.com", chat_id=222, line_uid="Uboth")
        # Excluded: synthetic (e.g. App Review demo tenant) even though linked,
        # and a real-but-unlinked tenant.
        _linked_tenant("synthetic@e.com", chat_id=333, synthetic=True)
        _linked_tenant("unlinked@e.com")  # no chat_id, no line_uid

        target_tids = set(u.tenant.id for u in cmd.build_target_queryset())

        self.assertEqual(target_tids, {tg.id, line.id, both.id})

    def test_empty_string_line_uid_is_not_linked(self):
        # A blank line_user_id ("") must not count as linked.
        _linked_tenant("blank@e.com", line_uid="")
        self.assertEqual(list(cmd.build_target_queryset()), [])

    def test_synthetic_demo_tenant_excluded_even_when_linked(self):
        # The App Review demo tenant is synthetic — the synthetic filter alone
        # keeps it out, independent of any channel linkage.
        demo = _linked_tenant("demo@e.com", chat_id=999, line_uid="Udemo", synthetic=True)
        tids = set(u.tenant.id for u in cmd.build_target_queryset())
        self.assertNotIn(demo.id, tids)

    def test_service_opt_out_still_targeted(self):
        # Opt-out narrows the EMAIL leg only — the tenant stays in the
        # broadcast targeting (they still get the in-product channel notice).
        t = _linked_tenant("optout@e.com", chat_id=444, service_opt_out=True)
        self.assertIn(t.id, set(u.tenant.id for u in cmd.build_target_queryset()))

    def test_tenant_id_scope(self):
        a = _linked_tenant("a@e.com", chat_id=1)
        _linked_tenant("b@e.com", chat_id=2)
        scoped = list(cmd.build_target_queryset(tenant_ids=[str(a.id)]))
        self.assertEqual([u.tenant.id for u in scoped], [a.id])


class DryRunTest(TestCase):
    def test_dry_run_sends_nothing(self):
        _linked_tenant("tg@e.com", chat_id=111)
        _linked_tenant("line@e.com", line_uid="Uline")

        out = StringIO()
        with (
            patch.object(cmd, "send_telegram_notice") as m_tg,
            patch.object(cmd, "send_line_notice") as m_line,
        ):
            call_command(_CMD, stdout=out)  # no --execute → dry run

        m_tg.assert_not_called()
        m_line.assert_not_called()
        self.assertEqual(len(mail.outbox), 0)
        text = out.getvalue()
        self.assertIn("Targets: 2 tenant(s)", text)
        self.assertIn("[dry-run]", text)


@override_settings(
    LINE_CHANNEL_ACCESS_TOKEN="test-token",
    SUPPORT_CONTACT_EMAIL=_SUPPORT,
    API_BASE_URL="https://api.nbhd.test",
)
class ExecuteFanoutTest(TestCase):
    def test_execute_fans_out_to_every_channel_and_email(self):
        _linked_tenant("tg@e.com", chat_id=111, display_name="Tess")
        _linked_tenant("line@e.com", line_uid="Uline", display_name="Lin")
        _linked_tenant("both@e.com", chat_id=222, line_uid="Uboth", display_name="Bo")

        out = StringIO()
        with (
            patch.object(cmd, "send_telegram_notice", return_value=True) as m_tg,
            patch.object(cmd, "send_line_notice", return_value=True) as m_line,
        ):
            call_command(_CMD, "--execute", "--cutover-date", "2026-07-28", stdout=out)

        # Telegram: tg + both = 2 sends; LINE: line + both = 2 sends.
        self.assertEqual(m_tg.call_count, 2)
        self.assertEqual(m_line.call_count, 2)
        # Email: all three targeted tenants.
        self.assertEqual(len(mail.outbox), 3)

        # Copy carries the rendered cutover date, canonical App Store URL, and
        # the support contact from settings.
        sent_tg_text = m_tg.call_args_list[0].args[1]
        self.assertIn("July 28, 2026", sent_tg_text)
        self.assertIn(cmd.APP_STORE_URL, sent_tg_text)
        self.assertIn(_SUPPORT, sent_tg_text)

        email = mail.outbox[0]
        self.assertIn("July 28, 2026", email.body)
        self.assertIn(cmd.APP_STORE_URL, email.body)
        self.assertIn(_SUPPORT, email.body)
        self.assertEqual(email.subject, cmd.EMAIL_SUBJECT)

        self.assertIn("Telegram: sent=2", out.getvalue())
        self.assertIn("LINE:     sent=2", out.getvalue())
        self.assertIn("Email:    sent=3", out.getvalue())

    def test_service_opt_out_skips_email_but_not_channel(self):
        _linked_tenant("optout@e.com", chat_id=111, service_opt_out=True)
        _linked_tenant("normal@e.com", chat_id=222)

        out = StringIO()
        with patch.object(cmd, "send_telegram_notice", return_value=True) as m_tg:
            call_command(_CMD, "--execute", "--cutover-date", "2026-07-28", stdout=out)

        # BOTH tenants got the channel message; only the non-opted-out one
        # got the email.
        self.assertEqual(m_tg.call_count, 2)
        recipients = {addr for m in mail.outbox for addr in m.to}
        self.assertEqual(recipients, {"normal@e.com"})
        self.assertIn("skipped-optout=1", out.getvalue())

    def test_email_reply_to_headers_and_unsubscribe_footer(self):
        t = _linked_tenant("tg@e.com", chat_id=111)

        with patch.object(cmd, "send_telegram_notice", return_value=True):
            call_command(_CMD, "--execute", "--cutover-date", "2026-07-28", stdout=StringIO())

        email = mail.outbox[0]
        # Reply-To is the env-backed support mailbox (DEFAULT_FROM_EMAIL's
        # mailbox is send-only).
        self.assertEqual(email.reply_to, [_SUPPORT])
        # RFC 2369 / RFC 8058 one-click headers, exactly like the promo send.
        unsub_url = cmd.service_unsubscribe_url(t.user)
        self.assertEqual(email.extra_headers["List-Unsubscribe"], f"<{unsub_url}>")
        self.assertEqual(email.extra_headers["List-Unsubscribe-Post"], "List-Unsubscribe=One-Click")
        self.assertTrue(unsub_url.startswith("https://api.nbhd.test/api/v1/tenants/unsubscribe/"))
        # Footer carries the same service-category link in the body.
        self.assertIn(unsub_url, email.body)
        self.assertIn("service notices", email.body)

    def test_per_tenant_failure_isolation(self):
        _linked_tenant("boom@e.com", chat_id=111)
        _linked_tenant("ok@e.com", chat_id=222)

        def _tg(chat_id, text):
            if chat_id == 111:
                raise RuntimeError("telegram exploded")
            return True

        out = StringIO()
        with patch.object(cmd, "send_telegram_notice", side_effect=_tg):
            call_command(_CMD, "--execute", "--cutover-date", "2026-07-28", stdout=out)

        # The failing tenant did not halt the run: the healthy tenant's email
        # still went out. (The failing tenant's own email is skipped because
        # its telegram step raised inside the per-tenant guard.)
        recipients = {addr for m in mail.outbox for addr in m.to}
        self.assertIn("ok@e.com", recipients)
        self.assertNotIn("boom@e.com", recipients)
        self.assertIn("Tenant-level errors: 1", out.getvalue())

    def test_execute_requires_cutover_date(self):
        _linked_tenant("tg@e.com", chat_id=111)
        with self.assertRaises(CommandError):
            call_command(_CMD, "--execute", stdout=StringIO())

    def test_execute_rejects_malformed_cutover_date(self):
        _linked_tenant("tg@e.com", chat_id=111)
        with self.assertRaises(CommandError):
            call_command(_CMD, "--execute", "--cutover-date", "28-07-2026", stdout=StringIO())


@override_settings(SUPPORT_CONTACT_EMAIL=_SUPPORT, API_BASE_URL="https://api.nbhd.test")
class CopyTest(TestCase):
    def test_app_store_url_matches_frontend_badge(self):
        """Drift guard: the URL in the command must equal the one the frontend
        badge links to (``frontend/components/app-store-badge.tsx``)."""
        repo_root = Path(__file__).resolve().parents[2]
        badge = repo_root / "frontend" / "components" / "app-store-badge.tsx"
        if not badge.exists():  # pragma: no cover — frontend absent in some checkouts
            self.skipTest("frontend badge file not present")
        self.assertIn(cmd.APP_STORE_URL, badge.read_text())

    def test_channel_and_email_copy_render(self):
        channel = cmd.render_channel_message("July 28, 2026")
        self.assertIn("iOS app", channel)
        self.assertIn("July 28, 2026", channel)
        self.assertIn(cmd.APP_STORE_URL, channel)
        self.assertIn(f"Email {_SUPPORT}", channel)
        # No unrendered placeholders left behind.
        self.assertNotIn("{", channel)

        subject, body = cmd.render_email("Alex", "July 28, 2026", "https://api.nbhd.test/unsub/")
        self.assertEqual(subject, cmd.EMAIL_SUBJECT)
        self.assertIn("Alex", body)
        self.assertIn("personal assistant", body)
        self.assertIn(f"Email {_SUPPORT}", body)
        self.assertIn("https://api.nbhd.test/unsub/", body)
        self.assertNotIn("{", body)

    def test_copy_regressions_from_mj_review(self):
        # MJ review of PR #1195: no promises, no reply-to-this-email (the
        # from-address is send-only).
        _, body = cmd.render_email("Alex", "July 28, 2026", "https://api.nbhd.test/unsub/")
        channel = cmd.render_channel_message("July 28, 2026")
        for text in (body, channel):
            self.assertNotIn("nothing is lost", text.lower())
            self.assertNotIn("reply to this email", text.lower())
        # The plain factual replacement line is present in the email.
        self.assertIn("stops working", body)
