import dataclasses
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from bot import FreshSheetRoster
from deploy.roster_service import GoogleRequests, dispatch
from test_bot import config


class ServiceBoundaryTests(unittest.TestCase):
    def test_wrong_uid_or_oversized_packet_never_reaches_reader(self):
        class Reader:
            def handle(self, value):raise AssertionError('must not read')
        for raw,peer in [(b'{}\n',43),(b'x'*4097+b'\n',42),(b'{}',42)]:
            self.assertEqual(json.loads(dispatch(Reader(),raw,peer_uid=peer,allowed_uid=42)),{'status':'held'})

    def test_failure_response_suppresses_internal_error_text(self):
        class Reader:
            def handle(self, value):raise ValueError('private authentication response')
        result=dispatch(Reader(),b'{}\n',peer_uid=42,allowed_uid=42)
        self.assertEqual(result,b'{"status":"held"}\n')

    def test_json_and_response_bounds_fail_closed(self):
        class Reader:
            def handle(self, value):return {'oversized':'x'*131073}
        for raw in [b'not-json\n',b'{}\n']:
            self.assertEqual(dispatch(Reader(),raw,peer_uid=42,allowed_uid=42),b'{"status":"held"}\n')

    def test_google_endpoint_and_source_fixed_before_transmission(self):
        google=GoogleRequests('fixture-sheet')
        with patch.object(google,'_http',return_value={}) as request:
            for url in ['https://invalid.example/','https://sheets.googleapis.com/v4/spreadsheets/other?fields=values',
                        google.api+'/values/A1:Z1000']:
                with self.assertRaises(ValueError):google(url,token='fixture')
            with self.assertRaises(ValueError):google.batch('other-sheet',['Sheet1!D1:D10','Sheet1!J1:J10'],token='fixture')
            request.assert_not_called()
            google(google.metadata_url,token='fixture')
            google.batch('fixture-sheet',['Sheet1!D1:D10','Sheet1!J1:J10'],token='fixture')
            self.assertEqual(request.call_count,2)

    def test_redirects_are_not_followed(self):
        from deploy.roster_service import NoRedirect
        self.assertIsNone(NoRedirect().redirect_request(None,None,None,None,None,None))

    def test_google_transport_error_has_only_safe_reason(self):
        with patch('urllib.request.build_opener',side_effect=ValueError('private response')):
            with self.assertRaisesRegex(ValueError,'^google_read_unavailable$'):
                GoogleRequests._http('https://oauth2.googleapis.com/token',data=b'fixture')


@unittest.skipUnless(sys.platform.startswith('linux') and hasattr(socket,'SO_PEERCRED') and os.getuid()!=0,
                     'Linux nonroot Unix peer-credential integration')
class UnixIntegrationTests(unittest.TestCase):
    def test_real_private_socket_two_fresh_reads_and_clean_shutdown(self):
        with tempfile.TemporaryDirectory() as temp:
            directory=Path(temp)/'reader';directory.mkdir(mode=0o700)
            root=Path(__file__).resolve().parent.parent
            code="""
from datetime import datetime,timezone
from pathlib import Path
import os,sys
from deploy.roster_service import serve
class Reader:
 def __init__(self):self.count=0
 def handle(self,value):
  self.count+=1;now=datetime.now(timezone.utc).isoformat()
  return {'schema_version':1,'source_id':value['source_id'],'sheet':'Sheet1',
          'request_id':value['request_id'],'event_id':value['event_id'],
          'read_started_at':now,'read_finished_at':now,
          'rows':[[True,'U%08d'%self.count]]}
serve(Reader(),Path(sys.argv[1]),allowed_uid=os.getuid())
"""
            process=subprocess.Popen([sys.executable,'-B','-c',code,str(directory)],cwd=root,
                                     stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            try:
                deadline=time.monotonic()+10;path=directory/'reader.sock'
                while not path.exists() and time.monotonic()<deadline and process.poll() is None:time.sleep(0.02)
                self.assertTrue(path.exists());self.assertEqual(stat.S_IMODE(path.stat().st_mode),0o600)
                cfg=dataclasses.replace(config('fixture'),roster_mode='fresh_sheet',roster_source_id='fixture-sheet')
                with patch.object(FreshSheetRoster,'PATH',str(path)):
                    reader=FreshSheetRoster(cfg)
                    self.assertEqual(reader('Ev01'),('U00000001',))
                    self.assertEqual(reader('Ev02'),('U00000002',))
            finally:
                process.terminate();out,err=process.communicate(timeout=10)
            self.assertEqual(process.returncode,0)
            self.assertEqual(out,b'');self.assertEqual(err,b'');self.assertFalse(path.exists())
