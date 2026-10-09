import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import time
import unittest
from urllib import request

import execution_budget


class BudgetTests(unittest.TestCase):
    def test_slow_http_body_uses_remaining_budget(self):
        class SlowBody(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header('Content-Length', '3')
                self.end_headers()
                self.wfile.write(b'a')
                self.wfile.flush()
                time.sleep(.6)
                try:
                    self.wfile.write(b'bc')
                except OSError:
                    pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), SlowBody)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            started = time.monotonic()
            with self.assertRaises((TimeoutError, execution_budget.DeadlineExceeded)):
                with execution_budget.until(started + .15):
                    with request.urlopen(f'http://127.0.0.1:{server.server_port}', timeout=1) as response:
                        execution_budget.read_response(response, 100)
            self.assertLess(time.monotonic() - started, .5)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_executor_inherits_deadline_and_cannot_acquire_lock_after_expiry(self):
        lock = threading.Lock()
        lock.acquire()

        def write():
            with execution_budget.locked(lock):
                self.fail('Expired worker must not reach a write')

        async def invoke():
            with execution_budget.until(time.monotonic() + .02):
                await asyncio.to_thread(write)

        try:
            with self.assertRaises(execution_budget.DeadlineExceeded):
                asyncio.run(invoke())
        finally:
            lock.release()
