"""Tablekeeper reservation API and locally bundled browser product."""
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
from pathlib import Path
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


def string(body, key, required=True, maxlen=None, nonempty=False):
    if key not in body:
        if required:
            fail()
        return None
    value = body[key]
    if not isinstance(value, str):
        fail(400, 'malformed_request')
    if (nonempty and not value) or (maxlen is not None and len(value) > maxlen):
        fail()
    return value


def ident(body, key):
    return string(body, key, maxlen=64, nonempty=True)


def integer(body, key, minimum=1):
    if key not in body:
        fail()
    value = body[key]
    if type(value) is not int and not isinstance(value, Decimal):
        fail(400, 'malformed_request')
    if not integral_number(value) or value < minimum:
        fail()
    return int(value)


def integral_number(value):
    return type(value) is int or (isinstance(value, Decimal) and value.is_finite()
                                 and value == value.to_integral_value())


def party(body):
    value = body.get('party_size')
    if not integral_number(value) or value < 1:
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
        try:
            instant = aware.astimezone(UTC)
            valid = instant.astimezone(zone).replace(tzinfo=None) == local
        except OverflowError:
            # A valid local boundary date can lie outside datetime's UTC range.
            # Gregorian weekday/leap patterns repeat after 400 years; ZoneInfo's
            # ancient fixed offsets and extrapolated future rules remain the same.
            shifted = local.replace(year=local.year + (400 if local.year < 400 else -400))
            probe = shifted.replace(tzinfo=zone, fold=fold)
            valid = (probe.utcoffset() == aware.utcoffset() and
                     probe.astimezone(UTC).astimezone(zone).replace(tzinfo=None) == shifted)
            instant = aware
        if valid:
            candidates.append(instant)
    if not candidates:
        fail(422, 'invalid_local_time')
    return min(candidates, key=absolute_seconds)


def absolute_seconds(instant):
    # Arithmetic on naive ordinals does not need a representable UTC datetime.
    return ((instant.replace(tzinfo=None) - datetime(1970, 1, 1)).total_seconds()
            - instant.utcoffset().total_seconds())


def duration_end(start, minutes, zone):
    if start.tzinfo is UTC:
        try:
            return start + timedelta(minutes=minutes)
        except OverflowError:
            pass
    local = start.astimezone(zone)
    shift = 400 if local.year < 400 else -400
    shifted = local.replace(year=local.year + shift)
    end = (shifted.astimezone(UTC) + timedelta(minutes=minutes)).astimezone(zone)
    return end.replace(year=end.year - shift)


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
    return {'users': {}, 'tokens': {}, 'restaurants': {}, 'reservations': {}, 'receipts': [],
            'policies': {}, 'histories': {}, 'series': {}, 'restaurant_revisions': {}}


STATE = empty_state()


def same_json(a, b):
    pending = [(a, b)]
    while pending:
        left, right = pending.pop()
        if isinstance(left, bool) or isinstance(right, bool):
            if type(left) is not type(right) or left != right:
                return False
        elif isinstance(left, dict) and isinstance(right, dict):
            if left.keys() != right.keys():
                return False
            pending.extend((left[k], right[k]) for k in left)
        elif isinstance(left, list) and isinstance(right, list):
            if len(left) != len(right):
                return False
            pending.extend(zip(left, right))
        elif isinstance(left, (dict, list)) or isinstance(right, (dict, list)) or left != right:
            return False
    return True


def json_copy(value):
    """Copy parsed JSON without depending on Python's call-stack depth."""
    if not isinstance(value, (dict, list)):
        return value
    result = {} if isinstance(value, dict) else []
    pending = [(value, result)]
    while pending:
        source, target = pending.pop()
        entries = source.items() if isinstance(source, dict) else enumerate(source)
        for key, child in entries:
            cloned = {} if isinstance(child, dict) else [] if isinstance(child, list) else child
            if isinstance(target, dict):
                target[key] = cloned
            else:
                target.append(cloned)
            if isinstance(child, (dict, list)):
                pending.append((child, cloned))
    return result


def json_text(value):
    """Preserve arbitrary JSON numbers in ignored fields and retry receipts."""
    output, pending = [], [(False, value)]
    while pending:
        literal, current = pending.pop()
        if literal:
            output.append(current)
        elif isinstance(current, (dict, list)):
            mapping = isinstance(current, dict)
            output.append('{' if mapping else '[')
            pending.append((True, '}' if mapping else ']'))
            entries = list(current.items()) if mapping else list(enumerate(current))
            for index in range(len(entries) - 1, -1, -1):
                key, child = entries[index]
                pending.append((False, child))
                if mapping:
                    pending.append((True, json.dumps(key, ensure_ascii=False) + ':'))
                if index:
                    pending.append((True, ','))
        else:
            output.append(str(current) if isinstance(current, Decimal) else
                          json.dumps(current, ensure_ascii=False, allow_nan=False))
    return ''.join(output)


def validate_restaurant(raw):
    obj(raw)
    result = {k: string(raw, k, maxlen=64 if k == 'id' else None, nonempty=k == 'id')
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
    pairs = raw.get('combinable', [])
    if not isinstance(pairs, list):
        fail(400, 'malformed_request')
    result['combinable'] = []
    seen = set()
    for pair in pairs:
        if not isinstance(pair, list):
            fail(400, 'malformed_request')
        if len(pair) != 2:
            fail()
        # Fixture members have the same JSON string/type and ID range rules
        # as reservation selections. Cardinality is a separate value rule.
        pair = [ident({'id': member}, 'id') for member in pair]
        if len(set(pair)) != 2 or any(t not in ids for t in pair) or frozenset(pair) in seen:
            fail()
        seen.add(frozenset(pair))
        result['combinable'].append(list(pair))
    managers = raw.get('manager_user_ids', [])
    if not isinstance(managers, list):
        fail(400, 'malformed_request')
    result['manager_user_ids'] = [ident({'id': uid}, 'id') for uid in managers]
    if len(set(result['manager_user_ids'])) != len(managers):
        fail()
    return result


def restaurant(state, restaurant_id):
    if restaurant_id not in state['restaurants']:
        fail(404, 'not_found')
    return state['restaurants'][restaurant_id]


def table_selection(body):
    if 'table_id' in body and 'table_ids' in body:
        fail()
    if 'table_id' in body:
        return [ident(body, 'table_id')]
    if 'table_ids' not in body:
        fail()
    tids = body['table_ids']
    if not isinstance(tids, list):
        fail(400, 'malformed_request')
    if not tids:
        fail()
    for tid in tids:
        ident({'id': tid}, 'id')
    if len(set(tids)) != len(tids):
        fail()
    if len(tids) > 2:
        fail(422, 'combination_not_allowed')
    return list(tids)


def table_fields(tids):
    return {'table_ids': list(tids), **({'table_id': tids[0]} if len(tids) == 1 else {})}


def members(record):
    return record['table_ids'] if 'table_ids' in record else [record['table_id']]


def fixture_terms(r):
    return {'policy_version': 0,
            **{k: json_copy(r[k]) for k in ('slot_minutes', 'reservation_duration_minutes',
                                          'cancellation_cutoff_minutes', 'opening_hours')},
            'capacities': {t['id']: t['capacity'] for t in r['tables']}}


def selected_terms(state, r, day):
    eligible = [p for p in state['policies'].get(r['id'], []) if p['effective_from'] <= day]
    if not eligible:
        return fixture_terms(r)
    selected = max(eligible, key=lambda p: (p['effective_from'], p['policy_version']))
    return json_copy({k: v for k, v in selected.items() if k != 'effective_from'})


def bounded_integer(body, key, minimum, maximum):
    value = body.get(key)
    if not integral_number(value) or not minimum <= value <= maximum:
        fail()
    return int(value)


def policy_input(body, r):
    # The policy contract explicitly classifies every invalid policy as422.
    try:
        day = string(body, 'effective_from')
        if not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', day):
            fail()
        date.fromisoformat(day)
        result = {'effective_from': day,
                  'slot_minutes': bounded_integer(body, 'slot_minutes', 1, 1440),
                  'reservation_duration_minutes': bounded_integer(body, 'reservation_duration_minutes', 1, 1440),
                  'cancellation_cutoff_minutes': bounded_integer(body, 'cancellation_cutoff_minutes', 0, 10080)}
        capacities = body.get('capacities')
        if not isinstance(capacities, dict) or set(capacities) != {t['id'] for t in r['tables']}:
            fail()
        result['capacities'] = {tid: bounded_integer(capacities, tid, 1, 100) for tid in capacities}
        configured = validate_restaurant({**r, **result, 'opening_hours': body.get('opening_hours')})
        result['opening_hours'] = configured['opening_hours']
        return result
    except (ApiError, ValueError, TypeError, OverflowError):
        fail()


def canonical_tables(r, tids):
    if len(tids) == 2:
        return list(next((pair for pair in r['combinable'] if set(pair) == set(tids)), tids))
    return list(tids)


def proposal(state, body, restaurant_id=None, terms=None):
    rid = restaurant_id if restaurant_id is not None else ident(body, 'restaurant_id')
    tids = table_selection(body)
    size = party(body)
    wall = local_input(body)
    r = restaurant(state, rid)
    tables = {t['id']: t for t in r['tables']}
    if any(tid not in tables for tid in tids):
        fail(404, 'not_found')
    if len(tids) == 2 and not any(set(tids) == set(pair) for pair in r['combinable']):
        fail(422, 'combination_not_allowed')
    tids = canonical_tables(r, tids)
    terms = json_copy(terms) if terms is not None else selected_terms(state, r, wall.date().isoformat())
    zone = ZoneInfo(r['timezone'])
    start = resolve(wall, zone)
    hours = next((h for h in terms['opening_hours'] if h['weekday'] == WEEKDAYS[wall.weekday()]), None)
    if hours is None:
        fail(422, 'outside_opening_hours')
    opens = datetime.combine(wall.date(), datetime.strptime(hours['opens'], '%H:%M').time())
    closes = datetime.combine(wall.date(), datetime.strptime(hours['closes'], '%H:%M').time())
    # Compare available absolute time before adding duration. This also handles
    # valid dates near datetime.max and durations too large for timedelta.
    remaining_seconds = absolute_seconds(resolve(closes, zone)) - absolute_seconds(start)
    if wall < opens or wall >= closes or terms['reservation_duration_minutes'] * 60 > remaining_seconds:
        fail(422, 'outside_opening_hours')
    minutes = int((wall - opens).total_seconds() // 60)
    if minutes % terms['slot_minutes']:
        fail(422, 'not_on_slot_grid')
    if size > sum(terms['capacities'][tid] for tid in tids):
        fail(422, 'party_exceeds_capacity')
    end = duration_end(start, terms['reservation_duration_minutes'], zone)
    return {'restaurant_id': rid, **table_fields(tids), 'party_size': int(size),
            'starts_at_local': wall.isoformat(timespec='minutes'),
            'starts_at': stamp(start, zone), 'ends_at': stamp(end, zone), 'accepted_terms': terms}


def overlaps(a, b):
    return (a['restaurant_id'] == b['restaurant_id'] and bool(set(members(a)) & set(members(b)))
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
    minutes = record['accepted_terms']['cancellation_cutoff_minutes']
    if absolute_seconds(datetime.fromisoformat(record['starts_at'])) - absolute_seconds(datetime.now(UTC)) <= minutes * 60:
        fail(409, 'cutoff_passed')


def amended(state, record, changes):
    if 'expected_revision' in changes:
        value = changes['expected_revision']
        if not integral_number(value) or value < 1:
            fail()
        if value != record['revision']:
            fail(409, 'stale_revision')
    if record['status'] == 'cancelled':
        fail(409, 'reservation_cancelled')
    cutoff(state, record)
    body = {k: changes.get(k, record[k]) for k in ('party_size', 'starts_at_local')}
    if 'table_id' in changes or 'table_ids' in changes:
        body.update({k: changes[k] for k in ('table_id', 'table_ids') if k in changes})
    else:
        body['table_ids'] = members(record)
    tids = table_selection(body)
    size, wall = party(body), local_input(body)
    if (set(tids) == set(members(record)) and size == record['party_size']
            and wall.isoformat(timespec='minutes') == record['starts_at_local']):
        return json_copy(record)
    changed = {k: v for k, v in record.items() if k not in ('table_id', 'table_ids')}
    return {**changed, **proposal(state, body, record['restaurant_id']), 'revision': record['revision'] + 1}


def history_changes(before, after):
    changes = []
    if before is None or set(members(before)) != set(members(after)):
        pair = len(members(after)) == 2 or (before is not None and len(members(before)) == 2)
        changes.append({'field': 'table_ids' if pair else 'table_id',
                        'from': None if before is None else (list(members(before)) if pair else members(before)[0]),
                        'to': list(members(after)) if pair else members(after)[0]})
    for field in ('starts_at_local', 'party_size'):
        if before is None or before[field] != after[field]:
            changes.append({'field': field, 'from': None if before is None else before[field], 'to': after[field]})
    return changes


def history_event(state, record, event, before=None, at=None):
    entries = state['histories'].setdefault(record['reference'], [])
    instant = at or stamp(datetime.now(UTC))
    if entries and datetime.fromisoformat(instant) < datetime.fromisoformat(entries[-1]['at']):
        instant = entries[-1]['at']
    entries.append({'seq': len(entries) + 1, 'at': instant, 'event': event,
                    'changes': [] if event == 'cancelled' else history_changes(before, record),
                    'revision': record['revision'], 'accepted_terms': json_copy(record['accepted_terms'])})


def bump_restaurant(state, rid):
    state['restaurant_revisions'][rid] += 1


def update_series(state, records, exception):
    refs = {r['reference'] for r in records}
    for series in state['series'].values():
        affected = [o for o in series['occurrences'] if o['reference'] in refs]
        if affected:
            series['revision'] += 1
            if exception:
                for occurrence in affected:
                    occurrence['exception'] = True


def new_reservation(state, proposed, uid, reserved=()):
    ref = secrets.token_hex(4).upper()
    while ref in state['reservations'] or ref in reserved:
        ref = secrets.token_hex(4).upper()
    return {**proposed, 'reservation_id': 'res_' + secrets.token_hex(16), 'reference': ref,
            'user_id': uid, 'status': 'confirmed', 'created_at': stamp(datetime.now(UTC)), 'revision': 1}


def series_response(state, agreement):
    return {k: json_copy(agreement[k]) for k in ('series_id', 'revision', 'interval_weeks')} | {
        'occurrences': [{**o, 'reservation': public(state['reservations'][o['reference']])}
                        for o in agreement['occurrences']]}


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
        candidate['policies'][r['id']] = []
        candidate['restaurant_revisions'][r['id']] = 0
    for raw in body['reservations']:
        obj(raw)
        rid, ref, uid = ident(raw, 'id'), string(raw, 'reference'), ident(raw, 'user_id')
        if not re.fullmatch(r'[A-Z0-9]{6,12}', ref) or ref in candidate['reservations']:
            fail()
        if uid not in candidate['users'] or any(r['reservation_id'] == rid for r in candidate['reservations'].values()):
            fail()
        status = raw.get('status', 'confirmed')
        if not isinstance(status, str):
            fail(400, 'malformed_request')
        if status not in ('confirmed', 'cancelled'):
            fail()
        record = {**proposal(candidate, raw), 'reservation_id': rid, 'reference': ref,
                  'user_id': uid, 'status': status, 'created_at': stamp(datetime.now(UTC)), 'revision': 1}
        if status == 'confirmed':
            check_occupancy(candidate, [record])
        candidate['reservations'][ref] = record
        history_event(candidate, record, 'created', at=record['created_at'])
    return candidate


def validate_terms(state, r, terms):
    if not isinstance(terms, dict):
        fail()
    version = bounded_integer(terms, 'policy_version', 0, len(state['policies'][r['id']]))
    expected = (fixture_terms(r) if version == 0 else
                {k: v for k, v in state['policies'][r['id']][version - 1].items() if k != 'effective_from'})
    if not same_json(terms, expected):
        fail()


def validate_histories(state):
    if set(state['histories']) != set(state['reservations']):
        fail()
    for ref, record in state['reservations'].items():
        entries = state['histories'][ref]
        if not isinstance(entries, list) or not entries:
            fail()
        previous_at, values, previous_terms = None, {}, None
        for seq, entry in enumerate(entries, 1):
            if (not isinstance(entry, dict) or entry.get('seq') != seq or entry.get('revision') != seq
                    or not integral_number(entry.get('seq')) or not integral_number(entry.get('revision'))):
                fail()
            at = datetime.fromisoformat(entry['at'])
            if at.tzinfo is None or (previous_at is not None and at < previous_at):
                fail()
            previous_at = at
            validate_terms(state, restaurant(state, record['restaurant_id']), entry['accepted_terms'])
            event, changes = entry['event'], entry['changes']
            if not isinstance(changes, list):
                fail()
            if seq == 1:
                if event != 'created' or len(changes) != 3:
                    fail()
            elif event not in ('changed', 'cancelled') or entries[seq - 2]['event'] == 'cancelled':
                fail()
            if event == 'cancelled':
                if changes or entry['accepted_terms'] != previous_terms or seq != len(entries) or record['status'] != 'cancelled':
                    fail()
            else:
                if not changes:
                    fail()
                fields = [c['field'] for c in changes]
                order = {'table_id': 0, 'table_ids': 0, 'starts_at_local': 1, 'party_size': 2}
                if any(f not in order for f in fields) or len(set(order[f] for f in fields)) != len(fields) or fields != sorted(fields, key=order.get):
                    fail()
                for change in changes:
                    field = change['field']
                    old = values.get('table_ids') if field == 'table_ids' else values.get('table_ids', [None])[0] if field == 'table_id' else values.get(field)
                    if change['from'] != old or (seq != 1 and change['from'] == change['to']):
                        fail()
                    if field in ('table_id', 'table_ids'):
                        values['table_ids'] = [change['to']] if field == 'table_id' else change['to']
                    else:
                        values[field] = change['to']
            previous_terms = entry['accepted_terms']
        if (len(entries) != record['revision'] or previous_terms != record['accepted_terms']
                or values != {'table_ids': members(record), 'starts_at_local': record['starts_at_local'], 'party_size': record['party_size']}):
            fail()


def validate_import(body):
    # Imports validate a detached candidate before replacing any live data.
    if body.get('track') != 'tablekeeper' or not integral_number(body.get('format_version')) or body['format_version'] != 1:
        fail()
    candidate = json_copy(body.get('state'))
    legacy_keys = {'users', 'tokens', 'restaurants', 'reservations', 'receipts'}
    if not isinstance(candidate, dict) or set(candidate) not in (legacy_keys, set(empty_state())):
        fail()
    legacy = set(candidate) == legacy_keys
    if legacy:
        candidate.update(policies={}, histories={}, series={}, restaurant_revisions={})
    try:
        for k in ('users', 'tokens', 'restaurants', 'reservations', 'policies', 'histories', 'series', 'restaurant_revisions'):
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
            # Stage 1 snapshots have no declared combinations.
            raw.setdefault('combinable', [])
            raw.setdefault('manager_user_ids', [])
            if validate_restaurant(raw) != raw or raw['id'] != rid:
                fail()
            if legacy:
                candidate['policies'][rid] = []
                candidate['restaurant_revisions'][rid] = 0
            publications = candidate['policies'][rid]
            if not isinstance(publications, list):
                fail()
            for version, policy in enumerate(publications, 1):
                if (not integral_number(policy.get('policy_version')) or policy['policy_version'] != version
                        or not same_json(policy, policy_input(policy, raw) | {'policy_version': version})):
                    fail()
            revision = candidate['restaurant_revisions'][rid]
            if not integral_number(revision) or revision < 0:
                fail()
        if set(candidate['policies']) != set(candidate['restaurants']) or set(candidate['restaurant_revisions']) != set(candidate['restaurants']):
            fail()
        ids = set()
        for ref, record in candidate['reservations'].items():
            if record['reference'] != ref or not re.fullmatch(r'[A-Z0-9]{6,12}', ref):
                fail()
            rid = ident(record, 'reservation_id')
            if rid in ids or record['user_id'] not in candidate['users'] or record['status'] not in ('confirmed', 'cancelled'):
                fail()
            ids.add(rid)
            r = restaurant(candidate, record['restaurant_id'])
            if legacy:
                record['revision'] = 1
                record['accepted_terms'] = fixture_terms(r)
            if not integral_number(record.get('revision')) or record['revision'] < 1:
                fail()
            validate_terms(candidate, r, record['accepted_terms'])
            selection = {k: v for k, v in record.items() if k != 'table_id' or 'table_ids' not in record}
            expected = proposal(candidate, selection, terms=record['accepted_terms'])
            if 'table_ids' in record and ('table_id' in record) != (len(record['table_ids']) == 1):
                fail()
            if 'table_ids' not in record:
                record.update(table_fields([record['table_id']]))
            if legacy:
                record.update(table_fields(canonical_tables(r, members(record))))
            if any(record.get(k) != v for k, v in expected.items()):
                fail()
            created = datetime.fromisoformat(record['created_at'])
            if created.tzinfo is None:
                fail()
            if legacy:
                history_event(candidate, record, 'created', at=record['created_at'])
        validate_histories(candidate)
        check_occupancy(empty_state(), [r for r in candidate['reservations'].values() if r['status'] == 'confirmed'])
        adopted = set()
        for sid, agreement in candidate['series'].items():
            if ident(agreement, 'series_id') != sid or agreement['user_id'] not in candidate['users']:
                fail()
            if not integral_number(agreement['revision']) or agreement['revision'] < 1:
                fail()
            bounded_integer(agreement, 'interval_weeks', 1, 4)
            occurrences = agreement['occurrences']
            if not isinstance(occurrences, list) or not 2 <= len(occurrences) <= 12:
                fail()
            rid = None
            for index, occurrence in enumerate(occurrences):
                ref = occurrence['reference']
                record = owned(candidate, ref, agreement['user_id'])
                if (not integral_number(occurrence['index']) or occurrence['index'] != index
                        or type(occurrence['exception']) is not bool or ref in adopted
                        or (rid is not None and rid != record['restaurant_id'])):
                    fail()
                rid = record['restaurant_id']
                adopted.add(ref)
        keys = set()
        for receipt in candidate['receipts']:
            if isinstance(receipt.get('body'), str):
                receipt['body'] = obj(json.loads(receipt['body'], parse_float=Decimal,
                                                parse_constant=lambda _: fail()))
            key = (receipt['user_id'], receipt['method'], receipt['path'], receipt['key'])
            policy_path = re.fullmatch(r'/restaurants/[^/]+/policies', key[2])
            if key in keys or key[0] not in candidate['users'] or key[1] != 'POST' or (key[2] not in ('/reservations', '/reservation-moves', '/series') and not policy_path) or not 1 <= len(key[3]) <= 255:
                fail()
            if not isinstance(receipt['body'], dict) or not isinstance(receipt['response'], dict):
                fail()
            keys.add(key)
    except (ApiError, KeyError, TypeError, ValueError, OverflowError):
        fail()
    return json_copy(candidate)


def dispatch(method, path, query, body, headers):
    global STATE
    state = STATE
    if method == 'GET' and path == '/health':
        return 200, {'status': 'ok'}
    if method == 'POST' and path == '/_test/reset':
        STATE = build_fixture(body)
        return 204, None
    if method == 'GET' and path == '/_test/export':
        snapshot = json_copy(state)
        # Opaque receipt bodies are JSON strings in the portable snapshot, so client
        # numeric ranges cannot silently round or overflow their request values.
        for receipt in snapshot['receipts']:
            receipt['body'] = json_text(receipt['body'])
        return 200, {'track': 'tablekeeper', 'format_version': 1, 'state': snapshot}
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
        return 200, json_copy(restaurant(state, unquote(path.split('/')[2])))
    if method == 'GET' and re.fullmatch(r'/restaurants/[^/]+/policies', path):
        rid = unquote(path.split('/')[2])
        restaurant(state, rid)
        return 200, {'policies': json_copy(state['policies'][rid])}
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
        if 'explain' in query and query['explain'] != ['true']:
            fail()
        r = restaurant(state, rid)
        terms = selected_terms(state, r, day)
        slots = []
        hours = next((h for h in terms['opening_hours'] if h['weekday'] == WEEKDAYS[calendar.weekday()]), None)
        if hours:
            zone = ZoneInfo(r['timezone'])
            wall = datetime.combine(calendar, datetime.strptime(hours['opens'], '%H:%M').time())
            closes = datetime.combine(calendar, datetime.strptime(hours['closes'], '%H:%M').time())
            while wall < closes:
                try:
                    start = resolve(wall, zone)
                    remaining_seconds = absolute_seconds(resolve(closes, zone)) - absolute_seconds(start)
                    if terms['reservation_duration_minutes'] * 60 <= remaining_seconds:
                        end = duration_end(start, terms['reservation_duration_minutes'], zone)
                        slot = {'restaurant_id': rid, 'starts_at': stamp(start, zone), 'ends_at': stamp(end, zone)}
                        available, options = [], []
                        capacities = terms['capacities']
                        for tids in [[t['id']] for t in r['tables']] + r['combinable']:
                            capacity = sum(capacities[t] for t in tids)
                            if capacity >= size and not any(rec['status'] == 'confirmed' and overlaps({**slot, 'table_ids': tids}, rec) for rec in state['reservations'].values()):
                                options.append({'table_ids': list(tids), 'capacity': capacity})
                                if len(tids) == 1:
                                    available.append(tids[0])
                        result = {'starts_at_local': wall.isoformat(timespec='minutes'), 'starts_at': slot['starts_at'], 'available_table_ids': available, 'available_options': options}
                        if 'explain' in query:
                            explanations = []
                            for table in r['tables']:
                                fits = capacities[table['id']] >= size
                                free = not any(rec['status'] == 'confirmed' and overlaps({**slot, 'table_ids': [table['id']]}, rec) for rec in state['reservations'].values())
                                explanations.append({'table_id': table['id'], 'policy_version': terms['policy_version'], 'available': fits and free,
                                                     'rules': [{'rule': 'capacity', 'holds': fits}, {'rule': 'no_overlap', 'holds': free}]})
                            result['explain'] = explanations
                        slots.append(result)
                except ApiError as error:
                    if error.code != 'invalid_local_time':
                        raise
                try:
                    wall += timedelta(minutes=terms['slot_minutes'])
                except OverflowError:
                    break
        return 200, {'restaurant_id': rid, 'date': day, 'timezone': r['timezone'], 'slots': slots}
    auth = headers.get('Authorization', '')
    match = re.fullmatch(r'Bearer ([^\s]+)', auth, re.IGNORECASE)
    uid = state['tokens'].get(match[1]) if match else None
    if method == 'GET' and re.fullmatch(r'/reservations/[^/]+/(?:history|decision)', path):
        record = owned(state, unquote(path.split('/')[2]), uid)
        if path.endswith('/history'):
            return 200, {'reference': record['reference'], 'entries': json_copy(state['histories'][record['reference']])}
        return 200, {'reference': record['reference'], 'revision': record['revision'], 'accepted_terms': json_copy(record['accepted_terms'])}
    if method == 'GET' and re.fullmatch(r'/series/[^/]+', path):
        agreement = state['series'].get(unquote(path.split('/')[2]))
        if agreement is None or agreement['user_id'] != uid:
            fail(404, 'not_found')
        return 200, series_response(state, agreement)
    if uid is None:
        fail(401, 'unauthenticated')
    receipt_key = None
    policy_write = method == 'POST' and re.fullmatch(r'/restaurants/[^/]+/policies', path)
    if method == 'POST' and (path in ('/reservations', '/reservation-moves', '/series') or policy_write):
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
                return 200, json_copy(receipt['response'])
        # Prepare request-owned receipt data before a write can change occupancy.
        receipt_body = json_copy(body)
    if policy_write:
        rid = unquote(path.split('/')[2])
        r = restaurant(state, rid)
        if uid not in r['manager_user_ids']:
            fail(403, 'forbidden')
        response = policy_input(body, r)
        response['policy_version'] = len(state['policies'][rid]) + 1
        state['policies'][rid].append(json_copy(response))
        bump_restaurant(state, rid)
    elif method == 'POST' and path == '/reservations':
        record = proposal(state, body)
        check_occupancy(state, [record])
        record = new_reservation(state, record, uid)
        state['reservations'][record['reference']] = record
        history_event(state, record, 'created', at=record['created_at'])
        bump_restaurant(state, record['restaurant_id'])
        response = public(record)
    elif method == 'POST' and path == '/series':
        anchor_ref = string(body, 'anchor_reference', nonempty=True)
        count = bounded_integer(body, 'count', 2, 12)
        interval = bounded_integer(body, 'interval_weeks', 1, 4)
        anchor = owned(state, anchor_ref, uid)
        if anchor['status'] == 'cancelled':
            fail(409, 'reservation_cancelled')
        if any(o['reference'] == anchor_ref for s in state['series'].values() for o in s['occurrences']):
            fail(409, 'already_in_series')
        cutoff(state, anchor)
        local = local_input(anchor)
        proposals = []
        for index in range(1, count):
            try:
                wall = local + timedelta(days=index * interval * 7)
            except OverflowError:
                fail()
            proposed = proposal(state, {'table_ids': members(anchor), 'party_size': anchor['party_size'],
                                        'starts_at_local': wall.isoformat(timespec='minutes')}, anchor['restaurant_id'])
            # Resolve first failure in occurrence order, including occupancy.
            check_occupancy(state, [*proposals, proposed])
            proposals.append(proposed)
        records = []
        for proposed in proposals:
            records.append(new_reservation(state, proposed, uid, [r['reference'] for r in records]))
        sid = 'series_' + secrets.token_hex(16)
        agreement = {'series_id': sid, 'revision': 1, 'interval_weeks': interval, 'user_id': uid,
                     'occurrences': [{'index': i, 'reference': r['reference'], 'exception': False}
                                     for i, r in enumerate([anchor, *records])]}
        for record in records:
            state['reservations'][record['reference']] = record
            history_event(state, record, 'created', at=record['created_at'])
        state['series'][sid] = agreement
        bump_restaurant(state, anchor['restaurant_id'])
        response = series_response(state, agreement)
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
        changed_records = [r for r in proposals if r['revision'] != state['reservations'][r['reference']]['revision']]
        for record in changed_records:
            history_event(state, record, 'changed', state['reservations'][record['reference']])
        for record in proposals:
            state['reservations'][record['reference']] = record
        if changed_records:
            bump_restaurant(state, rid)
            update_series(state, changed_records, True)
        response = {'reservations': [public(r) for r in proposals]}
    elif method == 'GET' and path == '/reservations':
        records = [r for r in state['reservations'].values() if r['user_id'] == uid]
        records.sort(key=lambda r: datetime.fromisoformat(r['starts_at']), reverse=True)
        return 200, {'reservations': [public(r) for r in records]}
    elif re.fullmatch(r'/reservations/[^/]+(?:/cancel)?', path):
        parts = path.split('/')
        record = owned(state, unquote(parts[2]), uid)
        if method == 'POST' and len(parts) == 4:
            if record['status'] != 'cancelled':
                cutoff(state, record)
                record['status'] = 'cancelled'
                record['revision'] += 1
                history_event(state, record, 'cancelled')
                bump_restaurant(state, record['restaurant_id'])
                update_series(state, [record], False)
            return 200, public(record)
        if method == 'GET' and len(parts) == 3:
            return 200, public(record)
        if method == 'PATCH' and len(parts) == 3:
            changed = amended(state, record, body)
            check_occupancy(state, [changed], [record['reference']])
            if changed['revision'] != record['revision']:
                history_event(state, changed, 'changed', record)
                bump_restaurant(state, record['restaurant_id'])
                update_series(state, [changed], True)
            state['reservations'][record['reference']] = changed
            return 200, public(changed)
        fail(404, 'not_found')
    else:
        fail(404, 'not_found')
    state['receipts'].append({**receipt_key, 'body': receipt_body, 'response': json_copy(response)})
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
        asset = {'/': 'index.html', '/signup': 'index.html', '/login': 'index.html',
                 '/lookup': 'index.html', '/assets/app.js': 'app.js',
                 '/assets/style.css': 'style.css'}.get(urlsplit(self.path).path)
        if self.command == 'GET' and asset:
            payload = (Path(__file__).parent / 'web' / asset).read_bytes()
            content_type = ('text/html' if asset.endswith('.html') else
                            'text/css' if asset.endswith('.css') else 'text/javascript')
            self.send_response(200)
            self.send_header('Content-Type', content_type + '; charset=utf-8')
            self.send_header('Content-Length', str(len(payload)))
            self.send_header('Cache-Control', 'no-cache')
            self.end_headers()
            try:
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
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
                # Segment first, decode opaque IDs afterwards: an encoded slash
                # is part of an identifier, not a new route component.
                status, result = dispatch(self.command, url.path, parse_qs(url.query, keep_blank_values=True), body, self.headers)
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
