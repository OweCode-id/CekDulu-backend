from unittest.mock import MagicMock

from django.test import SimpleTestCase
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from analyses.services.tokopedia_collector import (
    CollectorConfig,
    CollectorError,
    PageStatus,
    build_output,
    collect_review_sample,
    extract_official_store_status,
    extract_variations,
    navigate_with_retry,
    parse_compact_metric,
)


class CollectorPureFunctionTest(SimpleTestCase):
    def test_compact_metric_preserves_provenance(self):
        metric = parse_compact_metric('Terjual 2 rb+')

        self.assertEqual(metric['value'], 2_000)
        self.assertEqual(metric['raw'], 'Terjual 2 rb+')
        self.assertTrue(metric['approximate'])
        self.assertTrue(metric['lowerBound'])

    def test_collector_config_rejects_unbounded_values(self):
        with self.assertRaises(ValueError):
            CollectorConfig(max_product_reviews=41)
        with self.assertRaises(ValueError):
            CollectorConfig(navigation_attempts=4)
        with self.assertRaises(ValueError):
            CollectorConfig(retry_backoff_ms=())

    def test_navigation_retries_transient_network_error(self):
        page = MagicMock()
        page.url = 'https://www.tokopedia.com/demo-shop/demo-product'
        response = MagicMock(status=200)
        page.goto.side_effect = [
            PlaywrightError('net::ERR_HTTP2_PROTOCOL_ERROR'),
            response,
        ]
        config = CollectorConfig(navigation_attempts=3, retry_backoff_ms=(1, 1))

        response_status, attempts = navigate_with_retry(page, page.url, config)

        self.assertEqual(response_status, 200)
        self.assertEqual(attempts, 2)
        page.wait_for_timeout.assert_called_once_with(1)

    def test_navigation_does_not_retry_non_transient_error(self):
        page = MagicMock()
        page.url = 'about:blank'
        page.goto.side_effect = PlaywrightError('net::ERR_CERT_AUTHORITY_INVALID')

        with self.assertRaises(CollectorError) as raised:
            navigate_with_retry(page, 'https://www.tokopedia.com/demo-shop/item', CollectorConfig())

        self.assertEqual(raised.exception.code, 'NAVIGATION_ERROR')
        self.assertEqual(raised.exception.navigation_attempts, 1)
        page.wait_for_timeout.assert_not_called()

    def test_quality_marks_stratified_reviews_as_non_representative(self):
        status = PageStatus(
            requestedUrl='https://www.tokopedia.com/demo-shop/item',
            finalUrl='https://www.tokopedia.com/demo-shop/item',
            responseStatus=200,
            durationMs=100,
            navigationAttempts=1,
            blockSignals={},
        )
        product_data = {
            'product': {
                'name': 'Produk Demo',
                'price': 100_000,
                'variationCollection': {'collected': False},
            },
            'storeSummary': {'name': 'Demo Shop', 'isOfficialStore': False},
            'productReviews': {
                'sampleSize': 5,
                'distinctRatingCount': 2,
                'buckets': [],
                'items': [],
            },
        }
        result = build_output(status.requestedUrl, status, product_data, None, {})

        self.assertFalse(
            result['quality']['analyzers']['productReviews']['representativeOfPopulation']
        )
        self.assertEqual(result['quality']['confidenceCap'], 'medium')


class CollectorBrowserFixtureTest(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        super().tearDownClass()

    def setUp(self):
        self.page = self.browser.new_page()

    def tearDown(self):
        self.page.close()

    def test_only_verified_official_badge_counts(self):
        self.page.set_content('<img data-testid="imgPDPFooterShopBadge" alt="generic">')
        self.assertFalse(extract_official_store_status(self.page)['detected'])

        self.page.set_content('<img data-testid="pdpShopBadgeOS" alt="Official Store">')
        result = extract_official_store_status(self.page)

        self.assertTrue(result['detected'])
        self.assertEqual(result['testId'], 'pdpShopBadgeOS')

    def test_structural_variation_controls_are_collected(self):
        self.page.set_content(
            """
            <section>
              <h3>Pilih Warna</h3>
              <button data-selected="true">White</button>
              <button>Midnight Black</button>
            </section>
            """
        )

        result = extract_variations(self.page)

        self.assertTrue(result['collected'])
        self.assertEqual(result['selectedOptions'], ['White'])
        self.assertEqual([item['label'] for item in result['options']], ['White', 'Midnight Black'])

    def test_review_sampling_captures_dates_and_low_rating_bucket(self):
        self.page.set_content(
            """
            <button data-testid="reviewSorting"
              onclick="document.querySelector('#lowest').style.display='block'">Terbaru</button>
            <button id="lowest" style="display:none"
              onclick="showLowest()">Rating Terendah</button>
            <section id="reviews">
              <article><div><span data-testid="icnStarRating" aria-label="5 bintang"></span><span>2 hari lalu</span></div><p data-testid="lblItemUlasan">Bagus sekali</p></article>
              <article><div><span data-testid="icnStarRating" aria-label="4 bintang"></span><span>12 Juli 2026</span></div><p data-testid="lblItemUlasan">Cukup baik</p></article>
            </section>
            <script>
              function showLowest() {
                document.querySelector('#reviews').innerHTML = `
                  <article><div><span data-testid="icnStarRating" aria-label="1 bintang"></span><span>1 bulan lalu</span></div><p data-testid="lblItemUlasan">Barang rusak</p></article>
                  <article><div><span data-testid="icnStarRating" aria-label="2 bintang"></span><span>10 Juni 2026</span></div><p data-testid="lblItemUlasan">Pengiriman buruk...</p></article>`;
              }
            </script>
            """
        )

        result = collect_review_sample(self.page, 4, 'product_review')

        self.assertFalse(result['representativeOfPopulation'])
        self.assertEqual(result['purpose'], 'risk_signal_discovery')
        self.assertEqual(result['sampleSize'], 4)
        self.assertTrue(result['buckets'][1]['filterApplied'])
        self.assertEqual(result['items'][0]['dateRaw'], '2 hari lalu')
        self.assertEqual(result['items'][-1]['dateRaw'], '10 Juni 2026')
        self.assertTrue(result['items'][-1]['textTruncated'])
