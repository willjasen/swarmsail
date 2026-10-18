"""Verify the Swarmsail HTTP health page served to browsers."""

import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.request import urlopen

from swarmsail_web import Handler


class HealthPageTests(unittest.TestCase):
    def test_health_route_serves_a_branded_operational_page(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        with urlopen(f"http://127.0.0.1:{server.server_port}/health", timeout=3) as response:
            page = response.read().decode()
            self.assertEqual(response.status, 200)
            self.assertIn("text/html", response.headers["Content-Type"])
            self.assertIn("<title>Swarmsail · Service status</title>", page)
            self.assertIn("Control plane is healthy", page)
            self.assertIn("Last checked", page)
            self.assertNotIn("__CHECKED_AT__", page)


if __name__ == "__main__":
    unittest.main()
