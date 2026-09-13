# LMS CLI + SlimProto Logging Proxy

A transparent, logging man-in-the-middle proxy for the Lyrion/Logitech Media
Server (LMS) protocol suite — built for reverse-engineering how squeezebox
clients (piCorePlayer, JiveLite, squeezelite) actually talk to LMS, so you can
build compatible integrations (e.g. a Home Assistant / Music Assistant
LMS-compatible server) with confidence instead of guesswork.

No external dependencies — pure standard library, single-file, cross-platform
(tested on Linux and Windows).

## What it does

LMS clients don't talk to the server over just one channel. This proxy sits
between a real client and a real LMS server and transparently relays **all
four** of the protocols involved, logging everything in a readable form
without altering what either side actually sends or receives:

- **UDP discovery (port 3483/UDP)** — the query/response handshake clients
  use to validate a server address before committing to it. Without
  relaying this, a manually-configured "library"/server address will
  silently fail validation and the client will fall back to whatever
  server it already knows works.
- **SlimProto (port 3483/TCP)** — the binary, frame-based control protocol
  used for player registration, transport control, and status heartbeats.
  Frames are decoded (4-byte tag + 4-byte length + payload) for logging;
  noisy heartbeat frames (`STAT`) are automatically rate-limited so they
  don't drown out everything else.
- **The LMS text CLI (port 9090/TCP)** — the classic line-based
  query/response protocol.
- **HTTP (port 9000/TCP)** — the web UI, JSON-RPC/CometD API (used by
  JiveLite's actual menu system, over a Bayeux `/cometd` channel), artwork
  fetches, and audio stream URLs. Request/status lines and a configurable
  preview of JSON bodies are logged (headers are summarized, not dumped in
  full, to keep the log readable); binary bodies like artwork and audio
  are safely skipped rather than logged as garbage.

## Key features

- **Zero dependencies.** Just Python 3 and the standard library — nothing
  to `pip install`, runs the same way on Linux or Windows.
- **Selective logging per protocol.** Each of the four channels can be
  fully disabled (`--no-cli`, `--no-slim`, `--no-http`,
  `--no-udp-discovery`) or kept forwarding traffic while silencing its log
  output (`--slim-no-log`, `--http-no-log`, `--udp-no-log`), so you can
  focus on exactly the layer you're debugging.
- **Self-broadcast filtering.** LMS servers periodically broadcast their
  own UDP discovery queries to find peer servers on the LAN. The proxy
  recognizes and drops these automatically instead of relaying them back
  to the server under a different name, which would otherwise create a
  confusing phantom "peer server" entry.
- **HTTP traffic can be redirected to a completely different backend.**
  `--http-target-host` (and `--http-target-port`) let port 9000 traffic
  go somewhere other than `--target-host` — e.g. a test/scaffold server
  implementing the JSON-RPC/CometD API on its own, while SlimProto, the
  CLI, and UDP discovery continue relaying to a real LMS instance for
  everything else the client needs before it'll fully boot (player
  registration, heartbeats, etc.). This is the setup used to build and
  test an LMS-compatible scaffold server against a real device, without
  needing to reimplement SlimProto or the CLI just to get that far.
  ```
  python3 lms_cli_proxy.py --target-host 192.168.1.50 \
    --http-target-host 127.0.0.1 --http-target-port 9001 \
    --udp-rename "LMS Proxy"
  ```
- **Server identity rewriting.** The proxy can rewrite three fields in
  UDP discovery responses so the traffic it relays presents a distinct,
  self-consistent server identity, separate from whatever it's actually
  forwarding to:
  - `--udp-rename NAME` — rewrites the `NAME` field (e.g. `"LMS Proxy"`)
    so it's instantly distinguishable from the real server in
    client-side library/server pickers.
  - **`JSON` (the advertised HTTP port) is always corrected** to match
    `--http-listen-port`, regardless of `--udp-rename`. Discovery
    replies otherwise echo whatever port the *real* backend reports
    about itself — harmless if it happens to match the proxy's own
    listen port, but a silent, total connection failure the moment it
    doesn't (e.g. if the real server's HTTP port ever gets moved for any
    reason). Nothing in any log explains this on its own, since the
    server's own name is still reported correctly - only the port is
    wrong.
  - `--udp-rename-uuid` (defaults to a fixed placeholder,
    `00000000-0000-4000-8000-000000000000`) rewrites the `UUID` field.
    This one isn't cosmetic: real JiveLite/SqueezePlay clients track
    servers **by this UUID**, not by name or address (confirmed
    directly from the client's own source:
    `SlimServer(jnt, uuid, name, version)` followed by
    `server:updateAddress(ip, port, name)` on whatever object that UUID
    resolves to). If the proxy's UUID matches the real backend's own
    (which it does by default unless rewritten), the client doesn't see
    two distinct servers at all — it sees **one** server object whose
    address gets silently overwritten back and forth every time a fresh
    discovery reply arrives from either the proxy or the real server's
    own independent broadcast (which the proxy can never suppress,
    since discovery queries are LAN broadcasts every listener answers
    directly - not something relayed through the proxy at all). Each
    address flip forces whatever connection was open to disconnect and
    reconnect at the new address - in practice this showed up as
    repeated, rapid disconnect/reconnect churn that looked like a
    timing or reliability problem, and had nothing to do with either.
    Pass `--udp-rename-uuid ''` to disable this rewrite and let the
    real UUID through unmodified.
- **Wireshark-ready PCAP capture.** `--pcap-file capture.pcap` writes a
  standard pcap file alongside the text log, hand-synthesized (no scapy/
  dpkt dependency) to look like a **direct** capture between the real
  client and the real server — the proxy itself is invisible as a network
  hop. IP/TCP/UDP checksums are computed correctly, so Wireshark's
  checksum validation passes cleanly and its built-in HTTP + JSON
  dissectors work as expected.
  - Handles LMS's persistent CometD "streaming" transport (which holds an
    HTTP response open indefinitely and therefore often never sends a
    valid chunked-encoding terminator) by injecting a synthetic
    terminating chunk **into the capture file only** when such a
    connection closes — the real wire traffic sent to the client is
    completely unaffected. This lets Wireshark treat the response as
    complete and correctly decode the JSON.
  - Shutdown-safe: still-open connections get a proper synthetic FIN
    sequence written on exit (via `Ctrl+C`), using a plain OS signal
    handler rather than relying on asyncio's own cancellation behavior,
    specifically because that path is unreliable on Windows.

## Requirements

- Python 3.8+
- No third-party packages required

## Usage

Point the client (e.g. piCorePlayer's manual server/library address) at the
machine running this proxy instead of at the real LMS server directly:

```
python3 lms_cli_proxy.py --target-host 192.168.1.50 --log-file lms.log
```

That's it — all four protocols start listening on their standard ports
(`9090` CLI, `3483` SlimProto TCP, `3483` UDP discovery, `9000` HTTP) and
forward to the same ports on the real server.

### Common examples

Capture everything to Wireshark, rename the proxy so it's identifiable, and
quiet down the noisy SlimProto heartbeat traffic in the text log:

```
python3 lms_cli_proxy.py \
  --target-host 192.168.1.50 \
  --slim-no-log \
  --udp-rename "LMS Proxy" \
  --pcap-file lms_capture.pcap \
  --log-file lms.log
```

Only care about the CometD/JSON-RPC traffic on port 9000, with a large
body preview so you can actually read the menu payloads:

```
python3 lms_cli_proxy.py \
  --target-host 192.168.1.50 \
  --no-cli --no-slim \
  --http-body-preview 20000 \
  --log-file lms.log
```

Test a from-scratch server implementation of the CometD/JSON-RPC API
against a real client, while still relying on a real LMS instance for
everything else (SlimProto, the CLI, UDP discovery) the client needs
before it'll fully boot — this is the setup that made it possible to
build and validate an LMS-compatible scaffold server entirely against
real device behavior, one capture at a time:

```
python3 lms_cli_proxy.py \
  --target-host 192.168.1.50 \
  --http-target-host 127.0.0.1 --http-target-port 9001 \
  --udp-rename "LMS Proxy" \
  --pcap-file lms_capture.pcap \
  --log-file lms.log
```

### Full option reference

```
--target-host HOST         Real LMS server host/IP (required)
--listen-host HOST         Address to listen on (default 0.0.0.0)

--cli-listen-port PORT     CLI listen port (default 9090)
--cli-target-port PORT     Real LMS CLI port (default 9090)
--no-cli                   Disable the CLI (9090) proxy

--slim-listen-port PORT    SlimProto listen port (default 3483)
--slim-target-port PORT    Real LMS SlimProto port (default 3483)
--no-slim                  Disable the SlimProto (3483) proxy entirely
--slim-no-log              Keep forwarding, but skip per-frame logging
--slim-no-connect-log      With --slim-no-log, also hide connect/close lines

--http-listen-port PORT    HTTP listen port (default 9000)
--http-target-port PORT    Real LMS HTTP port (default 9000)
--http-target-host HOST    Override just the HTTP target's host, e.g. to
                            point port 9000 at a different server (like a
                            scaffold implementation) while CLI/SlimProto/UDP
                            still go to --target-host. Defaults to
                            --target-host if not given.
--no-http                  Disable the HTTP (9000) proxy entirely
--http-no-log              Keep forwarding, but skip header/request logging
--http-no-connect-log      With --http-no-log, also hide connect/close lines
--http-body-preview N      Bytes of HTTP body to log as a preview (default 8000)

--udp-listen-port PORT     UDP discovery listen port (default 3483)
--udp-target-port PORT     Real LMS UDP discovery port (default 3483)
--no-udp-discovery         Disable the UDP discovery relay entirely
--udp-no-log               Keep relaying, but skip logging it
--udp-rename NAME          Rewrite the server NAME field in discovery
                            responses (e.g. "LMS Proxy")
--udp-rename-uuid UUID     Rewrite the server UUID field in discovery
                            responses (default: a fixed placeholder). Not
                            cosmetic - see "Server identity rewriting" above.
                            Pass '' to disable and let the real UUID through.
                            (The JSON/port field is always corrected to
                            --http-listen-port, unconditionally - no flag
                            needed to enable that part.)

--pcap-file PATH            Also write all proxied traffic to this .pcap
                             file, openable in Wireshark
--log-file PATH              Optional path to also write logs to a file
```

## Companion tools

**`pcap_json_extract.py`** — extracts and pretty-prints JSON message bodies
directly from a pcap file, without relying on Wireshark's own HTTP/chunked
reassembly (useful since Wireshark's dissection can be finicky with LMS's
persistent CometD streaming transport, and since LMS concatenates multiple
JSON documents back-to-back in one body with no separator, which
`json.loads()` alone can't parse). No dependencies.

```
python3 pcap_json_extract.py capture.pcap --grep "some artist name"
python3 pcap_json_extract.py capture.pcap --port 9000
```

**`test_udp_discovery.py`** — sends a raw UDP discovery query straight at a
target host/port and reports whether it answers, useful for isolating
whether a discovery failure is on the proxy's side or the real server's
side.

```
python3 test_udp_discovery.py 192.168.1.50
```

## How it works

Each protocol gets its own async TCP or UDP relay: bytes are forwarded
byte-for-byte in both directions (nothing is altered on the wire, other
than the specific UDP discovery fields covered above when explicitly
requested or, for the JSON port field, always), while a protocol-aware
parser runs alongside purely for logging purposes — splitting CLI text
into lines, decoding SlimProto's tag+length framing, and walking HTTP's
request/response/chunked framing to produce a readable summary and body
preview per message.

## Background

Built while reverse-engineering how JiveLite (piCorePlayer's touchscreen UI)
actually retrieves its media library data, in order to extend a Home
Assistant Music Assistant integration with LMS-compatible library browsing.
It turned out JiveLite's menu system is driven primarily by a CometD/Bayeux
session over HTTP (`/cometd`), not the classic text CLI — a detail this
proxy's traffic capture made possible to confirm directly from the wire
rather than from documentation or guesswork. The same capture-driven
approach later tracked down two much subtler issues that had nothing to do
with the JSON payloads themselves: a UDP discovery response silently
advertising the wrong HTTP port after the real server was moved to a
non-default one, and a UUID collision that made two genuinely different
servers look like one to the client's own connection-tracking logic.
