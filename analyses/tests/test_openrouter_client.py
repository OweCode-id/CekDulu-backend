from unittest.mock import Mock

from django.test import SimpleTestCase

from analyses.services.openrouter_client import (
    OpenRouterClient,
    OpenRouterConfig,
    OpenRouterError,
)


class OpenRouterClientTest(SimpleTestCase):
    def config(self) -> OpenRouterConfig:
        return OpenRouterConfig(
            api_key='test-key',
            model='deepseek/deepseek-v4-flash',
            base_url='https://openrouter.ai/api/v1',
            timeout_seconds=10,
            app_name='CekDulu',
            site_url='',
        )

    def test_explain_parses_structured_json(self):
        response = Mock()
        response.json.return_value = {
            'choices': [
                {
                    'message': {
                        'content': (
                            '```json\n'
                            '{"summary":"Waspada.","reasons":["Ada keluhan."],'
                            '"followUpQuestions":["Sudah bandingkan harga?"]}'
                            '\n```'
                        )
                    }
                }
            ]
        }
        session = Mock()
        session.post.return_value = response
        client = OpenRouterClient(self.config(), session=session)

        result = client.explain(
            {'riskScore': 60, 'verdict': 'high_risk', 'signals': []},
            {'product': {}, 'storeSummary': {}, 'store': {}},
        )

        self.assertEqual(result['summary'], 'Waspada.')
        request = session.post.call_args
        self.assertEqual(request.kwargs['json']['model'], 'deepseek/deepseek-v4-flash')
        self.assertEqual(
            request.kwargs['json']['reasoning'],
            {'effort': 'none', 'exclude': True},
        )
        self.assertEqual(request.kwargs['timeout'], 10)
        self.assertEqual(request.kwargs['headers']['Authorization'], 'Bearer test-key')

    def test_explain_rejects_invalid_response(self):
        response = Mock()
        response.json.return_value = {'choices': []}
        session = Mock()
        session.post.return_value = response
        client = OpenRouterClient(self.config(), session=session)

        with self.assertRaises(OpenRouterError):
            client.explain(
                {'riskScore': 60, 'verdict': 'high_risk', 'signals': []},
                {'product': {}, 'storeSummary': {}, 'store': {}},
            )
