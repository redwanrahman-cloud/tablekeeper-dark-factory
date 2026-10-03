"""Browser adversity and responsive checks. Requires Playwright and httpx for testing only."""
import argparse
import asyncio
import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from playwright.async_api import async_playwright


async def main(base, out, previous):
    out.mkdir(parents=True, exist_ok=True)
    day = (datetime.now(timezone.utc) + timedelta(days=30)).date().isoformat()
    restaurant = {'id':'room','name':'The Olive Room','timezone':'UTC','slot_minutes':30,
                  'reservation_duration_minutes':90,'cancellation_cutoff_minutes':120,
                  'opening_hours':[{'weekday':d,'opens':'18:00','closes':'23:00'} for d in ['mon','tue','wed','thu','fri','sat','sun']],
                  'tables':[{'id':'terrace','label':'Terrace','capacity':2},{'id':'garden','label':'Garden','capacity':4},{'id':'window','label':'Window','capacity':6}],
                  'combinable':[['terrace','garden']]}
    fixture = {'users':[{'id':'diner','email':'diner@example.test','password':'browser-password','display_name':'Robin'}],
               'restaurants':[restaurant],'reservations':[]}
    results = []
    async with httpx.AsyncClient(base_url=base) as client, async_playwright() as p:
        browser = await p.chromium.launch(channel='chromium',args=[f'--unsafely-treat-insecure-origin-as-secure={base}'])
        async def setup(width=1280):
            assert (await client.post('/_test/reset',json=fixture)).status_code == 204
            context = await browser.new_context(base_url=base,viewport={'width':width,'height':900})
            page = await context.new_page()
            page.set_default_timeout(10000)
            await page.goto('/login')
            await page.get_by_test_id('login-email').fill('diner@example.test')
            await page.get_by_test_id('login-password').fill('browser-password')
            await page.get_by_test_id('login-submit').click()
            await page.get_by_test_id('current-user').wait_for()
            await page.goto('/')
            await page.get_by_test_id('restaurant-select').select_option('room')
            await page.get_by_test_id('date-input').fill(day)
            await page.get_by_test_id('party-size-input').fill('6')
            await page.get_by_test_id('search-button').click()
            await page.get_by_test_id('availability-grid').wait_for()
            return context,page

        for width in (1280,375):
            context,page = await setup(width)
            errors = []
            page.on('pageerror',lambda error:errors.append(str(error)))
            assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            await page.screenshot(path=str(out/f'search-{width}.png'),full_page=True)
            await page.get_by_test_id('slot-terrace+garden-19:00').click()
            assert 'Terrace' in await page.get_by_test_id('booking-summary').inner_text()
            assert 'Garden' in await page.get_by_test_id('booking-summary').inner_text()
            await page.get_by_test_id('booking-submit').click()
            await page.get_by_test_id('confirmation').wait_for()
            reference = await page.get_by_test_id('confirmation-reference').inner_text()
            await page.get_by_test_id('booking-submit').click()
            await page.get_by_test_id('confirmation').wait_for()
            assert await page.get_by_test_id('confirmation-reference').inner_text() == reference
            assert await page.get_by_test_id('booking-error').count() == 0
            await page.screenshot(path=str(out/f'confirmation-{width}.png'),full_page=True)
            await page.goto('/lookup')
            await page.get_by_test_id('lookup-reference-input').fill(reference)
            await page.get_by_test_id('lookup-submit').click()
            await page.get_by_test_id('reservation-detail').wait_for()
            assert await page.get_by_test_id('reservation-status').inner_text() == 'confirmed'
            assert 'Garden' in await page.get_by_test_id('reservation-tables').inner_text()
            await page.screenshot(path=str(out/f'lookup-{width}.png'),full_page=True)
            assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            await page.get_by_test_id('reservation-cancel-button').click()
            await page.get_by_test_id('reservation-cancel-button').wait_for(state='detached')
            assert await page.get_by_test_id('reservation-status').inner_text() == 'cancelled'
            assert not errors,errors
            results.append(f'{width}px combined flow/replay/lookup/cancel/no overflow: PASS')
            await context.close()

        context,page = await setup()
        held = asyncio.Event(); release = asyncio.Event()
        async def delay(route):
            response = await route.fetch()
            if 'party_size=2' in route.request.url:
                held.set(); await release.wait()
            await route.fulfill(response=response)
        await page.route('**/availability?*',delay)
        await page.get_by_test_id('party-size-input').fill('2')
        await page.get_by_test_id('search-button').click()
        await held.wait()
        await page.get_by_test_id('party-size-input').fill('6')
        await page.get_by_test_id('search-button').click()
        await page.get_by_test_id('availability-grid').wait_for()
        release.set(); await page.wait_for_timeout(250)
        assert await page.get_by_test_id('slot-terrace-19:00').get_attribute('data-available') == 'false'
        await page.get_by_test_id('slot-terrace+garden-19:00').click()
        assert await page.get_by_test_id('booking-party-size').input_value() == '6'
        results.append('Out-of-order A after B retains B grid and booking: PASS')
        await context.close()

        context,page = await setup()
        await page.get_by_test_id('slot-terrace+garden-19:00').click()
        attempts = []
        async def lose_response(route):
            attempts.append((route.request.headers.get('idempotency-key'),route.request.post_data))
            if len(attempts) == 1:
                committed = await route.fetch()
                assert committed.status == 201
                await route.abort('failed')
            else:
                await route.continue_()
        await page.route('**/reservations',lose_response)
        await page.get_by_test_id('booking-submit').click()
        await page.get_by_test_id('booking-uncertain').wait_for()
        assert await page.get_by_test_id('booking-error').count() == 0
        assert await page.get_by_test_id('confirmation').count() == 0
        snapshot = (await client.get('/_test/export')).json()
        assert (await client.post('/_test/reset',json=fixture)).status_code == 204
        assert (await client.post('/_test/import',json=snapshot)).status_code == 204
        await page.get_by_test_id('booking-submit').click()
        await page.get_by_test_id('confirmation').wait_for()
        assert attempts[0] == attempts[1]
        assert await page.get_by_test_id('booking-uncertain').count() == 0
        assert await page.get_by_test_id('booking-error').count() == 0
        token = await page.evaluate('JSON.parse(localStorage.getItem("tablekeeper.session")).token')
        mine = (await client.get('/reservations',headers={'Authorization':'Bearer '+token})).json()['reservations']
        assert len(mine) == 1
        assert await page.get_by_test_id('confirmation-reference').inner_text() == mine[0]['reference']
        results.append('Committed lost pair response/import/unchanged same-key retry: PASS')
        await context.close()

        context,page = await setup()
        await page.get_by_test_id('slot-terrace+garden-19:00').click()
        token = await page.evaluate('JSON.parse(localStorage.getItem("tablekeeper.session")).token')
        taken = await client.post('/reservations',json={'restaurant_id':'room','table_id':'garden','starts_at_local':day+'T19:00','party_size':4},headers={'Authorization':'Bearer '+token,'Idempotency-Key':'another-client'})
        assert taken.status_code == 201
        await page.get_by_test_id('booking-submit').click()
        await page.get_by_test_id('booking-error').wait_for()
        await page.wait_for_function('document.querySelector(`[data-testid="slot-garden-19:00"]`).dataset.available === "false"')
        assert await page.get_by_test_id('booking-party-size').input_value() == '6'
        assert await page.get_by_test_id('booking-form').count() == 1
        assert await page.get_by_test_id('confirmation').count() == 0
        results.append('Stale pair rejection refresh preserves selected form and inputs: PASS')
        await context.close()

        if previous:
            async with httpx.AsyncClient(base_url=previous) as old:
                seed = copy.deepcopy(fixture); seed['restaurants'][0].pop('combinable')
                assert (await old.post('/_test/reset',json=seed)).status_code == 204
                login = (await old.post('/auth/login',json={'email':'diner@example.test','password':'browser-password'})).json()
                context = await browser.new_context(base_url=base)
                page = await context.new_page(); await page.goto('/')
                await page.evaluate('(s)=>localStorage.setItem("tablekeeper.session",JSON.stringify(s))',login)
                assert (await client.post('/_test/import',json=(await old.get('/_test/export')).json())).status_code == 204
                await page.reload()
                await page.get_by_test_id('current-user').wait_for()
                await page.get_by_test_id('restaurant-select').select_option('room')
                await page.get_by_test_id('date-input').fill(day)
                await page.get_by_test_id('party-size-input').fill('4')
                await page.get_by_test_id('search-button').click(); await page.get_by_test_id('availability-grid').wait_for()
                await page.get_by_test_id('slot-garden-19:00').click()
                requests = []
                async def old_commit(route):
                    requests.append((route.request.headers['idempotency-key'],route.request.post_data))
                    if len(requests) == 1:
                        response = await old.post('/reservations',content=route.request.post_data,headers={'Content-Type':'application/json','Authorization':'Bearer '+login['token'],'Idempotency-Key':requests[0][0]})
                        assert response.status_code == 201
                        await route.abort('failed')
                    else: await route.continue_()
                await page.route('**/reservations',old_commit)
                await page.get_by_test_id('booking-submit').click(); await page.get_by_test_id('booking-uncertain').wait_for()
                exported = (await old.get('/_test/export')).json()
                assert (await client.post('/_test/import',json=exported)).status_code == 204
                await page.get_by_test_id('booking-submit').click(); await page.get_by_test_id('confirmation').wait_for()
                assert requests[0] == requests[1]
                reference = await page.get_by_test_id('confirmation-reference').inner_text()
                await page.goto('/lookup'); await page.get_by_test_id('lookup-reference-input').fill(reference)
                await page.get_by_test_id('lookup-submit').click(); await page.get_by_test_id('reservation-detail').wait_for()
                results.append('Actual preceding accepted service export preserves session/pending browser retry/lookup: PASS')
                await context.close()
        original_fixture = fixture
        fixture = copy.deepcopy(original_fixture)
        fixture['restaurants'][0]['manager_user_ids'] = ['diner']
        fixture['restaurants'][0]['name'] = 'The Olive Room ' + 'Welcoming'*35
        fixture['restaurants'][0]['tables'][0]['label'] = 'Terrace ' + 'Together'*25
        for width in (1440,375):
            context,page = await setup(width)
            errors = []; page.on('pageerror',lambda error:errors.append(str(error)))
            token = await page.evaluate('JSON.parse(localStorage.getItem("tablekeeper.session")).token')
            headers = {'Authorization':'Bearer '+token,'Idempotency-Key':'dated-policy'}
            policy = {'effective_from':day,'slot_minutes':30,'reservation_duration_minutes':60,
                      'cancellation_cutoff_minutes':45,'opening_hours':restaurant['opening_hours'],
                      'capacities':{'terrace':2,'garden':5,'window':6}}
            assert (await client.post('/restaurants/room/policies',json=policy,headers=headers)).status_code == 201
            await page.get_by_test_id('search-button').click()
            await page.get_by_test_id('availability-grid').wait_for()
            await page.get_by_test_id('availability-explanation').first.locator('summary').click()
            assert 'Too few seats' in await page.get_by_test_id('availability-explanation').first.inner_text()
            assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            await page.screenshot(path=str(out/f'policy-explanation-{width}.png'),full_page=True)
            await page.get_by_test_id('slot-terrace+garden-19:00').click()
            assert '60 minutes' in await page.locator('.fine-print').inner_text()
            await page.get_by_test_id('booking-submit').click(); await page.get_by_test_id('confirmation').wait_for()
            assert '60 minutes' in await page.get_by_test_id('accepted-terms').inner_text()
            reference = await page.get_by_test_id('confirmation-reference').inner_text()
            await page.goto('/lookup'); await page.get_by_test_id('lookup-reference-input').fill(reference)
            await page.get_by_test_id('lookup-submit').click(); await page.get_by_test_id('reservation-history').wait_for()
            assert 'Reservation recorded' in await page.get_by_test_id('reservation-history').inner_text()
            summary = page.locator('.agreement-tool').nth(1).locator('summary')
            await summary.focus(); assert await summary.evaluate('(e)=>document.activeElement===e')
            await summary.press('Enter')
            await page.get_by_test_id('series-count').fill('3')
            attempts = []; committed_series = None
            async def lose_series(route):
                nonlocal committed_series
                attempts.append((route.request.headers['idempotency-key'],route.request.post_data))
                if len(attempts) == 1:
                    response = await route.fetch(); assert response.status == 201
                    committed_series = await response.json(); await route.abort('failed')
                else: await route.continue_()
            await page.route('**/series',lose_series)
            await page.get_by_test_id('series-submit').click(); await page.get_by_test_id('series-uncertain').wait_for()
            assert await page.get_by_test_id('series-occurrences').count() == 0
            await page.get_by_test_id('series-submit').click(); await page.get_by_test_id('series-occurrences').wait_for()
            assert attempts[0] == attempts[1]
            assert await page.get_by_test_id('series-uncertain').count() == 0
            assert await page.get_by_test_id('series-occurrences').locator(':scope > li').count() == 3
            await page.screenshot(path=str(out/f'terms-history-series-{width}.png'),full_page=True)
            await page.locator('.agreement-tool').first.locator('summary').click()
            await page.get_by_test_id('amend-party').fill('5')
            await page.get_by_test_id('amend-submit').click()
            await page.wait_for_function('document.querySelector(`[data-testid="reservation-history"]`)?.textContent.includes("Booking changed")')
            agreement = (await client.get('/series/'+committed_series['series_id'],headers={'Authorization':'Bearer '+token})).json()
            assert agreement['revision'] == 2 and agreement['occurrences'][0]['exception'] is True
            assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            await page.screenshot(path=str(out/f'changed-history-{width}.png'),full_page=True)
            assert not errors,errors
            results.append(f'{width}px dated terms/explanations/history/recurrence lost response/amendment/long labels/focus: PASS')
            await context.close()
        await browser.close()
    print('\n'.join(results))
    (out/'browser-results.json').write_text(json.dumps(results,indent=2)+'\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--base-url',required=True)
    parser.add_argument('--previous-base-url')
    parser.add_argument('--out',type=Path,required=True)
    args = parser.parse_args()
    asyncio.run(main(args.base_url,args.out,args.previous_base_url))
