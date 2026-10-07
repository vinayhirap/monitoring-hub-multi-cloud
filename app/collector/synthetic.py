# app/collector/synthetic.py
"""
Synthetic/uptime (blackbox) monitoring -- active HTTP/HTTPS/TCP/DNS probes
run FROM this app AGAINST a configured target, on a schedule. See
db/migrations/031_synthetic_monitoring.sql's module docstring for why
this exists (this app is otherwise 100% passive) and the integration
design (a failing check becomes a normal `alerts` row against an
auto-created `resources` row, so correlate.py/health_score.py/
escalation.py/rca.py/the LLM summarizer all pick it up with zero
changes to any of them).

Runs in scheduler.py's "critical" tier (2-min cadence, see run_loop's
docstring) but only ever probes checks that are actually DUE
(next_check_at <= NOW()) -- a check configured for a 5-minute interval
genuinely only runs every ~5 minutes, not every 2-minute scheduler
tick; the tick is just how often this module LOOKS for due work.
"""
import ipaddress
import json
import logging
import math
import os
import socket
import ssl
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool

from app.db import get_connection

logger = logging.getLogger(__name__)

# How many checks to probe in a single scheduler tick -- bounds
# worst-case tick duration if many checks come due at once (e.g. right
# after a bulk import). Remaining due checks are simply picked up on
# the next 2-min tick, a few minutes later than ideal but never
# blocking the rest of this tier's other work.
MAX_CHECKS_PER_CYCLE = 100

# How long synthetic_check_results history is kept -- same 30-day
# horizon as metric_history (see baseline.py's LOOKBACK_DAYS), enough
# for a meaningful uptime-% trend without unbounded table growth.
RESULT_RETENTION_DAYS = 30

_HTTP_USER_AGENT = "monitoring-hub-synthetic-check/1.0"

# Hard bounds on user-configurable probe parameters (audit b20). A
# probe runs inline in the leader's critical-tier thread, so an
# unbounded timeout lets one check stall the whole tier.
MIN_TIMEOUT_SECONDS = 1
MAX_TIMEOUT_SECONDS = 60
MAX_REDIRECTS = 5
MAX_BODY_BYTES = 1_000_000  # keyword search reads at most this much

# ── TLS certificate expiry alerting ('https' checks) ────────────────
# Fires its own alert (metric_name 'synthetic_cert_expiry') on the check's
# synthetic resource, separate from the 'synthetic_uptime' outage alert.
# Constants rather than rows in `thresholds`: that mechanism evaluates the
# `metrics` table, which synthetic checks never write to. Override per
# deployment with SYNTHETIC_CERT_WARN_DAYS / SYNTHETIC_CERT_CRIT_DAYS
# (whole days; CRIT must be smaller than WARN or both fall back).
CERT_ALERT_METRIC = "synthetic_cert_expiry"
_DEFAULT_CERT_WARN_DAYS = 30
_DEFAULT_CERT_CRIT_DAYS = 7


def _load_cert_thresholds():
    def _env(name, default):
        raw = (os.getenv(name) or "").strip()
        if not raw:
            return default
        try:
            v = int(raw)
        except ValueError:
            v = 0
        if v < 1:
            logger.warning(f"[synthetic] ignoring invalid {name}={raw!r}")
            return default
        return v
    warn = _env("SYNTHETIC_CERT_WARN_DAYS", _DEFAULT_CERT_WARN_DAYS)
    crit = _env("SYNTHETIC_CERT_CRIT_DAYS", _DEFAULT_CERT_CRIT_DAYS)
    if crit >= warn:
        logger.warning("[synthetic] SYNTHETIC_CERT_CRIT_DAYS must be smaller than SYNTHETIC_CERT_WARN_DAYS -- using defaults")
        return _DEFAULT_CERT_WARN_DAYS, _DEFAULT_CERT_CRIT_DAYS
    return warn, crit


CERT_WARN_DAYS, CERT_CRIT_DAYS = _load_cert_thresholds()

_TLS_FIELDS = ("cert_days_left", "cert_not_after", "cert_subject", "cert_issuer",
               "tls_version", "tls_cipher", "handshake_ms", "cert_valid")

# ── SSRF guard (audit b20) ───────────────────────────────────────────
# Probes are created by any synthetic.manage holder and run from inside
# the app's VPC with the instance role attached, so without this a
# check could target 169.254.169.254 (IMDS credentials), 127.0.0.1
# (MySQL/Redis/VictoriaMetrics/uvicorn) or any VPC-internal host, and
# read the result back through status_code / expected_keyword /
# error_message. Enforced at CONNECT time on every hop (redirects
# included) against the IP actually dialled, so DNS rebinding between
# validation and connect cannot bypass it.
#
# Always blocked: loopback, link-local (cloud metadata), multicast,
# unspecified, reserved. Other non-global ranges (RFC1918, CGNAT, ULA)
# are blocked unless listed in SYNTHETIC_ALLOWED_PRIVATE_CIDRS
# (comma-separated), for deployments that deliberately probe internal
# endpoints.
_NAT64_PREFIX = ipaddress.ip_network("64:ff9b::/96")


def _load_allowed_private_nets():
    nets = []
    for raw in (os.getenv("SYNTHETIC_ALLOWED_PRIVATE_CIDRS") or "").split(","):
        raw = raw.strip()
        if not raw:
            continue
        try:
            nets.append(ipaddress.ip_network(raw, strict=False))
        except ValueError:
            logger.warning(f"[synthetic] ignoring invalid SYNTHETIC_ALLOWED_PRIVATE_CIDRS entry {raw!r}")
    return nets


_ALLOWED_PRIVATE_NETS = _load_allowed_private_nets()


class UnsafeTargetError(Exception):
    """Target resolves to an address synthetic probes may not reach."""


def _is_blocked_ip(ip) -> bool:
    if isinstance(ip, str):
        ip = ipaddress.ip_address(ip.split("%", 1)[0])
    if ip.version == 6:
        if ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        elif ip in _NAT64_PREFIX:
            ip = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    if ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified or ip.is_reserved:
        return True
    if ip.version == 4 and ip in ipaddress.ip_network("0.0.0.0/8"):
        return True
    if not ip.is_global:
        return not any(ip.version == n.version and ip in n for n in _ALLOWED_PRIVATE_NETS)
    return False


def _resolve_safe_ip(host: str, port: int) -> str:
    """Resolves host and returns one address to dial. Raises
    UnsafeTargetError if ANY resolved address is blocked (so a
    round-robin record mixing public and internal IPs is rejected
    outright), socket.gaierror if it doesn't resolve."""
    host = (host or "").strip().strip("[]").rstrip(".")
    if not host:
        raise UnsafeTargetError("empty host")
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    addrs = [info[4][0] for info in infos]
    if not addrs:
        raise socket.gaierror(f"no addresses for {host}")
    for a in addrs:
        if _is_blocked_ip(a):
            raise UnsafeTargetError(
                f"target {host} resolves to a blocked (internal/metadata) address"
            )
    return addrs[0]


class _GuardedHTTPConnection(HTTPConnection):
    def _new_conn(self):
        ip = _resolve_safe_ip(self._dns_host, self.port)
        original = self._dns_host
        self._dns_host = ip
        try:
            return super()._new_conn()
        finally:
            self._dns_host = original


class _GuardedHTTPSConnection(HTTPSConnection):
    # TLS SNI + certificate verification still use self.host (the
    # hostname), only the TCP dial goes to the pre-validated IP.
    def _new_conn(self):
        ip = _resolve_safe_ip(self._dns_host, self.port)
        original = self._dns_host
        self._dns_host = ip
        try:
            return super()._new_conn()
        finally:
            self._dns_host = original


class _GuardedHTTPPool(HTTPConnectionPool):
    ConnectionCls = _GuardedHTTPConnection


class _GuardedHTTPSPool(HTTPSConnectionPool):
    ConnectionCls = _GuardedHTTPSConnection


class _GuardedAdapter(HTTPAdapter):
    def __init__(self, *args, https_pool_cls=None, **kwargs):
        # set BEFORE super().__init__(): it calls init_poolmanager()
        self._https_pool_cls = https_pool_cls or _GuardedHTTPSPool
        super().__init__(*args, **kwargs)

    def init_poolmanager(self, *args, **kwargs):
        super().init_poolmanager(*args, **kwargs)
        self.poolmanager.pool_classes_by_scheme = {
            "http": _GuardedHTTPPool, "https": self._https_pool_cls,
        }


# ── TLS inspection (https checks) ────────────────────────────────────
# Reads version / cipher / peer certificate from the SAME connection the
# probe uses (no second handshake). Subclasses the guarded connection, so
# the SSRF guard (pre-validated IP dial) and certificate + hostname
# verification are exactly the ones the plain probe uses: nothing here
# relaxes either. A failed verification raises out of connect() as before;
# the exception is only noted (per host) so the probe can report WHY.

def _sink_key(host) -> str:
    return (host or "").strip().strip("[]").rstrip(".").lower()


def _dn_short(rdns, prefer=("commonName", "organizationName")) -> str:
    """peercert subject/issuer ((('commonName','x'),), ...) -> one short label."""
    flat = {}
    for rdn in rdns or ():
        for k, v in rdn:
            flat.setdefault(k, v)
    for k in prefer:
        if flat.get(k):
            return str(flat[k])[:120]
    return ""


def _summarize_peercert(cert: dict, now_ts: float = None) -> dict:
    """getpeercert() dict -> the cert_* result fields. Never raises: a field
    that can't be read is None."""
    out = {"cert_days_left": None, "cert_not_after": None, "cert_subject": None, "cert_issuer": None}
    if not cert:
        return out
    try:
        not_after_ts = ssl.cert_time_to_seconds(cert.get("notAfter"))
        now_ts = time.time() if now_ts is None else now_ts
        out["cert_days_left"] = math.floor((not_after_ts - now_ts) / 86400)
        out["cert_not_after"] = datetime.fromtimestamp(not_after_ts, timezone.utc).replace(tzinfo=None)
    except (TypeError, ValueError, OverflowError):
        pass
    subject = _dn_short(cert.get("subject"))
    if not subject:
        sans = [v for k, v in (cert.get("subjectAltName") or ()) if k == "DNS"]
        subject = (sans[0] if sans else "")[:120]
    out["cert_subject"] = subject or None
    out["cert_issuer"] = _dn_short(cert.get("issuer")) or None
    return out


def _tls_info_from_socket(sock, handshake_ms=None) -> dict:
    info = {k: None for k in _TLS_FIELDS}
    try:
        info["tls_version"] = sock.version()
        cipher = sock.cipher()
        info["tls_cipher"] = (cipher[0] if cipher else None)
        info.update(_summarize_peercert(sock.getpeercert()))
    except (ssl.SSLError, ValueError, OSError, AttributeError):
        pass
    info["handshake_ms"] = handshake_ms
    return info


# OpenSSL X509_V_ERR_* codes -> wording (ssl.SSLCertVerificationError.verify_code)
_X509_EXPIRED, _X509_NOT_YET_VALID = 10, 9
_X509_SELF_SIGNED = (18, 19)
_X509_NO_ISSUER = (20, 21)
_X509_HOSTNAME_MISMATCH = (62, 64)


def _describe_tls_error(exc, host: str):
    """ssl exception from the handshake -> (specific message, cert_valid).
    cert_valid is False for a verification failure, None for any other
    handshake failure (no verdict on the certificate)."""
    host = host or "the host"
    if isinstance(exc, ssl.SSLCertVerificationError):
        code = getattr(exc, "verify_code", None)
        msg = str(getattr(exc, "verify_message", None) or exc)
        if code == _X509_EXPIRED:
            return "TLS: certificate has expired", False
        if code == _X509_NOT_YET_VALID:
            return "TLS: certificate is not yet valid", False
        if code in _X509_HOSTNAME_MISMATCH or (code is None and ("hostname" in msg.lower() or "doesn't match" in msg.lower())):
            return f"TLS: certificate is not valid for '{host}' (hostname mismatch)", False
        if code in _X509_SELF_SIGNED:
            return "TLS: self-signed certificate (not trusted)", False
        if code in _X509_NO_ISSUER:
            return "TLS: cannot verify the certificate chain (unknown issuer or missing intermediate certificate)", False
        return f"TLS: certificate verification failed: {msg}"[:490], False
    if isinstance(exc, ssl.SSLError):
        reason = getattr(exc, "reason", None) or str(exc)
        return f"TLS handshake failed: {reason}"[:490], None
    return f"TLS handshake failed: {exc}"[:490], None


def _make_inspecting_https_pool(sink: dict):
    """Per-probe pool class that records, per hostname, the first connection's
    TLS facts (or the handshake error) into `sink`. First-wins on purpose: a
    redirect to another host must not overwrite the configured target's cert."""

    class _InspectingHTTPSConnection(_GuardedHTTPSConnection):
        def _new_conn(self):
            sock = super()._new_conn()
            self._synth_tcp_done = time.monotonic()
            return sock

        def connect(self):
            self._synth_tcp_done = None
            key = _sink_key(self.host)
            try:
                super().connect()
            except ssl.SSLError as e:
                sink.setdefault(key, {"error": e})
                raise
            done = self._synth_tcp_done
            handshake_ms = int((time.monotonic() - done) * 1000) if done else None
            sink.setdefault(key, {"info": _tls_info_from_socket(self.sock, handshake_ms)})

    class _InspectingHTTPSPool(HTTPSConnectionPool):
        ConnectionCls = _InspectingHTTPSConnection

    return _InspectingHTTPSPool


def _guarded_session(tls_sink: dict = None) -> requests.Session:
    session = requests.Session()
    # Never route probes through an env-configured proxy: the guard
    # would validate the proxy's address instead of the real target.
    session.trust_env = False
    session.max_redirects = MAX_REDIRECTS
    adapter = _GuardedAdapter(
        max_retries=0,
        https_pool_cls=_make_inspecting_https_pool(tls_sink) if tls_sink is not None else None,
    )
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def _split_tcp_target(target: str):
    """'host:port' or '[v6]:port' -> (host, port). Raises ValueError."""
    target = (target or "").strip()
    if target.startswith("["):
        host, sep, rest = target[1:].partition("]")
        if not sep or not rest.startswith(":"):
            raise ValueError("tcp target must be host:port")
        port_str = rest[1:]
    else:
        host, _, port_str = target.rpartition(":")
    port = int(port_str)
    if not host or not (1 <= port <= 65535):
        raise ValueError("tcp target must be host:port with port 1-65535")
    return host, port


def validate_target(check_type: str, target: str, strict_scheme: bool = False) -> None:
    """API-side validation (create/update). Raises ValueError with a
    user-safe message. A hostname that doesn't resolve yet is accepted
    (the connect-time guard still applies on every probe); one that
    resolves to a blocked address is rejected.

    'https' needs an https:// URL. 'http' keeps accepting http:// AND https://
    (existing checks rely on it); strict_scheme=True -- used by the API on
    CREATE only -- additionally steers a new https:// URL to the 'https' type."""
    target = (target or "").strip()
    if not target:
        raise ValueError("target is required")
    if len(target) > 500:
        raise ValueError("target must be at most 500 characters")
    if check_type == "https":
        parts = urlsplit(target)
        if parts.scheme != "https" or not parts.hostname:
            raise ValueError(
                "HTTPS checks need a full https:// URL "
                "(use the HTTP type for http:// URLs)")
        try:
            host, port = parts.hostname, parts.port or 443
        except ValueError:
            raise ValueError("https target has an invalid port")
    elif check_type == "http":
        parts = urlsplit(target)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("http checks need a full URL (http:// or https://)")
        if strict_scheme and parts.scheme == "https":
            raise ValueError(
                "This is an https:// URL - choose the 'HTTPS (with certificate check)' "
                "check type (the HTTP type is for http:// URLs)")
        try:
            host, port = parts.hostname, parts.port or (443 if parts.scheme == "https" else 80)
        except ValueError:
            raise ValueError("http target has an invalid port")
    elif check_type == "tcp":
        host, port = _split_tcp_target(target)
    elif check_type == "dns":
        if any(c in target for c in "/: "):
            raise ValueError("dns checks take a bare hostname")
        return
    else:
        raise ValueError("unknown check_type")
    try:
        _resolve_safe_ip(host, port)
    except UnsafeTargetError as e:
        raise ValueError(str(e))
    except (socket.gaierror, UnicodeError, OSError):
        pass


def validate_https_redirect_option(check_type: str, target: str) -> None:
    """expect_https_redirect: only for https checks on the default port, since
    the plain-HTTP counterpart URL is derived by swapping scheme (port 80)."""
    if check_type != "https":
        raise ValueError("expect_https_redirect is only available for HTTPS checks")
    parts = urlsplit((target or "").strip())
    try:
        port = parts.port
    except ValueError:
        raise ValueError("https target has an invalid port")
    if parts.scheme != "https" or port not in (None, 443):
        raise ValueError("expect_https_redirect needs an https:// target on the default port (443)")


def _clamp_timeout(timeout) -> int:
    try:
        timeout = int(timeout)
    except (TypeError, ValueError):
        timeout = 10
    return max(MIN_TIMEOUT_SECONDS, min(MAX_TIMEOUT_SECONDS, timeout))


# Bounded pool for DNS probes -- replaces socket.setdefaulttimeout(),
# which mutated a process-wide default under 2 workers + collector
# threads and didn't bound getaddrinfo anyway.
_DNS_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="synthetic-dns")


def _probe_http(target: str, timeout: int, expected_status: int, expected_keyword: str):
    return _probe_http_impl(target, timeout, expected_status, expected_keyword)


def _probe_http_impl(target: str, timeout: int, expected_status: int, expected_keyword: str,
                     tls_sink: dict = None):
    timeout = _clamp_timeout(timeout)
    start = time.monotonic()
    session = _guarded_session(tls_sink)
    try:
        resp = session.get(
            target, timeout=timeout, allow_redirects=True, stream=True,
            headers={"User-Agent": _HTTP_USER_AGENT},
        )
        try:
            status_ok = resp.status_code == (expected_status or 200)
            keyword_ok = True
            if expected_keyword:
                body = bytearray()
                for chunk in resp.iter_content(chunk_size=16384):
                    body.extend(chunk)
                    if len(body) >= MAX_BODY_BYTES or time.monotonic() - start > timeout:
                        break
                text = bytes(body[:MAX_BODY_BYTES]).decode(resp.encoding or "utf-8", errors="replace")
                keyword_ok = expected_keyword in text
        finally:
            resp.close()
        elapsed_ms = int((time.monotonic() - start) * 1000)
        success = status_ok and keyword_ok
        error = None
        if not success:
            reasons = []
            if not status_ok:
                reasons.append(f"expected status {expected_status or 200}, got {resp.status_code}")
            if not keyword_ok:
                reasons.append(f"expected keyword '{expected_keyword}' not found in response")
            error = "; ".join(reasons)[:490]
        return success, elapsed_ms, resp.status_code, error
    except UnsafeTargetError as e:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        return False, elapsed_ms, None, f"blocked: {e}"[:490]
    except requests.exceptions.Timeout:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        return False, elapsed_ms, None, f"timed out after {timeout}s"
    except requests.exceptions.RequestException as e:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        return False, elapsed_ms, None, str(e)[:490]
    finally:
        session.close()


def _probe_https_redirect(target: str, timeout: int):
    """expect_https_redirect: GET the plain-HTTP twin of an https:// target
    WITHOUT following redirects and require a 3xx to an https:// Location.
    One extra request per probe, only for checks that opted in; same SSRF
    guard as every other probe. Returns (ok, error)."""
    parts = urlsplit(target)
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    url = urlunsplit(("http", host, parts.path or "/", parts.query, ""))
    session = _guarded_session()
    try:
        resp = session.get(url, timeout=timeout, allow_redirects=False, stream=True,
                           headers={"User-Agent": _HTTP_USER_AGENT})
        try:
            status = resp.status_code
            location = resp.headers.get("Location", "")
        finally:
            resp.close()
        if status in (301, 302, 303, 307, 308):
            if urlsplit(urljoin(url, location)).scheme == "https":
                return True, None
            return False, f"expected HTTP->HTTPS redirect from {url}, but it redirects to {location or 'nowhere'}"[:490]
        return False, f"expected HTTP->HTTPS redirect from {url}, got status {status} (no redirect)"[:490]
    except UnsafeTargetError as e:
        return False, f"blocked: {e}"[:490]
    except requests.exceptions.Timeout:
        return False, f"HTTP->HTTPS redirect check timed out after {timeout}s"
    except requests.exceptions.RequestException as e:
        return False, f"expected HTTP->HTTPS redirect, but {url} is not reachable: {e}"[:490]
    finally:
        session.close()


def _probe_https(target: str, timeout: int, expected_status: int, expected_keyword: str,
                 expect_redirect: bool = False):
    """HTTP probe + TLS facts from the same connection. Returns
    (success, elapsed_ms, status_code, error, tls) where `tls` always has
    every key in _TLS_FIELDS (None when no TLS session was reached).

    A certificate that is valid but near expiry is NOT a probe failure -- the
    separate cert-expiry alert covers it. A handshake / expired / hostname /
    chain failure IS a failure, with specific text."""
    timeout = _clamp_timeout(timeout)
    start = time.monotonic()
    sink = {}
    success, elapsed_ms, status_code, error = _probe_http_impl(
        target, timeout, expected_status, expected_keyword, tls_sink=sink)

    host = _sink_key(urlsplit(target).hostname)
    tls = {k: None for k in _TLS_FIELDS}
    entry = sink.get(host)
    if entry and "info" in entry:
        tls.update(entry["info"])
        tls["cert_valid"] = 1          # reached only if chain + hostname verified
    elif entry and "error" in entry:
        msg, valid = _describe_tls_error(entry["error"], host)
        tls["cert_valid"] = None if valid is None else int(valid)
        success, error = False, msg

    if success and expect_redirect:
        remaining = max(MIN_TIMEOUT_SECONDS, int(timeout - (time.monotonic() - start)))
        ok, redirect_error = _probe_https_redirect(target, remaining)
        if not ok:
            success, error = False, redirect_error
    return success, elapsed_ms, status_code, error, tls


def _cert_alert_level(days_left):
    """days until expiry -> ('CRITICAL'|'WARNING', threshold_days) or None."""
    if days_left is None:
        return None
    if days_left <= CERT_CRIT_DAYS:
        return "CRITICAL", CERT_CRIT_DAYS
    if days_left <= CERT_WARN_DAYS:
        return "WARNING", CERT_WARN_DAYS
    return None


def _probe_tcp(target: str, timeout: int):
    timeout = _clamp_timeout(timeout)
    start = time.monotonic()
    try:
        host, port = _split_tcp_target(target)
        ip = _resolve_safe_ip(host, port)
        with socket.create_connection((ip, port), timeout=timeout):
            elapsed_ms = int((time.monotonic() - start) * 1000)
            return True, elapsed_ms, None, None
    except UnsafeTargetError as e:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        return False, elapsed_ms, None, f"blocked: {e}"[:490]
    except (socket.timeout, TimeoutError):
        elapsed_ms = int((time.monotonic() - start) * 1000)
        return False, elapsed_ms, None, f"connection timed out after {timeout}s"
    except (socket.gaierror, ConnectionRefusedError, OSError, ValueError) as e:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        return False, elapsed_ms, None, str(e)[:490]


def _probe_dns(target: str, timeout: int):
    timeout = _clamp_timeout(timeout)
    start = time.monotonic()
    future = _DNS_EXECUTOR.submit(socket.gethostbyname, target)
    try:
        future.result(timeout=timeout)
        elapsed_ms = int((time.monotonic() - start) * 1000)
        return True, elapsed_ms, None, None
    except FutureTimeout:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        return False, elapsed_ms, None, f"DNS resolution timed out after {timeout}s"
    except (socket.gaierror, UnicodeError, OSError) as e:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        return False, elapsed_ms, None, f"DNS resolution failed: {e}"[:490]


def _run_probe(check: dict):
    if check["check_type"] == "https":
        return _probe_https(check["target"], check["timeout_seconds"],
                            check["expected_status_code"], check["expected_keyword"],
                            bool(check.get("expect_https_redirect")))[:4]
    if check["check_type"] == "http":
        return _probe_http(check["target"], check["timeout_seconds"],
                            check["expected_status_code"], check["expected_keyword"])
    elif check["check_type"] == "tcp":
        return _probe_tcp(check["target"], check["timeout_seconds"])
    elif check["check_type"] == "dns":
        return _probe_dns(check["target"], check["timeout_seconds"])
    return False, None, None, f"unknown check_type '{check['check_type']}'"


def _synthetic_resource_id(check_id: int) -> str:
    return f"synthetic-{check_id}"


def _ensure_resource_row(cursor, check: dict) -> str:
    """Auto-creates/upserts the `resources` row a synthetic check's
    alerts key off of -- see migration 031's docstring for why this is
    the integration point that gets correlate.py/health_score.py/
    escalation.py/rca.py for free. Returns the resource_id string used
    as `resources.resource_id` / `alerts.resource_id`."""
    resource_id = _synthetic_resource_id(check["id"])
    cursor.execute("""
        INSERT INTO resources (aws_account_id, resource_type, resource_id, name, tags)
        VALUES (%s, 'synthetic_check', %s, %s, %s)
        ON DUPLICATE KEY UPDATE name = VALUES(name), tags = VALUES(tags)
    """, (
        check["aws_account_id"], resource_id, check["name"],
        json.dumps({"environment": check["environment"]}),
    ))
    return resource_id


def _write_or_update_alert(cursor, resource_id: str, check: dict, error_message: str):
    # aws_account_id is REQUIRED on every alert row (migration 048): all
    # readers join on it, so the old INSERT (which omitted it) made a synthetic
    # "site is DOWN" alert invisible on every screen.
    account_id = check["aws_account_id"]
    group_key = f"{account_id}:synthetic_check:synthetic_uptime"
    cursor.execute("""
        SELECT id FROM alerts
        WHERE aws_account_id = %s AND resource_id = %s AND metric_name = 'synthetic_uptime'
          AND status IN ('active', 'acknowledged')
        LIMIT 1
    """, (account_id, resource_id))
    existing = cursor.fetchone()
    if existing:
        cursor.execute("""
            UPDATE alerts
            SET current_value = %s, last_seen_at = UTC_TIMESTAMP()
            WHERE id = %s
        """, (check["consecutive_failures"], existing["id"]))
        return

    cursor.execute("""
        INSERT INTO alerts
            (aws_account_id, resource_id, metric_name, severity, environment, group_key, status,
             triggered_at, last_seen_at, healthy_streak, current_value, threshold)
        VALUES (%s, %s, 'synthetic_uptime', 'CRITICAL', %s, %s, 'active',
                UTC_TIMESTAMP(), UTC_TIMESTAMP(), 0, %s, %s)
    """, (
        account_id, resource_id, check["environment"], group_key,
        check["consecutive_failures"], check["consecutive_failure_threshold"],
    ))
    logger.warning(
        f"[synthetic] check '{check['name']}' (id={check['id']}) is DOWN after "
        f"{check['consecutive_failures']} consecutive failures: {error_message}"
    )


def _resolve_alert(cursor, resource_id: str, account_id=None, metric_name: str = "synthetic_uptime"):
    scope = "AND aws_account_id = %s" if account_id is not None else ""
    params = (resource_id,) + ((account_id,) if account_id is not None else ()) + (metric_name,)
    cursor.execute(f"""
        UPDATE alerts SET status = 'resolved', resolved_at = UTC_TIMESTAMP(), last_seen_at = UTC_TIMESTAMP(),
                          resolution_reason = 'recovered', resolved_by = 'system'
        WHERE resource_id = %s {scope} AND metric_name = %s
          AND status IN ('active', 'acknowledged')
    """, params)
    if cursor.rowcount:
        logger.info(f"[synthetic] {resource_id} recovered -- resolved its active {metric_name} alert")


def _write_or_update_cert_alert(cursor, resource_id: str, check: dict, days_left: int,
                                severity: str, threshold_days: int):
    """Opens / refreshes the cert-expiry alert. Fires immediately (no
    consecutive-failure gate: the expiry date is a fact, not a flaky probe).
    Severity follows the days left; a worsening acknowledged alert is reopened,
    same rule as the threshold evaluator."""
    account_id = check["aws_account_id"]
    cursor.execute("""
        SELECT id, severity, status FROM alerts
        WHERE aws_account_id = %s AND resource_id = %s AND metric_name = %s
          AND status IN ('active', 'acknowledged')
        LIMIT 1
    """, (account_id, resource_id, CERT_ALERT_METRIC))
    existing = cursor.fetchone()
    if existing:
        fields = ["current_value = %s", "threshold = %s", "breach_value = %s",
                  "breach_threshold = %s", "last_seen_at = UTC_TIMESTAMP()"]
        params = [days_left, threshold_days, days_left, threshold_days]
        if existing["severity"] != severity:
            fields.append("severity = %s")
            params.append(severity)
            if severity == "CRITICAL" and existing["status"] == "acknowledged":
                fields += ["status = 'active'", "acked = 0", "acked_by = NULL", "acked_at = NULL"]
        params.append(existing["id"])
        cursor.execute(f"UPDATE alerts SET {', '.join(fields)} WHERE id = %s", params)
        return

    group_key = f"{account_id}:synthetic_check:{CERT_ALERT_METRIC}"
    cursor.execute("""
        INSERT INTO alerts
            (aws_account_id, resource_id, metric_name, severity, environment, group_key, status,
             triggered_at, last_seen_at, healthy_streak, current_value, threshold,
             breach_value, breach_threshold)
        VALUES (%s, %s, %s, %s, %s, %s, 'active',
                UTC_TIMESTAMP(), UTC_TIMESTAMP(), 0, %s, %s, %s, %s)
    """, (
        account_id, resource_id, CERT_ALERT_METRIC, severity, check["environment"], group_key,
        days_left, threshold_days, days_left, threshold_days,
    ))
    logger.warning(
        f"[synthetic] check '{check['name']}' (id={check['id']}): TLS certificate expires in "
        f"{days_left} day(s) -- {severity}"
    )


def _apply_cert_alert(cursor, resource_id: str, check: dict, tls: dict):
    """Cert-expiry alert state for an https check, from this probe's TLS facts.
    No certificate seen this probe (handshake failed, host down): leave any
    open alert exactly as it is -- unknown is not 'renewed'."""
    days_left = (tls or {}).get("cert_days_left")
    if days_left is None:
        return
    level = _cert_alert_level(days_left)
    if level is None:
        _resolve_alert(cursor, resource_id, check["aws_account_id"], CERT_ALERT_METRIC)
    else:
        _write_or_update_cert_alert(cursor, resource_id, check, days_left, level[0], level[1])


def _insert_result(cursor, check: dict, success, elapsed_ms, status_code, error, tls=None):
    if tls is None:
        # http / tcp / dns: the original statement, unchanged
        cursor.execute("""
            INSERT INTO synthetic_check_results
                (check_id, checked_at, success, response_time_ms, status_code, error_message)
            VALUES (%s, NOW(), %s, %s, %s, %s)
        """, (check["id"], success, elapsed_ms, status_code, error))
        return
    cursor.execute("""
        INSERT INTO synthetic_check_results
            (check_id, checked_at, success, response_time_ms, status_code, error_message,
             cert_days_left, cert_not_after, cert_subject, cert_issuer,
             tls_version, tls_cipher, handshake_ms, cert_valid)
        VALUES (%s, NOW(), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """, (check["id"], success, elapsed_ms, status_code, error,
          *(tls.get(k) for k in _TLS_FIELDS)))


def run_due_checks() -> int:
    """Probes every enabled check whose next_check_at has passed (or
    was never set -- brand new checks run on the very next tick).
    Returns the number of checks probed this cycle. Non-fatal per
    check -- one check's probe raising an unexpected exception never
    blocks the rest of the batch."""
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    probed = 0
    try:
        cursor.execute("""
            SELECT id, aws_account_id, name, check_type, target,
                   expected_status_code, expected_keyword, expect_https_redirect,
                   timeout_seconds, interval_seconds, consecutive_failure_threshold,
                   environment, consecutive_failures, current_status
            FROM synthetic_checks
            WHERE enabled = 1 AND (next_check_at IS NULL OR next_check_at <= NOW())
            ORDER BY next_check_at IS NULL DESC, next_check_at ASC
            LIMIT %s
        """, (MAX_CHECKS_PER_CYCLE,))
        due = cursor.fetchall()

        for check in due:
            try:
                tls = None
                if check["check_type"] == "https":
                    success, elapsed_ms, status_code, error, tls = _probe_https(
                        check["target"], check["timeout_seconds"], check["expected_status_code"],
                        check["expected_keyword"], bool(check.get("expect_https_redirect")))
                else:
                    success, elapsed_ms, status_code, error = _run_probe(check)

                _insert_result(cursor, check, success, elapsed_ms, status_code, error, tls)

                if success:
                    new_consecutive_failures = 0
                    new_status = "up"
                else:
                    new_consecutive_failures = check["consecutive_failures"] + 1
                    new_status = "down" if new_consecutive_failures >= check["consecutive_failure_threshold"] else check["current_status"]

                cursor.execute("""
                    UPDATE synthetic_checks
                    SET last_checked_at = NOW(),
                        next_check_at = DATE_ADD(NOW(), INTERVAL interval_seconds SECOND),
                        consecutive_failures = %s,
                        current_status = %s
                    WHERE id = %s
                """, (new_consecutive_failures, new_status, check["id"]))

                check["consecutive_failures"] = new_consecutive_failures

                resource_id = _ensure_resource_row(cursor, check)
                if new_status == "down" and check["current_status"] != "down":
                    # Just crossed the threshold this cycle -- fire the alert.
                    _write_or_update_alert(cursor, resource_id, check, error)
                elif new_status == "down":
                    # Still down -- keep the existing active alert's
                    # current_value/last_seen_at fresh.
                    _write_or_update_alert(cursor, resource_id, check, error)
                elif new_status == "up" and check["current_status"] == "down":
                    _resolve_alert(cursor, resource_id, check["aws_account_id"])

                if tls is not None:
                    _apply_cert_alert(cursor, resource_id, check, tls)

                # Commit per check: keeps row locks on alerts/
                # synthetic_checks short (probes are slow network I/O)
                # and stops one failing check's partial writes from
                # being committed alongside everyone else's.
                conn.commit()
                probed += 1

            except Exception:
                conn.rollback()
                logger.exception(
                    f"[synthetic] probe failed unexpectedly for check id={check['id']} "
                    f"('{check.get('name')}') -- skipping this cycle, will retry next tick"
                )
                continue

        conn.commit()
        if probed:
            logger.info(f"[synthetic] probed {probed} due check(s)")
        return probed
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()


def prune_synthetic_results(retain_days: int = RESULT_RETENTION_DAYS) -> int:
    """Deletes synthetic_check_results older than retain_days. Called
    from scheduler.py's low tier, same pattern as
    metrics_writer.prune_metric_history()."""
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "DELETE FROM synthetic_check_results WHERE checked_at < DATE_SUB(NOW(), INTERVAL %s DAY)",
            (retain_days,),
        )
        deleted = cursor.rowcount
        conn.commit()
        return deleted
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()
