"""Static server with HTTP Range support (PMTiles needs byte ranges)."""
import os, re, sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
class H(SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Accept-Ranges", "bytes"); self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store"); super().end_headers()
    def send_head(self):
        rng = self.headers.get("Range"); path = self.translate_path(self.path)
        if not rng or not os.path.isfile(path): return super().send_head()
        size = os.path.getsize(path); a, b = re.match(r"bytes=(\d*)-(\d*)", rng).groups()
        start = int(a) if a else size - int(b); end = int(b) if a and b else size - 1; end = min(end, size - 1)
        f = open(path, "rb"); f.seek(start); self._remaining = end - start + 1
        self.send_response(206); self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}"); self.send_header("Content-Length", str(self._remaining))
        self.end_headers(); return f
    def copyfile(self, src, dst):
        n = getattr(self, "_remaining", None)
        if n is None: return super().copyfile(src, dst)
        while n > 0:
            buf = src.read(min(1 << 16, n))
            if not buf: break
            dst.write(buf); n -= len(buf)
        self._remaining = None
    def log_message(self, *a): pass
os.chdir(sys.argv[1]); ThreadingHTTPServer(("127.0.0.1", int(sys.argv[2])), H).serve_forever()
