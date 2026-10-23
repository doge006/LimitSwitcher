"""The app's loopback HTTP servers.

The standard server looks up the machine's name when it starts (socket.getfqdn), a lookup that
can go out to the local network. These servers only ever listen on 127.0.0.1, so they skip it."""
from http.server import ThreadingHTTPServer
import socketserver
import sys


class LocalServer(ThreadingHTTPServer):
    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]

    def handle_error(self, request, client_address):
        # A client that goes away mid-request (a closed panel, Codex cancelling) is normal.
        if isinstance(sys.exc_info()[1], (ConnectionResetError, BrokenPipeError, TimeoutError)):
            return
        super().handle_error(request, client_address)
