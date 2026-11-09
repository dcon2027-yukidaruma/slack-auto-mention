"""Fixed two-column reader for a separately approved local Unix service.

Credentials are supplied by an already-authorized host reader in memory. This
module never creates, copies, stores or discovers credentials, or starts a service.
"""
from datetime import datetime, timezone
import re
import threading
import urllib.parse

from deploy.roster_projection import rows_from_columns


class FreshRosterReader:
    def __init__(self, sheet_reader, batch_get, *, source_id, clock=None):
        self.reader, self.batch_get, self.source_id = sheet_reader, batch_get, source_id
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.lock = threading.Lock()

    def handle(self, envelope):
        expected = {'schema_version', 'operation', 'source_id', 'sheet', 'request_id', 'event_id'}
        if (not isinstance(envelope, dict) or set(envelope) != expected
                or type(envelope['schema_version']) is not int or envelope['schema_version'] != 1
                or envelope['operation'] != 'fresh_members' or envelope['source_id'] != self.source_id
                or envelope['sheet'] != 'Sheet1'
                or not isinstance(envelope['request_id'], str) or not re.fullmatch('[a-f0-9]{32}', envelope['request_id'])
                or not isinstance(envelope['event_id'], str) or not re.fullmatch('[A-Za-z0-9_-]{1,100}', envelope['event_id'])):
            raise ValueError('reader_request_invalid')
        # One serialized actual read per request, with no roster result cache.
        with self.lock:
            started = self.clock()
            meta = self.reader.metadata()
            if meta.get('spreadsheetId') != self.source_id:
                raise ValueError('reader_source_mismatch')
            tabs = [v.get('properties', {}) for v in meta.get('sheets', [])
                    if v.get('properties', {}).get('sheetId') == 0]
            if (len(tabs) != 1 or tabs[0].get('title') != 'Sheet1' or tabs[0].get('hidden')):
                raise ValueError('reader_tab_mismatch')
            grid = tabs[0].get('gridProperties', {})
            rows, columns = grid.get('rowCount'), grid.get('columnCount')
            if type(rows) is not int or not 2 <= rows <= 5000 or type(columns) is not int or columns < 10:
                raise ValueError('reader_grid_invalid')
            ranges = [f'Sheet1!D1:D{rows}', f'Sheet1!J1:J{rows}']
            # One Google batchGet response supplies both columns, preserving row alignment.
            result = self.batch_get(self.source_id, ranges, token=self.reader.token())
            projection = rows_from_columns(result, source_id=self.source_id, row_count=rows)
            finished = self.clock()
            if (started.tzinfo is None or finished.tzinfo is None
                    or not 0 <= (finished-started).total_seconds() <= 15):
                raise ValueError('reader_time_invalid')
            return {'schema_version': 1, 'source_id': self.source_id, 'sheet': 'Sheet1',
                    'request_id': envelope['request_id'], 'event_id': envelope['event_id'],
                    'read_started_at': started.isoformat(), 'read_finished_at': finished.isoformat(),
                    'rows': projection}


def batch_url(source_id, ranges):
    """Construct only the fixed reviewed endpoint; caller binds the configured source."""
    if not isinstance(source_id, str) or not re.fullmatch('[A-Za-z0-9_-]{10,100}', source_id):
        raise ValueError('reader_source_invalid')
    if (not isinstance(ranges, list) or len(ranges) != 2
            or not re.fullmatch(r'Sheet1!D1:D[1-9][0-9]{0,3}', ranges[0])
            or ranges[1] != ranges[0].replace('D', 'J') or int(ranges[0].rsplit('D', 1)[1]) > 5000):
        raise ValueError('reader_range_invalid')
    query = urllib.parse.urlencode({'ranges': ranges, 'majorDimension': 'COLUMNS',
                                   'valueRenderOption': 'UNFORMATTED_VALUE',
                                   'fields': 'spreadsheetId,valueRanges(range,majorDimension,values)'}, doseq=True)
    return 'https://sheets.googleapis.com/v4/spreadsheets/'+source_id+'/values:batchGet?'+query
