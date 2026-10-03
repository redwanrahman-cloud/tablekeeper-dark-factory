"""Real rendered seating repair, retained retries, closure availability and terms contrast."""
import argparse
import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from playwright.async_api import async_playwright, expect


async def main(base,out):
    out.mkdir(exist_ok=True,parents=True)
    day=(datetime.now(timezone.utc)+timedelta(days=30)).date().isoformat()
    r={'id':'venue','name':'The Olive Room','timezone':'UTC','slot_minutes':30,
       'reservation_duration_minutes':90,'cancellation_cutoff_minutes':120,
       'opening_hours':[{'weekday':d,'opens':'18:00','closes':'23:00'} for d in ['mon','tue','wed','thu','fri','sat','sun']],
       'tables':[{'id':'a','label':'Garden','capacity':2},{'id':'b','label':'Terrace','capacity':4},
                 {'id':'c','label':'Window','capacity':6}],
       'combinable':[['a','b'],['b','c']], 'manager_user_ids':['owner']}
    fixture={'users':[{'id':'owner','email':'owner@example.test','password':'long-password','display_name':'Robin'}],
             'restaurants':[r],'reservations':[]}
    measurements=[]; results=[]
    async with httpx.AsyncClient(base_url=base) as client,async_playwright() as p:
        browser=await p.chromium.launch(channel='chromium',args=['--unsafely-treat-insecure-origin-as-secure='+base])
        for width in (375,1440):
            assert (await client.post('/_test/reset',json=fixture)).status_code==204
            context=await browser.new_context(base_url=base,viewport={'width':width,'height':900})
            page=await context.new_page(); errors=[]
            page.on('pageerror',lambda error:errors.append(str(error)))
            await page.goto('/login')
            await page.get_by_test_id('login-email').fill('owner@example.test')
            await page.get_by_test_id('login-password').fill('long-password')
            await page.get_by_test_id('login-submit').click()
            await expect(page.get_by_test_id('current-user')).to_contain_text('Robin')
            await page.goto('/')
            await page.get_by_test_id('restaurant-select').select_option('venue')
            await page.get_by_test_id('date-input').fill(day)
            await page.get_by_test_id('party-size-input').fill('2')
            await page.get_by_test_id('search-button').click()
            await page.get_by_test_id('slot-a-19:00').click()
            requests=[]
            def record_request(request):
                if request.method=='POST' and request.url.endswith('/reservations'):
                    requests.append((request.headers.get('idempotency-key'),request.post_data_json))
            page.on('request',record_request)
            await page.get_by_test_id('booking-submit').click()
            await expect(page.get_by_test_id('confirmation-tables')).to_have_text('Garden')
            ref=await page.get_by_test_id('confirmation-reference').inner_text()
            # A manager's independent API session applies the repair.
            login=(await client.post('/auth/login',json={'email':'owner@example.test','password':'long-password'})).json()
            headers={'Authorization':'Bearer '+login['token']}
            closure={'table_id':'a','from':day+'T18:00:00+00:00','to':day+'T23:00:00+00:00'}
            preview=await client.post('/restaurants/venue/replans',json=closure,headers=headers|{'Idempotency-Key':'preview'})
            assert preview.status_code==201
            pid=preview.json()['plan_id']
            assert (await client.post('/restaurants/venue/replans/'+pid+'/apply',json={},headers=headers|{'Idempotency-Key':'apply'})).status_code==201
            # A retained form replays the original request and shows the current
            # owner record rather than the receipt's old seating assignment.
            await page.get_by_test_id('booking-submit').click()
            await expect(page.get_by_test_id('confirmation-tables')).to_have_text('Terrace')
            await expect(page.get_by_test_id('confirmation-reference')).to_have_text(ref)
            assert len(requests)==2 and requests[0]==requests[1]
            async def audit(stage):
                row=await page.get_by_test_id('accepted-terms').locator('span').evaluate('''e=>{
                  let s=getComputedStyle(e),n=e,bg;
                  while(n){bg=getComputedStyle(n).backgroundColor;if(bg!=='rgba(0, 0, 0, 0)'&&bg!=='transparent')break;n=n.parentElement;}
                  const luminance=c=>c.match(/[\\d.]+/g).slice(0,3).map(Number).map(v=>v/255)
                    .map(v=>v<=.04045?v/12.92:((v+.055)/1.055)**2.4)
                    .reduce((v,x,i)=>v+x*[.2126,.7152,.0722][i],0);
                  let a=luminance(s.color),b=luminance(bg);
                  return {color:s.color,background:bg,fontSize:s.fontSize,contrast:(Math.max(a,b)+.05)/(Math.min(a,b)+.05)};
                }''')
                row.update(width=width,stage=stage);measurements.append(row)
                assert row['contrast']>=4.5,row
                assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                await page.screenshot(path=str(out/f'{width}-{stage}.png'),full_page=True)
            await audit('confirmation')
            await page.goto('/lookup')
            await page.get_by_test_id('lookup-reference-input').fill(ref)
            await page.get_by_test_id('lookup-submit').click()
            await expect(page.get_by_test_id('reservation-tables')).to_have_text('Terrace')
            await expect(page.get_by_test_id('reservation-history')).to_contain_text('Seating reassigned')
            await audit('lookup')
            await page.goto('/')
            await page.get_by_test_id('date-input').fill(day)
            await page.get_by_test_id('party-size-input').fill('2')
            await page.get_by_test_id('search-button').click()
            await expect(page.get_by_test_id('slot-a-19:00')).to_have_attribute('data-available','false')
            assert await page.get_by_test_id('slot-a-19:00').is_disabled()
            assert await page.get_by_test_id('slot-a+b-19:00').count()==0
            await page.screenshot(path=str(out/f'{width}-closure-grid.png'),full_page=True)
            assert not errors,errors
            results.append(f'{width}px repair/retained confirmation retry/lookup/history/closure grid/terms contrast PASS')
            await context.close()
        await browser.close()
    (out/'terms-contrast.json').write_text(json.dumps(measurements,indent=2))
    (out/'results.json').write_text(json.dumps(results,indent=2))
    print('\n'.join(results))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--base-url',required=True);p.add_argument('--out',required=True,type=Path)
    a=p.parse_args();asyncio.run(main(a.base_url,a.out))
