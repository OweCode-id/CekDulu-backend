from __future__ import annotations

import logging
from typing import Any

from billiard.exceptions import SoftTimeLimitExceeded
from celery import Task, shared_task
from django.conf import settings
from django.utils import timezone

from analyses.models import AnalysisJob
from analyses.services import (
    CollectorConfig,
    OpenRouterClient,
    OpenRouterError,
    TokopediaCollector,
    build_result,
    fallback_explanation,
    score_evidence,
)
from analyses.services.tokopedia_collector import CollectorError

logger = logging.getLogger(__name__)

TRANSIENT_COLLECTION_ERRORS = frozenset(
    {
        'BROWSER_UNAVAILABLE',
        'COLLECTION_TIMEOUT',
        'NAVIGATION_ERROR',
        'NETWORK_ERROR',
    }
)

PUBLIC_ERROR_MESSAGES = {
    'BLOCKED_OR_CAPTCHA': (
        'Tokopedia meminta verifikasi tambahan sehingga data belum dapat dikumpulkan.'
    ),
    'BROWSER_UNAVAILABLE': 'Browser collector sementara tidak tersedia.',
    'COLLECTION_TIMEOUT': 'Pengumpulan data melewati batas waktu.',
    'INSUFFICIENT_EVIDENCE': 'Data yang terkumpul belum cukup untuk dianalisis.',
    'INVALID_COLLECTOR_OUTPUT': 'Collector menghasilkan data yang tidak dapat diproses.',
    'NAVIGATION_ERROR': 'Halaman Tokopedia tidak dapat dibuka oleh collector.',
    'NETWORK_ERROR': 'Koneksi ke Tokopedia gagal setelah beberapa percobaan.',
    'ANALYSIS_ERROR': 'Data berhasil dikumpulkan, tetapi analisis tidak dapat diselesaikan.',
}


class CollectionAttemptError(RuntimeError):
    """Internal exception used to tell Celery why an attempt is retried."""


def _public_error_message(code: str) -> str:
    return PUBLIC_ERROR_MESSAGES.get(
        code,
        'Terjadi kesalahan internal saat mengumpulkan data produk.',
    )


def _claim_job(analysis_id: str, *, is_retry: bool) -> bool:
    expected_status = AnalysisJob.Status.COLLECTING if is_retry else AnalysisJob.Status.QUEUED
    now = timezone.now()
    updates: dict[str, Any] = {
        'status': AnalysisJob.Status.COLLECTING,
        'error_code': '',
        'error_message': '',
        'completed_at': None,
        'updated_at': now,
    }
    if not is_retry:
        updates['started_at'] = now

    updated = AnalysisJob.objects.filter(
        pk=analysis_id,
        status=expected_status,
    ).update(**updates)
    return updated == 1


def _canonical_url(report: dict[str, Any]) -> str:
    return (
        report.get('product', {}).get('canonicalUrl')
        or report.get('collection', {}).get('productPage', {}).get('finalUrl')
        or report.get('sourceUrl')
        or ''
    )


def _report_failure_code(report: dict[str, Any]) -> str:
    product_page = report.get('collection', {}).get('productPage') or {}
    return product_page.get('errorCode') or 'COLLECTION_FAILED'


def _report_failure_detail(report: dict[str, Any]) -> str:
    product_page = report.get('collection', {}).get('productPage') or {}
    return str(product_page.get('errorMessage') or '')


def _compact_log_detail(detail: str | None) -> str:
    return ' '.join(str(detail or 'detail unavailable').split())[:1_000]


def _collector_config() -> CollectorConfig:
    channel = str(settings.TOKOPEDIA_BROWSER_CHANNEL).strip() or None
    return CollectorConfig(
        browser_channel=channel,
        headed=bool(settings.TOKOPEDIA_BROWSER_HEADED),
        block_resources=bool(settings.TOKOPEDIA_BLOCK_RESOURCES),
    )


def _finish_failed(
    analysis_id: str,
    code: str,
    *,
    report: dict[str, Any] | None = None,
) -> dict[str, str]:
    now = timezone.now()
    updates: dict[str, Any] = {
        'status': AnalysisJob.Status.FAILED,
        'error_code': code,
        'error_message': _public_error_message(code),
        'completed_at': now,
        'updated_at': now,
    }
    if report is not None:
        updates.update(
            evidence=report,
            collector_schema_version=report.get('schemaVersion', ''),
            canonical_url=_canonical_url(report),
        )

    AnalysisJob.objects.filter(
        pk=analysis_id,
        status=AnalysisJob.Status.COLLECTING,
    ).update(**updates)
    return {'status': AnalysisJob.Status.FAILED, 'errorCode': code}


def _mark_analyzing(analysis_id: str, report: dict[str, Any]) -> None:
    now = timezone.now()
    AnalysisJob.objects.filter(
        pk=analysis_id,
        status=AnalysisJob.Status.COLLECTING,
    ).update(
        status=AnalysisJob.Status.ANALYZING,
        canonical_url=_canonical_url(report),
        collector_schema_version=report.get('schemaVersion', ''),
        evidence=report,
        updated_at=now,
    )


def _finish_analysis_failed(analysis_id: str) -> dict[str, str]:
    now = timezone.now()
    AnalysisJob.objects.filter(
        pk=analysis_id,
        status=AnalysisJob.Status.ANALYZING,
    ).update(
        status=AnalysisJob.Status.FAILED,
        error_code='ANALYSIS_ERROR',
        error_message=_public_error_message('ANALYSIS_ERROR'),
        completed_at=now,
        updated_at=now,
    )
    return {'status': AnalysisJob.Status.FAILED, 'errorCode': 'ANALYSIS_ERROR'}


def _analyze_report(analysis_id: str, report: dict[str, Any]) -> dict[str, str]:
    scoring = score_evidence(report)
    explanation = fallback_explanation(scoring)
    explanation_source = 'deterministic_fallback'
    model = None

    client = OpenRouterClient()
    if client.enabled:
        try:
            explanation = client.explain(scoring, report)
            explanation_source = 'openrouter'
            model = client.config.model
        except OpenRouterError:
            logger.warning(
                'OpenRouter explanation failed for analysis %s; using fallback.',
                analysis_id,
            )

    result = build_result(
        scoring,
        explanation,
        explanation_source=explanation_source,
        model=model,
    )
    now = timezone.now()
    AnalysisJob.objects.filter(
        pk=analysis_id,
        status=AnalysisJob.Status.ANALYZING,
    ).update(
        status=AnalysisJob.Status.COMPLETED,
        result=result,
        risk_score=scoring['riskScore'],
        verdict=scoring['verdict'],
        summary=explanation['summary'],
        error_code='',
        error_message='',
        completed_at=now,
        updated_at=now,
    )
    return {'status': AnalysisJob.Status.COMPLETED}


def _retry_or_fail(
    task: Task,
    analysis_id: str,
    code: str,
    *,
    report: dict[str, Any] | None = None,
    detail: str | None = None,
) -> dict[str, str]:
    log_detail = _compact_log_detail(detail)
    if code in TRANSIENT_COLLECTION_ERRORS and task.request.retries < task.max_retries:
        logger.warning(
            'Retrying collection for analysis %s after %s: %s',
            analysis_id,
            code,
            log_detail,
        )
        raise task.retry(
            exc=CollectionAttemptError(code),
            countdown=5,
        )
    logger.warning(
        'Collection failed for analysis %s after %s: %s',
        analysis_id,
        code,
        log_detail,
    )
    return _finish_failed(analysis_id, code, report=report)


@shared_task(
    bind=True,
    ignore_result=True,
    max_retries=1,
    name='analyses.tasks.collect_analysis_evidence',
)
def collect_analysis_evidence(task: Task, analysis_id: str) -> dict[str, str]:
    """Collect bounded Tokopedia evidence and prepare the job for AI analysis."""
    if not _claim_job(analysis_id, is_retry=task.request.retries > 0):
        return {'status': 'ignored'}

    source_url = (
        AnalysisJob.objects.filter(pk=analysis_id)
        .values_list('source_url', flat=True)
        .first()
    )
    if source_url is None:
        return {'status': 'missing'}
    try:
        report = TokopediaCollector(config=_collector_config()).collect(source_url)
    except CollectorError as exc:
        return _retry_or_fail(task, analysis_id, exc.code, detail=str(exc))
    except SoftTimeLimitExceeded:
        return _retry_or_fail(task, analysis_id, 'COLLECTION_TIMEOUT')
    except Exception:
        logger.exception('Unexpected collector failure for analysis %s.', analysis_id)
        return _finish_failed(analysis_id, 'INTERNAL_ERROR')

    if not isinstance(report, dict) or report.get('status') not in {
        'completed',
        'partial',
        'failed',
    }:
        return _finish_failed(analysis_id, 'INVALID_COLLECTOR_OUTPUT')

    if report['status'] == 'failed':
        code = _report_failure_code(report)
        return _retry_or_fail(
            task,
            analysis_id,
            code,
            report=report,
            detail=_report_failure_detail(report),
        )

    if report.get('quality', {}).get('sufficientForAnalysis') is not True:
        return _finish_failed(
            analysis_id,
            'INSUFFICIENT_EVIDENCE',
            report=report,
        )

    _mark_analyzing(analysis_id, report)
    try:
        return _analyze_report(analysis_id, report)
    except Exception:
        logger.exception('Unexpected risk analysis failure for analysis %s.', analysis_id)
        return _finish_analysis_failed(analysis_id)
