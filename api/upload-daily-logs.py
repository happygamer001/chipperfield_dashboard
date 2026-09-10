from http.server import BaseHTTPRequestHandler
import json
import sys
import os

# Allow importing the shared script logic from /scripts
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "scripts"))
from daily_log_uploader import run_gmail_djl_uploader  # noqa: E402


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            summary = run_gmail_djl_uploader()
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok", "summary": summary}).encode())
        except Exception as e:
            self.send_response(500)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"status": "error", "message": str(e)}).encode())
