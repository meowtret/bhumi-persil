# -*- coding: utf-8 -*-
"""
Server tile lokal untuk plugin BHUMI Persil.

Hanya memakai pustaka standar Python (tanpa requests/pillow), sehingga bisa
berjalan langsung di Python bawaan QGIS.

QGIS meminta tile XYZ ke  http://127.0.0.1:<port>/bhumi/{z}/{x}/{y}.png
Server ini mengubahnya menjadi permintaan WMS GetMap 512x512 ke BHUMI
(layer bhumi_persil), lalu meneruskan PNG-nya apa adanya.

Berdasarkan BHUMI Connector ArcMap v3.3.0 oleh Al Qadri.
"""
import http.cookiejar
import json
import math
import os
import re
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM_URL = "https://bhumi.atrbpn.go.id/mprx/service"
BOOTSTRAP_URL = "https://bhumi.atrbpn.go.id/"
LAYER = "bhumi_persil"
TILE_SIZE = 512
WORLD_MIN = -20037508.342789244
WORLD_MAX = 20037508.342789244
WORLD = WORLD_MAX - WORLD_MIN
MAX_ZOOM = 19

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


# ---------------------------------------------------------------------------
# Geometri tile (skema XYZ, Web Mercator)
# ---------------------------------------------------------------------------

def tile_span(z):
    return WORLD / float(2 ** z)


def tile_bbox(z, x, y):
    span = tile_span(z)
    minx = WORLD_MIN + x * span
    maxy = WORLD_MAX - y * span
    return minx, maxy - span, minx + span, maxy


def lonlat_to_tile(lon, lat, z):
    mx = lon * 20037508.342789244 / 180.0
    my = math.log(math.tan((90.0 + lat) * math.pi / 360.0)) * 6378137.0
    span = tile_span(z)
    n = 2 ** z
    x = int((mx - WORLD_MIN) // span)
    y = int((WORLD_MAX - my) // span)
    return max(0, min(n - 1, x)), max(0, min(n - 1, y))


def upstream_tile_url(z, x, y, upstream=UPSTREAM_URL, layer=LAYER):
    minx, miny, maxx, maxy = tile_bbox(z, x, y)
    pairs = [
        ("SERVICE", "WMS"), ("REQUEST", "GetMap"), ("LAYERS", layer),
        ("STYLES", ""), ("FORMAT", "image/png"), ("TRANSPARENT", "true"),
        ("VERSION", "1.3.0"), ("SRS", "EPSG:3857"), ("TILED", "true"),
        ("WIDTH", str(TILE_SIZE)), ("HEIGHT", str(TILE_SIZE)), ("CRS", "EPSG:3857"),
        ("BBOX", "{:.15f},{:.15f},{:.15f},{:.15f}".format(minx, miny, maxx, maxy)),
    ]
    q = "&".join("%s=%s" % (urllib.parse.quote(k, safe=""), urllib.parse.quote(v, safe=",:/"))
                 for k, v in pairs)
    return upstream + "?" + q


def looks_like_image(ctype, body):
    ct = (ctype or "").split(";", 1)[0].strip().lower()
    return (ct.startswith("image/") or body[:8] == b"\x89PNG\r\n\x1a\n"
            or body[:3] == b"\xff\xd8\xff")


RETRY_STATUS = (429, 502, 503, 504)


class UpstreamError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Layanan tile
# ---------------------------------------------------------------------------

class TileService:
    def __init__(self, cache_dir, preferred_port=8765, ttl_hours=24.0, log=None,
                 upstream=UPSTREAM_URL, bootstrap=BOOTSTRAP_URL, layer=LAYER,
                 timeout=45, retries=2, max_parallel=4, http_retries=3, backoff=0.7):
        self.cache_dir = cache_dir
        self.preferred_port = int(preferred_port)
        self.ttl = float(ttl_hours) * 3600.0
        self._log = log or (lambda msg: None)
        self.upstream = upstream
        self.bootstrap_url = bootstrap
        self.layer = layer
        self.timeout = timeout
        self.retries = retries
        self.http_retries = http_retries   # ulangi bila BHUMI membalas 429/502/503/504
        self.backoff = backoff
        self._sem = threading.BoundedSemaphore(max_parallel)
        self._lock = threading.RLock()
        self._boot_lock = threading.Lock()
        self._opener = None
        self._httpd = None
        self._thread = None
        self.port = None
        self.stats = {"requests": 0, "tiles_served": 0, "upstream": 0, "cache_hits": 0, "errors": 0, "retried": 0,
                      "last_status": None, "last_error": "", "started": None}

    # -- utilitas ----------------------------------------------------------
    def log(self, msg):
        try:
            self._log(msg)
        except Exception:
            pass

    def _bump(self, key, n=1):
        with self._lock:
            self.stats[key] += n

    @property
    def running(self):
        return self._httpd is not None and self._thread is not None and self._thread.is_alive()

    def url_template(self):
        return "http://127.0.0.1:%d/bhumi/{z}/{x}/{y}.png" % self.port

    # -- sesi HTTP ke BHUMI -------------------------------------------------
    def _new_opener(self):
        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        opener.addheaders = [
            ("User-Agent", USER_AGENT),
            ("Accept", "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8"),
            ("Accept-Language", "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7"),
            ("Referer", self.bootstrap_url),
        ]
        opener._bhumi_jar = jar
        return opener

    def _bootstrap(self, opener):
        try:
            with opener.open(self.bootstrap_url, timeout=20) as r:
                body = r.read()
                self.log("Bootstrap BHUMI: HTTP %s, %d byte, cookie=%d"
                         % (r.status, len(body), len(opener._bhumi_jar)))
                return r.status, len(body), len(opener._bhumi_jar), ""
        except urllib.error.HTTPError as exc:
            self.log("Bootstrap BHUMI: HTTP %s" % exc.code)
            return exc.code, 0, len(opener._bhumi_jar), "HTTP %s" % exc.code
        except Exception as exc:
            self.log("Bootstrap BHUMI gagal: %s" % exc)
            return None, 0, 0, str(exc)

    def _get_opener(self):
        with self._boot_lock:
            if self._opener is None:
                op = self._new_opener()
                self._bootstrap(op)
                self._opener = op
            return self._opener

    def _refresh_opener(self):
        with self._boot_lock:
            op = self._new_opener()
            self._bootstrap(op)
            self._opener = op
            return op

    def _http_get(self, opener, url):
        last = None
        net_try = http_try = 0
        while True:
            try:
                with opener.open(url, timeout=self.timeout) as r:
                    return r.status, r.headers.get("Content-Type", ""), r.read()
            except urllib.error.HTTPError as exc:
                try:
                    body = exc.read() or b""
                except Exception:
                    body = b""
                ctype = exc.headers.get("Content-Type", "") if exc.headers else ""
                if exc.code in RETRY_STATUS and http_try < self.http_retries:
                    # server BHUMI sedang sibuk (mis. 502 Bad Gateway): tunggu lalu coba lagi
                    wait = self.backoff * (2 ** http_try) * (0.75 + 0.5 * random.random())
                    try:
                        ra = float(exc.headers.get("Retry-After", "")) if exc.headers else 0
                        wait = max(wait, min(ra, 10.0))
                    except (TypeError, ValueError):
                        pass
                    http_try += 1
                    self._bump("retried")
                    time.sleep(min(wait, 10.0))
                    continue
                return exc.code, ctype, body
            except Exception as exc:  # jaringan, timeout, SSL
                last = exc
                if net_try >= self.retries:
                    raise UpstreamError("Tidak bisa menghubungi BHUMI: %s" % exc)
                net_try += 1
                time.sleep(0.3 * net_try)

    def raw_get(self, url):
        """GET ke BHUMI dengan sesi + pemulihan 403. Mengembalikan (status, content_type, body)."""
        with self._sem:
            opener = self._get_opener()
            status, ctype, body = self._http_get(opener, url)
            if status in (401, 403):
                self.log("BHUMI membalas HTTP %s; mencoba sesi baru satu kali." % status)
                opener = self._refresh_opener()
                status, ctype, body = self._http_get(opener, url)
        with self._lock:
            self.stats["last_status"] = status
        return status, ctype, body

    def mapproxy_template(self):
        return self.bootstrap_url.rstrip("/") + "/mapproxy/wmts/bhumi_persil/localgrid_high/{z}/{x}/{y}.png"

    def probe_url(self, url, referer):
        """GET satu URL lain di BHUMI (untuk diagnosis) dengan Referer tertentu."""
        opener = self._get_opener()
        req = urllib.request.Request(url, headers={"Referer": referer})
        try:
            with opener.open(req, timeout=self.timeout) as r:
                return r.status, r.headers.get("Content-Type", ""), r.read()
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read() or b""
            except Exception:
                body = b""
            return exc.code, (exc.headers.get("Content-Type", "") if exc.headers else ""), body
        except Exception as exc:
            raise UpstreamError("Tidak bisa menghubungi BHUMI: %s" % exc)

    # -- cache -------------------------------------------------------------
    def _cache_path(self, z, x, y):
        return os.path.join(self.cache_dir, str(z), str(x), "%d.png" % y)

    def _cache_read(self, z, x, y):
        p = self._cache_path(z, x, y)
        try:
            if os.path.exists(p) and (time.time() - os.path.getmtime(p)) < self.ttl:
                with open(p, "rb") as f:
                    return f.read()
        except OSError:
            pass
        return None

    def _cache_write(self, z, x, y, data):
        p = self._cache_path(z, x, y)
        try:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            tmp = "%s.tmp%d" % (p, threading.get_ident())
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, p)
        except OSError:
            pass

    def clear_cache(self):
        n = 0
        for root, _dirs, files in os.walk(self.cache_dir):
            for name in files:
                try:
                    os.remove(os.path.join(root, name))
                    n += 1
                except OSError:
                    pass
        return n

    def cache_info(self):
        n = size = 0
        for root, _dirs, files in os.walk(self.cache_dir):
            for name in files:
                try:
                    size += os.path.getsize(os.path.join(root, name))
                    n += 1
                except OSError:
                    pass
        return n, size

    # -- tile --------------------------------------------------------------
    def get_tile(self, z, x, y):
        """Mengembalikan bytes PNG tile. Raise UpstreamError bila gagal."""
        cached = self._cache_read(z, x, y)
        if cached is not None:
            self._bump("cache_hits")
            return cached
        url = upstream_tile_url(z, x, y, self.upstream, self.layer)
        self._bump("upstream")
        status, ctype, body = self.raw_get(url)
        if status < 400 and looks_like_image(ctype, body):
            self._cache_write(z, x, y, body)
            return body
        preview = body[:300].decode("utf-8", errors="replace").replace("\n", " ")
        msg = "BHUMI z=%d x=%d y=%d gagal: HTTP %s; Content-Type=%s; isi=%r" % (z, x, y, status, ctype, preview)
        with self._lock:
            self.stats["last_error"] = msg
        raise UpstreamError(msg)

    # -- server ------------------------------------------------------------
    def start(self):
        """Menyalakan server di port pilihan (atau 9 port berikutnya). Mengembalikan port."""
        if self.running:
            return self.port
        service = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            server_version = "BhumiPersil/1.0"

            def log_message(self, fmt, *args):
                pass

            def _send(self, status, ctype, payload, cache=False):
                if isinstance(payload, str):
                    payload = payload.encode("utf-8")
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", ctype)
                    self.send_header("Content-Length", str(len(payload)))
                    self.send_header("Cache-Control", "max-age=600" if cache else "no-cache")
                    self.end_headers()
                    self.wfile.write(payload)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass  # QGIS membatalkan permintaan saat peta digeser; normal

            def do_GET(self):
                service._bump("requests")
                parts = urllib.parse.urlsplit(self.path)
                path = parts.path
                m = re.match(r"^/bhumi/(\d+)/(\d+)/(\d+)\.png$", path)
                if m:
                    if "diag=1" not in parts.query:   # uji diagnosis tidak dihitung sebagai permintaan QGIS
                        service._bump("tiles_served")
                    z, x, y = (int(g) for g in m.groups())
                    n = 2 ** z if z <= 24 else 0
                    if not (0 <= z <= 24 and 0 <= x < n and 0 <= y < n):
                        return self._send(400, "text/plain; charset=utf-8", "tile di luar grid")
                    if z > MAX_ZOOM:
                        return self._send(404, "text/plain; charset=utf-8", "zoom terlalu besar")
                    try:
                        data = service.get_tile(z, x, y)
                    except Exception as exc:
                        service._bump("errors")
                        service.log("Tile %d/%d/%d gagal: %s" % (z, x, y, exc))
                        return self._send(502, "text/plain; charset=utf-8", str(exc))
                    return self._send(200, "image/png", data, cache=True)
                if path == "/status":
                    with service._lock:
                        d = dict(service.stats)
                    d.update(port=service.port, template=service.url_template(), cache_dir=service.cache_dir)
                    return self._send(200, "application/json; charset=utf-8", json.dumps(d, indent=2))
                if path == "/":
                    return self._send(200, "text/html; charset=utf-8",
                                      "<h3>BHUMI Persil (plugin QGIS)</h3><p>Aktif di port %s.</p>" % service.port)
                return self._send(404, "text/plain; charset=utf-8", "Not Found")

        last_exc = None
        for port in range(self.preferred_port, self.preferred_port + 10):
            try:
                httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            except OSError as exc:
                last_exc = exc
                continue
            httpd.daemon_threads = True
            self._httpd = httpd
            self.port = port
            self._thread = threading.Thread(target=httpd.serve_forever, name="bhumi-persil-server", daemon=True)
            self._thread.start()
            self.stats["started"] = time.time()
            self.log("Server tile aktif di %s" % self.url_template())
            return port
        raise OSError("Tidak ada port kosong (%d-%d): %s"
                      % (self.preferred_port, self.preferred_port + 9, last_exc))

    def stop(self):
        httpd, thread = self._httpd, self._thread
        self._httpd = None
        self._thread = None
        if httpd is not None:
            try:
                httpd.shutdown()
            except Exception:
                pass
            try:
                httpd.server_close()
            except Exception:
                pass
        if thread is not None:
            thread.join(timeout=3)
        self.log("Server tile berhenti")
