"""HTTP regressions for operator repairs and recurring amendments."""
import copy
import itertools
import random
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import test_invariants as helpers


class Stage4(unittest.TestCase):
    setUpClass = classmethod(helpers.Invariants.setUpClass.__func__)
    tearDownClass = classmethod(helpers.Invariants.tearDownClass.__func__)
    setUp = helpers.Invariants.setUp
    call = helpers.Invariants.call
    create = helpers.Invariants.create
    booking = helpers.Invariants.booking
    load_fixture = helpers.Invariants.load_fixture
    managed = helpers.Invariants.managed
    combined_fixture = helpers.Invariants.combined_fixture
    policy = helpers.Invariants.policy
    history = helpers.Invariants.history

    def snapshot(self):
        return self.call('GET', '/_test/export', auth=False)[1]

    def closure(self, table='a', start='18:00', end='20:00', day=None):
        return {'table_id': table, 'from': (day or self.day)+'T'+start+':00+00:00',
                'to': (day or self.day)+'T'+end+':00+00:00'}

    def preview(self, body=None, key='preview'):
        return self.call('POST', '/restaurants/venue/replans', body or self.closure(), key)

    def apply(self, plan, key='apply'):
        return self.call('POST', '/restaurants/venue/replans/'+plan['plan_id']+'/apply', {}, key)

    def series(self, count=3):
        self.managed()
        anchor = self.create('anchor')
        body = {'anchor_reference': anchor['reference'], 'count': count, 'interval_weeks': 1}
        status, agreement = self.call('POST', '/series', body, 'adopt')
        self.assertEqual(status, 201)
        return agreement

    def amend(self, agreement, clock='20:00', index=0, key='amend'):
        return self.call('POST', '/series/'+agreement['series_id']+'/amend',
                         {'expected_revision': agreement['revision'], 'from_index': index,
                          'local_time': clock}, key)

    def test_preview_is_read_only_and_apply_preserves_bookings_and_receipts(self):
        self.managed()
        first, second = self.create('one'), self.create('two', 'b')
        before = self.snapshot()
        status, plan = self.preview()
        self.assertEqual(status, 201)
        self.assertEqual(plan['restaurant_revision'], 2)
        self.assertEqual([a['reference'] for a in plan['assignments']], sorted([first['reference'],second['reference']]))
        self.assertEqual(plan['moved_count'], 1)
        after = self.snapshot()
        for field in ('reservations','histories','restaurant_revisions','series','closures'):
            self.assertEqual(before['state'][field], after['state'][field])
        self.assertEqual(self.preview(key='preview'), (200, plan))
        status, receipt = self.apply(plan)
        self.assertEqual(status, 201)
        self.assertEqual(receipt['restaurant_revision'], 3)
        for old, new in [(first,self.call('GET','/reservations/'+first['reference'])[1]),
                         (second,self.call('GET','/reservations/'+second['reference'])[1])]:
            for field in ('reference','reservation_id','party_size','starts_at','ends_at','accepted_terms','created_at'):
                self.assertEqual(old[field],new[field])
            moved = new['table_ids'] != old['table_ids']
            self.assertEqual(new['revision'], old['revision']+moved)
            entries = self.history(old['reference'])
            self.assertEqual(len(entries), 1+moved)
            if moved:
                self.assertEqual(entries[-1]['event'],'reassigned')
                self.assertEqual(entries[-1]['plan_id'],plan['plan_id'])
                self.assertEqual(entries[-1]['changes'],[{'field':'table_ids','from':old['table_ids'],'to':new['table_ids']}])
        self.assertEqual(self.apply(plan,'other')[1]['error']['code'],'plan_already_applied')
        self.call('POST','/reservations/'+second['reference']+'/cancel')
        self.assertEqual(self.apply(plan),(200,receipt))
        self.assertEqual(self.call('POST','/reservations',self.booking(),'one'),(200,first))
        exported = self.snapshot()
        self.assertEqual(self.call('POST','/_test/import',exported,auth=False)[0],204)
        self.assertEqual(self.apply(plan),(200,receipt))
        self.assertEqual(self.snapshot(),exported)

    def test_closures_affect_pairs_explanations_boundary_and_failed_keys(self):
        self.managed()
        status, plan = self.preview()
        self.assertEqual(status,201)
        self.assertEqual(plan['assignments'],[])
        self.assertEqual(self.apply(plan)[0],201)
        status, error = self.call('POST','/reservations',self.booking(),'blocked')
        self.assertEqual((status,error['error']['code']),(409,'table_unavailable'))
        body = {k:v for k,v in self.booking().items() if k != 'table_id'} | {'table_ids':['a','b']}
        self.assertEqual(self.call('POST','/reservations',body,'pair')[1]['error']['code'],'table_unavailable')
        slots = self.call('GET','/availability?restaurant_id=venue&date='+self.day+'&party_size=2&explain=true',auth=False)[1]['slots']
        slot = next(s for s in slots if s['starts_at_local'].endswith('18:00'))
        self.assertNotIn('a',slot['available_table_ids'])
        self.assertTrue(all('a' not in o['table_ids'] for o in slot['available_options']))
        explanation = slot['explain'][0]
        self.assertEqual(explanation['rules'],[{'rule':'capacity','holds':True},{'rule':'no_overlap','holds':False}])
        self.assertEqual(self.call('POST','/reservations',self.booking(time='20:00'),'blocked')[0],201)
        self.assertEqual(self.call('POST','/reservations',self.booking(time='16:30'),'boundary')[0],201)
        outside = self.create('outside','b','20:00')
        before = self.snapshot()
        self.assertEqual(self.call('PATCH','/reservations/'+outside['reference'],{'starts_at_local':self.day+'T18:00','table_id':'a'})[1]['error']['code'],'table_unavailable')
        self.assertEqual(self.snapshot(),before)

    def test_stale_plan_and_restaurant_scope_failure_atomicity(self):
        self.managed()
        fixture = self.combined_fixture()
        fixture['restaurants'][0]['manager_user_ids'] = ['owner']
        other = copy.deepcopy(fixture['restaurants'][0]); other['id']='other'
        fixture['restaurants'].append(other); self.load_fixture(fixture)
        first = self.create('first')
        plan = self.preview()[1]
        self.assertEqual(self.call('PATCH','/reservations/'+first['reference'],{'party_size':2})[0],200)
        elsewhere = self.call('POST','/restaurants/other/replans',self.closure(), 'other-preview')[1]
        self.assertEqual(self.call('POST','/restaurants/other/replans/'+elsewhere['plan_id']+'/apply',{},'other-apply')[0],201)
        self.assertEqual(self.apply(plan)[0],201)
        nextplan = self.preview(self.closure('b','20:00','21:00'),key='second')[1]
        self.create('new','c','21:00')
        before = self.snapshot()
        self.assertEqual(self.apply(nextplan,'stale')[1]['error']['code'],'stale_plan')
        self.assertEqual(self.snapshot(),before)
        self.create('fill','b')
        before = self.snapshot()
        impossible = self.preview(self.closure('c'),key='impossible')
        self.assertEqual(impossible[1]['error']['code'],'no_feasible_plan')
        self.assertEqual(self.snapshot(),before)

    def test_optimizer_matches_exhaustive_oracle_and_each_accepted_policy(self):
        # Independent exhaustive comparison: all legal options and conflict pairs,
        # then the written objective. No implementation optimizer is imported.
        rng = random.Random(431)
        for case in range(12):
            fixture = copy.deepcopy(self.fixture)
            r = fixture['restaurants'][0]; r['manager_user_ids']=['owner']
            r['tables']=[{'id':str(i),'label':str(i),'capacity':rng.randint(2,6)} for i in range(5)]
            r['combinable']=[['1','2'],['3','4']]
            fixture['reservations']=[{'id':'seed'+str(i),'reference':'BOOK0'+str(i),'user_id':'owner',
                'restaurant_id':'venue','table_id':str(i),'party_size':rng.randint(1,r['tables'][i]['capacity']),
                'starts_at_local':self.day+'T18:00'} for i in range(3)]
            self.load_fixture(fixture)
            books = sorted(self.call('GET','/reservations')[1]['reservations'],key=lambda b:b['reference'])
            opts = [[t['id']] for t in r['tables']]+r['combinable']
            feasible=[]
            for ranks in itertools.product(range(len(opts)),repeat=len(books)):
                choices=[opts[i] for i in ranks]
                if any('0' in c for c in choices): continue
                if any(set(a)&set(b) for a,b in itertools.combinations(choices,2)): continue
                unused=[sum(b['accepted_terms']['capacities'][t] for t in c)-b['party_size'] for b,c in zip(books,choices)]
                if min(unused)<0: continue
                feasible.append((sum(set(b['table_ids'])!=set(c) for b,c in zip(books,choices)),sum(unused),ranks))
            status, plan = self.preview(self.closure('0'))
            if not feasible:
                self.assertEqual((status,plan['error']['code']),(409,'no_feasible_plan'))
            else:
                expected=min(feasible)
                self.assertEqual(status,201)
                actual=(plan['moved_count'],plan['unused_seats'],tuple(opts.index(a['table_ids']) for a in plan['assignments']))
                self.assertEqual(actual,expected,(case,actual,expected))
        self.managed()
        old = self.create('old')
        self.call('POST','/restaurants/venue/policies',self.policy(capacities={'a':1,'b':1,'c':1}),'shrink')
        plan = self.preview()[1]
        # Current policy cannot accommodate the party; the old accepted one can.
        self.assertEqual(plan['assignments'][0]['table_ids'],['c'])
        self.assertEqual(self.apply(plan)[0],201)
        self.assertEqual(self.call('GET','/reservations/'+old['reference'])[1]['accepted_terms'],old['accepted_terms'])

    def test_maximum_planning_bound_includes_all_six_bookings(self):
        fixture=copy.deepcopy(self.fixture)
        r=fixture['restaurants'][0]
        r.update(manager_user_ids=['owner'],reservation_duration_minutes=30,
                 tables=[{'id':str(i),'label':str(i),'capacity':2} for i in range(6)],
                 combinable=[['0','1'],['1','2'],['2','3'],['4','5']])
        times=['18:00','18:30','19:00','19:30','20:00','20:30']
        fixture['reservations']=[{'id':'seed'+str(i),'reference':'MAX00'+str(i),
                                 'user_id':'owner','restaurant_id':'venue','table_id':'0',
                                 'party_size':2,'starts_at_local':self.day+'T'+clock}
                                for i,clock in enumerate(times)]
        self.load_fixture(fixture)
        status,plan=self.preview(self.closure('0','18:00','21:00'))
        self.assertEqual(status,201)
        self.assertEqual(plan['moved_count'],6)
        self.assertEqual(plan['unused_seats'],0)
        self.assertEqual([a['reference'] for a in plan['assignments']],['MAX00'+str(i) for i in range(6)])
        self.assertTrue(all(a['table_ids']==['1'] for a in plan['assignments']))
        applied=self.apply(plan)[1]
        self.assertEqual(applied['restaurant_revision'],1)
        self.assertTrue(all(b['revision']==2 for b in applied['reservations']))

    def test_concurrent_apply_is_atomic_and_same_key_replays(self):
        agreement = self.series()
        plan = self.preview(self.closure(end='22:00',day=self.day))[1]
        barrier = threading.Barrier(50)
        def apply(_):
            barrier.wait()
            return self.apply(plan)
        with ThreadPoolExecutor(max_workers=50) as pool:
            outcomes=list(pool.map(apply,range(50)))
        self.assertEqual(sorted(s for s,_ in outcomes),[200]*49+[201])
        self.assertTrue(all(v == outcomes[0][1] for _,v in outcomes))
        current = self.call('GET','/series/'+agreement['series_id'])[1]
        self.assertEqual(current['revision'],2)
        self.assertTrue(all(not o['exception'] for o in current['occurrences']))
        self.assertEqual(self.snapshot()['state']['restaurant_revisions']['venue'],3)
        nextplan = self.preview(self.closure('b','18:00','22:00'),key='second')[1]
        barrier = threading.Barrier(2)
        def apply_distinct(i):
            barrier.wait(); return self.apply(nextplan,'distinct'+str(i))
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes=list(pool.map(apply_distinct,range(2)))
        self.assertEqual(sorted(s for s,_ in outcomes),[201,409])
        self.assertEqual(next(v['error']['code'] for s,v in outcomes if s==409),'plan_already_applied')

    def test_series_amendment_exclusions_terms_counters_and_portable_replays(self):
        agreement = self.series(4)
        sid=agreement['series_id']; path='/series/'+sid
        refs=[o['reference'] for o in agreement['occurrences']]
        self.call('PATCH','/reservations/'+refs[1],{'starts_at_local':self.day+'T21:00','table_id':'b'})
        self.call('POST','/reservations/'+refs[2]+'/cancel')
        current=self.call('GET',path)[1]
        self.call('POST','/restaurants/venue/policies',self.policy(),'policy')
        before=self.snapshot()
        status, receipt=self.amend(current)
        self.assertEqual(status,201)
        self.assertEqual(receipt['revision'],current['revision']+1)
        for i,o in enumerate(receipt['occurrences']):
            old=current['occurrences'][i]
            if i in (1,2): self.assertEqual(o,old)
            else:
                self.assertFalse(o['exception'])
                self.assertEqual(o['reservation']['starts_at_local'],old['reservation']['starts_at_local'][:10]+'T20:00')
                self.assertEqual(o['reservation']['accepted_terms']['policy_version'],1)
                self.assertEqual(o['reservation']['revision'],old['reservation']['revision']+1)
                self.assertEqual(self.history(o['reference'])[-1]['event'],'changed')
        self.assertEqual(self.snapshot()['state']['restaurant_revisions']['venue'],before['state']['restaurant_revisions']['venue']+1)
        self.assertEqual(self.amend(current),(200,receipt))
        status, noop=self.amend(receipt,key='noop')
        self.assertEqual((status,noop),(201,receipt))
        self.call('POST','/reservations/'+refs[0]+'/cancel')
        export=self.snapshot()
        self.assertEqual(self.call('POST','/_test/import',export,auth=False)[0],204)
        self.assertEqual(self.amend(current),(200,receipt))

    def test_series_amendment_precedence_noops_cutoff_and_race(self):
        agreement=self.series()
        refs=[o['reference'] for o in agreement['occurrences']]
        self.call('POST','/reservations',{**self.booking(),'starts_at_local':self.day+'T20:00'},'obstacle')
        later=agreement['occurrences'][1]['reservation']['starts_at_local'][:10]
        self.call('POST','/restaurants/venue/policies',self.policy(later,capacities={'a':1,'b':4,'c':2}),'small')
        before=self.snapshot()
        status,error=self.amend(agreement)
        self.assertEqual((status,error['error']['code']),(422,'party_exceeds_capacity'))
        self.assertEqual(self.snapshot(),before)
        # Stale revision precedes occurrence errors, and a failed key is free.
        stale={**agreement,'revision':99}
        self.assertEqual(self.amend(stale,key='stale')[1]['error']['code'],'stale_revision')
        self.assertEqual(self.amend(agreement,'18:00')[0],201)
        self.assertEqual(self.call('GET','/series/'+agreement['series_id'])[1],agreement)
        # Accepted cutoff is bypassed for series no-ops, but enforced for real changes.
        # Operator repairs remain permitted when diner changes are past cutoff.
        fixture=copy.deepcopy(self.fixture); fixture['restaurants'][0]['cancellation_cutoff_minutes']=10**9
        fixture['restaurants'][0]['manager_user_ids']=['owner']
        fixture['reservations']=[{'id':'past','reference':'PAST01','user_id':'owner',**self.booking()}]
        self.load_fixture(fixture)
        plan=self.preview()[1]
        self.assertEqual(self.apply(plan)[0],201)
        self.assertEqual(self.call('GET','/reservations/PAST01')[1]['revision'],2)
        agreement=self.series()
        barrier=threading.Barrier(2)
        def change(clock):
            barrier.wait();return self.amend(agreement,clock,key=clock)
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes=list(pool.map(change,['20:00','21:00']))
        self.assertEqual(sorted(s for s,_ in outcomes),[201,409])
        self.assertEqual(next(v['error']['code'] for s,v in outcomes if s==409),'stale_revision')
        # Move today's future occurrence to a past clock time, then prove that a
        # no-op succeeds while a further real change checks the now-past cutoff.
        now=datetime.now(helpers.timezone.utc)
        if now.hour < 20:
            self.day=now.date().isoformat()
            fixture=copy.deepcopy(self.fixture)
            fixture['restaurants'][0]['cancellation_cutoff_minutes']=0
            fixture['restaurants'][0]['manager_user_ids']=['owner']
            self.load_fixture(fixture)
            anchor=self.create('today',time='21:00')
            agreement=self.call('POST','/series',{'anchor_reference':anchor['reference'],'count':2,'interval_weeks':1},'today-series')[1]
            status,earlier=self.amend(agreement,'00:00',key='earlier')
            self.assertEqual(status,201)
            before=self.snapshot()
            self.assertEqual(self.amend(earlier,'00:00',key='past-noop'),(201,earlier))
            self.assertEqual(self.snapshot()['state']['histories'],before['state']['histories'])
            self.assertEqual(self.amend(earlier,'00:30',key='past-real')[1]['error']['code'],'cutoff_passed')

    def test_validation_auth_limits_and_invalid_import_are_atomic(self):
        self.managed()
        for interval in [self.closure()|{'from':True},self.closure()|{'to':self.closure()['from']},self.closure()|{'from':self.day+'T18:00:00'}]:
            before=self.snapshot()
            self.assertEqual(self.preview(interval)[1]['error']['code'],'validation_failed')
            self.assertEqual(self.snapshot(),before)
        self.assertEqual(self.preview(self.closure('missing'))[0],404)
        self.assertEqual(self.call('POST','/restaurants/venue/replans',self.closure(),auth=False)[0],401)
        self.assertEqual(self.call('POST','/restaurants/venue/replans',self.closure())[1]['error']['code'],'missing_idempotency_key')
        fixture=copy.deepcopy(self.fixture);fixture['restaurants'][0]['manager_user_ids']=['owner']
        fixture['restaurants'][0]['tables'] += [{'id':str(i),'label':str(i),'capacity':2} for i in range(5)]
        self.load_fixture(fixture)
        self.assertEqual(self.preview()[1]['error']['code'],'planning_limit')
        agreement=self.series()
        path='/series/'+agreement['series_id']+'/amend'
        valid={'expected_revision':1,'from_index':0,'local_time':'20:00'}
        for field,values in [('expected_revision',[True,0,'1']),('from_index',[True,-1,3]),('local_time',['24:00','1:00',False])]:
            for value in values:
                before=self.snapshot()
                self.assertEqual(self.call('POST',path,valid|{field:value},'invalid')[0],422)
                self.assertEqual(self.snapshot(),before)
        before=self.snapshot()
        for corrupt in [None,False,[],{'bad':'receipt'}]:
            broken=copy.deepcopy(before);broken['state']['receipts'].append(corrupt)
            self.assertEqual(self.call('POST','/_test/import',broken,auth=False)[0],422)
            self.assertEqual(self.snapshot(),before)


if __name__ == '__main__':
    unittest.main()
