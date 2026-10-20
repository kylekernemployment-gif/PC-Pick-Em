"""Placeholder for the old Render web service after the bot moved to the
`pc-bot` background worker. Answers health checks; does NOT run the bot,
so two copies of the bot never run at once. Safe to delete the service."""

import os
from http.server import BaseHTTPRequestHandler, HTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"PC Bot moved to the pc-bot background worker. This service can be deleted.")

    do_HEAD = do_GET

    def log_message(self, *args):
        pass


HTTPServer(("0.0.0.0", int(os.environ.get("PORT", "10000"))), Handler).serve_forever()
