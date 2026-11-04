import contextlib
import dataclasses
import hashlib
import hmac
import http.client
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from http.server import ThreadingHTTPServer

from bot import handler, listen_address, Store
from deploy.container_entrypoint import load_private_environment
from test_bot import config


class IngressTests(unittest.TestCase):
    def test_default_is_loopback_and_container_bind_is_explicit(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(listen_address(), ('127.0.0.1',8765))
        with patch.dict(os.environ, {'BIND_HOST':'0.0.0.0','PORT':'8765'},clear=True):
            self.assertEqual(listen_address(),('0.0.0.0',8765))
        for values in ({'BIND_HOST':'example.invalid'},{'PORT':'0'},{'PORT':'65536'}):
            with self.subTest(values=values),patch.dict(os.environ,values,clear=True),self.assertRaises(ValueError):
                listen_address()

    def test_signed_handshake_works_while_disabled_and_health_is_not_ready(self):
        with tempfile.TemporaryDirectory() as temp:
            cfg=dataclasses.replace(config(str(Path(temp)/'events.sqlite3')),enabled=False)
            store=Store(cfg.database)
            server=ThreadingHTTPServer(('127.0.0.1',0),handler(cfg,store,{'authenticated':True,'worker':time.monotonic()}))
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            sentinel='DUMMY_BODY_MUST_NOT_APPEAR_IN_LOGS'
            output=io.StringIO()
            try:
                with contextlib.redirect_stderr(output),contextlib.redirect_stdout(output):
                    def request(method,path,body=b'',signature=True,stale=False):
                        stamp=str(int(time.time())-(301 if stale else 0))
                        headers={'X-Slack-Request-Timestamp':stamp,'X-Slack-Signature':'v0='+hmac.new(cfg.signing_secret.encode(),b'v0:'+stamp.encode()+b':'+body,hashlib.sha256).hexdigest()} if signature else {}
                        connection=http.client.HTTPConnection(*server.server_address)
                        try:
                            connection.request(method,path,body,headers)
                            response=connection.getresponse();return response.status,json.loads(response.read())
                        finally:connection.close()
                    raw=json.dumps({'type':'url_verification','challenge':sentinel}).encode()
                    self.assertEqual(request('POST','/slack/events',raw),(200,{'challenge':sentinel}))
                    self.assertEqual(request('POST','/slack/events',raw,False)[0],401)
                    self.assertEqual(request('POST','/slack/events',raw,stale=True)[0],401)
                    self.assertEqual(request('POST','/other',raw)[0],404)
                    status,health=request('GET','/healthz')
                    self.assertEqual(status,503);self.assertFalse(health['ready'])
                self.assertNotIn(sentinel,output.getvalue())
                self.assertEqual(output.getvalue(),'')
            finally:
                server.shutdown();server.server_close();thread.join()


@unittest.skipUnless(hasattr(os,'getuid') and hasattr(os,'O_NOFOLLOW'),'Linux private-file semantics')
class PrivateRuntimeTests(unittest.TestCase):
    def test_private_file_loads_only_expected_keys_and_rejects_exposure(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'runtime.env'
            values={key:'DUMMY' for key in ('SLACK_BOT_TOKEN','SLACK_SIGNING_SECRET','SLACK_TEAM_ID','SLACK_BOT_USER_ID','SLACK_MEMBER_IDS','BOT_ENABLED','ALLOWLIST_VERIFIED')}
            original=''.join(k+'='+json.dumps(v)+'\n' for k,v in values.items())
            path.write_text(original);path.chmod(0o600)
            self.assertEqual(load_private_environment(path),values)
            path.chmod(0o644)
            with self.assertRaises(ValueError):load_private_environment(path)
            path.chmod(0o600)
            for appended in ('PYTHONPATH="/unreviewed"\n','SLACK_TEAM_ID="OTHER"\n'):
                path.write_text(original+appended)
                with self.assertRaises(ValueError):load_private_environment(path)
            path.write_text(original)
            link=Path(temp)/'link';link.symlink_to(path)
            with self.assertRaises(OSError):load_private_environment(link)


if __name__=='__main__':unittest.main()
