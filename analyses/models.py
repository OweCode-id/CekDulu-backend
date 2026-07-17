import uuid

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models


class AnalysisJob(models.Model):
    class Status(models.TextChoices):
        QUEUED = 'queued', 'Queued'
        COLLECTING = 'collecting', 'Collecting'
        ANALYZING = 'analyzing', 'Analyzing'
        COMPLETED = 'completed', 'Completed'
        FAILED = 'failed', 'Failed'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source_url = models.URLField(max_length=2_048)
    canonical_url = models.URLField(max_length=2_048, blank=True)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.QUEUED,
        db_index=True,
    )

    collector_schema_version = models.CharField(max_length=100, blank=True)
    evidence = models.JSONField(default=dict, blank=True)
    result = models.JSONField(default=dict, blank=True)
    risk_score = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
    )
    verdict = models.CharField(max_length=50, blank=True)
    summary = models.TextField(blank=True)

    error_code = models.CharField(max_length=100, blank=True)
    error_message = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'analysis_jobs'
        ordering = ('-created_at',)
        indexes = [
            models.Index(fields=('status', 'created_at'), name='analysis_status_created_idx'),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(risk_score__lte=100),
                name='analysis_risk_score_lte_100',
            ),
        ]

    def __str__(self) -> str:
        return f'{self.id} ({self.status})'
