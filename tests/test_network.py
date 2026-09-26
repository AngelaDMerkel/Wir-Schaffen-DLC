import http.server
import os
from pathlib import Path
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock
import urllib.error

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import civ5_network as NETWORK


class QuietHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'verified HTTPS')

    def log_message(self, *args):
        pass


@unittest.skipUnless(shutil.which('openssl'), 'OpenSSL CLI required for local certificate fixture')
class CertificateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        root = Path(cls.temporary.name)
        cls.certificate, key = root / 'localhost.pem', root / 'localhost.key'
        config = root / 'openssl.cnf'
        config.write_text('''[req]
distinguished_name = subject
x509_extensions = extensions
prompt = no
[subject]
CN = localhost
[extensions]
subjectAltName = DNS:localhost
basicConstraints = critical,CA:TRUE
keyUsage = critical,digitalSignature,keyEncipherment,keyCertSign
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid:always
''')
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                        '-config', str(config), '-keyout', str(key), '-out', str(cls.certificate)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cls.server = http.server.HTTPServer(('127.0.0.1', 0), QuietHandler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cls.certificate, key)
        cls.server.socket = context.wrap_socket(cls.server.socket, server_side=True)
        cls.thread = threading.Thread(target=cls.server.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        cls.temporary.cleanup()

    def setUp(self):
        root = Path(self.temporary.name)
        environment = mock.patch.dict(os.environ, {
            'SSL_CERT_FILE': str(root / 'missing-ca.pem'),
            'SSL_CERT_DIR': str(root / 'missing-ca-directory'),
            'NO_PROXY': 'localhost,127.0.0.1',
            'no_proxy': 'localhost,127.0.0.1',
        })
        environment.start()
        self.addCleanup(environment.stop)

    def test_bundle_loads_trusted_roots_without_external_certificate_paths(self):
        context = NETWORK.https_context()
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        self.assertGreater(context.cert_store_stats()['x509_ca'], 0)

    def test_https_accepts_a_certificate_trusted_by_the_selected_bundle(self):
        with mock.patch('certifi.where', return_value=str(self.certificate)):
            with NETWORK.urlopen(f'https://localhost:{self.port}/', timeout=3) as response:
                self.assertEqual(response.read(), b'verified HTTPS')

    def test_https_rejects_an_untrusted_certificate(self):
        with self.assertRaises(urllib.error.URLError) as raised:
            NETWORK.urlopen(f'https://localhost:{self.port}/', timeout=3)
        self.assertIsInstance(raised.exception.reason, ssl.SSLCertVerificationError)

    def test_https_rejects_a_trusted_certificate_for_the_wrong_hostname(self):
        with mock.patch('certifi.where', return_value=str(self.certificate)):
            with self.assertRaises(urllib.error.URLError) as raised:
                NETWORK.urlopen(f'https://127.0.0.1:{self.port}/', timeout=3)
        self.assertIsInstance(raised.exception.reason, ssl.SSLCertVerificationError)
