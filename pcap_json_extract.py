#!/usr/bin/env python3
"""
Extract and pretty-print JSON HTTP messages straight out of a pcap file,
without relying on Wireshark's dissection/reassembly settings.

Reads raw pcap packets (supports LINKTYPE_RAW/101, as produced by
lms_cli_proxy.py's --pcap-file, and LINKTYPE_ETHERNET/1), groups them into
TCP streams, reconstructs each direction's byte stream in sequence order,
then walks that stream as a sequence of HTTP messages (handling
Content-Length, chunked, and streaming/no-length bodies) and prints each
one's JSON body, pretty-printed.

Usage:
    python3 pcap_json_extract.py capture.pcap
    python3 pcap_json_extract.py capture.pcap --grep Dylan
    python3 pcap_json_extract.py capture.pcap --port 9000
"""

import argparse
import json
import re
import socket
import struct
import sys

_CL_RE = re.compile(rb"(?im)^content-length:\s*(\d+)\s*$")
_CHUNKED_RE = re.compile(rb"(?im)^transfer-encoding:\s*chunked\s*$")


def read_pcap_packets(path):
    """Yields (src_ip, src_port, dst_ip, dst_port, seq, payload) for every TCP packet."""
    with open(path, "rb") as f:
        global_header = f.read(24)
        if len(global_header) < 24:
            raise ValueError("Not a valid pcap file (truncated global header)")
        magic = global_header[:4]
        if magic == b"\xa1\xb2\xc3\xd4":
            endian = ">"
        elif magic == b"\xd4\xc3\xb2\xa1":
            endian = "<"
        else:
            raise ValueError(f"Unrecognized pcap magic number: {magic!r} (not a classic pcap file?)")
        (_, _, _, _, _, _, linktype) = struct.unpack(endian + "IHHiIII", global_header)

        while True:
            rec_header = f.read(16)
            if len(rec_header) < 16:
                break
            ts_sec, ts_usec, incl_len, orig_len = struct.unpack(endian + "IIII", rec_header)
            data = f.read(incl_len)
            if len(data) < incl_len:
                break

            if linktype == 101:  # LINKTYPE_RAW
                ip_packet = data
            elif linktype == 1:  # LINKTYPE_ETHERNET
                if len(data) < 14:
                    continue
                ip_packet = data[14:]
            else:
                continue  # unsupported link type, skip

            if len(ip_packet) < 20:
                continue
            ver_ihl = ip_packet[0]
            version = ver_ihl >> 4
            if version != 4:
                continue  # only IPv4 supported
            ihl = (ver_ihl & 0x0F) * 4
            proto = ip_packet[9]
            if proto != 6:
                continue  # only TCP
            src_ip = socket.inet_ntoa(ip_packet[12:16])
            dst_ip = socket.inet_ntoa(ip_packet[16:20])
            tcp_packet = ip_packet[ihl:]
            if len(tcp_packet) < 20:
                continue
            src_port, dst_port, seq = struct.unpack("!HHI", tcp_packet[:8])
            data_offset = (tcp_packet[12] >> 4) * 4
            payload = tcp_packet[data_offset:]

            yield src_ip, src_port, dst_ip, dst_port, seq, payload


def group_into_streams(packets):
    """Groups packets by 5-tuple conversation, each direction reconstructed in seq order."""
    streams = {}  # key -> {"a": (ip,port), "b": (ip,port), "a_to_b": [(seq,payload)], "b_to_a": [...]}
    for src_ip, src_port, dst_ip, dst_port, seq, payload in packets:
        endpoint_a = (src_ip, src_port)
        endpoint_b = (dst_ip, dst_port)
        key = tuple(sorted([endpoint_a, endpoint_b]))
        st = streams.setdefault(key, {"a": key[0], "b": key[1], "a_to_b": [], "b_to_a": []})
        if (src_ip, src_port) == st["a"]:
            st["a_to_b"].append((seq, payload))
        else:
            st["b_to_a"].append((seq, payload))

    reconstructed = {}
    for key, st in streams.items():
        def assemble(segs):
            segs = sorted(segs, key=lambda s: s[0])
            out = bytearray()
            for seq, payload in segs:
                if payload:
                    out += payload
            return bytes(out)
        reconstructed[key] = {
            "a": st["a"], "b": st["b"],
            "a_to_b": assemble(st["a_to_b"]),
            "b_to_a": assemble(st["b_to_a"]),
        }
    return reconstructed


def walk_http_messages(data: bytes):
    """Yields (first_line: bytes, body: bytes or None) for each HTTP message in data."""
    pos = 0
    n = len(data)
    while pos < n:
        sep = data.find(b"\r\n\r\n", pos)
        if sep == -1:
            return
        header_block = data[pos:sep]
        body_start = sep + 4
        first_line = header_block.split(b"\r\n", 1)[0]

        cl_match = _CL_RE.search(header_block)
        chunked = _CHUNKED_RE.search(header_block) is not None

        if cl_match:
            length = int(cl_match.group(1))
            body = data[body_start:body_start + length]
            yield first_line, body
            pos = body_start + length
        elif chunked:
            # reassemble chunk-by-chunk until the terminating 0-chunk
            body = bytearray()
            p = body_start
            terminated = False
            while p < n:
                line_end = data.find(b"\r\n", p)
                if line_end == -1:
                    break
                try:
                    size = int(data[p:line_end].split(b";")[0], 16)
                except ValueError:
                    break
                p = line_end + 2
                if size == 0:
                    terminated = True
                    p += 2  # trailing \r\n after the 0-size line
                    break
                body += data[p:p + size]
                p += size + 2
            yield first_line, bytes(body)
            pos = p
            if not terminated:
                return
        else:
            yield first_line, None
            pos = body_start


def try_pretty_json(body: bytes):
    """LMS's CometD stream often concatenates multiple JSON documents
    back-to-back in one body (e.g. "[...][...]") with no separator, which
    json.loads() rejects as a single document. Parse and pretty-print each
    concatenated document separately instead."""
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return None

    decoder = json.JSONDecoder()
    docs = []
    pos = 0
    n = len(text)
    while pos < n:
        while pos < n and text[pos].isspace():
            pos += 1
        if pos >= n:
            break
        try:
            obj, end = decoder.raw_decode(text, pos)
        except json.JSONDecodeError:
            return None  # not valid JSON (or not fully valid) - caller falls back to raw text
        docs.append(obj)
        pos = end

    if not docs:
        return None
    return "\n".join(json.dumps(d, indent=2, ensure_ascii=False) for d in docs)


def main():
    p = argparse.ArgumentParser(description="Extract and pretty-print JSON HTTP bodies from a pcap")
    p.add_argument("pcap_file")
    p.add_argument("--grep", default=None, help="Only print messages whose body contains this text (case-insensitive)")
    p.add_argument("--port", type=int, default=None, help="Only consider streams involving this TCP port")
    args = p.parse_args()

    packets = list(read_pcap_packets(args.pcap_file))
    if not packets:
        print("No TCP packets found (or unsupported pcap format).", file=sys.stderr)
        sys.exit(1)

    streams = group_into_streams(packets)

    grep_lower = args.grep.lower().encode() if args.grep else None
    found_any = False

    for key, st in streams.items():
        if args.port is not None and args.port not in (st["a"][1], st["b"][1]):
            continue

        for direction_label, endpoint_from, endpoint_to, data in (
            (f'{st["a"][0]}:{st["a"][1]} -> {st["b"][0]}:{st["b"][1]}', st["a"], st["b"], st["a_to_b"]),
            (f'{st["b"][0]}:{st["b"][1]} -> {st["a"][0]}:{st["a"][1]}', st["b"], st["a"], st["b_to_a"]),
        ):
            if not data:
                continue
            for first_line, body in walk_http_messages(data):
                if body is None:
                    continue
                if grep_lower is not None and grep_lower not in body.lower():
                    continue
                pretty = try_pretty_json(body)
                found_any = True
                print("=" * 100)
                print(f"{direction_label}   {first_line.decode('latin-1', errors='replace')}")
                print("-" * 100)
                if pretty is not None:
                    print(pretty)
                else:
                    try:
                        print(body.decode("utf-8"))
                    except UnicodeDecodeError:
                        print(f"<binary body, {len(body)} bytes, not shown>")
                print()

    if not found_any:
        if args.grep:
            print(f"No JSON message bodies containing {args.grep!r} were found.", file=sys.stderr)
        else:
            print("No JSON message bodies were found in this capture.", file=sys.stderr)


if __name__ == "__main__":
    main()
