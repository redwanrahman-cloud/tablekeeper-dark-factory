"""Stage 1 reservation API. No runtime dependencies beyond Python and IANA tzdata."""
import copy
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UTC = timezone.utc
WEEKDAYS = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']
LOCK = threading.RLock()


class ApiError(Exception):
    def __init__(self, status, code):
        self.status, self.code = status, code


def fail(status=422, code='validation_failed'):
    raise ApiError(status, code)


def obj(value):
    if not isinstance(value, dict):
        fail(400, 'malformed_request')
    return value


def string(body, key, required=True, maxlen=None):
    if key not in body:
        if required:
            fail()
        return None
    value = body[key]
    if not isinstance(value, str):
        fail(400, 'malformed_request')
    if not value or (maxlen is not None and len(value) > maxlen):
        fail()
    return value


def ident(body, key):
    return string(body, key, maxlen=64)


def integer(body, key, minimum=1):
    if key not in body:
        fail()
    value = body[key]
    if type(value) is not int:
        fail(400, 'malformed_request')
    if value < minimum:
        fail()
    return value


def party(body):
    value = body.get('party_size')
    if type(value) is not int or value < 1:
        fail()
    return value


def local_input(body):
    value = body.get('starts_at_local')
    if 'starts_at_local' not in body:
        fail()
    if not isinstance(value, str):
        fail(400, 'malformed_request')
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}', value):
        fail()
    try:
        return datetime.strptime(value, '%Y-%m-%dT%H:%M')
    except ValueError:
        fail()


def resolve(local, zone):
    # Round-trip candidates through UTC: skipped wall times fail both folds.
    candidates = []
    for fold in (0, 1):
        aware = local.replace(tzinfo=zone, fold=fold)
        instant = aware.astimezone(UTC)
        if instant.astimezone(zone).replace(tzinfo=None) == local:
            candidates.append(instant)
    if not candidates:
        fail(422, 'invalid_local_time')
    return min(candidates)


def stamp(instant, zone=UTC):
    return instant.astimezone(zone).isoformat(timespec='seconds')


def password_hash(password):
    salt = secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()
    return {'salt': salt, 'digest': digest}


def password_matches(password, stored):
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(stored['salt']), n=16384, r=8, p=1).hex()
    return hmac.compare_digest(digest, stored['digest'])


def empty_state():
    return {'users': {}, 'tokens': {}, 'restaurants': {}, 'reservations': {}, 'receipts': []}


STATE = empty_state()


def same_json(a, b):
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(same_json(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(same_json(x, y) for x, y in zip(a, b))
    return a == b


def json_text(value):
    """Preserve arbitrary JSON numbers in ignored fields and retry receipts."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return '{' + ','.join(json.dumps(k, ensure_ascii=False) + ':' + json_text(v)
                              for k, v in value.items()) + '}'
    if isinstance(value, list):
        return '[' + ','.join(json_text(v) for v in value) + ']'
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def validate_restaurant(raw):
    obj(raw)
    result = {k: string(raw, k, maxlen=64 if k == 'id' else None)
              for k in ('id', 'name', 'timezone')}
    try:
        ZoneInfo(result['timezone'])
    except (ValueError, ZoneInfoNotFoundError):
        fail()
    for key in ('slot_minutes', 'reservation_duration_minutes', 'cancellation_cutoff_minutes'):
        result[key] = integer(raw, key, 0 if key == 'cancellation_cutoff_minutes' else 1)
    result['opening_hours'], result['tables'] = [], []
    for key in ('opening_hours', 'tables'):
        if key not in raw:
            fail()
        if not isinstance(raw[key], list):
            fail(400, 'malformed_request')
    days = set()
    for hours in raw['opening_hours']:
        obj(hours)
        item = {k: string(hours, k) for k in ('weekday', 'opens', 'closes')}
        if item['weekday'] not in WEEKDAYS or item['weekday'] in days:
            fail()
        for key in ('opens', 'closes'):
            if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', item[key]):
                fail()
        if item['closes'] <= item['opens']:
            fail()
        days.add(item['weekday'])
        result['opening_hours'].append(item)
    ids = set()
    for table in raw['tables']:
        obj(table)
        item = {'id': ident(table, 'id'), 'label': string(table, 'label'),
                'capacity': integer(table, 'capacity')}
        if item['id'] in ids:
            fail()
        ids.add(item['id'])
        result['tables'].append(item)
    return result


def restaurant(state, restaurant_id):
    if restaurant_id not in state['restaurants']:
        fail(404, 'not_found')
    return state['restaurants'][restaurant_id]


def proposal(state, body, restaurant_id=None):
    rid = restaurant_id if restaurant_id is not None else ident(body, 'restaurant_id')
    tid = ident(body, 'table_id')
    size = party(body)
    wall = local_input(body)
    r = restaurant(state, rid)
    table = next((t for t in r['tables'] if t['id'] == tid), None)
    if table is None:
        fail(404, 'not_found')
    zone = ZoneInfo(r['timezone'])
    start = resolve(wall, zone)
    try:
        end = start + timedelta(minutes=r['reservation_duration_minutes'])
    except OverflowError:
        fail()
    hours = next((h for h in r['opening_hours'] if h['weekday'] == WEEKDAYS[wall.weekday()]), None)
    if hours is None:
        fail(422, 'outside_opening_hours')
    opens = datetime.combine(wall.date(), datetime.strptime(hours['opens'], '%H:%M').time())
    closes = datetime.combine(wall.date(), datetime.strptime(hours['closes'], '%H:%M').time())
    if wall < opens or wall >= closes or end > resolve(closes, zone):
        fail(422, 'outside_opening_hours')
    minutes = int((wall - opens).total_seconds() // 60)
    if minutes % r['slot_minutes']:
        fail(422, 'not_on_slot_grid')
    if size > table['capacity']:
        fail(422, 'party_exceeds_capacity')
    return {'restaurant_id': rid, 'table_id': tid, 'party_size': size,
            'starts_at_local': wall.strftime('%Y-%m-%dT%H:%M'),
            'starts_at': stamp(start, zone), 'ends_at': stamp(end, zone)}


def overlaps(a, b):
    return (a['restaurant_id'] == b['restaurant_id'] and a['table_id'] == b['table_id']
            and datetime.fromisoformat(a['starts_at']) < datetime.fromisoformat(b['ends_at'])
            and datetime.fromisoformat(b['starts_at']) < datetime.fromisoformat(a['ends_at']))


def check_occupancy(state, proposed, excluded=()):
    occupants = [r for ref, r in state['reservations'].items()
                 if ref not in excluded and r['status'] == 'confirmed']
    for record in proposed:
        if any(overlaps(record, other) for other in occupants):
            fail(409, 'table_unavailable')
        occupants.append(record)


def public(record):
    return {key: value for key, value in record.items() if key != 'user_id'}


def owned(state, ref, uid):
    record = state['reservations'].get(ref)
    if record is None or record['user_id'] != uid:
        fail(404, 'not_found')
    return record


def cutoff(state, record):
    minutes = restaurant(state, record['restaurant_id'])['cancellation_cutoff_minutes']
    if datetime.now(UTC) >= datetime.fromisoformat(record['starts_at']) - timedelta(minutes=minutes):
        fail(409, 'cutoff_passed')


def amended(state, record, changes):
    if record['status'] == 'cancelled':
        fail(409, 'reservation_cancelled')
    cutoff(state, record)
    body = {k: changes.get(k, record[k]) for k in ('table_id', 'party_size', 'starts_at_local')}
    return {**record, **proposal(state, body, record['restaurant_id'])}


def build_fixture(body):
    candidate = empty_state()
    for key in ('users', 'restaurants', 'reservations'):
        if key not in body:
            fail()
        if not isinstance(body[key], list):
            fail(400, 'malformed_request')
    emails = set()
    for raw in body['users']:
        obj(raw)
        uid = ident(raw, 'id')
        email = string(raw, 'email')
        password = string(raw, 'password')
        name = string(raw, 'display_name')
        if uid in candidate['users'] or email in emails:
            fail()
        emails.add(email)
        candidate['users'][uid] = {'id': uid, 'email': email, 'display_name': name,
                                   'password_hash': password_hash(password)}
    for raw in body['restaurants']:
        r = validate_restaurant(raw)
        if r['id'] in candidate['restaurants']:
            fail()
        candidate['restaurants'][r['id']] = r
    for raw in body['reservations']:
        obj(raw)
        rid, ref, uid = ident(raw, 'id'), string(raw, 'reference'), ident(raw, 'user_id')
        if not re.fullmatch(r'[A-Z0-9]{6,12}', ref) or ref in candidate['reservations']:
            fail()
        if uid not in candidate['users'] or any(r['reservation_id'] == rid for r in candidate['reservations'].values()):
            fail()
        record = {**proposal(candidate, raw), 'reservation_id': rid, 'reference': ref,
                  'user_id': uid, 'status': 'confirmed', 'created_at': stamp(datetime.now(UTC))}
        check_occupancy(candidate, [record])
        candidate['reservations'][ref] = record
    return candidate


def validate_import(body):
    # Imports validate a detached candidate before replacing any live data.
    if body.get('track') != 'tablekeeper' or type(body.get('format_version')) is not int or body['format_version'] != 1:
        fail()
    candidate = body.get('state')
    if not isinstance(candidate, dict) or set(candidate) != set(empty_state()):
        fail()
    try:
        for k in ('users', 'tokens', 'restaurants', 'reservations'):
            if not isinstance(candidate[k], dict):
                fail()
        if not isinstance(candidate['receipts'], list):
            fail()
        emails = set()
        for uid, user in candidate['users'].items():
            if ident(user, 'id') != uid or string(user, 'email') in emails:
                fail()
            emails.add(user['email'])
            string(user, 'display_name')
            ph = user['password_hash']
            if set(ph) != {'salt', 'digest'} or not re.fullmatch(r'[0-9a-f]{32}', ph['salt']) or not re.fullmatch(r'[0-9a-f]{128}', ph['digest']):
                fail()
        for token, uid in candidate['tokens'].items():
            if not token or uid not in candidate['users']:
                fail()
        for rid, raw in candidate['restaurants'].items():
            if validate_restaurant(raw) != raw or raw['id'] != rid:
                fail()
        ids = set()
        for ref, record in candidate['reservations'].items():
            if record['reference'] != ref or not re.fullmatch(r'[A-Z0-9]{6,12}', ref):
                fail()
            rid = ident(record, 'reservation_id')
            if rid in ids or record['user_id'] not in candidate['users'] or record['status'] not in ('confirmed', 'cancelled'):
                fail()
            ids.add(rid)
            expected = proposal(candidate, record)
            if any(record.get(k) != v for k, v in expected.items()):
                fail()
            created = datetime.fromisoformat(record['created_at'])
            if created.tzinfo is None:
                fail()
        check_occupancy(empty_state(), [r for r in candidate['reservations'].values() if r['status'] == 'confirmed'])
        keys = set()
        for receipt in candidate['receipts']:
            key = (receipt['user_id'], receipt['method'], receipt['path'], receipt['key'])
            if key in keys or key[0] not in candidate['users'] or key[1] != 'POST' or key[2] not in ('/reservations', '/reservation-moves') or not 1 <= len(key[3]) <= 255:
                fail()
            if not isinstance(receipt['body'], dict) or not isinstance(receipt['response'], dict):
                fail()
            keys.add(key)
    except (ApiError, KeyError, TypeError, ValueError, OverflowError):
        fail()
    return copy.deepcopy(candidate)


def dispatch(method, path, query, body, headers):
    global STATE
    state = STATE
    if method == 'GET' and path == '/health':
        return 200, {'status': 'ok'}
    if method == 'POST' and path == '/_test/reset':
        STATE = build_fixture(body)
        return 204, None
    if method == 'GET' and path == '/_test/export':
        return 200, {'track': 'tablekeeper', 'format_version': 1, 'state': copy.deepcopy(state)}
    if method == 'POST' and path == '/_test/import':
        STATE = validate_import(body)
        return 204, None
    if method == 'POST' and path in ('/auth/login', '/auth/signup'):
        email, password = string(body, 'email'), string(body, 'password')
        if not re.fullmatch(r'[^\s@]+@[^\s@]+', email):
            fail()
        user = next((u for u in state['users'].values() if u['email'] == email), None)
        if path == '/auth/signup':
            name = string(body, 'display_name')
            if len(password) < 8:
                fail()
            if user:
                fail(409, 'email_taken')
            uid = 'u_' + secrets.token_hex(16)
            user = {'id': uid, 'email': email, 'display_name': name, 'password_hash': password_hash(password)}
            state['users'][uid] = user
        elif user is None or not password_matches(password, user['password_hash']):
            fail(401, 'unauthenticated')
        token = secrets.token_urlsafe(32)
        state['tokens'][token] = user['id']
        return (201 if path == '/auth/signup' else 200), {'user_id': user['id'], 'display_name': user['display_name'], 'token': token}
    if method == 'GET' and path == '/restaurants':
        return 200, {'restaurants': [{k: r[k] for k in ('id', 'name', 'timezone')} for r in state['restaurants'].values()]}
    if method == 'GET' and re.fullmatch(r'/restaurants/[^/]+', path):
        return 200, copy.deepcopy(restaurant(state, path.split('/')[2]))
    if method == 'GET' and path == '/availability':
        rid, day, size = (query.get(k, [''])[0] for k in ('restaurant_id', 'date', 'party_size'))
        if not rid or len(rid) > 64 or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', day) or not re.fullmatch(r'[0-9]+', size):
            fail()
        try:
            calendar = date.fromisoformat(day)
            size = int(size)
        except ValueError:
            fail()
        if size < 1:
            fail()
        r = restaurant(state, rid)
        slots = []
        hours = next((h for h in r['opening_hours'] if h['weekday'] == WEEKDAYS[calendar.weekday()]), None)
        if hours:
            zone = ZoneInfo(r['timezone'])
            wall = datetime.combine(calendar, datetime.strptime(hours['opens'], '%H:%M').time())
            closes = datetime.combine(calendar, datetime.strptime(hours['closes'], '%H:%M').time())
            while wall < closes:
                try:
                    start = resolve(wall, zone)
                    end = start + timedelta(minutes=r['reservation_duration_minutes'])
                    if end <= resolve(closes, zone):
                        slot = {'restaurant_id': rid, 'starts_at': stamp(start, zone), 'ends_at': stamp(end, zone)}
                        available = []
                        for table in r['tables']:
                            if table['capacity'] >= size and not any(rec['status'] == 'confirmed' and overlaps({**slot, 'table_id': table['id']}, rec) for rec in state['reservations'].values()):
                                available.append(table['id'])
                        slots.append({'starts_at_local': wall.strftime('%Y-%m-%dT%H:%M'), 'starts_at': slot['starts_at'], 'available_table_ids': available})
                except ApiError as error:
                    if error.code != 'invalid_local_time':
                        raise
                try:
                    wall += timedelta(minutes=r['slot_minutes'])
                except OverflowError:
                    break
        return 200, {'restaurant_id': rid, 'date': day, 'timezone': r['timezone'], 'slots': slots}
    auth = headers.get('Authorization', '')
    match = re.fullmatch(r'Bearer ([^\s]+)', auth, re.IGNORECASE)
    uid = state['tokens'].get(match[1]) if match else None
    if uid is None:
        fail(401, 'unauthenticated')
    receipt_key = None
    if method == 'POST' and path in ('/reservations', '/reservation-moves'):
        key = headers.get('Idempotency-Key', '')
        if not key:
            fail(400, 'missing_idempotency_key')
        if len(key) > 255:
            fail()
        receipt_key = {'user_id': uid, 'method': method, 'path': path, 'key': key}
        for receipt in state['receipts']:
            if all(receipt[k] == v for k, v in receipt_key.items()):
                if not same_json(receipt['body'], body):
                    fail(409, 'idempotency_key_reuse')
                return 200, copy.deepcopy(receipt['response'])
    if method == 'POST' and path == '/reservations':
        record = proposal(state, body)
        check_occupancy(state, [record])
        ref = secrets.token_hex(4).upper()
        while ref in state['reservations']:
            ref = secrets.token_hex(4).upper()
        record.update(reservation_id='res_' + secrets.token_hex(16), reference=ref,
                      user_id=uid, status='confirmed', created_at=stamp(datetime.now(UTC)))
        state['reservations'][ref] = record
        response = public(record)
    elif method == 'POST' and path == '/reservation-moves':
        moves = body.get('moves')
        if not isinstance(moves, list) or not 1 <= len(moves) <= 8:
            fail()
        refs = []
        for move in moves:
            if not isinstance(move, dict) or not isinstance(move.get('reference'), str) or not move['reference'] or move['reference'] in refs:
                fail()
            refs.append(move['reference'])
        proposals, rid = [], None
        for ref, move in zip(refs, moves):
            current = owned(state, ref, uid)
            changed = amended(state, current, move)
            if rid is not None and rid != current['restaurant_id']:
                fail()
            rid = current['restaurant_id']
            proposals.append(changed)
        check_occupancy(state, proposals, refs)
        for record in proposals:
            state['reservations'][record['reference']] = record
        response = {'reservations': [public(r) for r in proposals]}
    elif method == 'GET' and path == '/reservations':
        records = [r for r in state['reservations'].values() if r['user_id'] == uid]
        records.sort(key=lambda r: datetime.fromisoformat(r['starts_at']), reverse=True)
        return 200, {'reservations': [public(r) for r in records]}
    elif re.fullmatch(r'/reservations/[^/]+(?:/cancel)?', path):
        parts = path.split('/')
        record = owned(state, parts[2], uid)
        if method == 'POST' and len(parts) == 4:
            if record['status'] != 'cancelled':
                cutoff(state, record)
                record['status'] = 'cancelled'
            return 200, public(record)
        if method == 'GET' and len(parts) == 3:
            return 200, public(record)
        if method == 'PATCH' and len(parts) == 3:
            changed = amended(state, record, body)
            check_occupancy(state, [changed], [record['reference']])
            state['reservations'][record['reference']] = changed
            return 200, public(changed)
        fail(404, 'not_found')
    else:
        fail(404, 'not_found')
    state['receipts'].append({**receipt_key, 'body': copy.deepcopy(body), 'response': copy.deepcopy(response)})
    return 201, response


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        # Request paths and bodies can contain private data; do not log them.
        pass

    def send_error(self, code, message=None, explain=None):
        # BaseHTTPRequestHandler's protocol errors must use the API envelope too.
        error_code = 'malformed_request'
        if code >= 500:
            code, error_code = 404, 'not_found'
        payload = json.dumps({'error': {'code': error_code,
                                       'message': 'Invalid HTTP request'}}).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def handle_request(self):
        try:
            body = {}
            if self.command in ('POST', 'PATCH', 'PUT'):
                try:
                    length = int(self.headers.get('Content-Length', '0'))
                    if length < 0:
                        fail(400, 'malformed_request')
                    raw = self.rfile.read(length)
                    if raw:
                        body = obj(json.loads(raw, parse_float=Decimal,
                                              parse_constant=lambda _: fail(400, 'malformed_request')))
                    elif self.path.split('?')[0] not in ('/health',) and not self.path.split('?')[0].endswith('/cancel'):
                        fail(400, 'malformed_request')
                except (ValueError, UnicodeError, json.JSONDecodeError):
                    fail(400, 'malformed_request')
            url = urlsplit(self.path)
            with LOCK:
                status, result = dispatch(self.command, unquote(url.path), parse_qs(url.query, keep_blank_values=True), body, self.headers)
                payload = json_text(result).encode() if result is not None else b''
        except ApiError as error:
            status = error.status
            payload = json.dumps({'error': {'code': error.code, 'message': error.code.replace('_', ' ')}}).encode()
        except (ValueError, TypeError, OverflowError, RecursionError):
            status = 422
            payload = b'{"error":{"code":"validation_failed","message":"Invalid value"}}'
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass

    do_GET = do_POST = do_PATCH = do_PUT = do_DELETE = do_OPTIONS = do_HEAD = handle_request


class Server(ThreadingHTTPServer):
    request_queue_size = 128
    daemon_threads = True


if __name__ == '__main__':
    Server(('0.0.0.0', int(os.environ.get('PORT', '8080'))), Handler).serve_forever()
