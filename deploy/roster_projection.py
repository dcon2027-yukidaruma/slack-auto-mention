"""Pure projection for a separately authorized host reader; no auth or network."""


def rows_from_columns(response, *, source_id, row_count):
    if (type(row_count) is not int or not 2 <= row_count <= 5000
            or not isinstance(response, dict)
            or set(response) != {'spreadsheetId', 'valueRanges'}
            or response['spreadsheetId'] != source_id
            or not isinstance(response['valueRanges'], list) or len(response['valueRanges']) != 2):
        raise ValueError('source_schema_invalid')
    expected = {'D': 'メンバーフラグ', 'J': 'Slackのmember ID'}
    columns = {}
    for result in response['valueRanges']:
        if (not isinstance(result, dict) or set(result) != {'range', 'majorDimension', 'values'}
                or result['majorDimension'] != 'COLUMNS'):
            raise ValueError('source_column_invalid')
        matched = [col for col in expected if result['range'] in
                   (f'Sheet1!{col}1:{col}{row_count}', f"'Sheet1'!{col}1:{col}{row_count}")]
        if len(matched) != 1 or matched[0] in columns:
            raise ValueError('source_range_invalid')
        col = matched[0];values = result['values']
        if (not isinstance(values, list) or len(values) != 1 or not isinstance(values[0], list)
                or not 1 <= len(values[0]) <= row_count or values[0][0] != expected[col]):
            raise ValueError('source_header_or_bounds_invalid')
        columns[col] = values[0]
    if set(columns) != set(expected):
        raise ValueError('source_columns_incomplete')
    def cell(col, i):return columns[col][i] if i < len(columns[col]) else ''
    return [[cell('D', i), cell('J', i)] for i in range(1, max(map(len, columns.values())))]
