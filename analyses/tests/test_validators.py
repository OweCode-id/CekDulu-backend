from django.test import SimpleTestCase

from analyses.validators import TokopediaURLValidationError, normalize_tokopedia_product_url


class NormalizeTokopediaProductURLTest(SimpleTestCase):
    def test_normalizes_tracking_parameters_fragment_and_trailing_slash(self):
        result = normalize_tokopedia_product_url(
            'https://www.tokopedia.com/demo-shop/demo-product/?extParam=abc#reviews'
        )

        self.assertEqual(
            result,
            'https://www.tokopedia.com/demo-shop/demo-product',
        )

    def test_accepts_supported_tokopedia_hosts(self):
        for host in ('tokopedia.com', 'www.tokopedia.com', 'm.tokopedia.com'):
            with self.subTest(host=host):
                result = normalize_tokopedia_product_url(f'https://{host}/demo-shop/item')
                self.assertEqual(result, f'https://{host}/demo-shop/item')

    def test_rejects_unsafe_or_non_product_urls(self):
        cases = {
            'http://www.tokopedia.com/demo-shop/item': 'https_required',
            'https://tokopedia.com.evil.example/demo-shop/item': 'unsupported_host',
            'https://user:password@www.tokopedia.com/demo-shop/item': (
                'credentials_not_allowed'
            ),
            'https://www.tokopedia.com:444/demo-shop/item': 'port_not_allowed',
            'https://www.tokopedia.com/demo-shop': 'product_url_required',
            'https://www.tokopedia.com/demo-shop/review': 'product_url_required',
        }

        for url, expected_code in cases.items():
            with self.subTest(url=url), self.assertRaises(TokopediaURLValidationError) as raised:
                normalize_tokopedia_product_url(url)
            self.assertEqual(raised.exception.code, expected_code)
