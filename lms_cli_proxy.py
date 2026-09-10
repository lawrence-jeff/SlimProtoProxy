#!/usr/bin/env python3
"""
LMS proxy for piCorePlayer/JiveLite testing.

Runs a UDP discovery relay plus three transparent, logging TCP proxies
against a real Logitech/Lyrion Media Server:

  * UDP discovery (port 3483/UDP) - Squeezebox-family clients validate a
    server address with a UDP query/response round trip before committing
    to it over TCP. Relayed straight through to the real server so manual
    "library"/server address entries actually pass validation.
  * CLI port (default 9090)     - plain text, line-based protocol. Used by
    JiveLite to drive its menu tree (browse artists/albums/tracks/etc).
  * SlimProto port (default 3483/TCP) - binary, frame-based protocol. Used
    by squeezelite (and JiveLite's underlying client) to register the
    player and handle control/status.
  * HTTP port (default 9000) - web UI, JSON-RPC/CometD API, artwork, and
    the actual audio stream URLs that SlimProto's 'strm' command points
    players at. Headers are logged; bodies (artwork bytes, audio bytes)
    are forwarded but not logged, since they're binary and can be large
    or effectively unbounded (a streaming audio response has no end until
    playback stops).

Point piCorePlayer's "LMS server" address at the host running this script.
Everything logs with a timestamp, connection id, and direction, so you can
see exactly what JiveLite/squeezelite send and what the real server answers.

Usage:
    python3 lms_cli_proxy.py --target-host 192.168.1.50 --log-file lms.log

Disable any piece with --no-cli / --no-slim / --no-http / --no-udp-discovery
if you don't want it running. Use --slim-no-log / --http-no-log / --udp-no-log
to keep something forwarding traffic (so the player still works) while
skipping its log output.
"""

import argparse
import asyncio
import datetime
import logging
import re
import socket
import struct
import sys
import time

logger = logging.getLogger("lms_proxy")

# ---------------------------------------------------------------------------
# Connection id counter (shared across both proxies so ids stay unique)
# ---------------------------------------------------------------------------


class ConnectionCounter:
    _n = 0

    @classmethod
    def next_id(cls) -> int:
        cls._n += 1
        return cls._n


def ts() -> str:
    return datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]


# ---------------------------------------------------------------------------
# Optional PCAP capture (--pcap-file). Hand-builds IPv4/TCP/UDP packets with
# real checksums (no scapy/dpkt dependency, so this stays a single portable
# script) and writes them to a classic pcap file using LINKTYPE_RAW (no
# Ethernet header needed). Each proxied connection is synthesized to look
# like a *direct* conversation between the real client (piCorePlayer) and
# the real target (your LMS) - the proxy itself is invisible in the capture
# - so Wireshark's "Follow TCP Stream" and its built-in HTTP/JSON dissectors
# work exactly as they would on a real wiretap.
# ---------------------------------------------------------------------------

_PCAP_LINKTYPE_RAW = 101


def _ip_checksum(data: bytes) -> int:
    if len(data) % 2:
        data += b"\x00"
    total = 0
    for i in range(0, len(data), 2):
        total += (data[i] << 8) + data[i + 1]
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    return (~total) & 0xFFFF


def _build_ipv4_header(src_ip: str, dst_ip: str, proto: int, payload_len: int, ident: int) -> bytes:
    ver_ihl = 0x45
    tos = 0
    total_len = 20 + payload_len
    flags_frag = 0x4000  # don't fragment
    ttl = 64
    header = struct.pack(
        "!BBHHHBBH4s4s",
        ver_ihl, tos, total_len, ident & 0xFFFF, flags_frag, ttl, proto, 0,
        socket.inet_aton(src_ip), socket.inet_aton(dst_ip),
    )
    checksum = _ip_checksum(header)
    return header[:10] + struct.pack("!H", checksum) + header[12:]


def _build_tcp_segment(src_ip, src_port, dst_ip, dst_port, seq, ack, flags, payload, window=64240) -> bytes:
    data_offset = 5 << 4
    tcp_header = struct.pack(
        "!HHIIBBHHH",
        src_port, dst_port, seq & 0xFFFFFFFF, ack & 0xFFFFFFFF,
        data_offset, flags, window, 0, 0,
    )
    pseudo = struct.pack(
        "!4s4sBBH",
        socket.inet_aton(src_ip), socket.inet_aton(dst_ip), 0, 6, len(tcp_header) + len(payload),
    )
    checksum = _ip_checksum(pseudo + tcp_header + payload)
    tcp_header = tcp_header[:16] + struct.pack("!H", checksum) + tcp_header[18:]
    return tcp_header + payload


def _build_udp_segment(src_ip, src_port, dst_ip, dst_port, payload) -> bytes:
    length = 8 + len(payload)
    # checksum 0 = not computed, which is valid for UDP over IPv4
    return struct.pack("!HHHH", src_port, dst_port, length, 0) + payload


# TCP flags
_TCP_SYN = 0x02
_TCP_ACK = 0x10
_TCP_PSH_ACK = 0x18
_TCP_FIN_ACK = 0x11
_TCP_SYN_ACK = 0x12


class PcapWriter:
    def __init__(self, path):
        self._f = open(path, "wb")
        self._f.write(struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 262144, _PCAP_LINKTYPE_RAW))
        self._ident = 0
        self._tcp_state = {}  # conn_id -> dict(client_seq, server_seq, client_ip, client_port, server_ip, server_port)

    def _next_ident(self):
        self._ident = (self._ident + 1) & 0xFFFF
        return self._ident

    def _write_record(self, ip_packet: bytes):
        if self._f is None:
            return
        now = time.time()
        sec = int(now)
        usec = int((now - sec) * 1_000_000)
        n = len(ip_packet)
        self._f.write(struct.pack("<IIII", sec, usec, n, n))
        self._f.write(ip_packet)
        self._f.flush()  # flush per-packet: this tool is often killed abruptly (Ctrl+C), so don't risk losing the capture

    def _write_tcp_packet(self, src_ip, src_port, dst_ip, dst_port, seq, ack, flags, payload=b""):
        seg = _build_tcp_segment(src_ip, src_port, dst_ip, dst_port, seq, ack, flags, payload)
        ip = _build_ipv4_header(src_ip, dst_ip, 6, len(seg), self._next_ident())
        self._write_record(ip + seg)

    def open_tcp(self, conn_id, client_ip, client_port, server_ip, server_port):
        st = {
            "client_ip": client_ip, "client_port": client_port,
            "server_ip": server_ip, "server_port": server_port,
            "client_seq": 1_000_000, "server_seq": 5_000_000,
        }
        self._tcp_state[conn_id] = st
        # synthetic 3-way handshake so the stream reconstructs cleanly
        self._write_tcp_packet(client_ip, client_port, server_ip, server_port, st["client_seq"], 0, _TCP_SYN)
        self._write_tcp_packet(server_ip, server_port, client_ip, client_port, st["server_seq"], st["client_seq"] + 1, _TCP_SYN_ACK)
        st["client_seq"] += 1
        self._write_tcp_packet(client_ip, client_port, server_ip, server_port, st["client_seq"], st["server_seq"] + 1, _TCP_ACK)
        st["server_seq"] += 1

    def write_tcp(self, conn_id, direction: str, payload: bytes):
        if not payload:
            return
        st = self._tcp_state.get(conn_id)
        if st is None:
            return
        if direction == "C->S":
            src_ip, src_port = st["client_ip"], st["client_port"]
            dst_ip, dst_port = st["server_ip"], st["server_port"]
            seq, ack = st["client_seq"], st["server_seq"]
            st["client_seq"] += len(payload)
        else:
            src_ip, src_port = st["server_ip"], st["server_port"]
            dst_ip, dst_port = st["client_ip"], st["client_port"]
            seq, ack = st["server_seq"], st["client_seq"]
            st["server_seq"] += len(payload)
        self._write_tcp_packet(src_ip, src_port, dst_ip, dst_port, seq, ack, _TCP_PSH_ACK, payload)

    def close_tcp(self, conn_id):
        st = self._tcp_state.pop(conn_id, None)
        if st is None:
            return
        self._write_tcp_packet(
            st["client_ip"], st["client_port"], st["server_ip"], st["server_port"],
            st["client_seq"], st["server_seq"], _TCP_FIN_ACK,
        )
        st["client_seq"] += 1
        self._write_tcp_packet(
            st["server_ip"], st["server_port"], st["client_ip"], st["client_port"],
            st["server_seq"], st["client_seq"], _TCP_FIN_ACK,
        )
        st["server_seq"] += 1
        self._write_tcp_packet(
            st["client_ip"], st["client_port"], st["server_ip"], st["server_port"],
            st["client_seq"], st["server_seq"], _TCP_ACK,
        )

    def write_udp(self, direction: str, client_ip, client_port, server_ip, server_port, payload: bytes):
        if direction == "C->S":
            src_ip, src_port, dst_ip, dst_port = client_ip, client_port, server_ip, server_port
        else:
            src_ip, src_port, dst_ip, dst_port = server_ip, server_port, client_ip, client_port
        seg = _build_udp_segment(src_ip, src_port, dst_ip, dst_port, payload)
        ip = _build_ipv4_header(src_ip, dst_ip, 17, len(seg), self._next_ident())
        self._write_record(ip + seg)

    def close_all_open(self):
        """Force-close any TCP streams still open (e.g. on shutdown) by
        writing a synthetic FIN exchange for each, so the capture never
        leaves a conversation dangling with no clean end."""
        for conn_id in list(self._tcp_state.keys()):
            self.close_tcp(conn_id)

    def close(self):
        if self._f is None:
            return
        try:
            self._f.close()
        except Exception:
            pass
        self._f = None


# ---------------------------------------------------------------------------
# CLI protocol (port 9090): plain text, lines terminated by LF/CR/NUL
# ---------------------------------------------------------------------------

_TERMINATORS = (b"\n", b"\r", b"\x00")


def _split_lines(buf: bytes):
    """Split buf on any CLI line terminator. Returns (lines, remaining_buf)."""
    lines = []
    start = 0
    i = 0
    n = len(buf)
    while i < n:
        if buf[i:i + 1] in _TERMINATORS:
            if i > start:
                lines.append(buf[start:i])
            i += 1
            while i < n and buf[i:i + 1] in _TERMINATORS:
                i += 1
            start = i
        else:
            i += 1
    return lines, buf[start:]


def _fmt_text(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return f"<{len(raw)} bytes binary: {raw[:64].hex()}{'...' if len(raw) > 64 else ''}>"


async def pump_cli(reader, writer, conn_id, direction, quiet=False, pcap=None):
    """CLI pump: forwards raw bytes, logs complete text lines (unless quiet)."""
    buf = b""
    try:
        while True:
            chunk = await reader.read(4096)
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()
            if pcap is not None:
                pcap.write_tcp(conn_id, direction, chunk)

            buf += chunk
            lines, buf = _split_lines(buf)
            if not quiet:
                for line in lines:
                    logger.info("[%s] conn=%d CLI %s %s", ts(), conn_id, direction, _fmt_text(line))
    except (ConnectionResetError, asyncio.IncompleteReadError):
        pass
    finally:
        if buf and not quiet:
            logger.info("[%s] conn=%d CLI %s %s (unterminated)", ts(), conn_id, direction, _fmt_text(buf))
        try:
            writer.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# SlimProto (port 3483): binary frames = 4-byte ASCII tag + 4-byte BE length
# + payload. Same framing is used in both directions.
# ---------------------------------------------------------------------------

# Tags that fire very frequently (status heartbeats) - log first few in full,
# then just count them so the log stays readable.
_NOISY_TAGS = {"STAT"}
_NOISY_LOG_LIMIT = 3


def _read_frame(buf: bytes):
    """Try to pull one complete SlimProto frame off the front of buf.

    Returns (tag, payload, remaining_buf) or (None, None, buf) if incomplete.
    """
    if len(buf) < 8:
        return None, None, buf
    tag = buf[0:4]
    length = int.from_bytes(buf[4:8], "big")
    if len(buf) < 8 + length:
        return None, None, buf
    payload = buf[8:8 + length]
    return tag, payload, buf[8 + length:]


def _fmt_tag(tag: bytes) -> str:
    try:
        return tag.decode("ascii").strip()
    except UnicodeDecodeError:
        return tag.hex()


async def pump_slim(reader, writer, conn_id, direction, quiet=False, pcap=None):
    """SlimProto pump: forwards raw bytes, logs decoded frames unless quiet."""
    buf = b""
    noisy_counts = {}
    try:
        while True:
            chunk = await reader.read(4096)
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()
            if pcap is not None:
                pcap.write_tcp(conn_id, direction, chunk)

            buf += chunk

            if quiet:
                # Still need to consume/discard frames to keep buf from growing
                # unbounded, but skip all logging.
                while True:
                    tag, payload, buf = _read_frame(buf)
                    if tag is None:
                        break
                continue

            while True:
                tag, payload, buf = _read_frame(buf)
                if tag is None:
                    break
                tag_str = _fmt_tag(tag)
                preview = payload[:48].hex()
                more = "..." if len(payload) > 48 else ""

                if tag_str in _NOISY_TAGS:
                    n = noisy_counts.get(tag_str, 0) + 1
                    noisy_counts[tag_str] = n
                    if n <= _NOISY_LOG_LIMIT:
                        logger.info(
                            "[%s] conn=%d SLIM %s tag=%s len=%d payload=%s%s",
                            ts(), conn_id, direction, tag_str, len(payload), preview, more,
                        )
                        if n == _NOISY_LOG_LIMIT:
                            logger.info(
                                "[%s] conn=%d SLIM %s tag=%s (further occurrences suppressed, counting only)",
                                ts(), conn_id, direction, tag_str,
                            )
                else:
                    logger.info(
                        "[%s] conn=%d SLIM %s tag=%s len=%d payload=%s%s",
                        ts(), conn_id, direction, tag_str, len(payload), preview, more,
                    )
    except (ConnectionResetError, asyncio.IncompleteReadError):
        pass
    finally:
        if not quiet:
            for tag_str, n in noisy_counts.items():
                if n > _NOISY_LOG_LIMIT:
                    logger.info(
                        "[%s] conn=%d SLIM %s tag=%s total occurrences=%d (%d suppressed)",
                        ts(), conn_id, direction, tag_str, n, n - _NOISY_LOG_LIMIT,
                    )
            if len(buf) != 0:
                logger.info(
                    "[%s] conn=%d SLIM %s %d leftover unparsed bytes: %s",
                    ts(), conn_id, direction, len(buf), buf[:64].hex(),
                )
        try:
            writer.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# HTTP (port 9000): logs one line per request/response - the request-line
# or status-line plus a short preview of the body content (if any) - rather
# than splitting the summary and the preview across separate log lines, or
# dumping full header blocks.
#
# This port carries the web UI, JSON-RPC/CometD API, artwork fetches, and -
# notably - the actual audio stream URLs that SlimProto's 'strm' command
# points players at. Artwork and audio bodies can be large or effectively
# unbounded (a streaming audio response has no end until playback stops),
# so only the preview is ever logged - never the full body.
# ---------------------------------------------------------------------------

_CL_RE = re.compile(rb"(?im)^content-length:\s*(\d+)\s*$")
_CHUNKED_RE = re.compile(rb"(?im)^transfer-encoding:\s*chunked\s*$")

_DEFAULT_HTTP_BODY_PREVIEW = 8000


def _fmt_preview(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return f"<binary: {raw[:64].hex()}{'...' if len(raw) > 64 else ''}>"


def _log_http_message(conn_id, direction, first_line: bytes, preview: bytes = None, note: str = ""):
    text = first_line.decode("latin-1", errors="replace")
    if preview is not None:
        logger.info(
            "[%s] conn=%d HTTP %s %s%s body=%s",
            ts(), conn_id, direction, text, note, _fmt_preview(preview),
        )
    else:
        logger.info("[%s] conn=%d HTTP %s %s%s", ts(), conn_id, direction, text, note)


async def pump_http(reader, writer, conn_id, direction, quiet=False, body_preview=_DEFAULT_HTTP_BODY_PREVIEW, pcap=None):
    """HTTP pump: forwards raw bytes, logs one summary+preview line per message."""
    buf = b""
    mode = "headers"  # "headers" or "body"
    body_remaining = None  # int (bytes left, Content-Length) or None (unbounded/chunked)
    body_kind = ""
    pending_first_line = None
    preview_buf = bytearray()
    preview_logged = False
    streaming = False  # once True: no more parsing at all, just relay silently
    try:
        while True:
            chunk = await reader.read(4096)
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()
            if pcap is not None:
                pcap.write_tcp(conn_id, direction, chunk)

            if quiet or streaming:
                continue

            buf += chunk
            while True:
                if mode == "headers":
                    idx = buf.find(b"\r\n\r\n")
                    sep_len = 4
                    if idx == -1:
                        idx = buf.find(b"\n\n")
                        sep_len = 2
                        if idx == -1:
                            break
                    header_block = buf[:idx]
                    buf = buf[idx + sep_len:]

                    first_line = header_block.split(b"\n", 1)[0].strip()
                    cl_match = _CL_RE.search(header_block)
                    chunked = _CHUNKED_RE.search(header_block) is not None

                    preview_buf = bytearray()
                    preview_logged = False

                    if cl_match:
                        n = int(cl_match.group(1))
                        if n == 0:
                            if not quiet:
                                _log_http_message(conn_id, direction, first_line, note=" (no body)")
                            mode = "headers"
                        else:
                            body_remaining = n
                            body_kind = f"len={n}"
                            pending_first_line = first_line
                            mode = "body"
                    elif chunked:
                        body_remaining = None
                        body_kind = "chunked"
                        pending_first_line = first_line
                        mode = "body"
                    elif first_line.startswith(b"HTTP/"):
                        # Response with no Content-Length and not chunked:
                        # typically an indefinite stream (e.g. audio).
                        body_remaining = None
                        body_kind = "streaming"
                        pending_first_line = first_line
                        mode = "body"
                    else:
                        # Request line with no body (GET/HEAD etc) - back to headers.
                        if not quiet:
                            _log_http_message(conn_id, direction, first_line, note=" (no body)")
                        mode = "headers"

                elif mode == "body":
                    if not preview_logged:
                        want = body_preview - len(preview_buf)
                        take_n = max(want, 0)
                        if body_remaining is not None:
                            take_n = min(take_n, body_remaining)
                        take_n = min(take_n, len(buf))

                        if take_n > 0:
                            preview_buf += buf[:take_n]
                            buf = buf[take_n:]
                            if body_remaining is not None:
                                body_remaining -= take_n

                        body_done = body_remaining is not None and body_remaining <= 0
                        preview_full = len(preview_buf) >= body_preview

                        if preview_full or body_done:
                            note = f" ({body_kind}, {len(preview_buf)}/{body_preview} preview bytes)"
                            if not quiet:
                                _log_http_message(conn_id, direction, pending_first_line, bytes(preview_buf), note)
                            preview_logged = True
                            if body_done:
                                mode = "headers"
                            # else: bounded body but preview cap hit first - fall
                            # through to the skip branch below on next loop pass
                            continue
                        else:
                            # Need more data (next read) to finish the preview.
                            break
                    else:
                        if body_remaining is not None:
                            if len(buf) >= body_remaining:
                                buf = buf[body_remaining:]
                                body_remaining = 0
                                mode = "headers"
                                continue
                            else:
                                body_remaining -= len(buf)
                                buf = b""
                                break
                        else:
                            # Unbounded body (chunked/streaming) and preview
                            # already captured - go fully silent for the rest.
                            streaming = True
                            break
    except (ConnectionResetError, asyncio.IncompleteReadError):
        pass
    finally:
        if not quiet and not streaming and mode == "body" and not preview_logged and pending_first_line is not None:
            note = f" ({body_kind}, connection ended, {len(preview_buf)} bytes)"
            _log_http_message(conn_id, direction, pending_first_line, bytes(preview_buf), note)

        if pcap is not None and mode == "body" and body_kind == "chunked":
            # The real chunked body never reached its own terminating
            # 0-length chunk before this connection closed (this is
            # normal for LMS's persistent CometD streaming transport -
            # it holds the response open indefinitely). Inject a
            # synthetic terminator into the CAPTURE ONLY so Wireshark can
            # treat it as a complete, valid chunked response and decode
            # the JSON - this byte never goes out over the real wire.
            pcap.write_tcp(conn_id, direction, b"0\r\n\r\n")
            if not quiet:
                logger.info(
                    "[%s] conn=%d HTTP %s injected synthetic chunk terminator into PCAP capture only (real chunked body never completed on the wire)",
                    ts(), conn_id, direction,
                )

        try:
            writer.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Optional rewrite of the server name in the UDP discovery response, so the
# proxy is visually distinguishable from the real server in library/server
# pickers that display this NAME field. The extended discovery response
# format is: 'E' + repeated(4-byte tag, 1-byte length, value bytes).
# Older SLIMP3-style 'D'-prefixed fixed responses are left untouched.
# ---------------------------------------------------------------------------


def _rewrite_discovery_name(data: bytes, new_name: bytes) -> bytes:
    if len(data) < 1 or data[0:1] != b"E":
        return data

    body = data[1:]
    out = bytearray(b"E")
    i = 0
    n = len(body)
    replaced = False
    while i + 5 <= n:
        tag = body[i:i + 4]
        length = body[i + 4]
        i += 5
        value = body[i:i + length]
        i += length
        if tag == b"NAME" and not replaced:
            trimmed = new_name[:255]
            out += tag + bytes([len(trimmed)]) + trimmed
            replaced = True
        else:
            out += tag + bytes([length]) + value
    if i < n:
        out += body[i:]  # any trailing/malformed bytes, pass through untouched
    return bytes(out)


# ---------------------------------------------------------------------------
# UDP discovery relay (port 3483/UDP): Squeezebox-family clients validate a
# server address with a UDP query/response round trip *before* committing to
# it over TCP. Without answering this, a manually-entered "library" address
# will appear to fail after a short timeout and the client falls back to
# whatever server it already knows works. This relays each incoming UDP
# datagram to the real server and pipes the reply straight back.
# ---------------------------------------------------------------------------


class DiscoveryRelay(asyncio.DatagramProtocol):
    def __init__(self, target_host, target_port, quiet=False, timeout=2.0, rename_to=None, pcap=None):
        self.target_host = target_host
        self.target_port = target_port
        self.quiet = quiet
        self.timeout = timeout
        self.rename_to = rename_to.encode() if rename_to else None
        self.pcap = pcap
        self.transport = None
        # Resolved once so we can recognize (and ignore) the real server's
        # own discovery broadcasts arriving at our listener - see
        # datagram_received() below for why this matters.
        try:
            self._target_ip = socket.gethostbyname(target_host)
        except OSError:
            self._target_ip = None
            logger.warning("Could not resolve --target-host %r for UDP self-broadcast filtering; that filter is disabled.", target_host)

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        if self._target_ip is not None and addr[0] == self._target_ip:
            # The real server periodically broadcasts its own discovery
            # queries to find peer LMS servers on the LAN. If we relayed
            # this back to itself and renamed the reply, the server would
            # believe a second, independent "LMS Proxy" server exists at
            # our IP. Drop it here instead - nothing useful comes from
            # answering the server's own broadcast.
            if not self.quiet:
                logger.info(
                    "[%s] UDP ignoring discovery broadcast FROM the target server itself (%s) - not relaying to avoid creating a phantom peer server",
                    ts(), addr,
                )
            return

        conn_id = ConnectionCounter.next_id()
        if not self.quiet:
            logger.info(
                "[%s] conn=%d UDP C->S from %s len=%d payload=%s",
                ts(), conn_id, addr, len(data), data[:64].hex(),
            )
        asyncio.create_task(self._relay(data, addr, conn_id))

    def error_received(self, exc):
        logger.error("UDP discovery relay socket error: %s", exc)

    async def _relay(self, data, addr, conn_id):
        loop = asyncio.get_running_loop()
        response_future = loop.create_future()

        class UpstreamProto(asyncio.DatagramProtocol):
            def datagram_received(self, resp_data, resp_addr):
                if not response_future.done():
                    response_future.set_result(resp_data)

            def error_received(self, exc):
                if not response_future.done():
                    response_future.set_exception(exc)

        try:
            upstream_transport, _ = await loop.create_datagram_endpoint(
                UpstreamProto, remote_addr=(self.target_host, self.target_port)
            )
        except OSError as e:
            logger.error("conn=%d UDP FAILED to reach target %s:%d -> %s", conn_id, self.target_host, self.target_port, e)
            return

        server_peer = upstream_transport.get_extra_info("peername")  # resolved numeric IP, needed for pcap

        try:
            upstream_transport.sendto(data)
            if self.pcap is not None:
                self.pcap.write_udp("C->S", addr[0], addr[1], server_peer[0], server_peer[1], data)
            try:
                resp = await asyncio.wait_for(response_future, timeout=self.timeout)
            except asyncio.TimeoutError:
                if not self.quiet:
                    logger.info("[%s] conn=%d UDP no response from target within %.1fs", ts(), conn_id, self.timeout)
                return

            if self.rename_to:
                original = resp
                resp = _rewrite_discovery_name(resp, self.rename_to)
                if not self.quiet and resp != original:
                    logger.info("[%s] conn=%d UDP rewrote server NAME to %r", ts(), conn_id, self.rename_to)

            if self.transport is not None:
                self.transport.sendto(resp, addr)
            if self.pcap is not None:
                self.pcap.write_udp("S->C", addr[0], addr[1], server_peer[0], server_peer[1], resp)
            if not self.quiet:
                logger.info(
                    "[%s] conn=%d UDP S->C to %s len=%d payload=%s",
                    ts(), conn_id, addr, len(resp), resp[:64].hex(),
                )
        finally:
            upstream_transport.close()


async def start_udp_relay(listen_host, listen_port, target_host, target_port, quiet=False, rename_to=None, pcap=None):
    loop = asyncio.get_running_loop()
    transport, protocol = await loop.create_datagram_endpoint(
        lambda: DiscoveryRelay(target_host, target_port, quiet, rename_to=rename_to, pcap=pcap),
        local_addr=(listen_host, listen_port),
    )
    logger.info(
        "UDP discovery listening on %s:%d -> forwarding to %s:%d%s",
        listen_host, listen_port, target_host, target_port, " (logging disabled)" if quiet else "",
    )
    try:
        await asyncio.Event().wait()  # run forever
    finally:
        transport.close()


# ---------------------------------------------------------------------------
# Generic connection handling (TCP)
# ---------------------------------------------------------------------------

async def handle_client(client_reader, client_writer, target_host, target_port, proto_label, pump_fn, quiet=False, quiet_connect_log=False, pump_kwargs=None, pcap=None):
    conn_id = ConnectionCounter.next_id()
    peer = client_writer.get_extra_info("peername")
    if not quiet_connect_log:
        logger.info("conn=%d %s NEW CONNECTION from %s (listen target %s:%d)", conn_id, proto_label, peer, target_host, target_port)

    try:
        server_reader, server_writer = await asyncio.open_connection(target_host, target_port)
    except OSError as e:
        logger.error("conn=%d %s FAILED to connect to target %s:%d -> %s", conn_id, proto_label, target_host, target_port, e)
        client_writer.close()
        return

    if not quiet_connect_log:
        logger.info("conn=%d %s connected to target %s:%d", conn_id, proto_label, target_host, target_port)

    if pcap is not None:
        client_ip, client_port = peer[0], peer[1]
        server_peer = server_writer.get_extra_info("peername")
        pcap.open_tcp(conn_id, client_ip, client_port, server_peer[0], server_peer[1])

    kwargs = pump_kwargs or {}
    to_server = asyncio.create_task(pump_fn(client_reader, server_writer, conn_id, "C->S", quiet, pcap=pcap, **kwargs))
    to_client = asyncio.create_task(pump_fn(server_reader, client_writer, conn_id, "S->C", quiet, pcap=pcap, **kwargs))

    try:
        done, pending = await asyncio.wait([to_server, to_client], return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
    finally:
        # This runs even if this task itself is cancelled (e.g. during
        # shutdown) mid-await above, so the pcap FIN always gets written.
        for w in (client_writer, server_writer):
            try:
                w.close()
            except Exception:
                pass

        if pcap is not None:
            pcap.close_tcp(conn_id)

        if not quiet_connect_log:
            logger.info("conn=%d %s CLOSED", conn_id, proto_label)


async def start_proxy(listen_host, listen_port, target_host, target_port, proto_label, pump_fn, quiet=False, quiet_connect_log=False, pump_kwargs=None, pcap=None):
    server = await asyncio.start_server(
        lambda r, w: handle_client(r, w, target_host, target_port, proto_label, pump_fn, quiet, quiet_connect_log, pump_kwargs, pcap),
        host=listen_host,
        port=listen_port,
    )
    addrs = ", ".join(str(s.getsockname()) for s in server.sockets)
    logger.info("%s listening on %s -> forwarding to %s:%d%s", proto_label, addrs, target_host, target_port, " (logging disabled)" if quiet else "")
    async with server:
        await server.serve_forever()


async def main_async(args, pcap):
    tasks = []
    if not args.no_cli:
        tasks.append(start_proxy(args.listen_host, args.cli_listen_port, args.target_host, args.cli_target_port, "CLI", pump_cli, pcap=pcap))
    if not args.no_slim:
        tasks.append(start_proxy(
            args.listen_host, args.slim_listen_port, args.target_host, args.slim_target_port, "SLIM", pump_slim,
            quiet=args.slim_no_log, quiet_connect_log=args.slim_no_log and args.slim_no_connect_log, pcap=pcap,
        ))
    if not args.no_http:
        tasks.append(start_proxy(
            args.listen_host, args.http_listen_port, args.target_host, args.http_target_port, "HTTP", pump_http,
            quiet=args.http_no_log, quiet_connect_log=args.http_no_log and args.http_no_connect_log,
            pump_kwargs={"body_preview": args.http_body_preview}, pcap=pcap,
        ))
    if not args.no_udp_discovery:
        tasks.append(start_udp_relay(
            args.listen_host, args.udp_listen_port, args.target_host, args.udp_target_port,
            quiet=args.udp_no_log, rename_to=args.udp_rename, pcap=pcap,
        ))

    if not tasks:
        logger.error("All proxies disabled (--no-cli, --no-slim, --no-http, --no-udp-discovery) - nothing to do.")
        return

    logger.info("Logging to: %s", args.log_file if args.log_file else "(stdout only)")
    await asyncio.gather(*tasks)


def parse_args():
    p = argparse.ArgumentParser(description="LMS CLI + SlimProto logging proxy")
    p.add_argument("--target-host", required=True, help="Real LMS server host/IP")
    p.add_argument("--listen-host", default="0.0.0.0", help="Address to listen on (default 0.0.0.0)")

    p.add_argument("--cli-listen-port", type=int, default=9090, help="CLI listen port (default 9090)")
    p.add_argument("--cli-target-port", type=int, default=9090, help="Real LMS CLI port (default 9090)")
    p.add_argument("--no-cli", action="store_true", help="Disable the CLI (9090) proxy")

    p.add_argument("--slim-listen-port", type=int, default=3483, help="SlimProto listen port (default 3483)")
    p.add_argument("--slim-target-port", type=int, default=3483, help="Real LMS SlimProto port (default 3483)")
    p.add_argument("--no-slim", action="store_true", help="Disable the SlimProto (3483) proxy entirely")
    p.add_argument("--slim-no-log", action="store_true", help="Keep forwarding SlimProto (3483) traffic but skip per-frame logging")
    p.add_argument("--slim-no-connect-log", action="store_true", help="With --slim-no-log, also suppress connect/close lines for SlimProto (only meaningful combined with --slim-no-log)")

    p.add_argument("--http-listen-port", type=int, default=9000, help="HTTP listen port (default 9000)")
    p.add_argument("--http-target-port", type=int, default=9000, help="Real LMS HTTP port (default 9000)")
    p.add_argument("--no-http", action="store_true", help="Disable the HTTP (9000) proxy entirely")
    p.add_argument("--http-no-log", action="store_true", help="Keep forwarding HTTP (9000) traffic but skip header/request logging")
    p.add_argument("--http-no-connect-log", action="store_true", help="With --http-no-log, also suppress connect/close lines for HTTP (only meaningful combined with --http-no-log)")
    p.add_argument("--http-body-preview", type=int, default=_DEFAULT_HTTP_BODY_PREVIEW, help=f"Bytes of HTTP body to log as a preview (default {_DEFAULT_HTTP_BODY_PREVIEW}); headers are no longer logged in full, just the request/status line")

    p.add_argument("--udp-listen-port", type=int, default=3483, help="UDP discovery listen port (default 3483)")
    p.add_argument("--udp-target-port", type=int, default=3483, help="Real LMS UDP discovery port (default 3483)")
    p.add_argument("--no-udp-discovery", action="store_true", help="Disable the UDP discovery relay entirely")
    p.add_argument("--udp-no-log", action="store_true", help="Keep relaying UDP discovery traffic but skip logging it")
    p.add_argument("--udp-rename", default=None, help="Rewrite the server NAME field in discovery responses to this string (e.g. 'LMS Proxy') so it's distinguishable from the real server in library pickers")

    p.add_argument("--pcap-file", default=None, help="Also write all proxied traffic to this .pcap file (openable in Wireshark) - synthesized to look like a direct capture between the real client and the real server, with full HTTP/JSON decoding available via Wireshark's built-in dissectors. Runs independently of --log-file and the --*-no-log flags.")

    p.add_argument("--log-file", default=None, help="Optional path to also write logs to a file")
    return p.parse_args()


def setup_logging(log_file):
    handlers = [logging.StreamHandler(sys.stdout)]
    if log_file:
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(level=logging.INFO, format="%(message)s", handlers=handlers)


def _install_shutdown_handler(pcap):
    """Registers a plain signal handler (not an asyncio one) so Ctrl+C
    reliably finalizes any still-open pcap TCP streams even on platforms
    (notably Windows) where asyncio's own task-cancellation-on-interrupt
    behavior isn't dependable."""
    import signal

    def _handler(signum, frame):
        logger.info("Interrupted - closing out any open PCAP connections...")
        pcap.close_all_open()
        raise KeyboardInterrupt()

    signal.signal(signal.SIGINT, _handler)
    try:
        signal.signal(signal.SIGTERM, _handler)
    except (ValueError, AttributeError):
        pass  # SIGTERM isn't available in every environment (e.g. some Windows setups)


def main():
    args = parse_args()
    setup_logging(args.log_file)

    pcap = None
    if args.pcap_file:
        pcap = PcapWriter(args.pcap_file)
        logger.info("Writing PCAP capture to: %s", args.pcap_file)
        _install_shutdown_handler(pcap)

    try:
        asyncio.run(main_async(args, pcap))
    except KeyboardInterrupt:
        logger.info("Shutting down.")
    finally:
        if pcap is not None:
            pcap.close_all_open()  # safe no-op if the signal handler already did this
            pcap.close()


if __name__ == "__main__":
    main()
