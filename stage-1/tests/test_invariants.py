"""Independent spec-derived HTTP regressions; also runnable against a container."""
import copy
import json
import os
import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import service


class Invariants(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = None
        cls.base = os.environ.get('TEST_BASE_URL')
        if not cls.base:
            cls.server = service.Server(('127.0.0.1', 0), service.Handler)
            threading.Thread(target=cls.server.serve_forever, daemon=True).start()
            cls.base = 'http://127.0.0.1:' + str(cls.server.server_port)

    @classmethod
    def tearDownClass(cls):
        if cls.server:
            cls.server.shutdown()
            cls.server.server_close()

    def call(self, method, path, body=None, key=None, auth=True):
        headers = {'Content-Type': 'application/json'}
        if auth and hasattr(self, 'token'):
            headers['Authorization'] = 'Bearer ' + self.token
        if key is not None:
            headers['Idempotency-Key'] = key
        req = Request(self.base + path, data=(body if isinstance(body, bytes) else json.dumps(body).encode()) if body is not None else None,
                      method=method, headers=headers)
        try:
            response = urlopen(req, timeout=5)
        except HTTPError as error:
            response = error
        with response:
            data = response.read()
            return response.status, json.loads(data) if data else None

    def setUp(self):
        self.day = (datetime.now(timezone.utc) + timedelta(days=30)).strftime('%Y-%m-%d')
        self.fixture = {'users': [{'id': 'owner', 'email': 'owner@example.test',
                                  'password': 'long-password', 'display_name': 'Owner'}],
                        'restaurants': [{'id': 'venue', 'name': 'Venue', 'timezone': 'UTC',
                                         'slot_minutes': 30, 'reservation_duration_minutes': 90,
                                         'cancellation_cutoff_minutes': 120,
                                         'opening_hours': [{'weekday': day, 'opens': '00:00', 'closes': '23:00'}
                                                           for day in service.WEEKDAYS],
                                         'tables': [{'id': 'a', 'label': 'A', 'capacity': 4},
                                                    {'id': 'b', 'label': 'B', 'capacity': 4}]}],
                        'reservations': []}
        self.assertEqual(self.call('POST', '/_test/reset', self.fixture, auth=False)[0], 204)
        status, login = self.call('POST', '/auth/login', {'email': 'owner@example.test', 'password': 'long-password'}, auth=False)
        self.assertEqual(status, 200)
        self.token = login['token']

    def booking(self, table='a', time='18:00'):
        return {'restaurant_id': 'venue', 'table_id': table,
                'starts_at_local': self.day + 'T' + time, 'party_size': 2}

    def create(self, key, table='a', time='18:00'):
        status, record = self.call('POST', '/reservations', self.booking(table, time), key)
        self.assertEqual(status, 201)
        return record

    def test_fifty_identical_retries_and_competing_bookings(self):
        body = self.booking()
        with ThreadPoolExecutor(max_workers=50) as pool:
            results = list(pool.map(lambda _: self.call('POST', '/reservations', body, 'shared'), range(50)))
        self.assertEqual(sum(status == 201 for status, _ in results), 1)
        self.assertEqual(sum(status == 200 for status, _ in results), 49)
        self.assertTrue(all(record == results[0][1] for _, record in results))
        with ThreadPoolExecutor(max_workers=50) as pool:
            results = list(pool.map(lambda i: self.call('POST', '/reservations', self.booking('b'), str(i)), range(50)))
        self.assertEqual(sum(status == 201 for status, _ in results), 1)
        self.assertEqual(sum(status == 409 for status, _ in results), 49)
        self.assertEqual(len(self.call('GET', '/reservations')[1]['reservations']), 2)

    def test_atomic_swap_failure_and_receipt_survives_import(self):
        first, second = self.create('first'), self.create('second', 'b')
        moves = {'moves': [{'reference': first['reference'], 'table_id': 'b'},
                           {'reference': second['reference'], 'table_id': 'a'}]}
        status, receipt = self.call('POST', '/reservation-moves', moves, 'swap')
        self.assertEqual(status, 201)
        failed = {'moves': [{'reference': first['reference'], 'table_id': 'a'},
                            {'reference': second['reference'], 'party_size': 5}]}
        status, error = self.call('POST', '/reservation-moves', failed, 'failed')
        self.assertEqual((status, error['error']['code']), (422, 'party_exceeds_capacity'))
        self.assertEqual(self.call('GET', '/reservations/' + first['reference'])[1]['table_id'], 'b')
        # Failed keys are reusable; no-op moves preserve their exact records.
        noop = {'moves': [{'reference': first['reference']}]}
        self.assertEqual(self.call('POST', '/reservation-moves', noop, 'failed')[0], 201)
        # Keys have independent namespaces on each idempotent path.
        self.assertEqual(self.call('POST', '/reservation-moves', noop, 'first')[0], 201)
        exported = self.call('GET', '/_test/export', auth=False)[1]
        self.call('POST', '/reservations/' + first['reference'] + '/cancel')
        self.assertEqual(self.call('POST', '/_test/reset', self.fixture, auth=False)[0], 204)
        self.assertEqual(self.call('POST', '/_test/import', exported, auth=False)[0], 204)
        self.assertEqual(self.call('POST', '/reservation-moves', moves, 'swap'), (200, receipt))
        self.assertEqual(self.call('POST', '/reservations', self.booking(), 'first'), (200, first))
        self.assertEqual(self.call('POST', '/_test/import', exported, auth=False)[0], 204)
        broken = copy.deepcopy(exported)
        broken['state']['reservations'][first['reference']]['table_id'] = 'missing'
        self.assertEqual(self.call('POST', '/_test/import', broken, auth=False)[0], 422)
        self.assertEqual(self.call('GET', '/_test/export', auth=False)[1], exported)
        self.assertEqual(self.call('POST', '/auth/login', {'email': 'owner@example.test', 'password': 'long-password'}, auth=False)[0], 200)

    def test_half_open_intervals_and_replay_precedence(self):
        first = self.create('key')
        self.create('adjacent', time='19:30')
        invalid = {**self.booking(), 'party_size': True}
        status, error = self.call('POST', '/reservations', invalid, 'key')
        self.assertEqual((status, error['error']['code']), (409, 'idempotency_key_reuse'))
        self.call('POST', '/reservations/' + first['reference'] + '/cancel')
        self.assertEqual(self.call('POST', '/reservations', self.booking(), 'key'), (200, first))
        reordered = dict(reversed(list(self.booking().items())))
        self.assertEqual(self.call('POST', '/reservations', reordered, 'key'), (200, first))
        status, error = self.call('POST', '/reservations', invalid, 'new')
        self.assertEqual((status, error['error']['code']), (422, 'validation_failed'))
        self.assertEqual(self.call('POST', '/reservations', self.booking(), 'new')[0], 201)

    def test_dst_gap_fold_and_absolute_duration(self):
        for zone, spring, fall, repeated, offset, end in [
            ('Europe/Berlin', '2026-03-29', '2026-10-25', '02:30', '+02:00', '03:00:00+01:00'),
            ('America/New_York', '2026-03-08', '2026-11-01', '01:30', '-04:00', '02:00:00-05:00')]:
            with self.subTest(zone=zone):
                fixture = copy.deepcopy(self.fixture)
                fixture['restaurants'][0]['timezone'] = zone
                self.assertEqual(self.call('POST', '/_test/reset', fixture, auth=False)[0], 204)
                self.token = self.call('POST', '/auth/login', {'email': 'owner@example.test', 'password': 'long-password'}, auth=False)[1]['token']
                slots = self.call('GET', '/availability?restaurant_id=venue&date=' + spring + '&party_size=2', auth=False)[1]['slots']
                self.assertFalse(any(s['starts_at_local'].endswith(('T02:00', 'T02:30')) for s in slots))
                body = {**self.booking(), 'starts_at_local': spring + 'T02:30'}
                status, error = self.call('POST', '/reservations', body, 'gap')
                self.assertEqual((status, error['error']['code']), (422, 'invalid_local_time'))
                body['starts_at_local'] = fall + 'T' + repeated
                status, record = self.call('POST', '/reservations', body, 'fold')
                self.assertEqual(status, 201)
                self.assertTrue(record['starts_at'].endswith(offset))
                self.assertTrue(record['ends_at'].endswith(end))
                slots = self.call('GET', '/availability?restaurant_id=venue&date=' + fall + '&party_size=2', auth=False)[1]['slots']
                self.assertEqual(sum(s['starts_at_local'] == body['starts_at_local'] for s in slots), 1)

    def test_invalid_shapes_types_and_failed_amendment(self):
        record = self.create('original')
        for body, code, status in [({'party_size': False}, 'validation_failed', 422),
                                   ({'table_id': 4}, 'malformed_request', 400),
                                   ({'starts_at_local': self.day + 'T18:00Z'}, 'validation_failed', 422),
                                   ({'starts_at_local': self.day + 'T18:15'}, 'not_on_slot_grid', 422)]:
            actual, error = self.call('PATCH', '/reservations/' + record['reference'], body)
            self.assertEqual((actual, error['error']['code']), (status, code))
            self.assertEqual(self.call('GET', '/reservations/' + record['reference'])[1], record)
        for size in ['1e9', '4.0', '%2B4', '-1']:
            self.assertEqual(self.call('GET', '/availability?restaurant_id=venue&date=' + self.day + '&party_size=' + size)[0], 422)

    def test_cutoff_precedes_changes_and_ownership_is_private(self):
        record = self.create('private')
        exported = self.call('GET', '/_test/export', auth=False)[1]
        # Use a seeded past booking: creation is legal, amendment is cut off.
        fixture = copy.deepcopy(self.fixture)
        fixture['reservations'] = [{**self.booking(), 'starts_at_local': '2020-01-01T18:00',
                                    'id': 'past', 'reference': 'PAST001', 'user_id': 'owner'}]
        self.assertEqual(self.call('POST', '/_test/reset', fixture, auth=False)[0], 204)
        self.token = self.call('POST', '/auth/login', {'email': 'owner@example.test', 'password': 'long-password'}, auth=False)[1]['token']
        status, error = self.call('PATCH', '/reservations/PAST001', {'party_size': False})
        self.assertEqual((status, error['error']['code']), (409, 'cutoff_passed'))
        status, error = self.call('POST', '/reservation-moves', {'moves': [{'reference': 'PAST001', 'party_size': False}]}, 'cutoff')
        self.assertEqual((status, error['error']['code']), (409, 'cutoff_passed'))
        self.assertEqual(self.call('POST', '/_test/import', exported, auth=False)[0], 204)
        status, other = self.call('POST', '/auth/signup', {'email': 'other@example.test', 'password': 'long-password', 'display_name': 'Other'}, auth=False)
        self.assertEqual(status, 201)
        self.token = other['token']
        self.assertEqual(self.call('GET', '/reservations/' + record['reference'])[0], 404)
        self.assertEqual(self.call('POST', '/reservation-moves', {'moves': [{'reference': record['reference']}]}, 'private')[0], 404)
        self.assertEqual(self.call('GET', '/reservations')[1], {'reservations': []})

    def test_ignored_large_numbers_remain_exportable_and_replayable(self):
        body = json.dumps(self.booking())[:-1].encode() + b',"unknown":1e999}'
        status, receipt = self.call('POST', '/reservations', body, 'large')
        self.assertEqual(status, 201)
        with urlopen(self.base + '/_test/export', timeout=5) as response:
            exported = response.read()
        self.assertEqual(self.call('POST', '/_test/reset', self.fixture, auth=False)[0], 204)
        self.assertEqual(self.call('POST', '/_test/import', exported, auth=False)[0], 204)
        self.assertEqual(self.call('POST', '/reservations', body, 'large'), (200, receipt))
        # The opaque state also survives ordinary clients decoding and re-encoding
        # with machine-range JSON numbers, rather than forwarding raw bytes.
        client_snapshot = self.call('GET', '/_test/export', auth=False)[1]
        self.assertEqual(self.call('POST', '/_test/import', client_snapshot, auth=False)[0], 204)
        self.assertEqual(self.call('POST', '/reservations', body, 'large'), (200, receipt))

    def test_deep_ignored_values_do_not_break_atomic_receipts(self):
        body = self.booking()
        body['ignored'] = 0
        for _ in range(600):
            body['ignored'] = [body['ignored']]
        status, receipt = self.call('POST', '/reservations', body, 'deep')
        self.assertEqual(status, 201)
        self.assertEqual(self.call('POST', '/reservations', body, 'deep'), (200, receipt))
        with urlopen(self.base + '/_test/export', timeout=5) as response:
            exported = response.read()
        self.assertEqual(self.call('POST', '/_test/import', exported, auth=False)[0], 204)
        self.assertEqual(self.call('POST', '/reservations', body, 'deep'), (200, receipt))

    def test_early_calendar_years_preserve_four_digit_local_dates(self):
        body = {**self.booking(), 'starts_at_local': '0001-01-15T19:00'}
        status, receipt = self.call('POST', '/reservations', body, 'early-year')
        self.assertEqual(status, 201)
        self.assertEqual(receipt['starts_at_local'], body['starts_at_local'])
        slots = self.call('GET', '/availability?restaurant_id=venue&date=0001-01-15&party_size=2', auth=False)[1]['slots']
        self.assertTrue(slots)
        self.assertTrue(all(slot['starts_at_local'].startswith('0001-01-15T') for slot in slots))
        exported = self.call('GET', '/_test/export', auth=False)[1]
        self.assertEqual(self.call('POST', '/_test/import', exported, auth=False)[0], 204)
        self.assertEqual(self.call('POST', '/reservations', body, 'early-year'), (200, receipt))

    def test_empty_login_password_follows_authentication_rules(self):
        status, error = self.call('POST', '/auth/login', {'email': 'owner@example.test', 'password': ''}, auth=False)
        self.assertEqual((status, error['error']['code']), (401, 'unauthenticated'))
        self.assertEqual(self.call('POST', '/auth/signup', {'email': 'empty@example.test', 'password': '', 'display_name': ''}, auth=False)[0], 422)
        # Signup's minimum is not a restriction on supplied seed passwords.
        fixture = copy.deepcopy(self.fixture)
        fixture['users'][0]['password'] = ''
        self.assertEqual(self.call('POST', '/_test/reset', fixture, auth=False)[0], 204)
        self.assertEqual(self.call('POST', '/auth/login', {'email': 'owner@example.test', 'password': ''}, auth=False)[0], 200)

    def test_empty_display_name_survives_signup_login_and_import(self):
        credentials = {'email': 'unnamed@example.test', 'password': 'long-password', 'display_name': ''}
        status, signup = self.call('POST', '/auth/signup', credentials, auth=False)
        self.assertEqual(status, 201)
        self.assertEqual(signup['display_name'], '')
        self.assertEqual(self.call('POST', '/auth/login', credentials, auth=False)[1]['display_name'], '')
        snapshot = self.call('GET', '/_test/export', auth=False)[1]
        self.assertEqual(self.call('POST', '/_test/reset', self.fixture, auth=False)[0], 204)
        self.assertEqual(self.call('POST', '/_test/import', snapshot, auth=False)[0], 204)
        self.assertEqual(self.call('POST', '/auth/login', credentials, auth=False)[1]['display_name'], '')

    def test_maximum_calendar_date_checks_fit_before_duration_arithmetic(self):
        fixture = copy.deepcopy(self.fixture)
        for hours in fixture['restaurants'][0]['opening_hours']:
            hours.update(opens='23:00', closes='23:30')
        self.assertEqual(self.call('POST', '/_test/reset', fixture, auth=False)[0], 204)
        self.token = self.call('POST', '/auth/login', {'email': 'owner@example.test', 'password': 'long-password'}, auth=False)[1]['token']
        status, availability = self.call('GET', '/availability?restaurant_id=venue&date=9999-12-31&party_size=2', auth=False)
        self.assertEqual(status, 200)
        self.assertEqual(availability['slots'], [])
        body = {**self.booking(), 'starts_at_local': '9999-12-31T23:00'}
        status, error = self.call('POST', '/reservations', body, 'last-day')
        self.assertEqual((status, error['error']['code']), (422, 'outside_opening_hours'))
        self.assertEqual(self.call('GET', '/reservations')[1], {'reservations': []})
        self.assertEqual(self.call('POST', '/reservations', {**body, 'table_id': 'missing'}, 'last-day')[0], 404)
        # Even an enormous duration must be classified as nonfitting, not overflow.
        fixture['restaurants'][0]['reservation_duration_minutes'] = 10 ** 30
        self.assertEqual(self.call('POST', '/_test/reset', fixture, auth=False)[0], 204)
        self.token = self.call('POST', '/auth/login', {'email': 'owner@example.test', 'password': 'long-password'}, auth=False)[1]['token']
        self.assertEqual(self.call('GET', '/availability?restaurant_id=venue&date=9999-12-31&party_size=2', auth=False)[1]['slots'], [])
        self.assertEqual(self.call('POST', '/reservations', body, 'last-day')[1]['error']['code'], 'outside_opening_hours')

    def test_timezone_offsets_at_extreme_calendar_dates(self):
        for zone, day in [('America/New_York', '9999-12-31'), ('Europe/Berlin', '0001-01-01')]:
            with self.subTest(zone=zone):
                fixture = copy.deepcopy(self.fixture)
                fixture['restaurants'][0]['timezone'] = zone
                for hours in fixture['restaurants'][0]['opening_hours']:
                    hours.update(opens='00:00', closes='04:00')
                if zone == 'America/New_York':
                    for hours in fixture['restaurants'][0]['opening_hours']:
                        hours.update(opens='18:00', closes='23:00')
                self.assertEqual(self.call('POST', '/_test/reset', fixture, auth=False)[0], 204)
                self.token = self.call('POST', '/auth/login', {'email': 'owner@example.test', 'password': 'long-password'}, auth=False)[1]['token']
                status, availability = self.call('GET', '/availability?restaurant_id=venue&date=' + day + '&party_size=2', auth=False)
                self.assertEqual(status, 200)
                self.assertTrue(availability['slots'])
                body = {**self.booking(), 'starts_at_local': availability['slots'][0]['starts_at_local']}
                status, record = self.call('POST', '/reservations', body, 'extreme-zone')
                self.assertEqual(status, 201)
                self.assertEqual(record['starts_at_local'], body['starts_at_local'])
                snapshot = self.call('GET', '/_test/export', auth=False)[1]
                self.assertEqual(self.call('POST', '/_test/import', snapshot, auth=False)[0], 204)
                if zone == 'Europe/Berlin':
                    status, error = self.call('POST', '/reservations/' + record['reference'] + '/cancel')
                    self.assertEqual((status, error['error']['code']), (409, 'cutoff_passed'))


if __name__ == '__main__':
    unittest.main()
