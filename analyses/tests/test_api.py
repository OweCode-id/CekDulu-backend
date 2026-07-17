from unittest.mock import patch

from django.core.cache import cache
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from analyses.models import AnalysisJob


class AnalysisAPITest(APITestCase):
    def setUp(self):
        cache.clear()

    @patch('analyses.views.collect_analysis_evidence.delay')
    def test_create_analysis_returns_accepted_job(self, enqueue):
        response = self.client.post(
            reverse('analysis-create'),
            {
                'url': (
                    'https://www.tokopedia.com/demo-shop/demo-product'
                    '?extParam=tracking#reviews'
                )
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        analysis = AnalysisJob.objects.get()
        detail_path = reverse('analysis-detail', kwargs={'pk': analysis.pk})
        self.assertEqual(analysis.status, AnalysisJob.Status.QUEUED)
        self.assertEqual(
            analysis.source_url,
            'https://www.tokopedia.com/demo-shop/demo-product',
        )
        self.assertEqual(response.data['id'], str(analysis.pk))
        self.assertEqual(response.data['status'], AnalysisJob.Status.QUEUED)
        self.assertEqual(response.data['sourceUrl'], analysis.source_url)
        self.assertEqual(response.data['statusUrl'], detail_path)
        self.assertEqual(response.headers['Location'], detail_path)
        self.assertIsNone(response.data['result'])
        self.assertIsNone(response.data['error'])
        enqueue.assert_called_once_with(str(analysis.pk))

    @patch(
        'analyses.views.collect_analysis_evidence.delay',
        side_effect=RuntimeError('broker offline'),
    )
    @patch('analyses.views.logger.exception')
    def test_create_analysis_returns_service_unavailable_when_enqueue_fails(
        self,
        log_exception,
        enqueue,
    ):
        response = self.client.post(
            reverse('analysis-create'),
            {'url': 'https://www.tokopedia.com/demo-shop/demo-product'},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        analysis = AnalysisJob.objects.get()
        self.assertEqual(analysis.status, AnalysisJob.Status.FAILED)
        self.assertEqual(analysis.error_code, 'QUEUE_UNAVAILABLE')
        self.assertIsNotNone(analysis.completed_at)
        self.assertEqual(response.data['error']['code'], 'QUEUE_UNAVAILABLE')
        enqueue.assert_called_once_with(str(analysis.pk))
        log_exception.assert_called_once()

    def test_create_analysis_rejects_unsupported_url(self):
        response = self.client.post(
            reverse('analysis-create'),
            {'url': 'https://tokopedia.com.evil.example/demo-shop/item'},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(AnalysisJob.objects.count(), 0)
        self.assertEqual(response.data['url'][0].code, 'unsupported_host')

    def test_detail_returns_completed_result(self):
        analysis = AnalysisJob.objects.create(
            source_url='https://www.tokopedia.com/demo-shop/demo-product',
            canonical_url='https://www.tokopedia.com/demo-shop/demo-product-canonical',
            status=AnalysisJob.Status.COMPLETED,
            risk_score=24,
            verdict='low_risk',
            summary='Tidak ditemukan sinyal risiko utama.',
            result={'signals': [{'code': 'OFFICIAL_STORE', 'impact': -10}]},
            evidence={
                'product': {
                    'imageUrl': 'https://images.tokopedia.net/img/cache/product.jpg'
                }
            },
        )

        response = self.client.get(reverse('analysis-detail', kwargs={'pk': analysis.pk}))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['riskScore'], 24)
        self.assertEqual(response.data['verdict'], 'low_risk')
        self.assertEqual(response.data['canonicalUrl'], analysis.canonical_url)
        self.assertEqual(
            response.data['productImageUrl'],
            'https://images.tokopedia.net/img/cache/product.jpg',
        )
        self.assertEqual(response.data['result'], analysis.result)
        self.assertIsNone(response.data['error'])

    def test_detail_returns_structured_failure(self):
        analysis = AnalysisJob.objects.create(
            source_url='https://www.tokopedia.com/demo-shop/demo-product',
            status=AnalysisJob.Status.FAILED,
            error_code='COLLECTION_TIMEOUT',
            error_message='Halaman produk melewati batas waktu.',
        )

        response = self.client.get(reverse('analysis-detail', kwargs={'pk': analysis.pk}))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.data['error'],
            {
                'code': 'COLLECTION_TIMEOUT',
                'message': 'Halaman produk melewati batas waktu.',
            },
        )

    def test_detail_returns_not_found_for_unknown_id(self):
        response = self.client.get(
            '/api/v1/analyses/00000000-0000-0000-0000-000000000000/'
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @patch('analyses.views.collect_analysis_evidence.delay')
    def test_create_analysis_is_rate_limited(self, enqueue):
        url = reverse('analysis-create')
        payload = {'url': 'https://www.tokopedia.com/demo-shop/demo-product'}

        for _ in range(10):
            response = self.client.post(url, payload, format='json')
            self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

        response = self.client.post(url, payload, format='json')

        self.assertEqual(response.status_code, status.HTTP_429_TOO_MANY_REQUESTS)
        self.assertEqual(enqueue.call_count, 10)
