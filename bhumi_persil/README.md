# BHUMI Persil

A QGIS plugin that shows the land parcel (*persil*) layer of **BHUMI ATR/BPN** as a map layer inside QGIS, with no extra program to install or run.

> **Unofficial visual aid.** This plugin is not a product or service of the Indonesian Ministry of Agrarian Affairs and Spatial Planning / National Land Agency (ATR/BPN) and is not affiliated with it. The parcel layer is an image for orientation only. Do not use it as a basis for determining boundaries, land rights, or any legal decision. For official data, use BHUMI or the official ATR/BPN services.

- **Version:** 1.0.3
- **QGIS:** 3.16 or newer (Qt5 and Qt6 builds)
- **Status:** experimental
- **Language of the interface:** Indonesian (menu labels are given below with English glosses)

## What it does

BHUMI Persil adds the parcel layer from BHUMI as an XYZ tile layer. You can place it over any basemap and combine it with your own layers (for example permit areas, forest area boundaries, or digitized polygons) to get a quick picture of where parcels lie.

A tiny tile server runs inside QGIS (on `127.0.0.1` only). QGIS asks that server for map tiles, and the server fetches them from the public BHUMI map service through your own internet connection, much like a browser opening the BHUMI website.

## What it does not do

- The result is a **raster image**, not vector data. It is **not a shapefile** and cannot be exported as one.
- Parcel attributes (NIB, rights type, area) **cannot be clicked or queried** in QGIS.
- It does not store a copy of BHUMI's data, and it does not work offline except for tiles already in the local cache.
- It is not meant for bulk downloading of large areas.

## Installation

1. In QGIS, open **Plugins > Manage and Install Plugins... > Install from ZIP**.
2. Select `bhumi_persil.zip` and click **Install Plugin**.
3. If the plugin is not active, go to the **Installed** tab and tick **BHUMI Persil**.
4. The plugin is flagged as experimental. If you cannot find it in the plugin list, open the **Settings** tab and tick **Show also experimental plugins**.

## Usage

1. Click the BHUMI Persil icon on the toolbar, or choose **Web > BHUMI Persil > Tambah Layer Persil BHUMI** (*Add BHUMI parcel layer*).
2. Zoom to your area of interest. Parcels become clearly visible at about **1:136,000 or larger scales** (for example, zoom in to a city).
3. Adjust the layer transparency in **Layer Properties > Transparency**, and drag the layer above or below your basemap as needed.

### Other menu items (Web > BHUMI Persil)

| Menu item | What it does |
| --- | --- |
| **Diagnosis BHUMI...** | Checks the QGIS version, proxy setting, Render option and layer status, the connection to BHUMI, and the path from QGIS to the local tile server. Use it when the layer looks empty, then press **Salin hasil** (*Copy result*) to copy the report and paste it into an issue. |
| **Bersihkan cache tile** (*Clear tile cache*) | Deletes the tiles stored on your computer. |

## How it works

- A local server listens on `127.0.0.1`, starting at port **8765** and falling back to ports 8766 to 8774 if the port is busy. It is not reachable from other computers.
- QGIS requests tiles from `http://127.0.0.1:<port>/bhumi/{z}/{x}/{y}.png`. The plugin fixes the port inside saved projects when they are opened again, so project files keep working even if the port changes.
- Tiles are fetched from the BHUMI map service over HTTPS, with a short-lived browser-like session, and cached on disk for **24 hours** in the QGIS profile folder (`bhumi_persil_cache`).
- If BHUMI answers with a temporary server error (HTTP 429, 502, 503, or 504), the plugin waits and retries up to 3 times. Failed tiles are never cached.
- Requests to BHUMI are limited to 4 in parallel to avoid putting extra load on the service.

## Updates to parcel data

The plugin shows whatever BHUMI currently serves, so parcel additions or removals made in BHUMI appear in QGIS too. Tiles are cached for up to 24 hours, so a change may take time to show. Use **Bersihkan cache tile** to force a reload. How quickly BHUMI itself updates its data is up to ATR/BPN.

## Troubleshooting

| Problem | What to try |
| --- | --- |
| The layer is empty | Zoom in to about 1:136,000 or larger, make sure the layer is ticked and the **Render** checkbox (bottom right) is on, then run **Diagnosis BHUMI...**. |
| Blank squares in the map | BHUMI is sometimes busy (HTTP 502). The plugin retries automatically. Pan or zoom a little, or wait a few minutes. |
| Nothing loads, network errors in the diagnosis | Check your internet connection and proxy settings (**Settings > Options > Network**). Corporate proxies that require a login may block the connection. |
| A tile server error mentioning the port | Another program may be using ports 8765 to 8774. Close it, or reload the plugin. |
| Need to see what happened | Open **View > Panels > Log Messages** and select the **BHUMI Persil** tab. |

## Privacy

The plugin does not collect or send any personal data. It only communicates with the BHUMI website (`bhumi.atrbpn.go.id`) to fetch map tiles, and with your own computer (`127.0.0.1`) for the local tile server.

## Responsible use

The tiles come from a public service operated by ATR/BPN. Please use the plugin reasonably: work on the areas you need rather than panning across very large regions at once, so the service stays available to everyone. The service can change or restrict access at any time, in which case the plugin may stop working. The software is provided without any warranty and you use it at your own risk.

## Credits

This plugin is based on the idea of **BHUMI Connector ArcMap** by **Al Qadri**, a tool that made BHUMI parcels available in ArcMap. BHUMI Persil is a separate implementation for QGIS, shared with the author's approval. Thank you, Mas Al Qadri.

## Author and feedback

Alfian (alfianq2@gmail.com). Bug reports and suggestions are welcome in the [issue tracker](https://github.com/meowtret/bhumi-persil/issues).

## License

GNU General Public License, version 2 or later (GPL-2.0-or-later).
