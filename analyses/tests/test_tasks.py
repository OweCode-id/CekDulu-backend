from unittest.mock import patch

from celery.exceptions import Retry
from django.test import TestCase, override_settings

from analyses.models import AnalysisJob
from analyses.services import OpenRouterError
from analyses.tasks import collect_analysis_evidence


def collection_report(
    *,
    status: str = 'completed',
    sufficient: bool = True,
    product_error: str | None = None,
    product_error_message: str | None = None,
) -> dict:
    return {
        'schemaVersion': 'cekdulu-targeted-collector-test',
        'status': status,
        'sourceUrl': 'https://www.tokopedia.com/demo-shop/demo-product',
        'collection': {
            'productPage': {
                'finalUrl': 'https://www.tokopedia.com/demo-shop/demo-product',
                'errorCode': product_error,
                'errorMessage': product_error_message,
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


@override_settings(OPENROUTER_API_KEY='')
class CollectAnalysisEvidenceTaskTest(TestCase):
    def create_job(self, **overrides) -> AnalysisJob:
        values = {
            'source_url': 'https://www.tokopedia.com/demo-shop/demo-product',
        }
        values.update(overrides)
        return AnalysisJob.objects.create(**values)

    @patch('analyses.tasks.TokopediaCollector')
    def test_successful_collection_completes_analysis(self, collector_class):
        report = collection_report()
        collector_class.return_value.collect.return_value = report
        analysis = self.create_job()

        result = collect_analysis_evidence.run(str(analysis.pk))

        analysis.refresh_from_db()
        self.assertEqual(result, {'status': AnalysisJob.Status.COMPLETED})
        self.assertEqual(analysis.status, AnalysisJob.Status.COMPLETED)
        self.assertEqual(analysis.evidence, report)
        self.assertEqual(
            analysis.collector_schema_version,
            'cekdulu-targeted-collector-test',
        )
        self.assertEqual(analysis.canonical_url, report['product']['canonicalUrl'])
        self.assertIsNotNone(analysis.started_at)
        self.assertIsNotNone(analysis.completed_at)
        self.assertEqual(analysis.risk_score, 30)
        self.assertEqual(analysis.verdict, 'caution')
        self.assertEqual(analysis.result['trustScore'], 70)
        self.assertEqual(analysis.result['explanationSource'], 'deterministic_fallback')
        collector_class.return_value.collect.assert_called_once_with(analysis.source_url)

    @override_settings(
        TOKOPEDIA_BROWSER_CHANNEL='chrome',
        TOKOPEDIA_BROWSER_HEADED=True,
        TOKOPEDIA_BLOCK_RESOURCES=False,
    )
    @patch('analyses.tasks.TokopediaCollector')
    def test_collection_uses_configured_browser(self, collector_class):
        collector_class.return_value.collect.return_value = collection_report()
        analysis = self.create_job()

        collect_analysis_evidence.run(str(analysis.pk))

        config = collector_class.call_args.kwargs['config']
        self.assertEqual(config.browser_channel, 'chrome')
        self.assertTrue(config.headed)
        self.assertFalse(config.block_resources)

    @patch('analyses.tasks.TokopediaCollector')
    def test_partial_but_sufficient_collection_can_be_analyzed(self, collector_class):
        collector_class.return_value.collect.return_value = collection_report(status='partial')
        analysis = self.create_job()

        collect_analysis_evidence.run(str(analysis.pk))

        analysis.refresh_from_db()
        self.assertEqual(analysis.status, AnalysisJob.Status.COMPLETED)

    @patch('analyses.tasks.OpenRouterClient')
    @patch('analyses.tasks.TokopediaCollector')
    def test_openrouter_explanation_is_saved_without_changing_score(
        self,
        collector_class,
        client_class,
    ):
        collector_class.return_value.collect.return_value = collection_report()
        client = client_class.return_value
        client.enabled = True
        client.config.model = 'deepseek/deepseek-v4-flash'
        client.explain.return_value = {
            'summary': 'Penjelasan model.',
            'reasons': ['Alasan model.'],
            'followUpQuestions': ['Pertanyaan model?'],
        }
        analysis = self.create_job()

        collect_analysis_evidence.run(str(analysis.pk))

        analysis.refresh_from_db()
        self.assertEqual(analysis.status, AnalysisJob.Status.COMPLETED)
        self.assertEqual(analysis.risk_score, 30)
        self.assertEqual(analysis.summary, 'Penjelasan model.')
        self.assertEqual(analysis.result['explanationSource'], 'openrouter')
        self.assertEqual(analysis.result['model'], 'deepseek/deepseek-v4-flash')

    @patch('analyses.tasks.logger.warning')
    @patch('analyses.tasks.OpenRouterClient')
    @patch('analyses.tasks.TokopediaCollector')
    def test_openrouter_failure_uses_deterministic_fallback(
        self,
        collector_class,
        client_class,
        log_warning,
    ):
        collector_class.return_value.collect.return_value = collection_report()
        client = client_class.return_value
        client.enabled = True
        client.explain.side_effect = OpenRouterError('temporary failure')
        analysis = self.create_job()

        collect_analysis_evidence.run(str(analysis.pk))

        analysis.refresh_from_db()
        self.assertEqual(analysis.status, AnalysisJob.Status.COMPLETED)
        self.assertEqual(analysis.result['explanationSource'], 'deterministic_fallback')
        log_warning.assert_called_once()

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
            product_error_message='Page.goto: net::ERR_HTTP2_PROTOCOL_ERROR',
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
        self.assertIn('ERR_HTTP2_PROTOCOL_ERROR', log_warning.call_args.args[-1])

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
