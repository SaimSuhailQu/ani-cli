#!/usr/bin/env python3
"""ani-cli iSH referer proxy.

Why this exists: the hianime CDN only serves playlists and segments when the
request carries the embed page as its Referer. Desktop players get that via
--referrer (mpv) / --http-referrer (VLC), but the iOS VLC app is launched
through the vlc:// URL scheme, which cannot set headers at all -- so the CDN
answers 403 and VLC reports it "cannot play" the stream.

This proxy runs on 127.0.0.1 inside iSH. Every request is forwarded with the
Referer (and ani-cli's User-Agent) added, and HLS playlists are rewritten so
that segments, keys and renditions also come back through the proxy. VLC then
streams http://127.0.0.1:<port>/... served by iSH itself.

URL format (base64url, so the shell side needs no quoting/escaping):
    /p/<b64url(target)>/<b64url(referer)>
    /master/<b64url(video)>/<b64url(referer)>/<b64url(sub)|->/<b64url(audio)|->
/master builds a small master playlist that also carries the external
subtitle / audio tracks, which the vlc:// scheme could never attach.

Stdlib only. Usage: python3 ani-cli-proxy.py [port]   (default 18787)
"""

import base64
import re
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urljoin

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def b64d(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4)).decode()


def b64e(s):
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # keep the terminal clean
        pass

    # -- helpers ---------------------------------------------------------
    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    @staticmethod
    def _purl(target, referer):
        return "/p/%s/%s" % (b64e(target), b64e(referer))

    def _fetch(self, url, referer):
        req = urllib.request.Request(
            url, headers={"User-Agent": UA, "Referer": referer}
        )
        rng = self.headers.get("Range")
        if rng:
            req.add_header("Range", rng)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, resp.headers.get("Content-Type", ""), resp.read()
        except urllib.error.HTTPError as exc:  # pass CDN errors through as-is
            ctype = exc.headers.get("Content-Type", "") if exc.headers else ""
            return exc.code, ctype, exc.read()

    # -- routing ---------------------------------------------------------
    def do_GET(self):
        parts = self.path.split("/")
        try:
            if len(parts) >= 2 and parts[1] == "health":
                return self._send(200, "text/plain", b"ok")
            if len(parts) >= 4 and parts[1] == "p":
                return self._proxy(b64d(parts[2]), b64d(parts[3]))
            if len(parts) >= 6 and parts[1] == "master":
                sub = None if parts[4] == "-" else b64d(parts[4])
                audio = None if parts[5] == "-" else b64d(parts[5])
                return self._master(b64d(parts[2]), b64d(parts[3]), sub, audio)
        except Exception as exc:  # never die on one bad request
            return self._send(502, "text/plain", ("proxy error: %s" % exc).encode())
        return self._send(404, "text/plain", b"not found")

    do_HEAD = do_GET

    # -- handlers --------------------------------------------------------
    def _master(self, video, referer, sub, audio):
        lines = ["#EXTM3U", "#EXT-X-VERSION:3"]
        attrs = ""
        if sub:
            lines.append(
                '#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",NAME="Subtitle",'
                'DEFAULT=YES,AUTOSELECT=YES,URI="%s"' % self._purl(sub, referer)
            )
            attrs += ',SUBTITLES="subs"'
        if audio:
            lines.append(
                '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",NAME="Audio",'
                'DEFAULT=YES,AUTOSELECT=YES,URI="%s"' % self._purl(audio, referer)
            )
            attrs += ',AUDIO="aud"'
        lines.append("#EXT-X-STREAM-INF:BANDWIDTH=2000000" + attrs)
        lines.append(self._purl(video, referer))
        body = ("\n".join(lines) + "\n").encode()
        self._send(200, "application/vnd.apple.mpegurl", body)

    def _proxy(self, target, referer):
        status, ctype, body = self._fetch(target, referer)
        bare = target.split("?")[0]
        if bare.endswith(".m3u8") or "mpegurl" in ctype:
            out = []
            for line in body.decode("utf-8", "replace").splitlines():
                line = line.strip()
                if line.startswith("#") and 'URI="' in line:
                    line = re.sub(
                        r'URI="([^"]+)"',
                        lambda m: 'URI="%s"'
                        % self._purl(urljoin(target, m.group(1)), referer),
                        line,
                    )
                    out.append(line)
                elif line and not line.startswith("#"):
                    out.append(self._purl(urljoin(target, line), referer))
                else:
                    out.append(line)
            body = ("\n".join(out) + "\n").encode()
            return self._send(200, "application/vnd.apple.mpegurl", body)
        if not ctype:
            ctype = "video/mp2t" if bare.endswith(".ts") else "application/octet-stream"
        self._send(status, ctype, body)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 18787
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
