import os
import socket
import sys
import ssl

# 0. Container Diagnostic: Print SSL/TLS/Proxy/Gateway env vars and certificate locations on cold start
if os.environ.get("DEBUG_PROXY_PATCH") == "true":
    print("=== CONTAINER DIAGNOSTIC: ENV VARS ===", file=sys.stderr)
    for k, v in sorted(os.environ.items()):
        if any(w in k.lower() for w in ["cert", "ssl", "tls", "ca", "proxy", "gateway", "spiffe", "mtls", "root", "bundle", "google_api", "vertex"]):
            print(f"ENV: {k} = {v}", file=sys.stderr)

# 1. Universal Egress Gateway MITM Proxy Trust Bundle Generator (for gRPC C++ C-core)
# (Commented out for initial standard mTLS / direct verification; enable when behind aggressive MITM inspection)
'''
try:
    _sys_cert = "/etc/ssl/certs/ca-certificates.crt"
    _combined_cert = "/tmp/universal_trust_bundle.pem"
    _loc = os.environ.get("GOOGLE_CLOUD_REGION") or os.environ.get("REGION") or "us-central1"
    with open(_combined_cert, "w") as _out_f:
        if os.path.exists(_sys_cert):
            _out_f.write(open(_sys_cert).read() + "\n")
        for _host in [
            f"{_loc}-aiplatform.googleapis.com", f"{_loc}-aiplatform.mtls.googleapis.com",
            "aiplatform.googleapis.com", "aiplatform.mtls.googleapis.com",
            "bigquery.googleapis.com", "bigquery.mtls.googleapis.com",
            "logging.googleapis.com", "logging.mtls.googleapis.com",
            "trace.googleapis.com", "trace.mtls.googleapis.com",
            "agentregistry.googleapis.com", "agentregistry.mtls.googleapis.com",
            f"{_loc}-agentregistry.googleapis.com", f"{_loc}-agentregistry.mtls.googleapis.com",
            "telemetry.googleapis.com", "telemetry.mtls.googleapis.com",
            "cloudresourcemanager.googleapis.com", "cloudresourcemanager.mtls.googleapis.com",
            "iamcredentials.googleapis.com", "iamcredentials.mtls.googleapis.com"
        ]:
            try:
                _gw_cert = ssl.get_server_certificate((_host, 443))
                _out_f.write(_gw_cert + "\n")
            except Exception:
                pass
    if os.path.exists(_combined_cert):
        os.environ["SSL_CERT_FILE"] = _combined_cert
        os.environ["REQUESTS_CA_BUNDLE"] = _combined_cert
        os.environ["CURL_CA_BUNDLE"] = _combined_cert
        os.environ["GRPC_DEFAULT_SSL_ROOTS_FILE_PATH"] = _combined_cert
except Exception:
    pass

# 2. Universal Python HTTP/SSL Bypass (requests, urllib3, aiohttp, httpx)
_orig_create_default_context = ssl.create_default_context
def _patched_create_default_context(purpose=ssl.Purpose.SERVER_AUTH, *, cafile=None, capath=None, cadata=None):
    if not cafile and os.path.exists("/tmp/universal_trust_bundle.pem"):
        cafile = "/tmp/universal_trust_bundle.pem"
    ctx = _orig_create_default_context(purpose, cafile=cafile, capath=capath, cadata=cadata)
    try:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    except Exception:
        pass
    return ctx
ssl.create_default_context = _patched_create_default_context

try:
    import requests as _requests
    _orig_requests_request = _requests.Session.request
    _requests.Session.request = lambda self, method, url, **kwargs: _orig_requests_request(self, method, url, **{**kwargs, "verify": False})
    from requests.adapters import HTTPAdapter as _HTTPAdapter
    _orig_cert_verify = _HTTPAdapter.cert_verify
    def _patched_cert_verify(self, conn, url, verify, cert):
        conn.cert_reqs = 'CERT_NONE'
        conn.assert_hostname = False
        conn.ca_certs = None
    _HTTPAdapter.cert_verify = _patched_cert_verify
except Exception:
    pass

try:
    import urllib3.util.ssl_ as _urllib3_ssl
    _orig_urllib3_ctx = _urllib3_ssl.create_urllib3_context
    def _patched_urllib3_ctx(ssl_version=None, cert_reqs=None, options=None, ciphers=None):
        ctx = _orig_urllib3_ctx(ssl_version, cert_reqs=ssl.CERT_NONE, options=options, ciphers=ciphers)
        try:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            if os.path.exists("/tmp/universal_trust_bundle.pem"):
                ctx.load_verify_locations("/tmp/universal_trust_bundle.pem")
        except Exception:
            pass
        return ctx
    _urllib3_ssl.create_urllib3_context = _patched_urllib3_ctx

    import urllib3.contrib.pyopenssl as _pyopenssl
    _PyOpenSSLContext = _pyopenssl.PyOpenSSLContext
    _orig_verify_mode_setter = _PyOpenSSLContext.verify_mode.fset
    def _patched_verify_mode_setter(self, value):
        if getattr(self, '_verify_mode', None) == value:
            return
        try:
            _orig_verify_mode_setter(self, value)
        except ValueError as e:
            if "already been used" in str(e):
                self._verify_mode = value
            else:
                raise
    _PyOpenSSLContext.verify_mode = property(_PyOpenSSLContext.verify_mode.fget, _patched_verify_mode_setter)

    for _meth_name in ["set_alpn_protocols", "set_ciphers", "load_cert_chain", "load_verify_locations"]:
        if hasattr(_PyOpenSSLContext, _meth_name):
            _orig_meth = getattr(_PyOpenSSLContext, _meth_name)
            def _make_safe_meth(_orig):
                def _safe_meth(self, *args, **kwargs):
                    try:
                        return _orig(self, *args, **kwargs)
                    except ValueError as e:
                        if "already been used" in str(e):
                            return None
                        raise
                return _safe_meth
            setattr(_PyOpenSSLContext, _meth_name, _make_safe_meth(_orig_meth))
except Exception:
    pass

try:
    import urllib3.contrib.pyopenssl
    urllib3.contrib.pyopenssl.extract_from_urllib3()
except Exception:
    pass

try:
    import httpx as _httpx
    _orig_httpx_init = _httpx.Client.__init__
    _httpx.Client.__init__ = lambda self, *args, **kwargs: _orig_httpx_init(self, *args, **{**kwargs, "verify": False})
    _orig_async_httpx_init = _httpx.AsyncClient.__init__
    _httpx.AsyncClient.__init__ = lambda self, *args, **kwargs: _orig_async_httpx_init(self, *args, **{**kwargs, "verify": False})
except Exception:
    pass
'''

# 3. IPv4 Socket Filter: Force socket resolution to IPv4 (AF_INET) to prevent errno 101 on unconfigured IPv6 namespaces
_orig_getaddrinfo = socket.getaddrinfo
socket.getaddrinfo = lambda host, port, family=0, type=0, proto=0, flags=0: _orig_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)

# 4. Proxy Compliance: Force aiohttp sessions to respect Egress Gateway proxy settings on cold starts
try:
    import aiohttp as _aiohttp
    _orig_aiohttp_init = _aiohttp.ClientSession.__init__
    _aiohttp.ClientSession.__init__ = lambda self, *args, **kwargs: _orig_aiohttp_init(self, *args, **{**kwargs, "trust_env": kwargs.get("trust_env", True)})
except Exception:
    pass

# 5. Startup Network Bypass: Override ADK get_project_id in memory to eliminate un-allowlisted CRM calls
try:
    import google.cloud.aiplatform.utils.resource_manager_utils as _rm_utils
    _rm_utils.get_project_id = lambda project_number, credentials=None: os.environ.get("GCP_PROJECT_ID", str(project_number))
except Exception:
    pass