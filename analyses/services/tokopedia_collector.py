from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse, urlunparse

from playwright.sync_api import (
    BrowserContext,
    Page,
    Playwright,
    sync_playwright,
)
from playwright.sync_api import (
    Error as PlaywrightError,
)
from playwright.sync_api import (
    TimeoutError as PlaywrightTimeoutError,
)

from analyses.validators import (
    ALLOWED_TOKOPEDIA_HOSTS,
    TokopediaURLValidationError,
    normalize_tokopedia_product_url,
)

SCHEMA_VERSION = 'cekdulu-targeted-collector-0.4.0'

SELECTORS = {
    'product_name': [
        '[data-testid="lblPDPDetailProductName"]',
        '[data-testid="lblPDPProductNameJumper"]',
        'h1',
    ],
    'price': [
        '[data-testid="lblPDPDetailProductPrice"]',
        '[data-testid="pdpProductPrice"]',
        'meta[property="product:price:amount"]',
    ],
    'original_price': [
        '[data-testid="lblPDPDetailOriginalPrice"]',
        '[data-testid*="OriginalPrice"]',
    ],
    'sold': ['[data-testid="lblPDPDetailProductSoldCounter"]'],
    'rating': ['[data-testid="lblPDPDetailProductRatingNumber"]'],
    'rating_count': ['[data-testid="lblPDPDetailProductRatingCounter"]'],
    'product_info': ['[data-testid="lblPDPInfoProduk"]'],
    'description': ['[data-testid="lblPDPDescriptionProduk"]'],
    'shop_name': [
        '[data-testid="llbPDPFooterShopName"]',
        '[data-testid="pdpShopCredibilityRow"]',
    ],
    'official_badge': ['[data-testid="pdpShopBadgeOS"]'],
    'store_header': ['[data-testid="shopRatingDetailHeader"]'],
    'store_name': ['[data-testid="shopNameHeader"]'],
}

BLOCK_PHRASES = {
    'captcha': ('captcha', 'verifikasi bahwa kamu manusia', 'verify you are human'),
    'accessDenied': ('access denied', 'akses ditolak', 'forbidden'),
    'loginRequired': ('silakan masuk untuk melanjutkan', 'login untuk melanjutkan'),
    'blocked': ('unusual traffic', 'aktivitas tidak biasa', 'temporarily blocked'),
}

TRANSIENT_NETWORK_MARKERS = (
    'ERR_HTTP2_PROTOCOL_ERROR',
    'ERR_NETWORK_CHANGED',
    'ERR_CONNECTION_RESET',
    'ERR_CONNECTION_CLOSED',
    'ERR_TIMED_OUT',
    'ERR_NAME_NOT_RESOLVED',
)


@dataclass(frozen=True)
class CollectorConfig:
    timeout_ms: int = 45_000
    max_product_reviews: int = 10
    max_store_reviews: int = 10
    navigation_attempts: int = 3
    retry_backoff_ms: tuple[int, ...] = (750, 2_000)
    headed: bool = False
    keep_images: bool = False
    collect_store_reviews: bool = True

    def __post_init__(self) -> None:
        if self.timeout_ms < 1_000:
            raise ValueError('timeout_ms minimal 1000.')
        if not 0 <= self.max_product_reviews <= 40:
            raise ValueError('max_product_reviews harus berada pada rentang 0-40.')
        if not 0 <= self.max_store_reviews <= 40:
            raise ValueError('max_store_reviews harus berada pada rentang 0-40.')
        if not 1 <= self.navigation_attempts <= 3:
            raise ValueError('navigation_attempts harus berada pada rentang 1-3.')
        if not self.retry_backoff_ms or any(delay < 0 for delay in self.retry_backoff_ms):
            raise ValueError('retry_backoff_ms harus berisi jeda non-negatif.')


@dataclass
class PageStatus:
    requestedUrl: str
    finalUrl: str | None
    responseStatus: int | None
    durationMs: int
    navigationAttempts: int
    blockSignals: dict[str, bool]
    errorCode: str | None = None
    errorMessage: str | None = None


class CollectorError(RuntimeError):
    def __init__(self, code: str, message: str, navigation_attempts: int = 0):
        super().__init__(message)
        self.code = code
        self.navigation_attempts = navigation_attempts


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def clean_text(value: str | None, max_length: int | None = None) -> str | None:
    if value is None:
        return None
    cleaned = re.sub(r'\s+', ' ', value).strip()
    if not cleaned:
        return None
    return cleaned[:max_length] if max_length is not None else cleaned


def clean_url(url: str) -> str:
    parsed = urlparse(url)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path.rstrip('/'), '', '', ''))


def ensure_allowed_redirect(url: str) -> None:
    parsed = urlparse(url)
    host = (parsed.hostname or '').lower().rstrip('.')
    if parsed.scheme != 'https' or host not in ALLOWED_TOKOPEDIA_HOSTS:
        raise CollectorError(
            'UNSUPPORTED_REDIRECT',
            'Tokopedia mengarahkan browser menuju host yang tidak diizinkan.',
        )


def derive_store_review_url(product_url: str) -> str:
    normalized = normalize_tokopedia_product_url(product_url)
    parsed = urlparse(normalized)
    path_parts = [part for part in parsed.path.split('/') if part]
    return f'https://www.tokopedia.com/{path_parts[0]}/review'


def first_text(page: Page, selectors: list[str], max_length: int | None = None) -> str | None:
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            if locator.count() == 0:
                continue
            value = (
                locator.get_attribute('content')
                if selector.startswith('meta')
                else locator.inner_text(timeout=2_000)
            )
            value = clean_text(value, max_length=max_length)
            if value:
                return value
        except PlaywrightTimeoutError:
            continue
    return None


def first_attr(page: Page, selectors: list[str], attribute: str) -> str | None:
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            if locator.count() == 0:
                continue
            value = clean_text(locator.get_attribute(attribute))
            if value:
                return value
        except PlaywrightTimeoutError:
            continue
    return None


def parse_idr(value: str | None) -> int | None:
    if not value:
        return None
    digits = re.sub(r'[^\d]', '', value)
    return int(digits) if digits else None


def parse_float(value: str | None) -> float | None:
    if not value:
        return None
    match = re.search(r'\d+(?:[.,]\d+)?', value)
    return float(match.group(0).replace(',', '.')) if match else None


def parse_compact_number(value: str | None) -> int | None:
    if not value:
        return None
    normalized = value.lower().replace('\xa0', ' ')
    match = re.search(r'(\d+(?:[.,]\d+)?)\s*(rb|ribu|k|jt|juta|m)?', normalized)
    if not match:
        return None
    number = float(match.group(1).replace(',', '.'))
    suffix = match.group(2)
    multiplier = 1
    if suffix in {'rb', 'ribu', 'k'}:
        multiplier = 1_000
    elif suffix in {'jt', 'juta', 'm'}:
        multiplier = 1_000_000
    return int(number * multiplier)


def parse_compact_metric(value: str | None) -> dict[str, Any]:
    raw = clean_text(value)
    normalized = (raw or '').lower().replace('\xa0', ' ')
    return {
        'value': parse_compact_number(raw),
        'raw': raw,
        'approximate': bool(re.search(r'\b(?:rb|ribu|k|jt|juta|m)\b', normalized)),
        'lowerBound': '+' in normalized,
    }


def parse_product_info(items: list[str], raw_text: str | None) -> dict[str, str | None]:
    result: dict[str, str | None] = {
        'condition': None,
        'unitWeight': None,
        'minimumPurchase': None,
        'category': None,
        'showcase': None,
    }
    joined = ' | '.join(clean_text(item) or '' for item in items if clean_text(item))
    text = joined or clean_text(raw_text) or ''
    if not text:
        return result

    labels = [
        ('condition', 'Kondisi'),
        ('unitWeight', 'Berat Satuan'),
        ('minimumPurchase', 'Min. Beli'),
        ('category', 'Kategori'),
        ('showcase', 'Etalase'),
    ]
    for index, (key, label) in enumerate(labels):
        following = [re.escape(next_label) for _, next_label in labels[index + 1 :]]
        boundary = '|'.join(following)
        if boundary:
            pattern = rf'{re.escape(label)}\s*:\s*(.*?)(?=(?:{boundary})\s*:|$)'
        else:
            pattern = rf'{re.escape(label)}\s*:\s*(.*)$'
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            result[key] = clean_text(match.group(1).strip(' |'))
    return result


def parse_store_header(value: str | None) -> dict[str, Any]:
    result = {
        'rating': None,
        'ratingRaw': None,
        'ratingCount': None,
        'ratingCountRaw': None,
        'ratingCountApproximate': False,
        'soldCount': None,
        'soldCountRaw': None,
        'soldCountApproximate': False,
        'headerRaw': clean_text(value),
    }
    if not value:
        return result

    rating_match = re.search(r'^\s*(\d+(?:[.,]\d+)?)', value)
    rating_count_match = re.search(r'\(([^)]+)\)', value)
    sold_match = re.search(
        r'(\d+(?:[.,]\d+)?\s*(?:rb|ribu|k|jt|juta|m)?\+?)\s+terjual',
        value,
        flags=re.IGNORECASE,
    )
    rating_raw = rating_match.group(1) if rating_match else None
    rating_count = parse_compact_metric(rating_count_match.group(1) if rating_count_match else None)
    sold_count = parse_compact_metric(sold_match.group(1) if sold_match else None)
    result.update(
        {
            'rating': parse_float(rating_raw),
            'ratingRaw': rating_raw,
            'ratingCount': rating_count['value'],
            'ratingCountRaw': rating_count['raw'],
            'ratingCountApproximate': rating_count['approximate'],
            'soldCount': sold_count['value'],
            'soldCountRaw': sold_count['raw'],
            'soldCountApproximate': sold_count['approximate'],
        }
    )
    return result


def detect_block_signals(page: Page) -> dict[str, bool]:
    try:
        body_text = page.locator('body').inner_text(timeout=5_000).lower()
    except PlaywrightTimeoutError:
        body_text = ''
    return {
        key: any(phrase in body_text for phrase in phrases)
        for key, phrases in BLOCK_PHRASES.items()
    }


def empty_block_signals() -> dict[str, bool]:
    return {key: False for key in BLOCK_PHRASES}


def detect_anonymous_ui(page: Page) -> dict[str, Any]:
    login_visible = False
    register_visible = False
    try:
        login_visible = page.get_by_text('Masuk', exact=True).first.is_visible(timeout=1_000)
    except Exception:
        pass
    try:
        register_visible = page.get_by_text('Daftar', exact=True).first.is_visible(timeout=1_000)
    except Exception:
        pass
    return {
        'anonymousUiSignal': login_visible or register_visible,
        'loginButtonVisible': login_visible,
        'registerButtonVisible': register_visible,
    }


def is_transient_network_error(error: Exception) -> bool:
    message = str(error).upper()
    return any(marker in message for marker in TRANSIENT_NETWORK_MARKERS)


def navigate_with_retry(
    page: Page,
    url: str,
    config: CollectorConfig,
) -> tuple[int | None, int]:
    last_error: Exception | None = None
    for attempt in range(1, config.navigation_attempts + 1):
        try:
            response = page.goto(url, wait_until='domcontentloaded', timeout=config.timeout_ms)
            ensure_allowed_redirect(page.url)
            return (response.status if response else None), attempt
        except (PlaywrightError, PlaywrightTimeoutError) as exc:
            last_error = exc
            if not is_transient_network_error(exc) or attempt >= config.navigation_attempts:
                break
            backoff_index = min(attempt - 1, len(config.retry_backoff_ms) - 1)
            page.wait_for_timeout(config.retry_backoff_ms[backoff_index])

    if last_error and is_transient_network_error(last_error):
        raise CollectorError(
            'NETWORK_ERROR',
            f'Navigasi gagal setelah {config.navigation_attempts} percobaan: {last_error}',
            navigation_attempts=config.navigation_attempts,
        ) from last_error
    if isinstance(last_error, PlaywrightTimeoutError):
        raise CollectorError(
            'COLLECTION_TIMEOUT',
            str(last_error),
            navigation_attempts=attempt,
        ) from last_error
    raise CollectorError(
        'NAVIGATION_ERROR',
        str(last_error or 'Navigasi gagal.'),
        navigation_attempts=attempt,
    ) from last_error


def wait_for_product(page: Page, timeout_ms: int) -> None:
    selectors = ', '.join(SELECTORS['product_name'][:2])
    page.locator(selectors).first.wait_for(state='attached', timeout=timeout_ms)


def scroll_to_reviews(page: Page) -> None:
    candidates = [
        '[data-testid="reviewSortingTitle"]',
        '[data-testid="lblItemUlasan"]',
        '[data-testid="review"]',
    ]
    for selector in candidates:
        locator = page.locator(selector).first
        try:
            if locator.count() > 0:
                locator.scroll_into_view_if_needed(timeout=3_000)
                page.wait_for_timeout(1_000)
                return
        except PlaywrightTimeoutError:
            continue
    page.evaluate('window.scrollTo(0, Math.floor(document.body.scrollHeight * 0.72))')
    page.wait_for_timeout(1_000)


def extract_official_store_status(page: Page) -> dict[str, Any]:
    selector = SELECTORS['official_badge'][0]
    locator = page.locator(selector)
    try:
        for index in range(locator.count()):
            badge = locator.nth(index)
            if not badge.is_visible(timeout=500):
                continue
            label = (
                clean_text(badge.get_attribute('aria-label'))
                or clean_text(badge.get_attribute('alt'))
                or clean_text(badge.get_attribute('title'))
                or clean_text(badge.inner_text(timeout=1_000))
                or 'Tokopedia Mall'
            )
            return {
                'detected': True,
                'badgeType': 'tokopedia_mall',
                'selector': selector,
                'testId': 'pdpShopBadgeOS',
                'labelRaw': label,
            }
    except PlaywrightTimeoutError:
        pass
    return {
        'detected': False,
        'badgeType': None,
        'selector': selector,
        'testId': 'pdpShopBadgeOS',
        'labelRaw': None,
    }


def extract_variations(page: Page) -> dict[str, Any]:
    raw_options = page.evaluate(
        """
        () => {
          const directSelector = [
            'button[data-testid*="variant" i]',
            '[role="radio"][data-testid*="variant" i]',
            '[role="option"][data-testid*="variant" i]',
            '[role="button"][aria-label*="varian" i]',
            '[role="radio"][aria-label*="varian" i]'
          ].join(',');
          const visible = (el) => {
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style.visibility !== 'hidden' && style.display !== 'none' &&
              rect.width > 0 && rect.height > 0;
          };
          const direct = Array.from(document.querySelectorAll(directSelector));
          const structural = [];
          const labels = Array.from(document.querySelectorAll('p, span, div, h2, h3'))
            .filter((el) => /^(pilih\\s+)?(varian|variasi|warna|ukuran|size|kapasitas)\\b/i
              .test((el.textContent || '').replace(/\\s+/g, ' ').trim()));
          for (const label of labels) {
            let root = label.parentElement;
            for (let depth = 0; depth < 4 && root; depth += 1) {
              const buttons = Array.from(root.querySelectorAll('button, [role="radio"], [role="option"]'));
              if (buttons.length >= 1 && buttons.length <= 30) {
                structural.push(...buttons);
                break;
              }
              root = root.parentElement;
            }
          }
          return Array.from(new Set([...direct, ...structural]))
            .filter(visible)
            .map((el) => {
              const text = (el.textContent || '').replace(/\\s+/g, ' ').trim();
              const ariaLabel = (el.getAttribute('aria-label') || '').replace(/\\s+/g, ' ').trim();
              const className = typeof el.className === 'string' ? el.className : '';
              const selected =
                el.getAttribute('aria-checked') === 'true' ||
                el.getAttribute('aria-pressed') === 'true' ||
                el.getAttribute('aria-selected') === 'true' ||
                el.getAttribute('data-selected') === 'true' ||
                /(?:^|[ _-])(active|selected)(?:$|[ _-])/i.test(className);
              return {
                label: text || ariaLabel || null,
                ariaLabel: ariaLabel || null,
                testId: el.getAttribute('data-testid'),
                selected,
                disabled: el.matches(':disabled') || el.getAttribute('aria-disabled') === 'true',
              };
            })
            .filter((item) => item.label && item.label.length <= 200);
        }
        """
    )
    options: list[dict[str, Any]] = []
    seen: set[tuple[str, str | None]] = set()
    for item in raw_options:
        label = clean_text(item.get('label'), max_length=200)
        key = ((label or '').casefold(), item.get('testId'))
        if not label or key in seen:
            continue
        seen.add(key)
        options.append(
            {
                'label': label,
                'ariaLabel': clean_text(item.get('ariaLabel'), max_length=200),
                'testId': clean_text(item.get('testId'), max_length=200),
                'selected': bool(item.get('selected')),
                'disabled': bool(item.get('disabled')),
            }
        )
    collected = bool(options)
    return {
        'status': 'collected' if collected else 'not_detected',
        'collected': collected,
        'selectedOptions': [item['label'] for item in options if item['selected']],
        'options': options if collected else None,
        'method': 'visible_interactive_dom_controls',
    }


def extract_review_bucket(
    page: Page,
    limit: int,
    prefix: str,
    bucket: str,
    include_product: bool = False,
) -> list[dict[str, Any]]:
    if limit <= 0:
        return []
    scroll_to_reviews(page)
    locator = page.locator('[data-testid="lblItemUlasan"]')
    try:
        locator.first.wait_for(state='attached', timeout=5_000)
    except PlaywrightTimeoutError:
        for fraction in (0.55, 0.75, 0.90, 1.0):
            page.evaluate(
                '(fraction) => window.scrollTo(0, Math.floor(document.body.scrollHeight * fraction))',
                fraction,
            )
            page.wait_for_timeout(700)
            if locator.count() > 0:
                break
        if locator.count() == 0:
            return []

    raw_reviews = locator.evaluate_all(
        """
        (nodes, options) => {
          function textOf(root, selector) {
            const el = root ? root.querySelector(selector) : null;
            return el ? (el.textContent || '').replace(/\\s+/g, ' ').trim() : null;
          }
          const datePattern = /\\b(?:\\d+\\s+(?:menit|jam|hari|minggu|bulan|tahun)\\s+lalu|\\d{1,2}\\s+(?:jan(?:uari)?|feb(?:ruari)?|mar(?:et)?|apr(?:il)?|mei|jun(?:i)?|jul(?:i)?|agu(?:stus)?|sep(?:tember)?|okt(?:ober)?|nov(?:ember)?|des(?:ember)?)\\s+\\d{4})\\b/i;
          return nodes.slice(0, options.limit).map((node, index) => {
            let root = node.closest('article, [data-testid*="reviewItem" i], [data-testid*="reviewCard" i]');
            root = root || node.parentElement;
            for (let depth = 0; depth < 8 && root && root.parentElement; depth += 1) {
              const parent = root.parentElement;
              if (parent.querySelectorAll('[data-testid="lblItemUlasan"]').length !== 1) break;
              root = parent;
            }
            let ratingEl = root ? root.querySelector('[data-testid="icnStarRating"][aria-label*="bintang"]') : null;
            if (!ratingEl) {
              let cursor = node.parentElement;
              for (let depth = 0; depth < 8 && cursor; depth += 1) {
                ratingEl = cursor.querySelector('[data-testid="icnStarRating"][aria-label*="bintang"]');
                if (ratingEl) break;
                cursor = cursor.parentElement;
              }
            }
            const ratingLabel = ratingEl ? (ratingEl.getAttribute('aria-label') || '') : '';
            const ratingMatch = ratingLabel.match(/(\\d+(?:[.,]\\d+)?)/);
            const variant = textOf(root, '[data-testid="lblVarian"]') ||
              textOf(root, '[data-testid*="Varian"]');
            const dateContexts = [
              textOf(root, 'time'),
              textOf(root, '[data-testid*="date" i]'),
              textOf(root, '[data-testid*="tanggal" i]'),
              ratingEl && ratingEl.parentElement ? ratingEl.parentElement.textContent : null,
              root ? root.textContent : null,
            ];
            let date = null;
            for (const context of dateContexts) {
              const match = context ? context.match(datePattern) : null;
              if (match) { date = match[0]; break; }
            }
            let productName = options.includeProduct ? (
              textOf(root, '[data-testid*="productName" i]') ||
              textOf(root, '[data-testid*="itemName" i]') ||
              textOf(root, 'a[href] h3')
            ) : null;
            let productLinkEl = null;
            if (options.includeProduct && root) {
              productLinkEl = Array.from(root.querySelectorAll('a[href]')).find((link) => {
                const text = (link.textContent || '').replace(/\\s+/g, ' ').trim();
                const href = link.getAttribute('href') || '';
                const linkRect = link.getBoundingClientRect();
                const reviewRect = node.getBoundingClientRect();
                return text.length >= 4 && linkRect.right <= reviewRect.left + 20 &&
                  /tokopedia\\.com|^\\//i.test(href);
              }) || null;
              productName = productName || (productLinkEl
                ? (productLinkEl.textContent || '').replace(/\\s+/g, ' ').trim()
                  .replace(/\\s*Varian\\s*:.*$/i, '')
                : null);
            }
            const hasImage = !!(root && root.querySelector('[data-testid="imgItemPhotoulasan"]'));
            const hasVideo = !!(root && root.querySelector('video[aria-label="review video"], video'));
            const fullText = (node.textContent || '').replace(/\\s+/g, ' ').trim();
            return {
              evidenceId: options.prefix + '_' + String(index + 1).padStart(2, '0'),
              samplingBucket: options.bucket,
              rating: ratingMatch ? Number(ratingMatch[1].replace(',', '.')) : null,
              text: fullText.slice(0, 500),
              textTruncated: fullText.length > 500 || /(?:\\.\\.\\.|…)$/u.test(fullText),
              variant: variant ? variant.replace(/^Varian\\s*:\\s*/i, '').slice(0, 200) : null,
              dateRaw: date ? date.slice(0, 100) : null,
              reviewedProductName: productName ? productName.slice(0, 300) : null,
              reviewedProductUrl: productLinkEl ? productLinkEl.href : null,
              hasMedia: hasImage || hasVideo,
              mediaTypes: [
                ...(hasImage ? ['image'] : []),
                ...(hasVideo ? ['video'] : []),
              ],
            };
          });
        }
        """,
        {
            'limit': limit,
            'prefix': prefix,
            'bucket': bucket,
            'includeProduct': include_product,
        },
    )
    result: list[dict[str, Any]] = []
    for review in raw_reviews:
        text = clean_text(review.get('text'), max_length=500)
        if not text or text in {'.', '-', '...'}:
            continue
        review['text'] = text
        result.append(review)
    return result


def review_identity(review: dict[str, Any]) -> tuple[Any, ...]:
    return (
        review.get('rating'),
        (review.get('text') or '').casefold(),
        (review.get('variant') or '').casefold(),
        (review.get('reviewedProductName') or '').casefold(),
    )


def try_apply_lowest_rating_sort(page: Page) -> dict[str, Any]:
    result = {'attempted': False, 'applied': False, 'control': None}
    sorting = page.locator('[data-testid="reviewSorting"]').first
    try:
        if sorting.count() == 0 or not sorting.is_visible(timeout=500):
            return result
        result['attempted'] = True
        result['control'] = 'reviewSorting'
        sorting.click(timeout=2_000)
        choices = page.get_by_text(
            re.compile(r'^(rating terendah|terendah|bintang terendah)$', re.IGNORECASE)
        )
        for index in range(choices.count()):
            choice = choices.nth(index)
            if choice.is_visible(timeout=500):
                choice.click(timeout=2_000)
                page.wait_for_timeout(1_200)
                result['applied'] = True
                return result
    except Exception:
        return result
    return result


def collect_review_sample(
    page: Page,
    limit: int,
    prefix: str,
    include_product: bool = False,
) -> dict[str, Any]:
    base = {
        'samplingMode': 'bounded_stratified_best_effort',
        'representativeOfPopulation': False,
        'purpose': 'risk_signal_discovery',
        'requestedSize': limit,
    }
    if limit <= 0:
        return {
            **base,
            'sampleSize': 0,
            'buckets': [],
            'ratingDistribution': {},
            'distinctRatingCount': 0,
            'items': [],
        }

    initial_target = limit if limit < 4 else (limit + 1) // 2
    initial_requested = initial_target
    initial = extract_review_bucket(
        page,
        initial_target,
        prefix,
        'initial_visible',
        include_product=include_product,
    )
    sort_result = {'attempted': False, 'applied': False, 'control': None}
    low_rated: list[dict[str, Any]] = []
    remaining = limit - len(initial)
    low_target = remaining
    if remaining > 0:
        sort_result = try_apply_lowest_rating_sort(page)
        if sort_result['applied']:
            low_rated = extract_review_bucket(
                page,
                remaining,
                prefix,
                'lowest_rating',
                include_product=include_product,
            )
        else:
            initial_requested = limit
            initial = extract_review_bucket(
                page,
                limit,
                prefix,
                'initial_visible',
                include_product=include_product,
            )
            remaining = limit - len(initial)

    merged: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for review in [*initial, *low_rated]:
        identity = review_identity(review)
        if identity in seen:
            continue
        seen.add(identity)
        merged.append(review)
        if len(merged) >= limit:
            break
    for index, review in enumerate(merged, start=1):
        review['evidenceId'] = f'{prefix}_{index:02d}'

    distribution: dict[str, int] = {}
    for review in merged:
        rating = review.get('rating')
        key = 'unknown' if rating is None else f'{rating:g}'
        distribution[key] = distribution.get(key, 0) + 1
    return {
        **base,
        'sampleSize': len(merged),
        'buckets': [
            {
                'name': 'initial_visible',
                'requested': initial_requested,
                'returned': len(initial),
            },
            {
                'name': 'lowest_rating',
                'requested': max(0, low_target),
                'returned': len(low_rated),
                'filterAttempted': sort_result['attempted'],
                'filterApplied': sort_result['applied'],
                'control': sort_result['control'],
            },
        ],
        'ratingDistribution': distribution,
        'distinctRatingCount': len([key for key in distribution if key != 'unknown']),
        'items': merged,
    }


def status_from_error(
    url: str,
    page: Page,
    started: float,
    response_status: int | None,
    attempts: int,
    error: CollectorError,
) -> PageStatus:
    final_url = page.url if page.url != 'about:blank' else None
    return PageStatus(
        requestedUrl=url,
        finalUrl=final_url,
        responseStatus=response_status,
        durationMs=int((time.perf_counter() - started) * 1_000),
        navigationAttempts=attempts,
        blockSignals=detect_block_signals(page) if final_url else empty_block_signals(),
        errorCode=error.code,
        errorMessage=str(error),
    )


def collect_product_page(
    page: Page,
    url: str,
    config: CollectorConfig,
) -> tuple[PageStatus, dict[str, Any]]:
    started = time.perf_counter()
    response_status: int | None = None
    attempts = 0
    try:
        response_status, attempts = navigate_with_retry(page, url, config)
        wait_for_product(page, timeout_ms=min(config.timeout_ms, 20_000))
        block_signals = detect_block_signals(page)
        if any(block_signals.values()):
            raise CollectorError(
                'BLOCKED_OR_CAPTCHA',
                'Halaman menunjukkan sinyal blokir, CAPTCHA, atau login wall.',
            )

        info_root = page.locator('[data-testid="lblPDPInfoProduk"]').first
        try:
            if info_root.count() > 0:
                info_root.scroll_into_view_if_needed(timeout=2_000)
                page.wait_for_timeout(500)
        except PlaywrightTimeoutError:
            pass
        info_locator = page.locator('[data-testid="lblPDPInfoProduk"] li')
        try:
            info_items = info_locator.all_inner_texts() if info_locator.count() else []
        except PlaywrightTimeoutError:
            info_items = []

        info_raw = first_text(page, SELECTORS['product_info'], max_length=2_000)
        canonical = first_attr(page, ['link[rel="canonical"]'], 'href') or page.url
        ensure_allowed_redirect(canonical)
        shop_name = first_text(page, SELECTORS['shop_name'], max_length=300)
        shop_name = re.sub(r'\s*Follow\s*$', '', shop_name or '', flags=re.IGNORECASE).strip()
        shop_name = shop_name or None

        price_raw = first_text(page, SELECTORS['price'])
        original_price_raw = first_text(page, SELECTORS['original_price'])
        sold_metric = parse_compact_metric(first_text(page, SELECTORS['sold']))
        rating_raw = first_text(page, SELECTORS['rating'])
        rating_count_metric = parse_compact_metric(first_text(page, SELECTORS['rating_count']))
        variation_collection = extract_variations(page)
        official_store = extract_official_store_status(page)

        product = {
            'name': first_text(page, SELECTORS['product_name'], max_length=500),
            'price': parse_idr(price_raw),
            'priceRaw': price_raw,
            'originalPrice': parse_idr(original_price_raw),
            'originalPriceRaw': original_price_raw,
            'currency': 'IDR',
            'variations': variation_collection['options'],
            'variationCollection': variation_collection,
            'soldCountLowerBound': sold_metric['value'],
            'soldCountRaw': sold_metric['raw'],
            'soldCountApproximate': sold_metric['approximate'],
            'soldCountIsLowerBound': sold_metric['lowerBound'],
            'rating': parse_float(rating_raw),
            'ratingRaw': rating_raw,
            'ratingCount': rating_count_metric['value'],
            'ratingCountRaw': rating_count_metric['raw'],
            'ratingCountApproximate': rating_count_metric['approximate'],
            **parse_product_info(info_items, info_raw),
            'description': first_text(page, SELECTORS['description'], max_length=4_000),
            'canonicalUrl': clean_url(canonical),
        }
        reviews = collect_review_sample(
            page,
            config.max_product_reviews,
            'product_review',
        )
        data = {
            'product': product,
            'storeSummary': {
                'name': shop_name,
                'isOfficialStore': official_store['detected'],
                'officialStoreEvidence': official_store,
            },
            'productReviews': reviews,
            'session': detect_anonymous_ui(page),
        }
        status = PageStatus(
            requestedUrl=url,
            finalUrl=page.url,
            responseStatus=response_status,
            durationMs=int((time.perf_counter() - started) * 1_000),
            navigationAttempts=attempts,
            blockSignals=block_signals,
        )
        return status, data
    except CollectorError as exc:
        return status_from_error(
            url,
            page,
            started,
            response_status,
            exc.navigation_attempts or attempts,
            exc,
        ), {}
    except PlaywrightTimeoutError as exc:
        error = CollectorError('COLLECTION_TIMEOUT', str(exc))
        return status_from_error(
            url,
            page,
            started,
            response_status,
            attempts,
            error,
        ), {}
    except Exception as exc:
        error = CollectorError('INTERNAL_ERROR', f'{type(exc).__name__}: {exc}')
        return status_from_error(
            url,
            page,
            started,
            response_status,
            attempts,
            error,
        ), {}


def collect_store_page(
    page: Page,
    review_url: str,
    config: CollectorConfig,
) -> tuple[PageStatus, dict[str, Any]]:
    started = time.perf_counter()
    response_status: int | None = None
    attempts = 0
    try:
        response_status, attempts = navigate_with_retry(page, review_url, config)
        try:
            page.locator('[data-testid="shopNameHeader"]').first.wait_for(
                state='attached',
                timeout=min(config.timeout_ms, 15_000),
            )
        except PlaywrightTimeoutError:
            pass
        block_signals = detect_block_signals(page)
        if any(block_signals.values()):
            raise CollectorError(
                'BLOCKED_OR_CAPTCHA',
                'Halaman toko menunjukkan sinyal blokir, CAPTCHA, atau login wall.',
            )

        header_text = first_text(page, SELECTORS['store_header'], max_length=500)
        reviews = collect_review_sample(
            page,
            config.max_store_reviews,
            'store_review',
            include_product=True,
        )
        canonical = first_attr(page, ['link[rel="canonical"]'], 'href') or page.url
        ensure_allowed_redirect(canonical)
        data = {
            'store': {
                'name': first_text(page, SELECTORS['store_name'], max_length=300),
                **parse_store_header(header_text),
                'reviewUrl': clean_url(canonical),
            },
            'storeReviews': reviews,
        }
        status = PageStatus(
            requestedUrl=review_url,
            finalUrl=page.url,
            responseStatus=response_status,
            durationMs=int((time.perf_counter() - started) * 1_000),
            navigationAttempts=attempts,
            blockSignals=block_signals,
        )
        return status, data
    except CollectorError as exc:
        return status_from_error(
            review_url,
            page,
            started,
            response_status,
            exc.navigation_attempts or attempts,
            exc,
        ), {}
    except PlaywrightTimeoutError as exc:
        error = CollectorError('COLLECTION_TIMEOUT', str(exc))
        return status_from_error(
            review_url,
            page,
            started,
            response_status,
            attempts,
            error,
        ), {}
    except Exception as exc:
        error = CollectorError('INTERNAL_ERROR', f'{type(exc).__name__}: {exc}')
        return status_from_error(
            review_url,
            page,
            started,
            response_status,
            attempts,
            error,
        ), {}


def install_resource_filter(context: BrowserContext, keep_images: bool) -> None:
    blocked_types = {'font', 'media'}
    if not keep_images:
        blocked_types.add('image')

    def handler(route: Any) -> None:
        if route.request.resource_type in blocked_types:
            route.abort()
        else:
            route.continue_()

    context.route('**/*', handler)


def build_output(
    source_url: str,
    product_status: PageStatus,
    product_data: dict[str, Any],
    store_status: PageStatus | None,
    store_data: dict[str, Any],
) -> dict[str, Any]:
    product_ok = product_status.errorCode is None
    store_ok = store_status is None or store_status.errorCode is None
    if not product_ok:
        status = 'failed'
    elif store_ok:
        status = 'completed'
    else:
        status = 'partial'

    result: dict[str, Any] = {
        'schemaVersion': SCHEMA_VERSION,
        'collectedAt': utc_now(),
        'status': status,
        'sourceUrl': clean_url(source_url),
        'collection': {
            'productPage': asdict(product_status),
            'storeReviewPage': asdict(store_status) if store_status else None,
        },
        **product_data,
        **store_data,
        'limitations': [
            'Ulasan adalah sampel bounded untuk menemukan sinyal risiko, bukan distribusi populasi.',
            "Jumlah terjual dengan tanda '+' disimpan sebagai batas bawah.",
            'Angka berakhiran rb/jt merupakan nilai pendekatan dari teks antarmuka.',
            'Status variasi not_detected berarti coverage belum diketahui.',
            'Selector Tokopedia dapat berubah dan perlu dimonitor.',
        ],
    }
    required_fields = {
        'product.name': result.get('product', {}).get('name'),
        'product.price': result.get('product', {}).get('price'),
        'storeSummary.name': result.get('storeSummary', {}).get('name'),
    }
    missing = [key for key, value in required_fields.items() if value in (None, '')]
    product = result.get('product', {})
    store_summary = result.get('storeSummary', {})
    store = result.get('store', {})
    product_reviews = result.get('productReviews', {})
    store_reviews = result.get('storeReviews', {})

    variation_collected = product.get('variationCollection', {}).get('collected', False)
    product_review_size = product_reviews.get('sampleSize', 0)
    product_rating_diversity = product_reviews.get('distinctRatingCount', 0)
    store_review_size = store_reviews.get('sampleSize', 0)
    store_rating_diversity = store_reviews.get('distinctRatingCount', 0)
    listing_missing = [
        key
        for key in ('product.name', 'product.price', 'storeSummary.name')
        if key in missing
    ]
    listing_ready = product_ok and not listing_missing
    price_ready = product_ok and product.get('price') is not None
    store_ready = product_ok and bool(store_summary.get('name')) and (
        store.get('rating') is not None
        or store.get('ratingCount') is not None
        or store.get('soldCount') is not None
        or store_summary.get('isOfficialStore') is True
    )
    product_reviews_ready = product_review_size >= 3
    store_reviews_ready = store_review_size >= 3

    warnings: list[str] = []
    if not variation_collected:
        warnings.append('VARIATIONS_NOT_COLLECTED')
    if product_review_size < 5:
        warnings.append('PRODUCT_REVIEW_SAMPLE_SMALL')
    elif product_rating_diversity < 2:
        warnings.append('PRODUCT_REVIEW_SAMPLE_NOT_DIVERSE')
    if store_status is not None:
        if store_review_size < 5:
            warnings.append('STORE_REVIEW_SAMPLE_SMALL')
        elif store_rating_diversity < 2:
            warnings.append('STORE_REVIEW_SAMPLE_NOT_DIVERSE')
    for sample_name, sample in (
        ('PRODUCT', product_reviews),
        ('STORE', store_reviews),
    ):
        lowest_bucket = next(
            (
                bucket
                for bucket in sample.get('buckets', [])
                if bucket.get('name') == 'lowest_rating'
            ),
            None,
        )
        if (
            lowest_bucket
            and lowest_bucket.get('requested', 0) > 0
            and not lowest_bucket.get('filterApplied')
        ):
            warnings.append(f'{sample_name}_LOW_RATING_SORT_NOT_APPLIED')

    summary_name = clean_text(store_summary.get('name'))
    store_name = clean_text(store.get('name'))
    names_match: bool | None = None
    if summary_name and store_name:
        normalized_summary = re.sub(r'[^a-z0-9]+', '', summary_name.casefold())
        normalized_store = re.sub(r'[^a-z0-9]+', '', store_name.casefold())
        names_match = normalized_summary == normalized_store
        if not names_match:
            warnings.append('STORE_NAME_MISMATCH')
    result['storeConsistency'] = {
        'productPageName': summary_name,
        'storeReviewPageName': store_name,
        'namesMatch': names_match,
    }
    if not listing_ready:
        confidence_cap = 'low'
    elif warnings:
        confidence_cap = 'medium'
    else:
        confidence_cap = 'high'

    result['quality'] = {
        'collectionComplete': product_ok and store_ok,
        'storeReviewPageCollected': store_status is not None and store_ok,
        'requiredFieldsPresent': len(required_fields) - len(missing),
        'requiredFieldsTotal': len(required_fields),
        'missingRequiredFields': missing,
        'analyzers': {
            'listing': {
                'ready': listing_ready,
                'confidence': 'high' if listing_ready else 'low',
                'missingFields': listing_missing,
            },
            'price': {
                'ready': price_ready,
                'confidence': (
                    'high'
                    if price_ready and variation_collected
                    else 'medium'
                    if price_ready
                    else 'low'
                ),
                'scope': 'selected_listing_state',
                'variationCoverageKnown': variation_collected,
            },
            'productReviews': {
                'ready': product_reviews_ready,
                'confidence': (
                    'high'
                    if product_review_size >= 8 and product_rating_diversity >= 3
                    else 'medium'
                    if product_review_size >= 5 and product_rating_diversity >= 2
                    else 'low'
                ),
                'sampleSize': product_review_size,
                'distinctRatingCount': product_rating_diversity,
                'representativeOfPopulation': False,
            },
            'store': {
                'ready': store_ready,
                'confidence': (
                    'high'
                    if store_ready and names_match is not False
                    else 'medium'
                    if store_ready
                    else 'low'
                ),
                'storeReviewSampleReady': store_reviews_ready,
                'reviewSampleRepresentativeOfPopulation': False,
            },
        },
        'warnings': list(dict.fromkeys(warnings)),
        'confidenceCap': confidence_cap,
        'sufficientForAnalysis': listing_ready
        and (price_ready or store_ready or product_reviews_ready),
    }
    return result


class TokopediaCollector:
    def __init__(self, config: CollectorConfig | None = None):
        self.config = config or CollectorConfig()

    def collect(self, url: str) -> dict[str, Any]:
        try:
            source_url = normalize_tokopedia_product_url(url)
        except TokopediaURLValidationError as exc:
            raise CollectorError(exc.code.upper(), exc.message) from exc

        try:
            with sync_playwright() as playwright:
                return self._collect_with_playwright(playwright, source_url)
        except CollectorError:
            raise
        except PlaywrightError as exc:
            raise CollectorError('BROWSER_UNAVAILABLE', str(exc)) from exc

    def _collect_with_playwright(
        self,
        playwright: Playwright,
        source_url: str,
    ) -> dict[str, Any]:
        browser = playwright.chromium.launch(headless=not self.config.headed)
        context = browser.new_context(
            locale='id-ID',
            timezone_id='Asia/Jakarta',
            viewport={'width': 1_440, 'height': 1_000},
        )
        install_resource_filter(context, keep_images=self.config.keep_images)
        try:
            product_page = context.new_page()
            try:
                product_status, product_data = collect_product_page(
                    product_page,
                    source_url,
                    self.config,
                )
            finally:
                product_page.close()

            store_status: PageStatus | None = None
            store_data: dict[str, Any] = {}
            if product_status.errorCode is None and self.config.collect_store_reviews:
                resolved_product_url = (
                    product_data.get('product', {}).get('canonicalUrl')
                    or product_status.finalUrl
                )
                try:
                    review_url = derive_store_review_url(resolved_product_url)
                except (TokopediaURLValidationError, TypeError) as exc:
                    store_status = PageStatus(
                        requestedUrl='',
                        finalUrl=None,
                        responseStatus=None,
                        durationMs=0,
                        navigationAttempts=0,
                        blockSignals=empty_block_signals(),
                        errorCode='STORE_URL_DERIVATION_FAILED',
                        errorMessage=str(exc),
                    )
                else:
                    store_page = context.new_page()
                    try:
                        store_status, store_data = collect_store_page(
                            store_page,
                            review_url,
                            self.config,
                        )
                    finally:
                        store_page.close()
            return build_output(
                source_url,
                product_status,
                product_data,
                store_status,
                store_data,
            )
        finally:
            context.close()
            browser.close()
