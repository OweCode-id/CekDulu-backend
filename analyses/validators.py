import re
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

ALLOWED_TOKOPEDIA_HOSTS = {
    'tokopedia.com',
    'www.tokopedia.com',
    'm.tokopedia.com',
}
MAX_URL_LENGTH = 2_048


@dataclass(frozen=True)
class TokopediaURLValidationError(ValueError):
    code: str
    message: str

    def __str__(self) -> str:
        return self.message


def normalize_tokopedia_product_url(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TokopediaURLValidationError('invalid_url', 'URL produk wajib diisi.')

    raw_url = value.strip()
    if len(raw_url) > MAX_URL_LENGTH:
        raise TokopediaURLValidationError(
            'url_too_long',
            f'URL produk tidak boleh lebih dari {MAX_URL_LENGTH} karakter.',
        )

    try:
        parsed = urlsplit(raw_url)
        port = parsed.port
    except ValueError as exc:
        raise TokopediaURLValidationError('invalid_url', 'Format URL produk tidak valid.') from exc

    if parsed.scheme.lower() != 'https':
        raise TokopediaURLValidationError(
            'https_required',
            'URL produk harus menggunakan HTTPS.',
        )
    if parsed.username or parsed.password:
        raise TokopediaURLValidationError(
            'credentials_not_allowed',
            'Credential di dalam URL tidak diizinkan.',
        )

    host = (parsed.hostname or '').lower().rstrip('.')
    if host not in ALLOWED_TOKOPEDIA_HOSTS:
        raise TokopediaURLValidationError(
            'unsupported_host',
            'URL harus berasal dari halaman produk Tokopedia.',
        )
    if port not in (None, 443):
        raise TokopediaURLValidationError(
            'port_not_allowed',
            'Port khusus di dalam URL tidak diizinkan.',
        )

    normalized_path = re.sub(r'/+', '/', parsed.path).rstrip('/')
    path_parts = [part for part in normalized_path.split('/') if part]
    if len(path_parts) < 2 or path_parts[-1].casefold() == 'review':
        raise TokopediaURLValidationError(
            'product_url_required',
            'URL harus menunjuk ke halaman produk Tokopedia, bukan halaman toko atau ulasan.',
        )

    normalized_host = host if port is None else f'{host}:{port}'
    return urlunsplit(('https', normalized_host, normalized_path, '', ''))
