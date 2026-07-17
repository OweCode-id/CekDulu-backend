from django.test import SimpleTestCase

from analyses.services.risk_analyzer import fallback_explanation, score_evidence


def evidence_fixture() -> dict:
    return {
        'product': {
            'rating': 4.8,
            'ratingCount': 100,
            'variationCollection': {'collected': True},
        },
        'storeSummary': {
            'isOfficialStore': True,
            'officialStoreEvidence': {'detected': True},
        },
        'store': {'rating': 4.8, 'ratingCount': 500},
        'storeConsistency': {'namesMatch': True},
        'productReviews': {'sampleSize': 5, 'items': []},
        'storeReviews': {'sampleSize': 5, 'items': []},
        'quality': {
            'confidenceCap': 'high',
            'storeReviewPageCollected': True,
        },
    }


class RiskAnalyzerTest(SimpleTestCase):
    def test_strong_reputation_produces_low_risk_score(self):
        result = score_evidence(evidence_fixture())

        self.assertEqual(result['riskScore'], 0)
        self.assertEqual(result['trustScore'], 100)
        self.assertEqual(result['verdict'], 'low_risk')
        self.assertEqual(result['confidence']['level'], 'high')

    def test_multiple_risk_signals_produce_high_risk(self):
        evidence = evidence_fixture()
        evidence['storeSummary']['isOfficialStore'] = False
        evidence['storeConsistency']['namesMatch'] = False
        evidence['productReviews']['items'] = [
            {
                'evidenceId': 'product_review_01',
                'text': 'Barang tidak dikirim sampai sekarang.',
            },
            {
                'evidenceId': 'product_review_02',
                'text': 'Paket kosong ketika diterima.',
            },
        ]

        result = score_evidence(evidence)

        signal = next(item for item in result['signals'] if item['code'] == 'ITEM_NOT_RECEIVED')
        self.assertEqual(signal['impact'], 35)
        self.assertEqual(result['riskScore'], 70)
        self.assertEqual(result['verdict'], 'high_risk')

    def test_missing_reviews_reduce_confidence_not_add_risk_signal(self):
        evidence = evidence_fixture()
        evidence['productReviews'] = {'sampleSize': 0, 'items': []}
        evidence['storeReviews'] = {'sampleSize': 0, 'items': []}
        evidence['quality'] = {
            'confidenceCap': 'low',
            'storeReviewPageCollected': False,
        }

        result = score_evidence(evidence)

        self.assertEqual(result['confidence']['level'], 'low')
        self.assertFalse(any(item['code'] == 'MISSING_REVIEWS' for item in result['signals']))

    def test_incoherent_high_value_listing_produces_high_risk(self):
        evidence = {
            'product': {
                'name': 'orang gila suka lepas kendaliiiiiiiiiii, merek toraja - kdkdkdkd',
                'description': 'nsjskakdjskakdjdiejkwkrjrkwkejdjdkwwkwek',
                'price': 200_000_000,
                'rating': None,
                'ratingCount': None,
                'soldCountLowerBound': None,
                'variationCollection': {
                    'collected': True,
                    'options': [
                        {'label': 'kdkdkdkd'},
                        {'label': 'kskdkdkd'},
                        {'label': 'nsjdjd'},
                        {'label': 'kwkdkdkdkdkdk'},
                    ],
                },
            },
            'storeSummary': {'isOfficialStore': False},
            'store': {},
            'storeConsistency': {'namesMatch': None},
            'productReviews': {'sampleSize': 0, 'items': []},
            'storeReviews': {'sampleSize': 0, 'items': []},
            'quality': {
                'confidenceCap': 'low',
                'storeReviewPageCollected': True,
            },
        }

        result = score_evidence(evidence)
        signal_codes = {signal['code'] for signal in result['signals']}

        self.assertEqual(result['riskScore'], 95)
        self.assertEqual(result['verdict'], 'high_risk')
        self.assertEqual(result['confidence']['level'], 'low')
        self.assertIn('LISTING_TEXT_ANOMALY', signal_codes)
        self.assertIn('VARIATION_TEXT_ANOMALY', signal_codes)
        self.assertIn('HIGH_VALUE_PRICE_WITHOUT_REPUTATION', signal_codes)

    def test_high_value_listing_with_strong_reputation_is_not_penalized(self):
        evidence = evidence_fixture()
        evidence['product'].update(
            {
                'name': 'Laptop workstation profesional 16 inci',
                'description': 'Produk resmi dengan garansi dan spesifikasi lengkap.',
                'price': 80_000_000,
                'variations': [
                    {'label': 'RAM 32 GB'},
                    {'label': 'RAM 64 GB'},
                ],
            }
        )

        result = score_evidence(evidence)

        self.assertEqual(result['riskScore'], 0)
        self.assertFalse(
            any(
                signal['code'] == 'HIGH_VALUE_PRICE_WITHOUT_REPUTATION'
                for signal in result['signals']
            )
        )

    def test_fallback_explanation_keeps_follow_up_questions(self):
        scoring = score_evidence(evidence_fixture())

        explanation = fallback_explanation(scoring)

        self.assertTrue(explanation['summary'])
        self.assertTrue(explanation['reasons'])
        self.assertLessEqual(len(explanation['followUpQuestions']), 3)
