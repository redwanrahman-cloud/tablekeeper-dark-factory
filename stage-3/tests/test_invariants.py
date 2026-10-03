"""Spec-derived HTTP regressions; also runnable against a container."""
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


    def test_integral_JSON_party_value_in_create_and_replay(self):
        body = {**self.booking(), 'party_size': 4.0}
        status, record = self.call('POST', '/reservations', body, 'integral')
        self.assertEqual(status, 201)
        self.assertEqual(record['party_size'], 4)
        self.assertEqual(self.call('POST', '/reservations', {**body, 'party_size': 4}, 'integral'), (200, record))

    def test_integral_JSON_party_value_in_patch(self):
        record = self.create('original')
        status, changed = self.call('PATCH', '/reservations/' + record['reference'], {'party_size': 4.0})
        self.assertEqual(status, 200)
        self.assertEqual(changed['party_size'], 4)
        for invalid in (4.5, True, '4', 0.0):
            status, error = self.call('PATCH', '/reservations/' + record['reference'], {'party_size': invalid})
            self.assertEqual((status, error['error']['code']), (422, 'validation_failed'))
            self.assertEqual(self.call('GET', '/reservations/' + record['reference'])[1], changed)

    def test_integral_JSON_party_value_in_batch(self):
        first, second = self.create('a'), self.create('b', 'b')
        body = {'moves': [{'reference': first['reference'], 'party_size': 4.0},
                          {'reference': second['reference'], 'party_size': 4.0}]}
        status, receipt = self.call('POST', '/reservation-moves', body, 'integral')
        self.assertEqual(status, 201)
        self.assertEqual([r['party_size'] for r in receipt['reservations']], [4, 4])
        self.assertEqual(self.call('POST', '/reservation-moves', body, 'integral'), (200, receipt))

    def combined_fixture(self):
        fixture = copy.deepcopy(self.fixture)
        restaurant = fixture['restaurants'][0]
        restaurant['tables'].append({'id': 'c', 'label': 'Window', 'capacity': 2})
        restaurant['combinable'] = [['b', 'a'], ['b', 'c']]
        return fixture

    def test_opaque_restaurant_identifier_is_decoded_after_route_segmentation(self):
        fixture = copy.deepcopy(self.fixture)
        fixture['restaurants'][0]['id'] = 'venue/dining % room'
        self.load_fixture(fixture)
        status, restaurant = self.call('GET', '/restaurants/venue%2Fdining%20%25%20room', auth=False)
        self.assertEqual(status, 200)
        self.assertEqual(restaurant['id'], 'venue/dining % room')
        status, booking = self.call('POST', '/reservations', {**self.booking(), 'restaurant_id':restaurant['id']}, 'opaque')
        self.assertEqual(status,201)
        self.assertEqual(booking['restaurant_id'],restaurant['id'])

    def test_combination_fixture_member_types_and_failed_reset_are_atomic(self):
        self.load_fixture(self.combined_fixture())
        original = self.create('original')
        before = self.call('GET', '/_test/export', auth=False)[1]
        for member in (False, 1, None, {}, []):
            invalid = self.combined_fixture()
            invalid['restaurants'][0]['combinable'] = [['a', member]]
            status, error = self.call('POST', '/_test/reset', invalid, auth=False)
            self.assertEqual((status,error['error']['code']),(400,'malformed_request'))
            self.assertEqual(self.call('GET', '/_test/export', auth=False)[1],before)
            self.assertEqual(self.call('POST', '/reservations', self.booking(), 'original'),(200,original))
        for pair in (['a'], ['a','b','c'], ['a','a'], ['a','missing'], ['a',''], ['a','x'*65]):
            invalid = self.combined_fixture()
            invalid['restaurants'][0]['combinable'] = [pair]
            self.assertEqual(self.call('POST', '/_test/reset', invalid, auth=False)[0],422)
            self.assertEqual(self.call('GET', '/_test/export', auth=False)[1],before)

    def load_fixture(self, fixture):
        self.assertEqual(self.call('POST', '/_test/reset', fixture, auth=False)[0], 204)
        self.token = self.call('POST', '/auth/login', {'email': 'owner@example.test', 'password': 'long-password'}, auth=False)[1]['token']

    def test_combined_options_order_occupancy_and_cancellation(self):
        self.load_fixture(self.combined_fixture())
        path = '/availability?restaurant_id=venue&date=' + self.day + '&party_size=4'
        slot = self.call('GET', path, auth=False)[1]['slots'][0]
        self.assertEqual(slot['available_table_ids'], ['a', 'b'])
        self.assertEqual(slot['available_options'], [{'table_ids': ['a'], 'capacity': 4}, {'table_ids': ['b'], 'capacity': 4}, {'table_ids': ['b', 'a'], 'capacity': 8}, {'table_ids': ['b', 'c'], 'capacity': 6}])
        body = {k: v for k, v in self.booking().items() if k != 'table_id'}
        body.update(table_ids=['a', 'b'], party_size=6)
        status, record = self.call('POST', '/reservations', body, 'pair')
        self.assertEqual(status, 201)
        self.assertNotIn('table_id', record)
        for table in ('a', 'b'):
            self.assertEqual(self.call('POST', '/reservations', self.booking(table), table)[0], 409)
        self.assertEqual(self.call('POST', '/reservations/' + record['reference'] + '/cancel')[0], 200)
        self.assertEqual(self.call('POST', '/reservations', body, 'pair'), (200, record))
        self.assertEqual(self.call('POST', '/reservations', body, 'pair-new')[0], 201)

    def test_combined_shape_nontransitivity_and_atomic_batch(self):
        self.load_fixture(self.combined_fixture())
        base = {k: v for k, v in self.booking().items() if k != 'table_id'}
        for selection, code, status in [({'table_ids': []}, 'validation_failed', 422), ({'table_ids': ['a','a']}, 'validation_failed', 422), ({'table_ids': ['a','c']}, 'combination_not_allowed', 422), ({'table_ids': ['a','b','c']}, 'combination_not_allowed', 422), ({'table_ids': ['a'], 'table_id': 'a'}, 'validation_failed', 422), ({'table_ids': 'a'}, 'malformed_request', 400), ({'table_ids': [True]}, 'malformed_request', 400), ({'table_ids': ['missing']}, 'not_found', 404)]:
            actual, error = self.call('POST', '/reservations', {**base, **selection}, 'failed')
            self.assertEqual((actual,error['error']['code']),(status,code))
        first, second = self.create('one','a'), self.create('two','b')
        moves = {'moves':[{'reference':first['reference'],'table_ids':['a','b']},{'reference':second['reference'],'table_ids':['c']}]}
        status, receipt = self.call('POST','/reservation-moves',moves,'batch')
        self.assertEqual(status,201)
        self.assertNotIn('table_id',receipt['reservations'][0])
        self.assertEqual(receipt['reservations'][1]['table_id'],'c')
        before = self.call('GET','/reservations')[1]
        collision = {'moves':[{'reference':first['reference'],'table_ids':['b','c']},{'reference':second['reference'],'table_ids':['c']}]}
        self.assertEqual(self.call('POST','/reservation-moves',collision,'reusable')[0],409)
        self.assertEqual(self.call('GET','/reservations')[1],before)
        self.assertEqual(self.call('POST','/reservation-moves',{'moves':[{'reference':first['reference']}]},'reusable')[0],201)
        snapshot = self.call('GET','/_test/export',auth=False)[1]
        self.assertEqual(self.call('POST','/_test/import',snapshot,auth=False)[0],204)
        self.assertEqual(self.call('POST','/reservation-moves',moves,'batch'),(200,receipt))

    def test_seed_cancelled_combination_and_stage_one_snapshot_migration(self):
        fixture = self.combined_fixture()
        fixture['reservations'] = [{**{k:v for k,v in self.booking().items() if k != 'table_id'},'table_ids':['b','a'],'id':'seed','reference':'SEED001','user_id':'owner','status':'cancelled'}]
        self.load_fixture(fixture)
        self.assertEqual(self.call('GET','/reservations/SEED001')[1]['status'],'cancelled')
        self.create('single')
        snapshot = self.call('GET','/_test/export',auth=False)[1]
        # Remove only the fields absent from our accepted Stage 1 snapshot format.
        snapshot['state']['restaurants']['venue'].pop('combinable')
        snapshot['state']['reservations'].pop('SEED001')
        for record in snapshot['state']['reservations'].values(): record.pop('table_ids')
        for receipt in snapshot['state']['receipts']: receipt['response'].pop('table_ids')
        # This helper emulates an actual Stage1 export, whose top-level state
        # has exactly five fields and whose reservations predate terms/revisions.
        for field in ('policies', 'histories', 'series', 'restaurant_revisions'):
            snapshot['state'].pop(field)
        for record in snapshot['state']['reservations'].values():
            record.pop('revision')
            record.pop('accepted_terms')
        for receipt in snapshot['state']['receipts']:
            receipt['response'].pop('revision')
            receipt['response'].pop('accepted_terms')
        original = copy.deepcopy(snapshot['state']['receipts'][0]['response'])
        self.assertEqual(self.call('POST','/_test/import',snapshot,auth=False)[0],204)
        self.assertEqual(self.call('POST','/reservations',self.booking(),'single'),(200,original))
        self.assertEqual(self.call('GET','/reservations/'+original['reference'])[1]['table_ids'],['a'])
        self.assertEqual(self.call('POST','/auth/login',{'email':'owner@example.test','password':'long-password'},auth=False)[0],200)

    def test_concurrent_pairs_and_singles_never_share_occupancy(self):
        self.load_fixture(self.combined_fixture())
        base = {k:v for k,v in self.booking().items() if k != 'table_id'}
        barrier = threading.Barrier(50)
        def request(index):
            barrier.wait()
            return self.call('POST','/reservations',{**base,'table_ids':['a','b'] if index % 2 else ['b','c']},'race-'+str(index))
        with ThreadPoolExecutor(max_workers=50) as pool: results = list(pool.map(request,range(50)))
        self.assertEqual(sum(status == 201 for status,_ in results),1)
        self.assertEqual(sum(status == 409 for status,_ in results),49)

    def test_integer_configuration_uses_JSON_number_value_semantics(self):
        fixture = copy.deepcopy(self.fixture)
        restaurant = fixture['restaurants'][0]
        for key in ('slot_minutes', 'reservation_duration_minutes', 'cancellation_cutoff_minutes'):
            restaurant[key] = float(restaurant[key])
        for table in restaurant['tables']:
            table['capacity'] = float(table['capacity'])
        self.assertEqual(self.call('POST', '/_test/reset', fixture, auth=False)[0], 204)
        snapshot = self.call('GET', '/_test/export', auth=False)[1]
        snapshot['format_version'] = 1.0
        self.assertEqual(self.call('POST', '/_test/import', snapshot, auth=False)[0], 204)
        for value, expected in [(3.5, 422), ('4', 400), (False, 400)]:
            invalid = copy.deepcopy(fixture)
            invalid['restaurants'][0]['tables'][0]['capacity'] = value
            self.assertEqual(self.call('POST', '/_test/reset', invalid, auth=False)[0], expected)
            self.assertEqual(self.call('GET', '/restaurants/venue', auth=False)[1]['tables'][0]['capacity'], 4)

    def managed(self):
        fixture = self.combined_fixture()
        fixture['restaurants'][0]['manager_user_ids'] = ['owner']
        self.load_fixture(fixture)

    def policy(self, day=None, **overrides):
        return {'effective_from': day or self.day, 'slot_minutes': 30,
                'reservation_duration_minutes': 60, 'cancellation_cutoff_minutes': 60,
                'opening_hours': self.fixture['restaurants'][0]['opening_hours'],
                'capacities': {'a': 4, 'b': 4, 'c': 2}, **overrides}

    def history(self, ref):
        status, data = self.call('GET', '/reservations/' + ref + '/history')
        self.assertEqual(status, 200)
        return data['entries']

    def test_policy_effective_order_snapshot_noop_and_truthful_history(self):
        self.managed()
        old = self.create('before')
        path = '/restaurants/venue/policies'
        self.assertEqual(self.call('POST', path, self.policy(), 'policy')[0], 201)
        self.assertEqual(self.call('POST', path, self.policy(), 'policy')[0], 200)
        self.assertEqual(self.call('POST', path, {'effective_from': False}, 'policy')[1]['error']['code'], 'idempotency_key_reuse')
        self.assertEqual(self.call('PATCH', '/reservations/' + old['reference'], {'party_size': 2}), (200, old))
        self.assertEqual(len(self.history(old['reference'])), 1)
        status, changed = self.call('PATCH', '/reservations/' + old['reference'], {'party_size': 3, 'expected_revision': 1})
        self.assertEqual(status, 200)
        self.assertEqual(changed['revision'], 2)
        self.assertEqual(changed['accepted_terms']['policy_version'], 1)
        self.assertTrue(changed['ends_at'].endswith('19:00:00+00:00'))
        entries = self.history(old['reference'])
        self.assertEqual([e['revision'] for e in entries], [1, 2])
        self.assertEqual(entries[0]['accepted_terms']['policy_version'], 0)
        self.assertEqual(entries[1]['changes'], [{'field': 'party_size', 'from': 2, 'to': 3}])
        self.assertEqual(self.call('PATCH', '/reservations/' + old['reference'], {'expected_revision': 1, 'party_size': False})[1]['error']['code'], 'stale_revision')
        later = (datetime.fromisoformat(self.day) + timedelta(days=7)).date().isoformat()
        self.assertEqual(self.call('POST', path, self.policy(later, reservation_duration_minutes=30), 'later')[0], 201)
        self.assertEqual(self.call('POST', path, self.policy(reservation_duration_minutes=120), 'tie')[0], 201)
        record = self.create('after', 'b')
        self.assertEqual(record['accepted_terms']['policy_version'], 3)
        self.assertEqual(self.call('GET', '/restaurants/venue')[1]['reservation_duration_minutes'], 90)
        snapshot = self.call('GET', '/_test/export', auth=False)[1]
        self.assertEqual(self.call('POST', '/_test/import', snapshot, auth=False)[0], 204)
        self.assertEqual(self.history(old['reference']), entries)
        self.assertEqual(self.call('POST', '/reservations', self.booking(), 'before'), (200, old))

    def test_policy_validation_permissions_and_explanation_independence(self):
        self.managed()
        baseline = self.call('GET', '/_test/export', auth=False)[1]
        for override in ({'slot_minutes': True}, {'slot_minutes': '30'}, {'slot_minutes': 1441},
                         {'effective_from': '2026-02-30'}, {'opening_hours': None},
                         {'capacities': {'a': 4}}, {'capacities': {'a': 0, 'b': 4, 'c': 2}}):
            self.assertEqual(self.call('POST', '/restaurants/venue/policies', self.policy(**override), 'reusable')[0], 422)
            self.assertEqual(self.call('GET', '/_test/export', auth=False)[1], baseline)
        self.assertEqual(self.call('POST', '/restaurants/venue/policies', self.policy(capacities={'a':1,'b':4,'c':2}), 'reusable')[0], 201)
        self.assertEqual(self.call('POST','/reservations',{**self.booking(),'party_size':1},'occupied')[0],201)
        query = '/availability?restaurant_id=venue&date=' + self.day + '&party_size=4'
        self.assertNotIn('explain', self.call('GET', query, auth=False)[1]['slots'][0])
        slots = self.call('GET', query + '&explain=true', auth=False)[1]['slots']
        slot = next(s for s in slots if s['starts_at_local'].endswith('18:00'))
        first = slot['explain'][0]
        self.assertEqual(first['rules'], [{'rule':'capacity','holds':False}, {'rule':'no_overlap','holds':False}])
        self.assertEqual([e['table_id'] for e in slot['explain'] if e['available']], slot['available_table_ids'])
        for invalid in ('false', '1', ''):
            self.assertEqual(self.call('GET', query + '&explain=' + invalid, auth=False)[0],422)
        self.assertEqual(self.call('POST', '/restaurants/venue/policies', self.policy(), 'anon', auth=False)[0], 401)
        signup = self.call('POST','/auth/signup',{'email':'other@example.test','password':'long-password','display_name':'Other'},auth=False)[1]
        self.token = signup['token']
        self.assertEqual(self.call('POST', '/restaurants/venue/policies', self.policy(), 'other')[0],403)

    def test_history_privacy_pair_order_revision_race_and_cancel(self):
        self.managed()
        body = {k:v for k,v in self.booking().items() if k != 'table_id'} | {'table_ids':['a','b']}
        status, record = self.call('POST','/reservations',body,'pair')
        self.assertEqual(status,201)
        self.assertEqual(record['table_ids'],['b','a'])
        self.assertEqual(self.history(record['reference'])[0]['changes'][0], {'field':'table_ids','from':None,'to':['b','a']})
        self.assertEqual(self.call('PATCH','/reservations/'+record['reference'],{'table_ids':['a','b']}),(200,record))
        self.assertEqual(self.call('GET','/reservations/'+record['reference']+'/history',auth=False)[0],404)
        self.assertEqual(self.call('GET','/reservations/'+record['reference']+'/decision',auth=False)[0],404)
        barrier = threading.Barrier(2)
        def change(size):
            barrier.wait()
            return self.call('PATCH','/reservations/'+record['reference'],{'expected_revision':1,'party_size':size})
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(change,[3,4]))
        self.assertEqual(sorted(s for s,_ in results),[200,409])
        self.assertEqual(len(self.history(record['reference'])),2)
        cancelled = self.call('POST','/reservations/'+record['reference']+'/cancel')[1]
        self.assertEqual(cancelled['revision'],3)
        self.assertEqual(self.call('POST','/reservations/'+record['reference']+'/cancel'),(200,cancelled))
        self.assertEqual(self.history(record['reference'])[-1]['changes'],[])
        self.assertEqual(self.call('POST','/reservations',body,'pair'),(200,record))
        snapshot = self.call('GET','/_test/export',auth=False)[1]
        self.assertEqual(self.call('POST','/_test/import',snapshot,auth=False)[0],204)

    def test_series_failure_atomicity_policies_exceptions_and_batch_counters(self):
        self.managed()
        anchor = self.create('anchor')
        nextday = (datetime.fromisoformat(self.day) + timedelta(days=7)).date().isoformat()
        obstacle = self.call('POST','/reservations',{**self.booking(),'starts_at_local':nextday+'T18:00'},'obstacle')[1]
        before = self.call('GET','/_test/export',auth=False)[1]
        body = {'anchor_reference':anchor['reference'],'count':3,'interval_weeks':1}
        self.assertEqual(self.call('POST','/series',body,'series')[1]['error']['code'],'table_unavailable')
        self.assertEqual(self.call('GET','/_test/export',auth=False)[1],before)
        self.call('POST','/reservations/'+obstacle['reference']+'/cancel')
        self.assertEqual(self.call('POST','/restaurants/venue/policies',self.policy(nextday),'policy')[0],201)
        status, agreement = self.call('POST','/series',body,'series')
        self.assertEqual(status,201)
        self.assertEqual(agreement['occurrences'][0]['reservation'],anchor)
        self.assertEqual(agreement['occurrences'][1]['reservation']['accepted_terms']['policy_version'],1)
        self.assertEqual(self.call('GET','/series/'+agreement['series_id'],auth=False)[0],404)
        refs = [o['reference'] for o in agreement['occurrences']]
        moves = {'moves':[{'reference':ref,'party_size':3,'expected_revision':1} for ref in refs]}
        counters = self.call('GET','/_test/export',auth=False)[1]['state']['restaurant_revisions']
        status, receipt = self.call('POST','/reservation-moves',moves,'moves')
        self.assertEqual(status,201)
        current = self.call('GET','/series/'+agreement['series_id'])[1]
        self.assertEqual(current['revision'],2)
        self.assertTrue(all(o['exception'] for o in current['occurrences']))
        self.assertEqual(self.call('GET','/_test/export',auth=False)[1]['state']['restaurant_revisions']['venue'],counters['venue']+1)
        self.assertEqual(self.call('POST','/reservation-moves',moves,'moves'),(200,receipt))
        self.assertEqual(self.call('POST','/series',body,'series'),(200,agreement))
        self.assertEqual(self.call('POST','/series',body,'new')[1]['error']['code'],'already_in_series')
        self.call('POST','/reservations/'+refs[0]+'/cancel')
        self.assertEqual(self.call('GET','/series/'+agreement['series_id'])[1]['revision'],3)
        self.assertEqual(self.call('GET','/reservations/'+refs[1])[1]['status'],'confirmed')
        snapshot = self.call('GET','/_test/export',auth=False)[1]
        self.assertEqual(self.call('POST','/_test/import',snapshot,auth=False)[0],204)
        self.assertEqual(self.call('POST','/series',body,'series'),(200,agreement))

    def test_recurring_dst_gap_first_failure_and_cutoff_snapshots(self):
        self.managed()
        fixture = self.combined_fixture()
        fixture['restaurants'][0]['timezone'] = 'Europe/Berlin'
        self.load_fixture(fixture)
        status, anchor = self.call('POST','/reservations',{**self.booking(),'starts_at_local':'2030-03-24T02:30'},'anchor')
        self.assertEqual(status,201)
        before = self.call('GET','/_test/export',auth=False)[1]
        body = {'anchor_reference':anchor['reference'],'count':2,'interval_weeks':1}
        self.assertEqual(self.call('POST','/series',body,'gap')[1]['error']['code'],'invalid_local_time')
        self.assertEqual(self.call('GET','/_test/export',auth=False)[1],before)
        status, fall = self.call('POST','/reservations',{**self.booking(),'starts_at_local':'2030-10-20T02:30'},'fall')
        self.assertEqual(status,201)
        status, made = self.call('POST','/series',{'anchor_reference':fall['reference'],'count':2,'interval_weeks':1},'fold')
        self.assertEqual(status,201)
        self.assertTrue(made['occurrences'][1]['reservation']['starts_at'].endswith('+02:00'))
        self.managed()
        fixture = self.combined_fixture()
        fixture['restaurants'][0]['manager_user_ids'] = ['owner']
        fixture['restaurants'][0]['cancellation_cutoff_minutes'] = 10**9
        self.load_fixture(fixture)
        fixed = self.create('fixed')
        self.assertEqual(self.call('POST','/restaurants/venue/policies',self.policy(cancellation_cutoff_minutes=0),'newcutoff')[0],201)
        self.assertEqual(self.call('POST','/reservations/'+fixed['reference']+'/cancel')[1]['error']['code'],'cutoff_passed')
        self.assertEqual(self.call('PATCH','/reservations/'+fixed['reference'],{'party_size':2})[1]['error']['code'],'cutoff_passed')
        self.assertEqual(self.call('POST','/series',{'anchor_reference':fixed['reference'],'count':2,'interval_weeks':1},'cutoff')[1]['error']['code'],'cutoff_passed')


if __name__ == '__main__':
    unittest.main()
