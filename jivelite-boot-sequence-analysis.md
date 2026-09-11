# JiveLite Boot Sequence — Capture Analysis

This document traces every request piCorePlayer/JiveLite makes during
device boot and initial menu population, reconstructed from a packet
capture taken with `lms_cli_proxy.py --pcap-file`. The test library
consists of **2 songs by 2 artists** (Dylan Scott, Jimmy Buffet), which is
reflected throughout the trace below.

Two entirely separate mechanisms are involved:

- **CometD/Bayeux over HTTP (`/cometd`)** — a persistent JSON-RPC-style
  channel that drives all menu data, server status, and player status.
- **Plain HTTP `GET` requests** — ordinary image fetches for icons and
  album art, completely independent of the CometD channel.

Both ride on the same TCP port (9000), but they are unrelated protocols
layered on top of it. Steps below are in actual chronological order, with
each artwork-fetching step placed right after the menu event that
triggers it.

---

## 1. Session establishment (Bayeux handshake)

Every session begins with the same three-message sequence on a fresh
connection:

| Channel | Purpose |
|---|---|
| `/meta/handshake` | Negotiates the CometD protocol version, returns a `clientId` |
| `/meta/connect` | Opens the long-poll channel used for server-to-client pushes |
| `/meta/subscribe` | Subscribes to `/<clientId>/**` — the client's own private channel namespace |

**Note:** this full handshake→connect→subscribe→browse sequence actually
repeats **three times** in the capture, each with a distinct `clientId`
(`aef2bcfd`, `d2f2988e`, `b6b15f09`) — two cycles about 200ms apart right
at the start, and a third beginning roughly 10 seconds later. This is
consistent with multiple subsystems (JiveLite's UI vs. the underlying
player component) each opening independent CometD sessions, though the
capture alone doesn't confirm which.

## 2. Server status subscription

```
/slim/subscribe → ["serverstatus", 0, 50, "subscribe:60"]
```

Asks LMS to push server-wide status (player count, library totals,
version) every 60 seconds. The response confirms the test library:

```json
"info total songs": 2, "info total artists": 3, "info total albums": 2
```

## 3. Player registration and artwork setup

| Request | Purpose |
|---|---|
| `/slim/request → ["artworkspec", "add", "225x225_m", "jiveliteskin"]` | Registers the artwork size/format the UI will request — this exact size (`225x225_m`) shows up as the filename suffix on every icon fetch in step 5 |
| `/slim/subscribe → ["menustatus"]` (by player MAC) | Subscribes to changes in the player's menu state |
| `/slim/subscribe → ["status", "-", 10, "menu:menu", ..., "subscribe:600"]` | Subscribes to ongoing player transport status |
| `/slim/subscribe → ["displaystatus", "subscribe:showbriefly"]` | Subscribes to display updates |

## 4. Building the home menu

```
/slim/request → ["menu", 0, 100, "direct:1"]
```

Fetches the entire home menu tree in one shot — My Music, Favorites,
Radio, Settings, Random Mix, Search, and every plugin-contributed entry
(TuneIn's radio categories, Sounds, RemoteLibrary, MyApps, etc.). This is
the response that determines what icons need fetching in step 5.

```
/slim/request → ["status", 0, 200, "menu:menu", "useContextMenu:1"]
```

Pulls the current player status snapshot for the Now Playing screen.

## 5. Home menu icon fetching

Right after the home menu tree comes back (step 4) — starting about
0.33s later, all landing within a 0.2-second burst — the client fetches
icons for every home-menu item that has one. These are ordinary plain
`GET` requests on port 9000, completely unrelated to the CometD channel.
All 17 got a `200 OK` with real PNG/JPEG data:

| Path | Content-Type | Size |
|---|---|---|
| `/plugins/Sounds/html/images/icon_225x225_m.png` | image/png | 20,113 bytes |
| `/plugins/ExtendedBrowseModes/html/composers_225x225_m.png` | image/png | 15,516 bytes |
| `/html/images/artists_225x225_m.png` | image/png | 18,563 bytes |
| `/html/images/albums_225x225_m.png` | image/png | 21,337 bytes |
| `/plugins/TuneIn/html/images/podcasts_225x225_m.png` | image/png | 18,895 bytes |
| `/plugins/TuneIn/html/images/radiosearch_225x225_m.png` | image/png | 16,642 bytes |
| `/plugins/TuneIn/html/images/radioworld_225x225_m.png` | image/png | 27,368 bytes |
| `/plugins/TuneIn/html/images/radiotalk_225x225_m.png` | image/png | 12,141 bytes |
| `/plugins/TuneIn/html/images/radiosports_225x225_m.png` | image/png | 25,622 bytes |
| `/plugins/TuneIn/html/images/radionews_225x225_m.png` | image/png | 19,203 bytes |
| `/plugins/TuneIn/html/images/radiomusic_225x225_m.png` | image/png | 21,431 bytes |
| `/plugins/TuneIn/html/images/radiolocal_225x225_m.png` | image/png | 19,512 bytes |
| `/plugins/TuneIn/html/images/radiopresets_225x225_m.png` | image/png | 13,991 bytes |
| `/plugins/RemoteLibrary/html/icon_225x225_m.png` | image/png | 13,354 bytes |
| `/plugins/MyApps/html/images/icon_225x225_m.png` | image/png | 19,823 bytes |
| `/plugins/ExtendedBrowseModes/html/icon_225x225_m.png` | image/png | 14,297 bytes |
| `/plugins/DontStopTheMusic/html/images/icon_225x225_m.png` | image/png | 15,816 bytes |

The `225x225_m` size matches the `artworkspec` registered in step 3.
Note that `artists_225x225_m.png` and `albums_225x225_m.png` appear here
too — these are the generic My Music category tiles on the home screen,
distinct from the artist- and album-*list* screens fetched in step 8.

## 6. Populating My Music → Artists

```
/slim/request → ["browselibrary", "items", 0, 200,
                  "role_id:ALBUMARTIST", "mode:artists", "menu:1", ...]
```

Returned the two artists in the library: **Dylan Scott** and **Jimmy
Buffet**, plus an "All Albums" entry.

## 7. Drilling into one artist

```
/slim/request → ["browselibrary", "items", 0, 200,
                  "role_id:ALBUMARTIST", "mode:albums", "artist_id:3", ...]
```

`artist_id:3` is Dylan Scott. This returned his one album, **"Livin' My
Best Life."**

The capture never issues a `mode:tracks` request, so it only shows
auto-population down through Artists → one artist's Albums — not the
final drill into that album's track listing (which would be the next
user click).

## 8. Artist/album screen icons and cover art

A **second, separate wave** of artwork requests follows the browse
requests from steps 6-7 — but not immediately. It starts about **6.5
seconds later**, consistent with these icons being fetched once the
corresponding list screens actually render on screen, rather than eagerly
alongside the browse request itself:

| Time after step 6-7 | Path | Content-Type | Size |
|---|---|---|---|
| +6.5s | `/html/images/artists_225x225_m.png` (again) | image/png | 18,563 bytes |
| +6.6s | `/html/images/albums_225x225_m.png` (again) | image/png | 21,337 bytes |
| +8.6s | `/html/images/artists_90x90_m.png` | image/png | 5,254 bytes |
| +8.7s | `/music/2557d132/cover_225x225_m` | image/jpeg | 19,142 bytes |

The repeated `artists`/`albums` fetches use the same `225x225_m` size as
step 5, but this time for the *list screen* itself rather than the
home-menu tile. The `90x90_m` request is a smaller size used for a
list-row thumbnail. Finally, `2557d132` matches the `icon-id` returned in
step 7's album JSON for "Livin' My Best Life" — this is the real cover
art, fetched only once the browse actually reached that specific album.

`artists_225x225_m.png` and `albums_225x225_m.png` being fetched twice
across the capture (once in step 5, once here) is consistent with the
repeated session cycles noted in step 1.

---

## Summary for implementation purposes

Two independent mechanisms need to be emulated for a compatible
LMS-like server:

1. **CometD/Bayeux over `/cometd`** — handshake, connect, subscribe, and
   `slim/request`/`slim/subscribe` commands carrying the same command
   vocabulary as the classic text CLI (`artists`, `albums`, `menu`,
   `status`, `browselibrary`, etc.), wrapped in Bayeux envelopes.
2. **Plain HTTP `GET`** — icon and cover art fetches at
   `/plugins/<plugin>/html/images/<name>_<size>.png`,
   `/html/images/<name>_<size>.png`, and `/music/<icon-id>/cover_<size>`
   paths, sized according to whatever `artworkspec` the client registered
   at connect time.
