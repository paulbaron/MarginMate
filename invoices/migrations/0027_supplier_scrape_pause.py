"""When AdminMate last signed in to a protected site, and until when it
leaves it alone (scrapers/metro.metro_pause) - and the Metro block already
under way when this came in, taken from the gathers' logs: without it, the
first gather after the upgrade would sign in straight into it."""

import re
from datetime import timedelta

from django.db import migrations, models

BLOCKED_RE = re.compile(r"bloqu[ée]e?s?\s+par\s+(?:notre|le)\s+pare-feu", re.I)
REFERENCE_RE = re.compile(r"#\d+\.[0-9a-f]+\.\d+\.[0-9a-f]+", re.I)
# As scrapers/metro.py at the time of writing - a migration keeps its own copy.
BLOCK_PAUSE = timedelta(days=7)
REPEAT_BLOCK_WITHIN = timedelta(days=30)


def record_past_blocks(apps, schema_editor):
    Supplier = apps.get_model("invoices", "Supplier")
    ScrapeJob = apps.get_model("invoices", "ScrapeJob")
    metro = Supplier.objects.filter(code="METRO").first()
    if metro is None:
        return
    blocks = []
    for job in ScrapeJob.objects.filter(log__icontains="pare-feu").order_by("started_at"):
        if BLOCKED_RE.search(job.log or ""):
            reference = REFERENCE_RE.search(job.log)
            blocks.append((job.started_at, reference.group(0) if reference else ""))
    contacted = ScrapeJob.objects.filter(progress__has_key="METRO").order_by("-started_at").first()
    if contacted is not None:
        metro.scrape_last_login_at = contacted.started_at
    if blocks:
        at, reference = blocks[-1]
        repeat = len(blocks) > 1 and at - blocks[-2][0] < REPEAT_BLOCK_WITHIN
        metro.scrape_last_block_at = at
        metro.scrape_paused_until = at + BLOCK_PAUSE * (2 if repeat else 1)
        metro.scrape_pause_reason = (
            "Metro a bloqué la connexion (pare-feu" + (f", référence {reference}" if reference else "") + ")."
        )
    metro.save()


class Migration(migrations.Migration):
    dependencies = [
        ("invoices", "0026_website_invoice_source"),
    ]

    operations = [
        migrations.AddField(
            model_name="supplier",
            name="scrape_last_block_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="supplier",
            name="scrape_last_login_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="supplier",
            name="scrape_pause_reason",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="supplier",
            name="scrape_paused_until",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunPython(record_past_blocks, migrations.RunPython.noop),
    ]
