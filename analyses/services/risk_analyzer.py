from __future__ import annotations

import re
from typing import Any

ANALYSIS_SCHEMA_VERSION = 'cekdulu-risk-analysis-0.1.0'

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
        'scoreMethod': 'deterministic_heuristic_v1',
        **scoring,
        'explanation': explanation,
        'explanationSource': explanation_source,
        'model': model,
    }
