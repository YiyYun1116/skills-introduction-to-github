import base64
import os
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from app.main import app


class AccessTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.secret='test-only-password-0123456789'
        self.env=patch.dict(os.environ,{'SIGNAL_ATLAS_ENV':'production','SIGNAL_ATLAS_ACCESS_USER':'research',
                           'SIGNAL_ATLAS_ACCESS_PASSWORD':self.secret,'SIGNAL_ATLAS_SYNC_MINUTES':'0',
                           'SIGNAL_ATLAS_DB':self.temp.name+'/db.sqlite3'})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_website_and_private_data_require_password_but_health_is_public(self):
        with TestClient(app) as client:
            self.assertEqual(client.get('/api/health').status_code,200)
            for path in ['/','/api/dashboard','/api/watchlist','/api/export','/static/app.js','/docs']:
                response=client.get(path)
                self.assertEqual(response.status_code,401,path)
                self.assertIn('Basic',response.headers['WWW-Authenticate'])
                self.assertEqual(response.headers['Cache-Control'],'no-store')
            self.assertEqual(client.post('/api/ingest',json={}).status_code,401)

    def test_correct_password_supports_browser_reads_and_same_origin_writes(self):
        with TestClient(app) as client:
            auth=('research',self.secret)
            self.assertEqual(client.get('/',auth=auth).status_code,200)
            self.assertEqual(client.get('/api/dashboard?mode=demo',auth=auth).json()['mode'],'demo')
            result=client.post('/api/watchlist',auth=auth,json={'symbol':'AAPL','name':'Apple','asset_class':'equity','region':'US'},headers={'Origin':'http://testserver'})
            self.assertEqual(result.status_code,201)
            self.assertEqual(len(client.get('/api/watchlist',auth=auth).json()['items']),1)

    def test_bad_and_malformed_credentials_never_grant_access(self):
        with TestClient(app) as client:
            for header in ['Bearer anything','Basic !!!','Basic '+base64.b64encode(b'\xff:bad').decode(),'Basic '+base64.b64encode(b'research').decode()]:
                self.assertEqual(client.get('/api/watchlist',headers={'Authorization':header}).status_code,401)
            self.assertEqual(client.get('/api/watchlist',auth=('research','wrong')).status_code,401)

    def test_password_does_not_bypass_cross_origin_protection(self):
        with TestClient(app) as client:
            response=client.post('/api/ingest',auth=('research',self.secret),json={},headers={'Origin':'https://other.example'})
            self.assertEqual(response.status_code,403)

    def test_production_cannot_start_with_missing_or_short_password(self):
        for password in ['', 'short']:
            with patch.dict(os.environ,{'SIGNAL_ATLAS_ACCESS_PASSWORD':password}):
                with self.assertRaises(ValueError):
                    with TestClient(app):
                        pass

    def test_local_workflow_is_preserved(self):
        with patch.dict(os.environ,{'SIGNAL_ATLAS_ENV':'local','SIGNAL_ATLAS_ACCESS_PASSWORD':''}):
            with TestClient(app) as client:
                self.assertEqual(client.get('/api/dashboard').status_code,200)
