# JiveLite / LMS Protocol Reference

Everything learned about how a real piCorePlayer/JiveLite client and a real
Lyrion/Logitech Media Server (LMS) actually talk to each other, reverse-
engineered from packet captures taken with `lms_cli_proxy.py`. This is a
reference document, organized by topic rather than chronologically — see the
appendix for an annotated walkthrough of one specific boot capture.

Everything here describes **real LMS and real JiveLite behavior**, verified
directly from the wire. It intentionally covers only the native protocol —
nothing here is specific to any particular compatible-server implementation.

---

## 1. Two independent mechanisms, one TCP port

LMS clients don't talk to the server over just one channel, though both ride
on the same TCP port (9000):

- **CometD/Bayeux over HTTP (`/cometd`)** — a persistent JSON-RPC-style
  channel that drives all menu data, server status, and player status.
- **Plain HTTP `GET`** — ordinary image fetches for icons and album art,
  completely independent of the CometD channel, and issued lazily (only once
  a screen actually renders on-device, not eagerly alongside the browse
  response that named the icon — observed gaps of several seconds between a
  list populating and its icons being requested).

---

## 2. Connection architecture — a small, reused connection pool

JiveLite does **not** open a new TCP connection per request. It maintains a
small, named pool of persistent, reused connections per server, confirmed
both from a real client's own debug logging (which names each connection)
and from proxy captures showing many requests pipelined on one physical
connection:

| Connection role | Purpose | Behavior |
|---|---|---|
| `..._Chunked` | The **one** persistent `/meta/connect` stream | Held open indefinitely; every server-to-client push for the session's lifetime arrives as a new chunk on this single, never-ending HTTP response. |
| `..._Request` | Dedicated, reused connection for `/cometd` POSTs (`/slim/subscribe`, `/slim/request`) | Multiple requests get pipelined onto this same connection sequentially over the session, not one connection per call. |
| `...1`, `...2`, ... | A small generic pool for HTTP `GET` icon/image fetches | Reused and pipelined too — a real capture showed **17 separate icon requests all served on a single reused connection**, with several requests sent back-to-back before earlier responses even arrived, and the connection only closing once all 17 were done. A second pooled connection exists but is only opened if concurrent demand needs it. |

**Implication for anyone implementing a compatible server:** it must handle
genuine HTTP/1.1 pipelining correctly (multiple requests arriving on one
connection before responses are sent) and must not assume "one request per
connection." A server that only handles one request per connection before
closing it will appear to work in simple tests and then fail unpredictably
once a client pipelines several requests at once.

### Idle connection recycling is normal, not a bug signal

JiveLite's own HTTP connection pool proactively closes connections it
considers idle (own client-side debug log wording: `"closing idle
connection"`, `"keep-alive timeout"`) and reconnects, redoing the Bayeux
handshake/connect/subscribe sequence as needed. **This happens identically
against real LMS and any other server** — a real capture showed this same
idle-close-and-reconnect cycle affecting connections to real LMS at the same
moments it affected connections to another server on the same network. A
burst of several handshakes within a few hundred milliseconds of boot, or
recurring roughly every ~10 seconds, is normal client housekeeping — not by
itself evidence that a server is misbehaving.

### The one thing real LMS does that's hard to replicate correctly

Real LMS tolerates a **second** `/meta/connect` (a different `clientId`)
reusing an already-open `_Chunked` connection, appending it as further
chunks onto the *same* ongoing HTTP response rather than starting a new one.
This is unusual — a raw TCP connection carrying two logical CometD sessions'
worth of push data multiplexed onto one continuous chunked body, with no new
HTTP header block for the second session. It is not expressible as ordinary
HTTP/1.1 request/response framing (a connection can't legitimately carry a
second full response while an earlier chunked one is still open), and
attempting to hand-replicate it is a likely source of stream-corruption bugs
(injecting a second header block mid-stream breaks the client's chunk
decoder, causing a disconnect/reconnect). Building on a mature HTTP library
that enforces correct framing sidesteps this entirely: the client's second
connect attempt simply queues behind the first and is never processed,
which in practice just causes the client to open a fresh connection for its
second session instead — a normal, well-tolerated client behavior, not an
error.

---

## 3. Bayeux/CometD message semantics

### Handshake
`/meta/handshake` is always sent as its own solo POST — never batched with
anything else in any observed capture. Response includes:
```json
"advice": {"reconnect": "retry", "interval": 0, "timeout": 60000}
```

### Connect
`/meta/connect` establishes (or re-establishes) the persistent stream.
Response `advice.interval` is `5000` (ms). If a `/meta/subscribe` arrives
batched in the same POST as the connect, its ack is delivered together with
the connect ack as one combined chunk.

### Subscribe
`/meta/subscribe` — ack is **always** sent regardless of whether the
client's message included an `id`.

### `/slim/subscribe`
- Ack is **always** sent (not gated on `id`), unlike `/slim/request`.
- An **immediate snapshot** is pushed right after the ack.
- The subscription then keeps receiving **periodic republished
  snapshots** for its entire lifetime, at whatever interval the client
  requested via a `subscribe:N` tag (seconds) — e.g. `subscribe:60` for
  `serverstatus`, `subscribe:600` for `playerstatus`. `displaystatus` uses
  `subscribe:showbriefly` — event-triggered, not interval-based.
- A client that only ever receives the one initial snapshot and nothing
  further has a legitimate reason to eventually treat the subscription (or
  the connection carrying it) as stale.

### `/slim/request`
- Both the synchronous ack **and** the pushed result are gated on the
  client's message having included an `id` at all. No `id` → neither is
  sent (e.g. `artworkspec` is observed sent with no `id`, and gets no
  response of any kind).
- When `id` **is** present: the synchronous ack echoes it in its original
  form (often an integer), but the **pushed result's `id` is stringified**
  — a consistent quirk across every observed capture (likely an artifact of
  the server's own implementation language), not something to "fix" when
  replicating it.

### `clientId` placement
Most Bayeux channels carry `clientId` at the top level of the message.
`/slim/subscribe` and `/slim/request` do **not** — the only place the
client ID appears is embedded in `data.response`, shaped like
`"/<clientId>/slim/<subtopic>"`.

---

## 4. Browse library views

All `browselibrary` requests use **positional** pagination args, not
tag:value pairs — `index` and `quantity` are the 3rd and 4th elements of
the `cmd` array (right after `"items"`), e.g.:
```json
["browselibrary", "items", 0, 200, "mode:artists", "menu:1", ...]
```
The response's `"count"` is always the **full total**, never the window
size — only `item_loop` and `"offset"` reflect the actual slice returned.

### Artists list (`mode:artists`)
- A synthetic **"All Albums"** entry is appended after the real artists.
- Every plain artist row relies on a single shared `base.actions` object
  (not per-item actions) covering `go`/`add`/`play`/`add-hold`/`more`/
  `playControl`/`set-preset-0`..`9`.
- Each item includes `"type": "playlist"`.

### Albums list — two distinct variants depending on whether `artist_id` is present

**Filtered (`mode:albums` + `artist_id`, browsing into one specific artist):**
- `window.windowStyle`: `"home_menu"`
- `text`: single line (just the album title — you already know the artist)
- `base.actions`' params echo whatever context tags the request itself
  carried (`role_id`, `menu_roles`, `menu_mode`, `artist_id`, `menu`) —
  nothing more, nothing defaulted.

**Unfiltered ("All Albums", `mode:albums`, no `artist_id`):**
- `window.windowStyle`: `"icon_list"` — genuinely different from the
  filtered case.
- `text`: **two lines** — `"Album Title\nArtist Name"` — since a flat,
  mixed-artist list needs to show whose album each one is.
- Adds `"textkey"` (first letter of the title, for an alphabetical
  jump-scroll bar) and a `"presetParams"` block (`favorites_title`,
  `favorites_url`, `favorites_type`, `icon`) — neither present in the
  filtered case.
- `base.actions`' params are genuinely sparse — confirmed directly from a
  real LMS response: just `{"mode": "tracks", "menu": 1}`. No `role_id` or
  `menu_roles` at all, even though those are otherwise a near-universal
  convention elsewhere in the protocol. Don't "correct" this when
  replicating it — it's how the real server actually behaves.

**Both variants**, every album item includes:
- `"performance": ""` in `commonParams` — always present, even for
  non-classical albums with no actual performance value.
- **Both** `"icon"` (the full wrapped path, `music/<icon-id>/cover`) **and**
  `"icon-id"` (the bare hash alone) — real LMS sends both fields on every
  item, unconditionally.
- `"type": "playlist"`.

### Tracks list (`mode:tracks` + `album_id`)
- `window.windowStyle`: `"text_list"`.
- Each item carries its own `"goAction": "play"` rather than relying on a
  shared default `base.actions` entry — selecting a track plays it, it
  doesn't browse deeper, so the per-item action differs from every other
  list type.
- Per-item `playallParams` (carries `play_index`) and `presetParams`.

---

## 5. Icon & artwork conventions

- The client registers a size/format once per session via
  `/slim/request → ["artworkspec", "add", "<W>x<H>_<mode>", "jiveliteskin"]`
  (e.g. `225x225_m`) — every subsequent icon/art request uses this exact
  suffix.
- Generic UI chrome icons: `/html/images/<name>_<size>.png` or
  `/plugins/<plugin>/html/images/<name>_<size>.png`.
- Album cover art: `/music/<icon-id>/cover_<size>` — note **no file
  extension at all** on cover-art paths, unlike chrome icons.
- Real cache headers: cover art gets `max-age=31536000` (1 year); generic
  chrome icons get `max-age=86400` (1 day).
- Icons for a given screen are fetched lazily, only once that screen
  actually renders — not eagerly alongside the browse response that
  contains the icon reference.

---

## 6. Server discovery & identity (UDP, port 3483)

The extended discovery response is a single `'E'`-prefixed byte, followed by
TLV-encoded fields (4-byte tag + 1-byte length + value), at minimum:
`NAME`, `JSON` (the HTTP/CometD port), `VERS`, `UUID`.

**The client tracks servers by `UUID`, not by name or address.** Confirmed
directly from real client source (`SlimDiscoveryApplet.lua`):
```lua
local server = SlimServer(jnt, uuid, name, version)
self:_serverUpdateAddress(server, ip, port, name)
```
Every discovery reply with a given UUID resolves to the same server object
internally, and `updateAddress()` is called on it — updating wherever the
client currently thinks that server lives. Two genuinely different servers
sharing the same UUID are not treated as two servers at all: they're the
same server object, and its address gets silently overwritten back and
forth every time a fresh reply arrives from either one. Each address change
forces any open connection to that "server" to disconnect and reconnect at
the new address.

Discovery queries are LAN **broadcasts** — every listener on the same
network segment hears and can answer them directly and independently.
Nothing about how a single server responds can suppress a different,
unrelated server also answering the same broadcast.

---

## Appendix: Annotated boot sequence trace

This section preserves the original capture walkthrough this document grew
out of — a concrete, chronological trace of one real boot sequence against
a small (2-song, 2-artist) test library, useful as a worked example
alongside the topic reference above.

### A1. Session establishment

Every session begins with the same three-message sequence on a fresh
connection: `/meta/handshake` → `/meta/connect` → `/meta/subscribe` (to
`/<clientId>/**`). In the reference capture this full cycle repeated three
times with distinct `clientId`s — two cycles ~200ms apart right at the
start, a third ~10 seconds later. Consistent with the idle-connection
recycling behavior described in §2 above, not evidence of a problem.

### A2. Server status subscription
```
/slim/subscribe → ["serverstatus", 0, 50, "subscribe:60"]
```
Confirmed the test library: `"info total songs": 2, "info total artists": 3, "info total albums": 2`.

### A3. Player registration and artwork setup

| Request | Purpose |
|---|---|
| `/slim/request → ["artworkspec", "add", "225x225_m", "jiveliteskin"]` | Registers the artwork size/format used for every subsequent icon fetch |
| `/slim/subscribe → ["menustatus"]` (by player MAC) | Player menu-state changes |
| `/slim/subscribe → ["status", "-", 10, "menu:menu", ..., "subscribe:600"]` | Ongoing player transport status |
| `/slim/subscribe → ["displaystatus", "subscribe:showbriefly"]` | Display updates |

### A4. Building the home menu
```
/slim/request → ["menu", 0, 100, "direct:1"]
```
Fetches the entire home menu tree in one shot (My Music, Favorites, Radio,
Settings, Random Mix, Search, every plugin-contributed entry). This
response is what determines which icons need fetching next.
```
/slim/request → ["status", 0, 200, "menu:menu", "useContextMenu:1"]
```
Current player status snapshot for the Now Playing screen.

### A5. Home menu icon fetching

Starting ~0.33s after the home menu tree arrives, all landing within a
0.2-second burst: 17 plain `GET` requests, all `200 OK` with real
PNG/JPEG data, all using the `225x225_m` suffix registered in A3 — e.g.
`/plugins/Sounds/html/images/icon_225x225_m.png`,
`/html/images/artists_225x225_m.png`, `/html/images/albums_225x225_m.png`,
several `/plugins/TuneIn/html/images/radio*_225x225_m.png` variants, and
similar for `RemoteLibrary`, `MyApps`, `ExtendedBrowseModes`,
`DontStopTheMusic`. All 17 were pipelined onto a single reused connection
(see §2).

### A6. Populating My Music → Artists
```
/slim/request → ["browselibrary", "items", 0, 200,
                  "role_id:ALBUMARTIST", "mode:artists", "menu:1", ...]
```
Returned the two artists (Dylan Scott, Jimmy Buffet) plus the synthetic
"All Albums" entry.

### A7. Drilling into one artist
```
/slim/request → ["browselibrary", "items", 0, 200,
                  "role_id:ALBUMARTIST", "mode:albums", "artist_id:3", ...]
```
`artist_id:3` = Dylan Scott. Returned his one album, "Livin' My Best Life."
(The reference capture never issued a `mode:tracks` request, so it doesn't
show the final drill into that album's track listing.)

### A8. Artist/album screen icons and cover art

A second, separate wave of artwork requests follows — starting about 6.5
seconds after the browse requests in A6/A7, not immediately, consistent
with icons being fetched once the list screen actually renders rather than
eagerly:

| Offset | Path | Note |
|---|---|---|
| +6.5s | `/html/images/artists_225x225_m.png` | Re-fetched for the list screen itself, not the home-menu tile |
| +6.6s | `/html/images/albums_225x225_m.png` | Same |
| +8.6s | `/html/images/artists_90x90_m.png` | Smaller size, for a list-row thumbnail |
| +8.7s | `/music/2557d132/cover_225x225_m` | Real cover art — `2557d132` matches the `icon-id` from A7's album JSON |
