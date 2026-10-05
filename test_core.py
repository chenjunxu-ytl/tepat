import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import server


class CoreRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.previous_checker = server._checker
        self.previous_words = server._words
        self.previous_error = server._grammar_error
        self.previous_lookup = server.prpm_lookup
        server._checker = None
        server._words = set()
        server._grammar_error = None
        server.prpm_lookup = lambda word: {
            "word": word,
            "status": "hit",
            "definition": "fixture",
            "from_cache": True,
        }
        self.service = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.thread = threading.Thread(target=self.service.serve_forever, daemon=True)
        self.thread.start()
        self.origin = f"http://127.0.0.1:{self.service.server_port}"

    def tearDown(self):
        self.service.shutdown()
        self.service.server_close()
        self.thread.join()
        server._checker = self.previous_checker
        server._words = self.previous_words
        server._grammar_error = self.previous_error
        server.prpm_lookup = self.previous_lookup

    def get(self, path):
        with urllib.request.urlopen(self.origin + path) as response:
            return response.status, json.load(response)

    def post(self, path, data):
        req = urllib.request.Request(
            self.origin + path,
            json.dumps(data).encode(),
            {"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req) as response:
            return response.status, json.load(response)

    def test_health_reports_prpm_when_grammar_is_not_installed(self):
        status, body = self.get("/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertTrue(body["capabilities"]["prpm"]["available"])
        self.assertFalse(body["capabilities"]["grammar"]["installed"])
        self.assertEqual(body["capabilities"]["grammar"]["status"], "not_installed")

    def test_prpm_remains_available_without_grammar(self):
        status, body = self.post("/api/prpm", {"word": "buku"})
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "hit")
        self.assertEqual(body["word"], "buku")

    def test_scan_reports_optional_module_as_missing(self):
        req = urllib.request.Request(
            self.origin + "/api/scan",
            json.dumps({"text": "Ali ialah doktor."}).encode(),
            {"Content-Type": "application/json"},
        )
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(req)
        self.assertEqual(error.exception.code, 409)
        body = json.load(error.exception)
        self.assertEqual(body["error"], "grammar_module_not_installed")
        self.assertEqual(body["module"], "grammar")


if __name__ == "__main__":
    unittest.main()
