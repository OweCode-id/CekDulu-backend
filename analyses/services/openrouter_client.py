from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

import requests
from django.conf import settings


class OpenRouterError(RuntimeError):
    pass


@dataclass(frozen=True)
class OpenRouterConfig:
    api_key: str
    model: str
    base_url: str
    timeout_seconds: int
    app_name: str
    site_url: str

    @classmethod
    def from_settings(cls) -> OpenRouterConfig:
        return cls(
            api_key=settings.OPENROUTER_API_KEY,
            model=settings.OPENROUTER_MODEL,
            base_url=settings.OPENROUTER_BASE_URL,
            timeout_seconds=settings.OPENROUTER_TIMEOUT_SECONDS,
            app_name=settings.OPENROUTER_APP_NAME,
            site_url=settings.OPENROUTER_SITE_URL,
        )


def _strip_code_fence(value: str) -> str:
    return re.sub(r'^```(?:json)?\s*|\s*```$', '', value.strip(), flags=re.IGNORECASE)


def _validate_explanation(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise OpenRouterError('Respons model bukan object JSON.')
    summary = value.get('summary')
    reasons = value.get('reasons')
    questions = value.get('followUpQuestions')
    if not isinstance(summary, str) or not summary.strip():
        raise OpenRouterError('Respons model tidak memiliki summary yang valid.')
    if not isinstance(reasons, list) or not all(isinstance(item, str) for item in reasons):
        raise OpenRouterError('Respons model tidak memiliki reasons yang valid.')
    if not isinstance(questions, list) or not all(isinstance(item, str) for item in questions):
        raise OpenRouterError('Respons model tidak memiliki followUpQuestions yang valid.')
    return {
        'summary': summary.strip()[:500],
        'reasons': [item.strip()[:300] for item in reasons if item.strip()][:5],
        'followUpQuestions': [item.strip()[:300] for item in questions if item.strip()][:3],
    }


class OpenRouterClient:
    def __init__(
        self,
        config: OpenRouterConfig | None = None,
        session: requests.Session | None = None,
    ):
        self.config = config or OpenRouterConfig.from_settings()
        self.session = session or requests.Session()

    @property
    def enabled(self) -> bool:
        return bool(self.config.api_key and self.config.model)

    def explain(self, scoring: dict[str, Any], evidence: dict[str, Any]) -> dict[str, Any]:
        if not self.enabled:
            raise OpenRouterError('OpenRouter belum dikonfigurasi.')
        reviews = []
        for key in ('productReviews', 'storeReviews'):
            for review in evidence.get(key, {}).get('items', [])[:6]:
                reviews.append(
                    {
                        'evidenceId': review.get('evidenceId'),
                        'rating': review.get('rating'),
                        'text': review.get('text'),
                    }
                )
        context = {
            'fixedScoring': scoring,
            'product': evidence.get('product', {}),
            'storeSummary': evidence.get('storeSummary', {}),
            'store': evidence.get('store', {}),
            'reviewSample': reviews,
        }
        headers = {
            'Authorization': f'Bearer {self.config.api_key}',
            'Content-Type': 'application/json',
            'X-Title': self.config.app_name,
        }
        if self.config.site_url:
            headers['HTTP-Referer'] = self.config.site_url
        payload = {
            'model': self.config.model,
            'temperature': 0.1,
            'max_tokens': 700,
            'reasoning': {
                'effort': 'none',
                'exclude': True,
            },
            'messages': [
                {
                    'role': 'system',
                    'content': (
                        'Kamu menjelaskan hasil risk scoring CekDulu dalam Bahasa Indonesia. '
                        'Jangan mengubah skor, verdict, atau confidence. Jangan menganggap sampel '
                        'review mewakili seluruh populasi. Jangan pernah menyatakan produk pasti '
                        'aman, terpercaya, scam, atau penipuan; gunakan bahasa indikasi berdasarkan '
                        'evidence dan sebutkan keterbatasannya. Jawab hanya JSON object dengan keys '
                        'summary, reasons (array), dan followUpQuestions (array maksimal 3).'
                    ),
                },
                {
                    'role': 'user',
                    'content': json.dumps(context, ensure_ascii=False),
                },
            ],
        }
        try:
            response = self.session.post(
                f"{self.config.base_url.rstrip('/')}/chat/completions",
                headers=headers,
                json=payload,
                timeout=self.config.timeout_seconds,
            )
            response.raise_for_status()
            content = response.json()['choices'][0]['message']['content']
            parsed = json.loads(_strip_code_fence(content))
        except (requests.RequestException, KeyError, IndexError, TypeError, ValueError) as exc:
            raise OpenRouterError('OpenRouter tidak menghasilkan respons yang valid.') from exc
        return _validate_explanation(parsed)
