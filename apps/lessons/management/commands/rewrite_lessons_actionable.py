"""Rewrite existing lessons to be actionable advice for the user's future self.

Calls an LLM to transform observation-style lessons ("photo was wrong size")
into actionable lessons ("always verify photo dimensions before proceeding").
Then regenerates embeddings and re-clusters.
"""

import requests
from django.conf import settings
from django.core.management.base import BaseCommand

from apps.lessons.models import Lesson
from apps.lessons.services import process_approved_lesson

REWRITE_MODEL = "anthropic/claude-sonnet-4.6"

REWRITE_SYSTEM = """\
You rewrite lessons into actionable advice for someone's future self.

Rules:
- Transform what happened into what to do next time.
- Keep it 1-2 sentences, concise and specific.
- Preserve important details (sizes, names, deadlines) but frame as guidance.
- If it's already actionable advice, return it unchanged.
- Return ONLY the rewritten text, nothing else.

Examples:
- Input: "The PR photo taken was the wrong size (45x35cm instead of 40x30cm) and may need to be redone."
  Output: "Always verify exact photo dimensions for government documents before proceeding — Japanese photo machines offer non-standard sizes (required: 40x30cm)."

- Input: "User noted they haven't gone to the gym in a week and suspects they might be going through a mini depression."
  Output: "When noticing a week without exercise, treat it as an early signal to check in on mental health — the two are closely linked."
"""


class Command(BaseCommand):
    help = "Rewrite approved lessons to be actionable using an LLM"

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Show rewrites without saving")
        parser.add_argument("--tenant", type=str, help="Only process lessons for this tenant ID")

    def handle(self, *args, **options):
        api_key = getattr(settings, "OPENROUTER_API_KEY", "")
        if not api_key:
            self.stderr.write(self.style.ERROR("OPENROUTER_API_KEY not configured"))
            return

        qs = Lesson.objects.filter(status="approved")
        if options.get("tenant"):
            qs = qs.filter(tenant_id=options["tenant"])

        lessons = list(qs)
        total = len(lessons)
        self.stdout.write(f"Found {total} approved lessons to rewrite")

        dry_run = options.get("dry_run", False)
        success = 0
        skipped = 0
        affected_tenants = set()

        for lesson in lessons:
            try:
                rewritten = self._rewrite(api_key, lesson.text, lesson.context or "")
                if not rewritten or rewritten == lesson.text:
                    skipped += 1
                    continue

                if dry_run:
                    self.stdout.write(f"\n  [{lesson.id}] BEFORE: {lesson.text}")
                    self.stdout.write(f"  [{lesson.id}] AFTER:  {rewritten}")
                    success += 1
                    continue

                lesson.text = rewritten
                lesson.save(update_fields=["text"])

                # Regenerate embedding for new text
                try:
                    process_approved_lesson(lesson)
                except Exception as e:
                    self.stdout.write(self.style.WARNING(f"  Embedding failed for {lesson.id}: {e}"))

                affected_tenants.add(lesson.tenant_id)
                success += 1
                self.stdout.write(f"  Rewrote lesson {lesson.id}")

            except Exception as e:
                self.stdout.write(self.style.ERROR(f"  Failed lesson {lesson.id}: {e}"))

        self.stdout.write(
            self.style.SUCCESS(f"Done: {success} rewritten, {skipped} unchanged, {total - success - skipped} failed")
        )

        # Re-cluster affected tenants
        if affected_tenants and not dry_run:
            from apps.lessons.clustering import refresh_constellation
            from apps.tenants.models import Tenant

            for tid in affected_tenants:
                try:
                    tenant = Tenant.objects.get(id=tid)
                    result = refresh_constellation(tenant)
                    self.stdout.write(f"  Re-clustered tenant {str(tid)[:8]}: {result}")
                except Exception as e:
                    self.stdout.write(self.style.ERROR(f"  Clustering failed for {str(tid)[:8]}: {e}"))

    def _rewrite(self, api_key: str, text: str, context: str) -> str:
        """Call LLM to rewrite a lesson as actionable advice."""
        user_msg = f'Original lesson: "{text}"'
        if context:
            user_msg += f'\nContext: "{context}"'

        resp = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": REWRITE_MODEL,
                "messages": [
                    {"role": "system", "content": REWRITE_SYSTEM},
                    {"role": "user", "content": user_msg},
                ],
                "temperature": 0.2,
                "max_tokens": 300,
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        return (data["choices"][0]["message"]["content"] or "").strip().strip('"')
