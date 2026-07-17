from unittest.mock import patch

from celery.exceptions import Retry
from django.test import TestCase

from analyses.models import AnalysisJob
from analyses.tasks import collect_analysis_evidence


def collection_report(
    *,
    status: str = 'completed',
    sufficient: bool = True,
    product_error: str | None = None,
) -> dict:
    return {
        'schemaVersion': 'cekdulu-targeted-collector-test',
        'status': status,
        'sourceUrl': 'https://www.tokopedia.com/demo-shop/demo-product',
        'collection': {
            'productPage': {
                'finalUrl': 'https://www.tokopedia.com/demo-shop/demo-product',
                'errorCode': product_error,
            },
            'storeReviewPage': None,
        },
        'product': {
            'name': 'Demo Product',
            'price': 100_000,
            'canonicalUrl': 'https://www.tokopedia.com/demo-shop/demo-product',
        },
        'storeSummary': {'name': 'Demo Shop'},
        'quality': {'sufficientForAnalysis': sufficient},
    }


class CollectAnalysisEvidenceTaskTest(TestCase):
    def create_job(self, **overrides) -> AnalysisJob:
        values = {
            'source_url': 'https://www.tokopedia.com/demo-shop/demo-product',
        }
        values.update(overrides)
        return AnalysisJob.objects.create(**values)

    @patch('analyses.tasks.TokopediaCollector')
    def test_successful_collection_moves_job_to_analyzing(self, collector_class):
        report = collection_report()
        collector_class.return_value.collect.return_value = report
        analysis = self.create_job()

        result = collect_analysis_evidence.run(str(analysis.pk))

        analysis.refresh_from_db()
        self.assertEqual(result, {'status': AnalysisJob.Status.ANALYZING})
        self.assertEqual(analysis.status, AnalysisJob.Status.ANALYZING)
        self.assertEqual(analysis.evidence, report)
        self.assertEqual(
            analysis.collector_schema_version,
            'cekdulu-targeted-collector-test',
        )
        self.assertEqual(analysis.canonical_url, report['product']['canonicalUrl'])
        self.assertIsNotNone(analysis.started_at)
        self.assertIsNone(analysis.completed_at)
        collector_class.return_value.collect.assert_called_once_with(analysis.source_url)

    @patch('analyses.tasks.TokopediaCollector')
    def test_partial_but_sufficient_collection_can_be_analyzed(self, collector_class):
        collector_class.return_value.collect.return_value = collection_report(status='partial')
        analysis = self.create_job()

        collect_analysis_evidence.run(str(analysis.pk))

        analysis.refresh_from_db()
        self.assertEqual(analysis.status, AnalysisJob.Status.ANALYZING)

    @patch('analyses.tasks.TokopediaCollector')
    def test_insufficient_collection_is_stored_and_failed(self, collector_class):
        report = collection_report(sufficient=False)
        collector_class.return_value.collect.return_value = report
        analysis = self.create_job()

        result = collect_analysis_evidence.run(str(analysis.pk))

        analysis.refresh_from_db()
        self.assertEqual(
            result,
            {
                'status': AnalysisJob.Status.FAILED,
                'errorCode': 'INSUFFICIENT_EVIDENCE',
            },
        )
        self.assertEqual(analysis.status, AnalysisJob.Status.FAILED)
        self.assertEqual(analysis.error_code, 'INSUFFICIENT_EVIDENCE')
        self.assertEqual(analysis.evidence, report)
        self.assertIsNotNone(analysis.completed_at)

    @patch('analyses.tasks.TokopediaCollector')
    def test_blocked_collection_fails_without_exposing_internal_error(self, collector_class):
        report = collection_report(
            status='failed',
            sufficient=False,
            product_error='BLOCKED_OR_CAPTCHA',
        )
        collector_class.return_value.collect.return_value = report
        analysis = self.create_job()

        collect_analysis_evidence.run(str(analysis.pk))

        analysis.refresh_from_db()
        self.assertEqual(analysis.status, AnalysisJob.Status.FAILED)
        self.assertEqual(analysis.error_code, 'BLOCKED_OR_CAPTCHA')
        self.assertNotIn('playwright', analysis.error_message.lower())
        self.assertEqual(analysis.evidence, report)

    @patch('analyses.tasks.TokopediaCollector')
    @patch('analyses.tasks.logger.warning')
    def test_transient_collection_failure_is_retried_once(
        self,
        log_warning,
        collector_class,
    ):
        report = collection_report(
            status='failed',
            sufficient=False,
            product_error='NETWORK_ERROR',
        )
        collector_class.return_value.collect.return_value = report
        analysis = self.create_job()

        with (
            patch.object(collect_analysis_evidence, 'retry', side_effect=Retry()) as retry,
            self.assertRaises(Retry),
        ):
            collect_analysis_evidence.run(str(analysis.pk))

        analysis.refresh_from_db()
        self.assertEqual(analysis.status, AnalysisJob.Status.COLLECTING)
        self.assertEqual(retry.call_args.kwargs['countdown'], 5)
        log_warning.assert_called_once()

    @patch('analyses.tasks.TokopediaCollector')
    def test_terminal_job_is_not_collected_again(self, collector_class):
        analysis = self.create_job(status=AnalysisJob.Status.COMPLETED)

        result = collect_analysis_evidence.run(str(analysis.pk))

        self.assertEqual(result, {'status': 'ignored'})
        collector_class.assert_not_called()

    @patch('analyses.tasks.logger.exception')
    @patch('analyses.tasks.TokopediaCollector')
    def test_unexpected_exception_is_recorded_as_internal_error(
        self,
        collector_class,
        log_exception,
    ):
        collector_class.return_value.collect.side_effect = ValueError('secret detail')
        analysis = self.create_job()

        collect_analysis_evidence.run(str(analysis.pk))

        analysis.refresh_from_db()
        self.assertEqual(analysis.status, AnalysisJob.Status.FAILED)
        self.assertEqual(analysis.error_code, 'INTERNAL_ERROR')
        self.assertNotIn('secret detail', analysis.error_message)
        log_exception.assert_called_once()
