"""Populate real prior services, transfer private in-memory snapshots, assert continuity."""
import argparse
from datetime import datetime, timedelta, timezone

import httpx


def check(base, sources):
    day=(datetime.now(timezone.utc)+timedelta(days=30)).date().isoformat()
    restaurant={'id':'venue','name':'The Olive Room','timezone':'UTC','slot_minutes':30,
                'reservation_duration_minutes':90,'cancellation_cutoff_minutes':120,
                'opening_hours':[{'weekday':d,'opens':'00:00','closes':'23:00'} for d in ['mon','tue','wed','thu','fri','sat','sun']],
                'tables':[{'id':'a','label':'Garden','capacity':2},{'id':'b','label':'Terrace','capacity':4},
                          {'id':'c','label':'Window','capacity':6}],
                'combinable':[['a','b'],['b','c']], 'manager_user_ids':['owner']}
    fixture={'users':[{'id':'owner','email':'owner@example.test','password':'long-password','display_name':'Robin'}],
             'restaurants':[restaurant],'reservations':[]}
    for stage,url in sources:
        with httpx.Client(base_url=url,timeout=5) as old,httpx.Client(base_url=base,timeout=5) as new:
            assert old.post('/_test/reset',json=fixture).status_code==204
            login=old.post('/auth/login',json={'email':'owner@example.test','password':'long-password'}).json()
            headers={'Authorization':'Bearer '+login['token']}
            body={'restaurant_id':'venue','table_id':'b','starts_at_local':day+'T18:00','party_size':3}
            receipt=old.post('/reservations',json=body,headers=headers|{'Idempotency-Key':'original'}).json()
            ref=receipt['reference']
            agreement=None
            if stage==3:
                adoption={'anchor_reference':ref,'count':4,'interval_weeks':1}
                made=old.post('/series',json=adoption,headers=headers|{'Idempotency-Key':'adoption'})
                assert made.status_code==201
                agreement=made.json(); sid=agreement['series_id']
                exception=agreement['occurrences'][1]['reference']
                assert old.patch('/reservations/'+exception,json={'starts_at_local':day+'T21:00'},headers=headers).status_code==200
                assert old.post('/reservations/'+agreement['occurrences'][2]['reference']+'/cancel',json={},headers=headers).status_code==200
                oldhistory=old.get('/reservations/'+exception+'/history',headers=headers).json()
            else:
                moves={'moves':[{'reference':ref,'table_id':'c'}]}
                moved=old.post('/reservation-moves',json=moves,headers=headers|{'Idempotency-Key':'move'})
                assert moved.status_code==201
            snapshot=old.get('/_test/export').json()
            assert new.post('/_test/reset',json={'users':[],'restaurants':[],'reservations':[]}).status_code==204
            assert new.post('/_test/import',json=snapshot).status_code==204
            assert new.post('/_test/import',json=snapshot).status_code==204
            retried=new.post('/reservations',json=body,headers=headers|{'Idempotency-Key':'original'})
            assert retried.status_code==200 and retried.json()==receipt
            assert new.post('/auth/login',json={'email':'owner@example.test','password':'long-password'}).status_code==200
            if stage==3:
                assert new.post('/series',json=adoption,headers=headers|{'Idempotency-Key':'adoption'}).json()==agreement
                assert new.get('/reservations/'+exception+'/history',headers=headers).json()==oldhistory
                current=new.get('/series/'+sid,headers=headers).json()
                internal=new.get('/_test/export').json()['state']['series'][sid]['occurrences']
                assert all(o['scheduled_date']==agreement['occurrences'][i]['reservation']['starts_at_local'][:10] for i,o in enumerate(internal))
            else:
                replay=new.post('/reservation-moves',json=moves,headers=headers|{'Idempotency-Key':'move'})
                assert replay.status_code==200 and replay.json()==moved.json()
                adopted=new.post('/series',json={'anchor_reference':ref,'count':3,'interval_weeks':1},headers=headers|{'Idempotency-Key':'adoption'})
                assert adopted.status_code==201
                current=adopted.json(); sid=current['series_id']
            # Stage 1/2 have no manager role to preserve; imports correctly grant
            # no new authority. Stage 3 carries manager IDs and permits repairs.
            if stage==3:
                close={'table_id':current['occurrences'][0]['reservation']['table_ids'][0],
                       'from':day+'T18:00:00+00:00','to':day+'T22:30:00+00:00'}
                preview=new.post('/restaurants/venue/replans',json=close,headers=headers|{'Idempotency-Key':'preview'})
                assert preview.status_code==201,preview.text
                pid=preview.json()['plan_id']
                applied=new.post('/restaurants/venue/replans/'+pid+'/apply',json={},headers=headers|{'Idempotency-Key':'apply'})
                assert applied.status_code==201
            repaired=new.get('/series/'+sid,headers=headers).json()
            assert [o['exception'] for o in repaired['occurrences']]==[o['exception'] for o in current['occurrences']]
            amendment={'expected_revision':repaired['revision'],'from_index':0,'local_time':'20:00'}
            # The applied closure could reject the new clock time only on its
            # closed member. Repaired eligible bookings retain their open seats.
            changed=new.post('/series/'+sid+'/amend',json=amendment,headers=headers|{'Idempotency-Key':'amend'})
            if stage==3:
                # The moved exception shares the repaired table at 21:00. Its
                # unchanged occupancy must reject a 20:00–21:30 amendment.
                assert changed.status_code==409 and changed.json()['error']['code']=='table_unavailable'
                amendment['local_time']='19:30'
                changed=new.post('/series/'+sid+'/amend',json=amendment,headers=headers|{'Idempotency-Key':'amend'})
            assert changed.status_code==201,changed.text
            amended=changed.json()
            for before,after in zip(repaired['occurrences'],amended['occurrences']):
                if before['exception'] or before['reservation']['status']=='cancelled':
                    assert after==before
                else:
                    assert after['reservation']['starts_at_local']==before['reservation']['starts_at_local'][:10]+'T'+amendment['local_time']
                    assert after['reservation']['table_ids']==before['reservation']['table_ids']
                    assert not after['exception']
            portable=new.get('/_test/export').json()
            assert new.post('/_test/import',json=portable).status_code==204
            if stage==3:
                assert new.post('/restaurants/venue/replans/'+pid+'/apply',json={},headers=headers|{'Idempotency-Key':'apply'}).json()==applied.json()
            assert new.post('/series/'+sid+'/amend',json=amendment,headers=headers|{'Idempotency-Key':'amend'}).json()==amended
            assert new.post('/reservations',json=body,headers=headers|{'Idempotency-Key':'original'}).json()==receipt
            print(f'Stage {stage} real export: sessions, original receipts, adoption, scheduled dates, amendments, portable retries'+(', moved/cancelled histories and repairs' if stage==3 else ', no manager authority invented')+' PASS')


if __name__=='__main__':
    p=argparse.ArgumentParser()
    p.add_argument('--base-url',required=True)
    for n in (1,2,3): p.add_argument('--stage'+str(n)+'-url',required=True)
    args=p.parse_args()
    check(args.base_url,[(n,getattr(args,'stage'+str(n)+'_url')) for n in (1,2,3)])
