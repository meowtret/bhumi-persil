# -*- coding: utf-8 -*-
"""Plugin QGIS: BHUMI Persil (kompatibel QGIS 3.x/Qt5 dan QGIS 4/Qt6)."""
import os
import platform
import re
import sys
import threading
import traceback
import urllib.parse

from qgis.PyQt.QtCore import QObject, QUrl, pyqtSignal, qVersion
from qgis.PyQt.QtNetwork import QNetworkRequest
from qgis.PyQt.QtGui import QIcon, QImage
from qgis.PyQt.QtWidgets import (QApplication, QDialog, QHBoxLayout, QPlainTextEdit,
                                 QPushButton, QVBoxLayout)
try:  # Qt6: QAction ada di QtGui
    from qgis.PyQt.QtGui import QAction
except ImportError:  # Qt5
    from qgis.PyQt.QtWidgets import QAction

from qgis.core import (Qgis, QgsApplication, QgsBlockingNetworkRequest, QgsCoordinateReferenceSystem,
                       QgsCoordinateTransform, QgsMessageLog, QgsProject, QgsRasterLayer, QgsSettings)

from .tile_server import (TileService, UpstreamError, looks_like_image, lonlat_to_tile,
                          upstream_tile_url)

PLUGIN_NAME = "BHUMI Persil"
LAYER_TITLE = "Persil BHUMI ATR/BPN"
DEFAULT_PORT = 8765
PALU = (119.8707, -0.8917)  # lon, lat


def _level(name):
    """Qgis.MessageLevel.Info (3.26+/4.x) atau Qgis.Info (versi lama)."""
    enum = getattr(Qgis, "MessageLevel", None)
    if enum is not None and hasattr(enum, name):
        return getattr(enum, name)
    return getattr(Qgis, name)


def alpha_fill_percent(data):
    """Persentase piksel yang tidak transparan penuh pada gambar PNG (None bila gagal)."""
    try:
        img = QImage.fromData(data)
        if img.isNull():
            return None
        img = img.convertToFormat(QImage.Format.Format_ARGB32)
        raw = bytes(img.constBits().asstring(img.sizeInBytes()))
        alpha = raw[3::4]  # ARGB32 little-endian: B,G,R,A
        return 100.0 * (len(alpha) - alpha.count(0)) / max(1, len(alpha))
    except Exception:
        return None


class DiagnosisDialog(QDialog):
    line_ready = pyqtSignal(str)
    finished_run = pyqtSignal()

    def __init__(self, plugin, parent=None):
        super().__init__(parent)
        self.plugin = plugin
        self.setWindowTitle("Diagnosis BHUMI Persil")
        self.resize(820, 600)
        lay = QVBoxLayout(self)
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setStyleSheet("font-family: Consolas, 'Courier New', monospace; font-size: 9pt;")
        lay.addWidget(self.text)
        row = QHBoxLayout()
        self.btn_run = QPushButton("Jalankan ulang")
        self.btn_copy = QPushButton("Salin hasil")
        self.btn_close = QPushButton("Tutup")
        for b in (self.btn_run, self.btn_copy, self.btn_close):
            row.addWidget(b)
        lay.addLayout(row)
        self.btn_run.clicked.connect(self.run)
        self.btn_copy.clicked.connect(self.copy)
        self.btn_close.clicked.connect(self.close)
        self.line_ready.connect(self._append)
        self.finished_run.connect(lambda: self.btn_run.setEnabled(True))

    def _append(self, line):
        self.text.appendPlainText(line)

    def copy(self):
        QApplication.clipboard().setText(self.text.toPlainText())

    # -- dijalankan di thread utama: kumpulkan info QGIS & layer ------------------
    def _gather(self):
        info = {"lines": [], "bhumi_layers": 0, "visible_ok": True, "center": PALU, "before": 0}
        L = info["lines"]
        iface = self.plugin.iface
        canvas = iface.mapCanvas()
        try:
            L.append("QGIS        : %s | Python %s | Qt %s | %s"
                     % (Qgis.version(), sys.version.split()[0], qVersion(), platform.platform()))
        except Exception:
            pass
        try:
            enabled = QgsSettings().value("proxy/proxyEnabled", False)
            L.append("Proxy QGIS  : %s" % ("AKTIF (localhost bisa ikut terproxy)" if str(enabled).lower() == "true" else "mati"))
        except Exception:
            pass
        try:
            ms = canvas.mapSettings()
            flag = canvas.renderFlag() if hasattr(canvas, "renderFlag") else True
            L.append("Peta        : CRS %s | skala 1:%d | Render %s"
                     % (ms.destinationCrs().authid(), int(canvas.scale()), "AKTIF" if flag else "MATI  <-- centang 'Render' di kanan bawah QGIS"))
            info["render_on"] = bool(flag)
        except Exception as exc:
            L.append("Peta        : gagal dibaca (%s)" % exc)
        info["center"] = self.plugin.canvas_center_lonlat()
        root = QgsProject.instance().layerTreeRoot()
        for lyr in QgsProject.instance().mapLayers().values():
            try:
                if not (isinstance(lyr, QgsRasterLayer) and lyr.providerType() == "wms"):
                    continue
                if "/bhumi/" not in lyr.source():
                    continue
                info["bhumi_layers"] += 1
                node = root.findLayer(lyr.id())
                visible = node.isVisible() if node is not None else None
                in_scale = lyr.isInScaleRange(canvas.scale()) if hasattr(lyr, "isInScaleRange") else True
                op = lyr.opacity() if hasattr(lyr, "opacity") else 1.0
                L.append("Layer       : '%s' | valid=%s | tercentang=%s | rentang skala OK=%s | opasitas=%.0f%%"
                         % (lyr.name(), lyr.isValid(), visible, in_scale, op * 100))
                src = urllib.parse.unquote(lyr.source())
                m = re.search(r"url=([^&]+)", src)
                L.append("              url: %s" % (m.group(1) if m else src[:120]))
                if visible is False or not in_scale or op < 0.05:
                    info["visible_ok"] = False
            except Exception as exc:
                L.append("Layer       : gagal dibaca (%s)" % exc)
        if info["bhumi_layers"] == 0:
            L.append("Layer       : BELUM ADA layer BHUMI di proyek ini (klik 'Tambah Layer Persil BHUMI')")
        svc = self.plugin.service
        if svc is not None:
            info["before"] = svc.stats["requests"]
        if info["bhumi_layers"]:
            try:
                canvas.refreshAllLayers()   # paksa QGIS menggambar ulang -> harus meminta tile
            except Exception:
                pass
        return info

    def run(self):
        self.text.clear()
        self.btn_run.setEnabled(False)
        try:
            info = self._gather()
        except Exception as exc:
            info = {"lines": ["Gagal mengumpulkan info QGIS: %s" % exc], "bhumi_layers": 0,
                    "visible_ok": True, "center": PALU, "before": 0}
        threading.Thread(target=self._work, args=(info,), daemon=True).start()

    # -- dijalankan di thread latar -----------------------------------------------
    def _loopback(self, svc, lon, lat):
        """Uji jalur QGIS -> server lokal memakai jaringan QGIS sendiri (menghormati proxy QGIS)."""
        x, y = lonlat_to_tile(lon, lat, 12)
        url = "http://127.0.0.1:%d/bhumi/12/%d/%d.png?diag=1" % (svc.port, x, y)
        blk = QgsBlockingNetworkRequest()
        blk.get(QNetworkRequest(QUrl(url)))
        reply = blk.reply()
        status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        data = bytes(reply.content())
        ok = status == 200 and data[:8] == b"\x89PNG\r\n\x1a\n"
        return ok, status, len(data), blk.errorMessage()

    def _work(self, info):
        emit = self.line_ready.emit
        try:
            svc = self.plugin.service
            emit("=== Lingkungan QGIS ===")
            for l in info["lines"]:
                emit(l)
            emit("")
            emit("=== Server tile plugin ===")
            if svc is None or not svc.running:
                emit("Server tile: TIDAK AKTIF. Matikan-nyalakan plugin, atau cek apakah port 8765-8774 dipakai program lain.")
                return
            n, size = svc.cache_info()
            emit("Aktif di     : %s" % svc.url_template())
            emit("Cache        : %d tile, %.1f MB (%s)" % (n, size / 1048576.0, svc.cache_dir))
            lon, lat = info["center"]

            emit("")
            emit("=== Uji BHUMI (WMS) di lon=%.4f lat=%.4f ===" % (lon, lat))
            svc._get_opener()
            emit("%-4s %-8s %-14s %-14s %s" % ("z", "HTTP", "Content-Type", "Piksel terisi", "Keterangan"))
            visible, clear, problems = [], [], set()
            for z in (10, 12, 14, 16, 18):
                tx0, ty0 = lonlat_to_tile(lon, lat, z)
                best, statuses, ctypes, notes = None, set(), set(), set()
                for dy in (0, 1):
                    for dx in (0, 1):
                        x, y = tx0 + dx, ty0 + dy
                        try:
                            status, ctype, body = svc.raw_get(upstream_tile_url(z, x, y, svc.upstream, svc.layer))
                        except UpstreamError as exc:
                            notes.add(str(exc)[:70])
                            problems.add("net")
                            continue
                        statuses.add(status)
                        ctypes.add(ctype.split(";")[0])
                        if status >= 400:
                            notes.add("DITOLAK server")
                            problems.add("http%d" % status)
                        elif not looks_like_image(ctype, body):
                            notes.add("BUKAN gambar: %r" % body[:50].decode("utf-8", "replace"))
                            problems.add("notimage")
                        else:
                            pct = alpha_fill_percent(body)
                            if pct is not None and (best is None or pct > best):
                                best = pct
                if best is not None:
                    if best > 0:
                        visible.append(z)
                        notes.add("ADA persil")
                    else:
                        notes.add("kosong")
                    if best >= 5:
                        clear.append(z)
                emit("%-4d %-8s %-14s %-14s %s" % (
                    z, ",".join(str(s) for s in sorted(statuses)) or "ERR",
                    ",".join(sorted(ctypes))[:14],
                    ("%.2f%%" % best) if best is not None else "-", "; ".join(sorted(notes))))

            from_qgis = svc.stats["tiles_served"]
            emit("")
            emit("Tile yang diminta QGIS ke server plugin sejak server menyala: %d (gagal ke BHUMI: %d)"
                 % (from_qgis, svc.stats["errors"]))

            emit("")
            emit("=== Uji jalur QGIS -> server lokal (memakai jaringan QGIS) ===")
            lb_ok = False
            try:
                lb_ok, status, nbytes, err = self._loopback(svc, lon, lat)
                if lb_ok:
                    emit("OK: HTTP %s, %d byte, PNG valid" % (status, nbytes))
                else:
                    emit("GAGAL: HTTP %s, %d byte, pesan: %s" % (status, nbytes, err or "-"))
            except Exception as exc:
                emit("GAGAL menjalankan uji: %s" % exc)

            emit("")
            emit("=== Uji alternatif: tile langsung mapproxy (seperti koneksi XYZ di QGIS) ===")
            tmpl = svc.mapproxy_template()
            referer = svc.bootstrap_url.rstrip("/") + "/peta"
            z = 16
            tx, ty = lonlat_to_tile(lon, lat, z)
            for label, yy in (("XYZ biasa", ty), ("y dibalik (TMS)", (2 ** z - 1) - ty)):
                u = tmpl.replace("{z}", str(z)).replace("{x}", str(tx)).replace("{y}", str(yy))
                try:
                    status, ctype, body = svc.probe_url(u, referer)
                    pct = alpha_fill_percent(body) if status < 400 and looks_like_image(ctype, body) else None
                    emit("z=%d %-16s HTTP %s %s %s" % (z, label, status, ctype.split(";")[0],
                                                       ("terisi %.1f%%" % pct) if pct is not None else "(bukan gambar/ditolak)"))
                except UpstreamError as exc:
                    emit("z=%d %-16s %s" % (z, label, str(exc)[:80]))

            emit("")
            emit("=== Kesimpulan ===")
            if problems & {"http401", "http403", "http429"}:
                emit("- BHUMI menolak permintaan (401/403/429): kemungkinan pemblokiran bot/rate limit. Tunggu, buka bhumi.atrbpn.go.id di browser, ulangi.")
            elif "notimage" in problems:
                emit("- BHUMI membalas teks/HTML, bukan gambar (lihat kolom Keterangan).")
            elif problems:
                emit("- Ada kesalahan jaringan ke BHUMI: %s. Cek koneksi internet / proxy." % ", ".join(sorted(problems)))
            elif visible:
                z0 = min(clear) if clear else min(visible)
                emit("- BHUMI OK: persil tampak jelas mulai zoom %d (skala sekitar 1:%d atau lebih besar)." % (z0, int(559082264.0 / (2 ** z0))))
            else:
                emit("- BHUMI membalas gambar valid tetapi kosong di lokasi ini; geser peta ke area yang ada persilnya.")
            if not lb_ok:
                emit("- QGIS TIDAK bisa menjangkau server lokal 127.0.0.1:%s. Periksa: proxy QGIS (Settings > Options > Network),"
                     " antivirus/firewall yang memblokir loopback, atau program lain di port itu." % svc.port)
            elif info["bhumi_layers"] == 0:
                emit("- Belum ada layer BHUMI di proyek ini. Klik 'Tambah Layer Persil BHUMI', lalu jalankan diagnosis lagi.")
            elif not info["visible_ok"] or info.get("render_on") is False:
                emit("- Layer ada tetapi tidak tampil: cek centang layer, rentang skala, opasitas, dan tombol 'Render' di kanan bawah QGIS.")
            elif from_qgis == 0:
                emit("- Layer ada dan jalur lokal OK, tetapi QGIS belum pernah meminta tile ke server plugin sejak server menyala.")
                emit("  (Wajar bila server baru dinyalakan ulang dan peta sudah pernah tampil: tile dilayani cache QGIS.)")
                emit("  Kalau peta tetap kosong, geser/zoom peta sedikit lalu jalankan diagnosis lagi, dan kirim hasil ini beserta panel Log Messages.")
            else:
                st = dict(svc.stats)
                emit("- QGIS sudah meminta %d tile; gagal ke BHUMI: %d." % (from_qgis, st["errors"]))
                if st["last_error"]:
                    emit("  Error terakhir: %s" % st["last_error"])
                else:
                    emit("  Semua normal. Kalau layer tampak kosong, zoom lebih dekat ke area yang ada persilnya.")
        except Exception as exc:  # jangan sampai dialog macet
            emit("ERROR diagnosis: %s\n%s" % (exc, traceback.format_exc()))
        finally:
            self.finished_run.emit()


class BhumiPersilPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.plugin_dir = os.path.dirname(__file__)
        self.menu = "&BHUMI Persil"
        self.actions = []
        self.service = None
        self._dialog = None

    # -- siklus hidup plugin -------------------------------------------------
    def initGui(self):
        icon = QIcon(os.path.join(self.plugin_dir, "icon.svg"))
        self._action(icon, "Tambah Layer Persil BHUMI", self.add_layer, toolbar=True)
        self._action(None, "Diagnosis BHUMI...", self.show_diagnosis)
        self._action(None, "Bersihkan cache tile", self.clear_cache)
        self._ensure_service(quiet=True)
        QgsProject.instance().readProject.connect(self._on_project_read)

    def unload(self):
        try:
            QgsProject.instance().readProject.disconnect(self._on_project_read)
        except Exception:
            pass
        for act, on_toolbar in self.actions:
            self.iface.removePluginWebMenu(self.menu, act)
            if on_toolbar:
                self.iface.removeToolBarIcon(act)
        self.actions = []
        if self.service is not None:
            self.service.stop()
            self.service = None

    def _action(self, icon, text, slot, toolbar=False):
        act = QAction(icon, text, self.iface.mainWindow()) if icon else QAction(text, self.iface.mainWindow())
        act.triggered.connect(lambda _checked=False, s=slot: s())
        self.iface.addPluginToWebMenu(self.menu, act)
        if toolbar:
            self.iface.addToolBarIcon(act)
        self.actions.append((act, toolbar))
        return act

    # -- util -------------------------------------------------------------
    def _log(self, msg):
        QgsMessageLog.logMessage(msg, PLUGIN_NAME, _level("Info"))

    def _say(self, text, level="Info", duration=8):
        self.iface.messageBar().pushMessage(PLUGIN_NAME, text, level=_level(level), duration=duration)

    def _ensure_service(self, quiet=False):
        if self.service is not None and self.service.running:
            return True
        cache_dir = os.path.join(QgsApplication.qgisSettingsDirPath(), "bhumi_persil_cache")
        self.service = TileService(cache_dir, preferred_port=DEFAULT_PORT, log=self._log)
        try:
            port = self.service.start()
        except OSError as exc:
            self._log("Gagal menyalakan server: %s" % exc)
            if not quiet:
                self._say("Server tile gagal menyala: %s" % exc, "Critical", 0)
            return False
        if port != DEFAULT_PORT:
            self._log("Port %d dipakai program lain, memakai port %d." % (DEFAULT_PORT, port))
        return True

    def layer_uri(self):
        url = urllib.parse.quote(self.service.url_template(), safe=":/")  # { } -> %7B %7D
        return "type=xyz&url=%s&zmin=0&zmax=19&tilePixelRatio=2" % url

    def canvas_center_lonlat(self):
        try:
            canvas = self.iface.mapCanvas()
            center = canvas.center()
            src = canvas.mapSettings().destinationCrs()
            dst = QgsCoordinateReferenceSystem("EPSG:4326")
            tr = QgsCoordinateTransform(src, dst, QgsProject.instance().transformContext())
            p = tr.transform(center)
            if -180 <= p.x() <= 180 and -85 <= p.y() <= 85 and (94 <= p.x() <= 142 and -12 <= p.y() <= 7):
                return p.x(), p.y()
        except Exception:
            pass
        return PALU

    def _on_project_read(self, *args):
        """Proyek tersimpan memakai port lama; sesuaikan bila port server sekarang berbeda."""
        if self.service is None or not self.service.running:
            return
        port = self.service.port
        for lyr in list(QgsProject.instance().mapLayers().values()):
            try:
                if not isinstance(lyr, QgsRasterLayer) or lyr.providerType() != "wms":
                    continue
                src = lyr.source()
                m = re.search(r"127\.0\.0\.1:(\d+)/bhumi/", src)
                if m and int(m.group(1)) != port:
                    new = src.replace(m.group(0), "127.0.0.1:%d/bhumi/" % port)
                    lyr.setDataSource(new, lyr.name(), "wms")
                    self._log("Layer '%s' dialihkan ke port %d." % (lyr.name(), port))
            except Exception as exc:
                self._log("Gagal menyesuaikan layer: %s" % exc)

    # -- aksi ---------------------------------------------------------------
    def add_layer(self):
        if not self._ensure_service():
            return
        layer = QgsRasterLayer(self.layer_uri(), LAYER_TITLE, "wms")
        if not layer.isValid():
            self._say("Layer tidak valid. URI: %s" % self.layer_uri(), "Critical", 0)
            return
        QgsProject.instance().addMapLayer(layer)
        scale = self.iface.mapCanvas().scale()
        if scale > 200000:
            self._say("Layer ditambahkan. Persil baru tampak jelas pada skala sekitar 1:136.000 atau lebih besar; "
                      "perbesar peta (mis. zoom ke Palu).", "Info", 12)
        else:
            self._say("Layer persil BHUMI ditambahkan. Tile diambil dari BHUMI saat peta digeser.", "Info", 6)

    def show_diagnosis(self):
        if not self._ensure_service(quiet=True):
            self._say("Server tile belum aktif.", "Warning", 6)
        self._dialog = DiagnosisDialog(self, self.iface.mainWindow())
        self._dialog.show()
        self._dialog.raise_()
        self._dialog.run()

    def clear_cache(self):
        if self.service is None:
            self._say("Server belum aktif; tidak ada cache yang dihapus.", "Warning", 5)
            return
        n = self.service.clear_cache()
        self._say("Cache dibersihkan (%d tile dihapus)." % n, "Info", 5)
