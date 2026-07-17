from django.db import IntegrityError, transaction
from django.test import TestCase

from analyses.models import AnalysisJob


class AnalysisJobModelTest(TestCase):
    def test_database_rejects_risk_score_above_one_hundred(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            AnalysisJob.objects.create(
                source_url='https://www.tokopedia.com/demo-shop/demo-product',
                risk_score=101,
            )
