from __future__ import annotations

import re
from typing import Any

ANALYSIS_SCHEMA_VERSION = 'cekdulu-risk-analysis-0.2.0'

HIGH_VALUE_PRICE_IDR = 10_000_000
VERY_HIGH_VALUE_PRICE_IDR = 50_000_000
VOWELS = frozenset('aiueo')

COMPLAINT_RULES = (
    (
        'ITEM_NOT_RECEIVED',
        'Keluhan barang tidak diterima',
        ('tidak dikirim', 'belum dikirim', 'tidak sampai', 'paket kosong', 'isi kosong'),
        25,
    ),
    (
        'COUNTERFEIT_CONCERN',
        'Keluhan keaslian produk',
        ('barang palsu', 'produk palsu', 'tidak original', 'tidak ori', 'barang fake'),
        30,
    ),
    (
        'WRONG_ITEM',
        'Keluhan barang tidak sesuai',
        ('barang tidak sesuai', 'beda dengan deskripsi', 'salah kirim', 'barang berbeda'),
        15,
    ),
    (
        'DAMAGED_OR_DEFECTIVE',
        'Keluhan barang rusak atau tidak berfungsi',
        ('barang rusak', 'produk rusak', 'tidak berfungsi', 'mati total', 'cacat'),
        10,
    ),
)


def _signal(
    code: str,
    title: str,
    impact: int,
    explanation: str,
    evidence_refs: list[str],
) -> dict[str, Any]:
    if impact >= 25:
        severity = 'high'
    elif impact > 0:
        severity = 'medium'
    else:
        severity = 'protective'
    return {
        'code': code,
        'title': title,
        'impact': impact,
        'severity': severity,
        'explanation': explanation,
        'evidenceRefs': evidence_refs,
    }


def _review_items(evidence: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for key in ('productReviews', 'storeReviews'):
        for review in evidence.get(key, {}).get('items', []):
            if isinstance(review, dict):
                items.append(review)
    return items


def _text_anomaly_markers(value: Any) -> set[str]:
    text = re.sub(r'\s+', ' ', str(value or '')).strip().casefold()
    if not text:
        return set()

    markers: set[str] = set()
    tokens = re.findall(r'[a-z]+', text)
    if re.search(r'([a-z])\1{5,}', text):
        markers.add('repeated_characters')
    if any(len(token) >= 24 for token in tokens):
        markers.add('very_long_token')
    if any(
        len(token) >= 8 and not any(character in VOWELS for character in token)
        for token in tokens
    ):
        markers.add('vowelless_token')
    if any(re.search(r'([a-z]{2,3})\1{3,}', token) for token in tokens):
        markers.add('repeated_fragment')
    return markers


def _variation_labels(product: dict[str, Any]) -> list[str]:
    options = product.get('variationCollection', {}).get('options')
    if not isinstance(options, list):
        options = product.get('variations')
    if not isinstance(options, list):
        return []

    labels: list[str] = []
    for option in options:
        value = option.get('label') if isinstance(option, dict) else option
        label = re.sub(r'\s+', ' ', str(value or '')).strip()
        if label:
            labels.append(label)
    return labels


def _listing_text_signals(product: dict[str, Any]) -> list[dict[str, Any]]:
    signals: list[dict[str, Any]] = []
    anomalous_fields = [
        (field, _text_anomaly_markers(product.get(field)))
        for field in ('name', 'description')
    ]
    anomalous_fields = [(field, markers) for field, markers in anomalous_fields if markers]
    if anomalous_fields:
        field_labels = {'name': 'nama produk', 'description': 'deskripsi'}
        labels = [field_labels[field] for field, _ in anomalous_fields]
        impact = 25 if len(anomalous_fields) >= 2 else 15
        signals.append(
            _signal(
                'LISTING_TEXT_ANOMALY',
                'Teks listing tidak lazim',
                impact,
                'Pola karakter berulang atau token yang tidak lazim terdeteksi pada '
                f"{' dan '.join(labels)}.",
                [f'product.{field}' for field, _ in anomalous_fields],
            )
        )

    variation_labels = _variation_labels(product)
    anomalous_variations = [
        label for label in variation_labels if _text_anomaly_markers(label)
    ]
    if len(anomalous_variations) >= 2:
        impact = 20 if len(anomalous_variations) >= 3 else 15
        signals.append(
            _signal(
                'VARIATION_TEXT_ANOMALY',
                'Label variasi tidak lazim',
                impact,
                f'{len(anomalous_variations)} dari {len(variation_labels)} label variasi '
                'memiliki pola teks yang tidak lazim.',
                ['product.variationCollection.options'],
            )
        )
    return signals


def _metric_at_least(value: Any, minimum: int) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value >= minimum


def _has_meaningful_reputation(evidence: dict[str, Any]) -> bool:
    product = evidence.get('product', {})
    store_summary = evidence.get('storeSummary', {})
    store = evidence.get('store', {})
    return any(
        (
            store_summary.get('isOfficialStore') is True,
            _metric_at_least(product.get('ratingCount'), 3),
            _metric_at_least(product.get('soldCountLowerBound'), 3),
            _metric_at_least(evidence.get('productReviews', {}).get('sampleSize'), 3),
            _metric_at_least(store.get('ratingCount'), 10),
            _metric_at_least(store.get('soldCount'), 10),
            _metric_at_least(evidence.get('storeReviews', {}).get('sampleSize'), 3),
        )
    )


def _format_idr(value: int | float) -> str:
    return f"Rp{value:,.0f}".replace(',', '.')


def _high_value_price_signal(evidence: dict[str, Any]) -> dict[str, Any] | None:
    price = evidence.get('product', {}).get('price')
    if (
        not isinstance(price, (int, float))
        or isinstance(price, bool)
        or price < HIGH_VALUE_PRICE_IDR
        or _has_meaningful_reputation(evidence)
    ):
        return None

    impact = 20 if price >= VERY_HIGH_VALUE_PRICE_IDR else 10
    return _signal(
        'HIGH_VALUE_PRICE_WITHOUT_REPUTATION',
        'Transaksi bernilai tinggi tanpa dukungan reputasi',
        impact,
        f'Harga {_format_idr(price)} memiliki nilai transaksi tinggi, sementara evidence yang '
        'terkumpul belum menunjukkan reputasi yang cukup dari badge official, rating, '
        'penjualan, atau sampel review.',
        [
            'product.price',
            'product.ratingCount',
            'product.soldCountLowerBound',
            'storeSummary.isOfficialStore',
            'store.ratingCount',
        ],
    )


def _complaint_signals(evidence: dict[str, Any]) -> list[dict[str, Any]]:
    reviews = _review_items(evidence)
    signals: list[dict[str, Any]] = []
    for code, title, phrases, base_impact in COMPLAINT_RULES:
        matches = []
        for review in reviews:
            text = re.sub(r'\s+', ' ', str(review.get('text') or '')).casefold()
            if text and any(phrase in text for phrase in phrases):
                matches.append(review.get('evidenceId') or 'review_unknown')
        if not matches:
            continue
        impact = min(base_impact + (10 if len(matches) >= 2 else 0), 40)
        signals.append(
            _signal(
                code,
                title,
                impact,
                f'Ditemukan pada {len(matches)} review dalam sampel bounded.',
                matches[:5],
            )
        )
    return signals


def _confidence(evidence: dict[str, Any]) -> dict[str, Any]:
    quality = evidence.get('quality', {})
    cap = quality.get('confidenceCap', 'low')
    score = {'high': 90, 'medium': 70, 'low': 45}.get(cap, 45)
    product_sample = evidence.get('productReviews', {}).get('sampleSize', 0) or 0
    store_sample = evidence.get('storeReviews', {}).get('sampleSize', 0) or 0
    if product_sample + store_sample == 0:
        score -= 15
    if not quality.get('storeReviewPageCollected', False):
        score -= 10
    score = max(20, min(score, 95))
    level = 'high' if score >= 75 else 'medium' if score >= 50 else 'low'
    return {
        'score': score,
        'level': level,
        'collectorCap': cap,
    }


def _limitations(evidence: dict[str, Any]) -> list[str]:
    limitations = [
        'Skor adalah heuristik berbasis evidence yang terkumpul, bukan jaminan keamanan.',
        'Belum tersedia pembanding harga pasar dari sumber eksternal.',
    ]
    product_sample = evidence.get('productReviews', {}).get('sampleSize', 0) or 0
    store_sample = evidence.get('storeReviews', {}).get('sampleSize', 0) or 0
    if product_sample + store_sample:
        limitations.append(
            'Review adalah sampel bounded untuk mencari sinyal risiko, bukan distribusi populasi.'
        )
    else:
        limitations.append('Tidak ada review yang berhasil dikumpulkan.')
    if not evidence.get('quality', {}).get('storeReviewPageCollected', False):
        limitations.append('Halaman review toko tidak berhasil dikumpulkan secara lengkap.')
    if not evidence.get('product', {}).get('variationCollection', {}).get('collected', False):
        limitations.append('Coverage seluruh variasi dan harga produk belum diketahui.')
    return limitations


def score_evidence(evidence: dict[str, Any]) -> dict[str, Any]:
    signals: list[dict[str, Any]] = []
    product = evidence.get('product', {})
    store_summary = evidence.get('storeSummary', {})
    store = evidence.get('store', {})

    signals.extend(_listing_text_signals(product))
    high_value_price_signal = _high_value_price_signal(evidence)
    if high_value_price_signal:
        signals.append(high_value_price_signal)

    if store_summary.get('isOfficialStore') is True:
        signals.append(
            _signal(
                'OFFICIAL_STORE',
                'Tokopedia Mall / Official Store terdeteksi',
                -15,
                'Badge official store terdeteksi langsung pada halaman produk.',
                ['storeSummary.officialStoreEvidence'],
            )
        )

    if evidence.get('storeConsistency', {}).get('namesMatch') is False:
        signals.append(
            _signal(
                'STORE_NAME_MISMATCH',
                'Identitas toko tidak konsisten',
                20,
                'Nama toko pada halaman produk berbeda dengan halaman review toko.',
                ['storeConsistency'],
            )
        )

    product_rating = product.get('rating')
    product_rating_count = product.get('ratingCount') or 0
    if isinstance(product_rating, (int, float)):
        if product_rating <= 3.5:
            signals.append(
                _signal(
                    'LOW_PRODUCT_RATING',
                    'Rating produk rendah',
                    20,
                    f'Rating produk tercatat {product_rating:g}.',
                    ['product.rating'],
                )
            )
        elif product_rating <= 4.2:
            signals.append(
                _signal(
                    'BELOW_AVERAGE_PRODUCT_RATING',
                    'Rating produk perlu diperhatikan',
                    10,
                    f'Rating produk tercatat {product_rating:g}.',
                    ['product.rating'],
                )
            )
        elif product_rating >= 4.7 and product_rating_count >= 50:
            signals.append(
                _signal(
                    'ESTABLISHED_PRODUCT_RATING',
                    'Rating produk kuat',
                    -5,
                    f'Rating {product_rating:g} berasal dari setidaknya {product_rating_count} rating.',
                    ['product.rating', 'product.ratingCount'],
                )
            )

    store_rating = store.get('rating')
    store_rating_count = store.get('ratingCount') or 0
    if isinstance(store_rating, (int, float)):
        if store_rating <= 4.0:
            signals.append(
                _signal(
                    'LOW_STORE_RATING',
                    'Rating toko rendah',
                    20,
                    f'Rating toko tercatat {store_rating:g}.',
                    ['store.rating'],
                )
            )
        elif store_rating <= 4.4:
            signals.append(
                _signal(
                    'BELOW_AVERAGE_STORE_RATING',
                    'Rating toko perlu diperhatikan',
                    10,
                    f'Rating toko tercatat {store_rating:g}.',
                    ['store.rating'],
                )
            )
        elif store_rating >= 4.7 and store_rating_count >= 100:
            signals.append(
                _signal(
                    'ESTABLISHED_STORE_RATING',
                    'Reputasi toko kuat',
                    -10,
                    f'Rating {store_rating:g} berasal dari setidaknya {store_rating_count} rating.',
                    ['store.rating', 'store.ratingCount'],
                )
            )

    signals.extend(_complaint_signals(evidence))
    risk_score = max(0, min(100, 30 + sum(signal['impact'] for signal in signals)))
    if risk_score >= 60:
        verdict = 'high_risk'
    elif risk_score >= 30:
        verdict = 'caution'
    else:
        verdict = 'low_risk'

    return {
        'riskScore': risk_score,
        'trustScore': 100 - risk_score,
        'verdict': verdict,
        'confidence': _confidence(evidence),
        'signals': signals,
        'limitations': _limitations(evidence),
    }


def fallback_explanation(scoring: dict[str, Any]) -> dict[str, Any]:
    summaries = {
        'low_risk': 'Tidak ditemukan sinyal risiko kuat dari data yang berhasil dikumpulkan.',
        'caution': 'Ada beberapa hal yang perlu diperiksa sebelum melanjutkan pembelian.',
        'high_risk': 'Ditemukan sinyal risiko kuat; sebaiknya jangan terburu-buru membeli.',
    }
    risk_signals = sorted(
        (signal for signal in scoring['signals'] if signal['impact'] > 0),
        key=lambda signal: signal['impact'],
        reverse=True,
    )
    protective_signals = [signal for signal in scoring['signals'] if signal['impact'] < 0]
    reasons = [signal['explanation'] for signal in (risk_signals or protective_signals)[:3]]
    if not reasons:
        reasons = ['Data dasar produk tersedia, tetapi sinyal reputasi yang kuat masih terbatas.']
    return {
        'summary': summaries[scoring['verdict']],
        'reasons': reasons,
        'followUpQuestions': [
            'Apakah harga produk sudah dibandingkan dengan penjual tepercaya lainnya?',
            'Apakah detail variasi, garansi, dan kebijakan pengembalian sudah sesuai kebutuhan?',
        ],
    }


def build_result(
    scoring: dict[str, Any],
    explanation: dict[str, Any],
    *,
    explanation_source: str,
    model: str | None,
) -> dict[str, Any]:
    return {
        'schemaVersion': ANALYSIS_SCHEMA_VERSION,
        'scoreMethod': 'deterministic_heuristic_v2',
        **scoring,
        'explanation': explanation,
        'explanationSource': explanation_source,
        'model': model,
    }
