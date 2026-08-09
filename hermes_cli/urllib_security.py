"""Security policy for credential-bearing stdlib urllib requests."""
from __future__ import annotations
import copy
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable
from typing import Any
_CROSS_ORIGIN_SAFE_HEADERS = frozenset({'accept', 'user-agent'})
_DEFAULT_PORTS = {'http': 80, 'https': 443}

def url_origin(url: str) -> tuple[str, str, int | None]:
    """Return a normalized (scheme, hostname, effective port) origin."""
    parsed = urllib.parse.urlparse(url)
    scheme = (parsed.scheme or '').lower()
    port = parsed.port
    return (scheme, (parsed.hostname or '').lower().rstrip('.'), port if port is not None else _DEFAULT_PORTS.get(scheme))

class SafeCredentialRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Preserve request headers only while redirects stay on one origin."""

    def __init__(self, original_url: str, *, cross_origin_safe_headers: Iterable[str]=_CROSS_ORIGIN_SAFE_HEADERS) -> None:
        self._original_origin = url_origin(original_url)
        self._cross_origin_safe_headers = frozenset((str(name).lower() for name in cross_origin_safe_headers))

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is None:
            return None
        resolved_url = urllib.parse.urljoin(req.full_url, newurl)
        if url_origin(resolved_url) != self._original_origin:
            for name, _value in list(redirected.header_items()):
                if name.lower() not in self._cross_origin_safe_headers:
                    redirected.remove_header(name)
        return redirected

class _CrossOriginRequestSanitizer(urllib.request.BaseHandler):
    """Strip headers after installed request processors have run."""
    handler_order = float('inf')

    def __init__(self, original_url: str) -> None:
        self._original_origin = url_origin(original_url)

    def _sanitize(self, request: urllib.request.Request):
        if url_origin(request.full_url) != self._original_origin:
            for name, _value in list(request.header_items()):
                if name.lower() not in _CROSS_ORIGIN_SAFE_HEADERS:
                    request.remove_header(name)
        return request
    http_request = _sanitize
    https_request = _sanitize

def _secure_opener_from_installed_policy(original_url: str):
    """Clone the installed opener's handlers, replacing redirect policy only."""
    installed = getattr(urllib.request, '_opener', None)
    if installed is None:
        installed = urllib.request.build_opener()
    handlers = [copy.copy(handler) for handler in getattr(installed, 'handlers', ()) if not isinstance(handler, urllib.request.HTTPRedirectHandler)]
    handlers.append(SafeCredentialRedirectHandler(original_url))
    handlers.append(_CrossOriginRequestSanitizer(original_url))
    secured = urllib.request.build_opener(*handlers)
    setattr(secured, '_hermes_initial_addheaders', list(getattr(installed, 'addheaders', ())))
    secured.addheaders = []
    return secured

def open_credentialed_url(request: urllib.request.Request, *, timeout: float, opener_factory: Callable[..., Any] | None=None):
    """Open a request without forwarding credentials across origins.

    The default preserves an application-installed opener's proxy, TLS,
    cookies, custom protocol handlers, and instrumentation while replacing its
    redirect handler. ``opener_factory`` is an explicit test seam; security is
    never disabled based on global ``urlopen`` identity.
    """
    if opener_factory is None:
        opener = _secure_opener_from_installed_policy(request.full_url)
        for name, value in getattr(opener, '_hermes_initial_addheaders', ()):
            if not request.has_header(name):
                request.add_header(name, value)
    else:
        opener = opener_factory(SafeCredentialRedirectHandler(request.full_url))
    return opener.open(request, timeout=timeout)
__all__ = ['SafeCredentialRedirectHandler', 'open_credentialed_url', 'url_origin']