'use strict';
const $ = (id) => document.getElementById(id);
const esc = (value) => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const test = (name) => `data-testid="${esc(name)}"`;
let session = null;
try { session = JSON.parse(localStorage.getItem('tablekeeper.session')); } catch (_) { /* No prior session. */ }
let searchVersion = 0, currentSearch = null, booking = null, lookupVersion = 0;
const main = $('main');
class Rejection extends Error { constructor(status, data) { super(data?.error?.message || 'The request was refused. Please check your details.'); this.status = status; this.code = data?.error?.code; } }
async function api(path, {method = 'GET', body, key, authenticated = false} = {}) {
  const headers = {'Accept':'application/json'};
  if (body !== undefined) headers['Content-Type'] = 'application/json; charset=utf-8';
  if (authenticated && session) headers.Authorization = 'Bearer ' + session.token;
  if (key) headers['Idempotency-Key'] = key;
  const response = await fetch(path, {method, headers, body:body === undefined ? undefined : JSON.stringify(body), cache:'no-store'});
  const data = response.status === 204 ? null : await response.json();
  if (!response.ok) throw new Rejection(response.status, data);
  return data;
}
function feedback(target, name, message, type = 'error') {
  $(target).innerHTML = message ? `<div class="feedback ${type}" ${test(name)} role="${type === 'error' ? 'alert' : 'status'}">${esc(message)}</div>` : '';
}
function friendly(error) {
  return ({table_unavailable:'That seating was just reserved by another guest. We’ve refreshed the times below. Your selection is here so you can choose again.',cutoff_passed:'This reservation is within the restaurant’s cancellation window and can no longer be changed.',unauthenticated:'We couldn’t sign you in. Check your email and password.',email_taken:'There’s already an account with this email. Please sign in.',not_found:'We couldn’t find a reservation with that reference for your account.',party_exceeds_capacity:'This party is too large for the selected seating. Choose a larger table or a combined pair.'})[error.code] || (error instanceof Rejection ? error.message : 'We couldn’t connect. Please try again.');
}
function renderSession() {
  $('session-nav').innerHTML = session ? `<span class="user-name" ${test('current-user')}>${esc(session.display_name || 'Signed in')}</span><button class="text-button" id="logout" ${test('logout-button')}>Sign out</button>` : '<a href="/login">Sign in</a><a class="small-link" href="/signup">Join us</a>';
  if (session) $('logout').onclick = () => { session = null; localStorage.removeItem('tablekeeper.session'); renderSession(); if (booking) { booking.attempt = null; $('confirmation-area').innerHTML = ''; feedback('booking-feedback','booking-error','Please sign in to make a reservation.'); } };
}
function saveSession(data) { session = data; localStorage.setItem('tablekeeper.session', JSON.stringify(data)); renderSession(); }
function field(id, label, type, attrs = '') { return `<div class="field"><label for="${id}">${label}</label><input id="${id}" ${test(id)} type="${type}" ${attrs}></div>`; }
function authScreen(signup) {
  const prefix = signup ? 'signup' : 'login';
  main.innerHTML = `<div class="auth-layout"><section class="auth-copy"><p class="eyebrow">A warm welcome</p><h1>${signup ? 'Good evenings<br>start here.' : 'Your next evening<br>is waiting.'}</h1><p class="muted">A little less planning. A little more time together. Keep your reservations in one comfortable place.</p><div class="auth-decoration" aria-hidden="true">✳ &nbsp; ◡ &nbsp; ✳</div></section><section class="form-card"><h2>${signup ? 'A seat for you' : 'Welcome back'}</h2><p>${signup ? 'Create your account to reserve a table.' : 'Sign in to book and manage your reservations.'}</p><form id="auth-form">${signup ? field('signup-display-name','Your name','text','autocomplete="name"') : ''}${field(prefix+'-email','Email address','email','autocomplete="email" required')}${field(prefix+'-password','Password','password',`autocomplete="${signup ? 'new-password' : 'current-password'}" ${signup ? 'minlength="8"' : ''} required`)}<div id="auth-feedback" aria-live="polite"></div><button class="primary" id="auth-submit" ${test(prefix+'-submit')}>${signup ? 'Create account' : 'Sign in'}</button></form><div class="form-tail">${signup ? 'Already have an account? <a href="/login">Sign in</a>' : 'New to Tablekeeper? <a href="/signup">Create an account</a>'}</div><div id="auth-next"></div></section></div>`;
  $('auth-form').onsubmit = async event => {
    event.preventDefault(); feedback('auth-feedback','auth-error','');
    const button = $('auth-submit'); button.disabled = true; button.textContent = 'One moment…';
    const body = {email:$(prefix+'-email').value,password:$(prefix+'-password').value};
    if (signup) body.display_name = $('signup-display-name').value;
    try { saveSession(await api('/auth/'+prefix,{method:'POST',body})); $('auth-next').innerHTML = '<div class="feedback success" role="status">You’re signed in. <a href="/">Find your table →</a></div>'; }
    catch (error) { feedback('auth-feedback','auth-error',friendly(error)); }
    finally { button.disabled = false; button.textContent = signup ? 'Create account' : 'Sign in'; }
  };
}
const illustration = `<div class="hero-art" aria-hidden="true"><svg viewBox="0 0 380 310" fill="none"><path d="M52 265H332M60 262V202M319 262V202" stroke="#adbb9e" stroke-width="2"/><ellipse cx="190" cy="176" rx="124" ry="72" fill="#d1d9be"/><ellipse cx="190" cy="154" rx="124" ry="72" fill="#fffaf0"/><ellipse cx="190" cy="154" rx="114" ry="63" stroke="#c7bca1"/><ellipse cx="135" cy="144" rx="34" ry="23" fill="#e3e8d8" stroke="#9bab8e"/><ellipse cx="135" cy="144" rx="23" ry="15" stroke="#b7c3aa"/><ellipse cx="251" cy="167" rx="34" ry="23" fill="#e3e8d8" stroke="#9bab8e"/><ellipse cx="251" cy="167" rx="23" ry="15" stroke="#b7c3aa"/><path d="M88 136L104 151M84 140L100 155M279 152L292 166M286 146L299 160" stroke="#8c977d" stroke-width="3" stroke-linecap="round"/><path d="M188 116V80M180 81H197L194 103H184Z" fill="#b94d2b"/><path d="M191 80C177 68 186 57 191 50C200 64 202 72 191 80Z" fill="#e4a64f"/><path d="M154 211L162 188M221 129L226 109" stroke="#9dab8c" stroke-width="2"/><path d="M157 211H148M231 130H216" stroke="#9dab8c" stroke-width="2"/><path d="M147 173L175 182L166 198L142 188Z" fill="#b95736"/><path d="M212 115L241 117L237 135L212 133Z" fill="#d6ac76"/><path d="M31 89C40 74 55 81 61 96M325 82C331 64 345 66 349 78" stroke="#748b64" stroke-width="2"/><path d="M40 95C33 68 37 44 46 27M341 99C350 72 344 48 336 35" stroke="#748b64" stroke-width="2"/><ellipse cx="44" cy="51" rx="9" ry="17" transform="rotate(-30 44 51)" fill="#9dae89"/><ellipse cx="339" cy="58" rx="9" ry="17" transform="rotate(30 339 58)" fill="#9dae89"/></svg><span class="art-caption">The best moments are shared</span></div>`;
function searchScreen() {
  main.innerHTML = `<section class="hero"><div class="hero-copy"><p class="eyebrow">Make room for a good evening</p><h1>A place at the table.<br><em>A moment together.</em></h1><p class="muted">Find the right restaurant, choose your time, and leave the rest to good company.</p><div class="hero-note"><span></span>Real availability · Thoughtful seating · Simple booking</div></div>${illustration}</section><section class="search-card" aria-label="Find a table"><form id="search-form" class="search-fields"><div class="restaurant-field"><label for="restaurant-select">Where shall we meet?</label><select id="restaurant-select" ${test('restaurant-select')} required><option value="">Loading restaurants…</option></select></div><div><label for="date-input">Date</label><input id="date-input" ${test('date-input')} type="date" required></div><div><label for="party-size-input">Guests</label><input id="party-size-input" ${test('party-size-input')} type="number" min="1" step="1" value="2" required></div><button class="primary" ${test('search-button')}>Find a table →</button></form><div id="search-feedback"></div><div id="auth-feedback"></div></section><section id="result-section" aria-live="polite"><div class="section-top"><div><h2>Your evening, your choice</h2><p class="muted">Choose a restaurant and date to see the tables waiting for you.</p></div></div><div class="state-card"><h3>Let’s find your favourite seat.</h3><p class="muted">Search above for single tables and approved shared seating.</p></div></section>`;
  const today = new Date(); $('date-input').value = [today.getFullYear(),String(today.getMonth()+1).padStart(2,'0'),String(today.getDate()).padStart(2,'0')].join('-');
  api('/restaurants').then(data => {
    $('restaurant-select').innerHTML = data.restaurants.map(r => `<option value="${esc(r.id)}">${esc(r.name || 'Restaurant')}</option>`).join('');
    if (!data.restaurants.length) { $('restaurant-select').innerHTML = '<option value="">No restaurants available</option>'; feedback('search-feedback','search-error','There are no restaurants to browse yet. Please check back soon.'); }
  }).catch(error => feedback('search-feedback','search-error',friendly(error)));
  $('search-form').onsubmit = event => { event.preventDefault(); runSearch({rid:$('restaurant-select').value,date:$('date-input').value,party:Number($('party-size-input').value)}); };
}
function dateLabel(day) { const [y,m,d] = day.split('-').map(Number); const value = new Date(0); value.setFullYear(y,m-1,d); value.setHours(12,0,0,0); return value.toLocaleDateString(undefined,{weekday:'short',month:'short',day:'numeric',year:'numeric'}); }
function seatingLabel(restaurant, tids) { return tids.map(id => restaurant.tables.find(t => t.id === id)?.label ?? id).join(' + '); }
async function runSearch(query, preserveBooking = false) {
  const version = ++searchVersion;
  feedback('search-feedback','search-error','');
  if (!preserveBooking) { booking = null; currentSearch = null; $('result-section').innerHTML = '<div class="loading" role="status">Finding a place for your evening…</div>'; }
  try {
    const params = new URLSearchParams({restaurant_id:query.rid,date:query.date,party_size:query.party});
    const [restaurant, availability] = await Promise.all([api('/restaurants/'+encodeURIComponent(query.rid)),api('/availability?'+params)]);
    if (version !== searchVersion) return;
    currentSearch = {query,restaurant,availability,version}; renderResults(preserveBooking);
  } catch (error) {
    if (version !== searchVersion) return;
    feedback('search-feedback','search-error',friendly(error));
    if (!preserveBooking) $('result-section').innerHTML = '<div class="state-card"><h3>We couldn’t find your tables.</h3><p>Please check the search details and try again.</p></div>';
  }
}
function renderResults(preserveBooking) {
  const {query,restaurant:r,availability} = currentSearch;
  if (!preserveBooking) $('result-section').innerHTML = '<div id="results-heading"></div><div class="results-layout"><div id="grid-area"></div><aside id="booking" aria-label="Your selected table"></aside></div>';
  $('results-heading').innerHTML = `<div class="section-top"><div><p class="eyebrow">A table for your plans</p><h2>${esc(r.name || 'Restaurant')}</h2><p class="muted">${esc(dateLabel(query.date))} · ${esc(query.party)} guests · Times in ${esc(r.timezone.replaceAll('_',' '))}</p></div><div class="legend"><span><i></i>Available</span><span><i class="taken"></i>Unavailable</span></div></div>`;
  if (!availability.slots.length) { $('grid-area').innerHTML = `<div class="state-card" ${test('no-slots')}><h3>A quiet day at ${esc(r.name || 'this restaurant')}.</h3><p class="muted">There are no booking times on this date. Try another day.</p></div>`; return; }
  const choices = r.tables.map(t => ({ids:[t.id],capacity:t.capacity}));
  for (const pair of r.combinable || []) if (availability.slots.some(s => s.available_options?.some(o => o.table_ids.length === 2 && o.table_ids.every(id => pair.includes(id))))) choices.push({ids:pair,capacity:pair.reduce((n,id)=>n+r.tables.find(t=>t.id===id).capacity,0)});
  $('grid-area').innerHTML = `<div class="seating-grid" ${test('availability-grid')}>${choices.map((choice,index) => `<article class="seating-card"><div>${choice.ids.length > 1 ? '<span class="pair-tag">Together at two tables</span>' : ''}<h3>${choice.ids.length === 1 ? 'Table ' : 'Tables '}${esc(seatingLabel(r,choice.ids))}</h3><div class="seat-meta">Up to ${esc(choice.capacity)} guests</div></div><div class="times">${availability.slots.map((slot,slotIndex) => {
    const available = choice.ids.length === 1 ? slot.available_table_ids.includes(choice.ids[0]) : slot.available_options?.some(o => o.table_ids.length === 2 && o.table_ids.every(id => choice.ids.includes(id)));
    const selected = booking && booking.r.id === r.id && booking.wall === slot.starts_at_local && booking.ids.join('|') === choice.ids.join('|');
    return `<button type="button" class="time-cell${selected ? ' selected' : ''}" ${test('slot-'+choice.ids.join('+')+'-'+slot.starts_at_local.slice(11))} data-available="${!!available}" data-choice="${index}" data-slot="${slotIndex}" ${available ? '' : 'disabled'} aria-label="${esc((choice.ids.length > 1 ? 'Tables ' : 'Table ')+seatingLabel(r,choice.ids)+', '+slot.starts_at_local.slice(11)+(available ? ', available' : ', unavailable'))}" aria-pressed="${!!selected}">${esc(slot.starts_at_local.slice(11))}</button>`;
  }).join('')}</div></article>`).join('')}</div>`;
  document.querySelectorAll('[data-choice]').forEach(button => button.onclick = () => {
    if (button.dataset.available !== 'true') return;
    if (!session) { feedback('auth-feedback','auth-error','Please sign in to reserve your table.'); $('auth-feedback').insertAdjacentHTML('beforeend','<a href="/login">Sign in →</a>'); return; }
    const choice = choices[Number(button.dataset.choice)], slot = availability.slots[Number(button.dataset.slot)];
    booking = {r,ids:[...choice.ids],wall:slot.starts_at_local,party:query.party,attempt:null}; renderBooking(); renderResults(true);
    $('booking-party-size').focus({preventScroll:true}); if (innerWidth < 900) $('booking').scrollIntoView({behavior:'smooth',block:'start'});
  });
}
function renderBooking() {
  const b = booking;
  $('booking').innerHTML = `<section class="booking-panel" ${test('booking-form')}><p class="eyebrow">Your place for the evening</p><h2>Make it a reservation.</h2><div class="booking-summary" ${test('booking-summary')}><strong>${esc(b.r.name)}</strong><br>${b.ids.length > 1 ? 'Tables ' : 'Table '}${esc(seatingLabel(b.r,b.ids))}<br>${esc(dateLabel(b.wall.slice(0,10)))} · ${esc(b.wall.slice(11))}</div><form id="book-form"><label for="booking-party-size">Guests at your table</label><input id="booking-party-size" ${test('booking-party-size')} type="number" min="1" step="1" value="${esc(b.party)}" required><div id="booking-feedback" aria-live="polite"></div><button class="primary" id="book-submit" ${test('booking-submit')}>Reserve this table</button></form><p class="fine-print">Your table is reserved for ${esc(b.r.reservation_duration_minutes)} minutes. Changes and cancellations close ${esc(b.r.cancellation_cutoff_minutes)} minutes before your reservation.</p><div id="confirmation-area"></div></section>`;
  $('booking-party-size').oninput = () => { b.attempt = null; $('confirmation-area').innerHTML = ''; feedback('booking-feedback','booking-error',''); $('book-submit').textContent = 'Reserve this table'; };
  $('book-form').onsubmit = event => { event.preventDefault(); submitBooking(b); };
}
function newKey() { if (globalThis.crypto?.randomUUID) return crypto.randomUUID(); return 'tk-'+Date.now().toString(36)+'-'+Array.from(crypto.getRandomValues(new Uint32Array(4)),n=>n.toString(16)).join(''); }
async function submitBooking(b) {
  if (!session) { feedback('booking-feedback','booking-error','Please sign in to reserve your table.'); return; }
  const body = {restaurant_id:b.r.id,starts_at_local:b.wall,party_size:Number($('booking-party-size').value),...(b.ids.length === 1 ? {table_id:b.ids[0]} : {table_ids:[...b.ids]})};
  const signature = JSON.stringify(body);
  if (!b.attempt || b.attempt.signature !== signature || b.attempt.user !== session.user_id) b.attempt = {key:newKey(),body,signature,user:session.user_id};
  const attempt = b.attempt, button = $('book-submit');
  if (attempt.inFlight) return;
  attempt.inFlight = true; button.disabled = true; button.textContent = 'Reserving…';
  feedback('booking-feedback','booking-error',''); $('confirmation-area').innerHTML = '';
  try {
    const result = await api('/reservations',{method:'POST',body:attempt.body,key:attempt.key,authenticated:true});
    if (booking !== b || b.attempt !== attempt) return;
    $('confirmation-area').innerHTML = `<div class="confirmation" ${test('confirmation')} role="status"><p class="eyebrow">You’re on the guest list</p><h3>We’ll save you a seat.</h3><p class="muted" style="font-size:12px;margin-bottom:0">Your confirmation reference</p><div class="reference" ${test('confirmation-reference')}>${esc(result.reference)}</div><div class="confirmation-details" ${test('confirmation-details')}>${esc(b.r.name)} · ${esc(seatingLabel(b.r,result.table_ids || [result.table_id]))}<br>${esc(result.starts_at_local.slice(0,10))} · ${esc(result.starts_at_local.slice(11))} · ${esc(result.party_size)} guests</div><p class="muted" ${test('confirmation-tables')}>${esc(seatingLabel(b.r,result.table_ids || [result.table_id]))}</p><a href="/lookup">Manage your reservation →</a></div>`;
    button.textContent = 'Reservation confirmed · Send again';
  } catch (error) {
    if (booking !== b || b.attempt !== attempt) return;
    if (!(error instanceof Rejection) || error.status >= 500) { feedback('booking-feedback','booking-uncertain','We couldn’t confirm the response. Your reservation may have been made. Keep these details unchanged and retry safely to recover your reference.','uncertain'); button.textContent = 'Retry this reservation'; }
    else { feedback('booking-feedback','booking-error',friendly(error)); button.textContent = 'Try this reservation again'; if (error.code === 'table_unavailable' && currentSearch?.query.rid === b.r.id) runSearch(currentSearch.query,true); }
  } finally { attempt.inFlight = false; if (booking === b) { button.disabled = false; if (b.attempt !== attempt) button.textContent = 'Reserve this table'; } }
}
function lookupScreen() {
  main.innerHTML = `<section class="lookup-header"><p class="eyebrow">Your plans, taken care of</p><h1>A table with your name on it.</h1><p class="muted">Find your reservation with the reference from your confirmation. Sign in to see your booking details.</p></section><div class="lookup-layout"><section class="form-card"><h2>Find your reservation</h2><p>One reference. All your evening’s details.</p><form id="lookup-form">${field('lookup-reference-input','Confirmation reference','text','autocomplete="off" spellcheck="false" placeholder="e.g. A1B2C3D4" required')}<button class="primary" ${test('lookup-submit')}>Find reservation →</button></form><div id="reservation-feedback"></div></section><div id="reservation-area"><div class="state-card"><h3>Looking forward to your evening?</h3><p class="muted">Your table, time and reservation status will appear here.</p></div></div></div>`;
  $('lookup-form').onsubmit = async event => {
    event.preventDefault(); const version = ++lookupVersion;
    feedback('reservation-feedback','reservation-error',''); $('reservation-area').innerHTML = '<div class="loading" role="status">Finding your reservation…</div>';
    try {
      const record = await api('/reservations/'+encodeURIComponent($('lookup-reference-input').value.trim()),{authenticated:true});
      const restaurant = await api('/restaurants/'+encodeURIComponent(record.restaurant_id));
      if (version === lookupVersion) renderReservation(record,restaurant);
    } catch(error) { if (version === lookupVersion) { $('reservation-area').innerHTML = ''; feedback('reservation-feedback','reservation-error',friendly(error)); } }
  };
}
function renderReservation(record,r) {
  $('reservation-area').innerHTML = `<section class="reservation-card" ${test('reservation-detail')}><p class="eyebrow">Your reservation</p><h2>${esc(r.name)}</h2><span class="status ${record.status}" ${test('reservation-status')}>${esc(record.status)}</span><dl class="reservation-info"><div><dt>Day</dt><dd>${esc(dateLabel(record.starts_at_local.slice(0,10)))}</dd></div><div><dt>Time · ${esc(r.timezone.replaceAll('_',' '))}</dt><dd>${esc(record.starts_at_local.slice(11))}</dd></div><div><dt>Your seating</dt><dd ${test('reservation-tables')}>${esc(seatingLabel(r,record.table_ids || [record.table_id]))}</dd></div><div><dt>Guests</dt><dd>${esc(record.party_size)}</dd></div><div><dt>Confirmation reference</dt><dd>${esc(record.reference)}</dd></div></dl>${record.status === 'confirmed' ? `<button class="secondary cancel-button" id="cancel-reservation" ${test('reservation-cancel-button')}>Cancel reservation</button><p class="muted" style="font-size:11px;margin:15px 0 0">Cancellations close ${esc(r.cancellation_cutoff_minutes)} minutes before your table is ready.</p>` : '<div class="feedback success" role="status">Your reservation is cancelled. We hope to welcome you another evening.</div>'}</section>`;
  if (record.status === 'confirmed') $('cancel-reservation').onclick = async () => {
    const version = lookupVersion; const button = $('cancel-reservation'); button.disabled = true; feedback('reservation-feedback','reservation-error','');
    try { const updated = await api('/reservations/'+encodeURIComponent(record.reference)+'/cancel',{method:'POST',authenticated:true}); if (version === lookupVersion) renderReservation(updated,r); }
    catch(error) { if (version === lookupVersion) { feedback('reservation-feedback','reservation-error',friendly(error)); button.disabled = false; } }
  };
}
renderSession();
document.querySelectorAll('[data-route]').forEach(link => { if (link.dataset.route === location.pathname) { link.classList.add('active'); link.setAttribute('aria-current','page'); } });
if (location.pathname === '/signup') authScreen(true);
else if (location.pathname === '/login') authScreen(false);
else if (location.pathname === '/lookup') lookupScreen();
else searchScreen();
