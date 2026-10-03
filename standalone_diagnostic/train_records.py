"""Turn the SD receive journal into one row per receiver callback."""
import json


def received_rows(report):
    rows = []
    for source in ('events', 'errors'):
        for index, item in enumerate(report.get(source, [])):
            if not isinstance(item, (list, tuple)) or len(item) < 3:
                continue
            seconds, kind, detail = item[:3]
            if not isinstance(kind, str) or not kind.startswith('rx_'):
                continue
            try:
                payload = json.loads(detail) if isinstance(detail, str) else detail
                if not isinstance(payload, dict):
                    raise ValueError('receive payload is not an object')
                parse_error = ''
            except (TypeError, ValueError) as exc:
                payload = {'raw_log_detail': str(detail)}
                parse_error = str(exc)
            basic = payload.get('basic') or {}
            extended = payload.get('extended') or {}
            if not isinstance(basic, dict):
                basic = {}
            if not isinstance(extended, dict):
                extended = {}
            rows.append({
                'seconds': seconds, 'type': payload.get('type') or kind[3:],
                'train_no': basic.get('train_no', payload.get('train_no', '')),
                'speed': basic.get('speed_kmh', ''), 'km_post': basic.get('km_post', ''),
                'loco': extended.get('loco_type', ''), 'rssi': payload.get('rssi', ''),
                'ric': payload.get('ric', ''), 'lat': extended.get('lat', ''),
                'lon': extended.get('lon', ''), 'raw': payload.get('raw', ''),
                'payload': payload, 'parse_error': parse_error,
                '_source': source, '_index': index,
            })
    rows.sort(key=lambda row: (row['seconds'], row['_source'], row['_index']))
    return rows


def has_train_number(row):
    value = str(row.get('train_no', ''))
    return value.isdigit() and 1 <= len(value) <= 8
