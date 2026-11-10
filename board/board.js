
const byId = id => document.getElementById(id);
const form = byId('settings');
const fields = form.elements;
const history = byId('history');
const feed = byId('transcript-list');
const activity = byId('activity-list');
const detail = byId('event-detail');
const meter = byId('meter');
const timeOfDay = new Intl.DateTimeFormat([], {hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23'});
const sessionDate = new Intl.DateTimeFormat([], {month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit'});
const meterShape = [4, 6, 9, 7, 12, 20, 15, 11, 21, 28, 35, 24, 40, 48, 36, 27, 33, 20, 30, 18, 25, 15, 20, 12, 15, 8, 11, 6, 9, 4];
const eventLabels = {
  start: 'Starting', ready: 'Streams opened', check: 'Audio check passed',
  phrase: 'Phrase recognized', playback: 'Dub played', warning: 'Warning',
  error: 'Error', stop: 'Session stopped',
};
const statusLabels = {starting: 'Loading', checking: 'Checking', listening: 'Listening', stopping: 'Stopping'};
let state;
let requestNumber = 0;
let selectedEvent;
let full = false;
let busy = false;
let connected = false;
let closed = false;
let eventKey = '';
let historyKey = '';
let initialized = false;

function element(name, text, className) {
  const node = document.createElement(name);
  if (text != null) node.textContent = text;
  if (className) node.className = className;
  return node;
}

for (let index = 0; index < meterShape.length; index++) {
  const bar = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
  bar.setAttribute('x', index * 10 + 2);
  bar.setAttribute('width', 5);
  bar.setAttribute('rx', 2.5);
  meter.append(bar);
}

function note(text) {
  byId('notice').hidden = !text;
  byId('notice').textContent = text || '';
}

function empty(title, text) {
  const card = element('div', null, 'empty');
  card.append(element('strong', title), element('p', text));
  return card;
}

function showHistory() {
  const sessions = state.recent;
  const key = sessions.map(item => item.id).join(',');
  if (historyKey === key) return;
  historyKey = key;
  const selection = history.value;
  const options = sessions.map(item => {
    const mode = item.task === 'check' ? 'Audio check' :
      item.settings.mode === 'dub' ? 'Two-way dubbing' : 'Mic transcription';
    return new Option('#' + item.id + ' · ' + sessionDate.format(item.started * 1000) + ' · ' + mode, item.id);
  });
  history.replaceChildren(new Option('Latest session', ''), ...options);
  history.value = selection;
  if (history.selectedIndex < 0) history.value = '';
}

function updateLanguageOptions() {
  const dubbing = fields.mode.value === 'dub';
  byId('language-label').textContent = dubbing ? 'Local → caller language' : 'Speech language';
  const options = byId('language').options;
  options[0].textContent = dubbing ? 'English → Mandarin' : 'English';
  options[1].textContent = dubbing ? 'Mandarin → English' : 'Mandarin';
  const report = state?.report;
  const session = report?.session;
  const matches = session?.settings.mode === fields.mode.value;
  const active = state?.active;
  const live = matches && active?.id === session.id && active.status === 'listening';
  const passed = matches && report.opened;
  const failed = matches && session.status === 'failed';
  const status = byId('routes-status');
  status.textContent = live ? (dubbing ? '4 verified' : 'Mic open') : failed ? 'Check routes' :
    passed ? 'Last opened' : 'Unchecked';
  status.dataset.tone = live || (passed && !failed) ? 'live' : failed ? 'error' : '';
  for (const route of byId('routes-list').querySelectorAll('[data-dub]')) route.hidden = !dubbing;
  byId('route-help').hidden = !dubbing;
}

function renderSession() {
  const active = state?.active;
  const report = state?.report;
  const live = active?.status === 'listening';
  const status = byId('status');
  const label = closed ? 'Closed' : !connected ? 'Disconnected' : statusLabels[active?.status] || 'Idle';
  if (status.textContent !== label) status.textContent = label;
  status.dataset.tone = live ? 'live' : '';
  const elapsed = active && state.recent.find(item => item.id === active.id);
  const seconds = elapsed ? Math.max(0, Math.floor(Date.now() / 1000 - elapsed.started)) : 0;
  const minutes = Math.floor(seconds / 60);
  byId('elapsed').textContent = String(minutes).padStart(2, '0') + ':' + String(seconds % 60).padStart(2, '0');
  const level = live && !active.muted ? Math.min(1, Math.max(0, active.level * 8)) : 0;
  for (const [index, bar] of Array.from(meter.children).entries()) {
    const height = 3 + level * meterShape[index];
    bar.setAttribute('height', height);
    bar.setAttribute('y', (54 - height) / 2);
  }
  byId('mic-status').textContent = active?.muted && live ? 'Echo guard · mic paused' :
    live ? (level > .02 ? 'Signal detected' : 'Mic open') : 'Mic idle';

  const failure = report?.session.status === 'failed' && report.events.findLast(item => item.kind === 'error');
  const messages = {
    starting: 'Opening audio and loading local models. New models can take longer on first use.',
    checking: 'Checking the audio paths for the selected mode.',
    listening: 'Text and activity are being saved on this PC.',
    stopping: 'Waiting for the engine to release the audio devices.',
  };
  const caption = closed ? 'You can close this browser tab.' : active ? messages[active.status] :
    failure ? failure.message : 'Start a session when you’re ready.';
  byId('session-caption').textContent = caption;
  byId('start').hidden = Boolean(active);
  byId('stop').hidden = !active || active.status === 'checking';
  byId('start').disabled = !connected || busy || Boolean(active);
  byId('stop').disabled = !connected || busy || !active || active.status === 'stopping';
  byId('check').disabled = !connected || busy || Boolean(active);
  byId('quit').disabled = !connected || busy || Boolean(active);
  byId('settings-fields').disabled = !connected || busy || Boolean(active);
  byId('settings-note').textContent = active ? 'Stop this session to change settings.' : 'Changes apply to the next session.';
}

function routeName(item) {
  return {'To caller': 'You → Caller', 'You hear': 'Caller → You', Microphone: 'AirPods microphone'}[item.route] || '';
}

function renderTranscript(events, record, atBottom, previousScroll) {
  const phrases = events.filter(item => item.kind === 'phrase');
  if (!phrases.length) {
    const hint = record?.task === 'check' ? 'Audio checks create activity entries only.' :
      'Recognized speech will appear here, with translations in dubbing mode.';
    feed.replaceChildren(empty('No phrases yet', hint));
  } else {
    const turns = phrases.map(item => {
      const turn = element('article', null, 'turn');
      turn.dataset.phrase = item.phrase;
      const info = element('div', null, 'turn-meta');
      const speaker = item.route === 'You hear' ? 'Caller' : 'You';
      const direction = item.target ? item.source.toUpperCase() + ' → ' + item.target.toUpperCase() : item.source.toUpperCase();
      info.append(element('strong', speaker), element('span', direction),
        element('time', timeOfDay.format(item.at * 1000)));
      const spoken = element('p', item.spoken);
      spoken.lang = item.source === 'zh' ? 'zh-Hans' : 'en';
      turn.append(info, spoken);
      if (item.translation) {
        const translation = element('p', '→ ' + item.translation, 'translated');
        translation.lang = item.target === 'zh' ? 'zh-Hans' : 'en';
        turn.append(translation);
      }
      return turn;
    });
    feed.replaceChildren(...turns);
    feed.scrollTop = atBottom ? feed.scrollHeight : previousScroll;
  }
  byId('jump-latest').hidden = !phrases.length || feed.scrollHeight <= feed.clientHeight;
}

function filteredEvents(events) {
  const filter = byId('filter').value;
  if (filter === 'audio') return events.filter(item => ['phrase', 'playback', 'ready', 'check'].includes(item.kind));
  if (filter === 'warnings') return events.filter(item => ['warning', 'error'].includes(item.kind));
  return events;
}

function renderActivity(events) {
  const visible = filteredEvents(events);
  const focusedId = activity.contains(document.activeElement) ? document.activeElement.dataset.eventId : null;
  const previousScroll = activity.scrollTop;
  if (!visible.length) {
    activity.replaceChildren(empty('No activity to show', 'Session events and audio checks are logged here.'));
  } else {
    activity.replaceChildren(...visible.toReversed().map(item => {
      const row = element('button', null, 'event');
      row.type = 'button';
      row.dataset.eventId = item.id;
      row.dataset.kind = item.kind;
      row.setAttribute('aria-pressed', String(item.id === selectedEvent));
      const description = element('span');
      description.append(element('strong', eventLabels[item.kind] || item.kind),
        element('small', routeName(item) || item.message || ''));
      row.append(element('time', timeOfDay.format(item.at * 1000)), description,
        element('span', item.delay == null ? '' : item.delay.toFixed(2) + ' s', 'event-meta'));
      return row;
    }));
    activity.scrollTop = previousScroll;
    if (focusedId) activity.querySelector('[data-event-id="' + focusedId + '"]')?.focus({preventScroll: true});
  }
}

function renderDetail(events, reveal = false, animate = false) {
  const item = filteredEvents(events).find(event => event.id === selectedEvent);
  detail.hidden = !item;
  for (const row of activity.querySelectorAll('.event')) {
    row.setAttribute('aria-pressed', String(Number(row.dataset.eventId) === item?.id));
  }
  for (const turn of feed.querySelectorAll('.highlight')) turn.classList.remove('highlight');
  if (!item) return;

  const linked = Array.from(feed.children).find(turn => item.phrase && turn.dataset.phrase === item.phrase);
  detail.replaceChildren(element('p', item.message || (eventLabels[item.kind] + ' · ' + routeName(item))));
  if (item.kind === 'playback') {
    detail.append(element('p', [
      ['Queue', item.queue], ['Recognition', item.recognition],
      ['Translation', item.translation_time], ['Voice', item.voice],
    ].filter(([, value]) => value != null).map(([label, value]) => label + ' ' + value.toFixed(2) + ' s').join(' · ')));
  }
  if (linked) {
    linked.classList.add('highlight');
    if (reveal) {
      const top = linked.getBoundingClientRect().top - feed.getBoundingClientRect().top;
      feed.scrollTop += top - 4;
    }
  } else if (item.phrase) {
    detail.append(element('p', 'The matching transcript line is outside this view. Open the full session to see earlier lines.'));
  }
  if (animate && !matchMedia('(prefers-reduced-motion: reduce)').matches) {
    detail.getAnimations().forEach(animation => animation.cancel());
    detail.animate([{opacity: .55}, {opacity: 1}], {duration: 180, easing: 'cubic-bezier(.23,1,.32,1)'});
  }
}

function render() {
  if (!initialized) {
    const settings = state.recent.find(item => item.task === 'session')?.settings || state.defaults;
    for (const [name, value] of Object.entries(settings)) {
      if (name === 'denoise') fields[name].checked = value;
      else fields[name].value = value;
    }
    initialized = true;
  }
  showHistory();
  renderSession();
  updateLanguageOptions();
  const report = state.report;
  const record = report?.session;
  const events = report?.events || [];
  const key = [record?.id, events.at(-1)?.id, events.length, byId('filter').value].join(':');
  if (key !== eventKey) {
    const atBottom = feed.scrollHeight - feed.clientHeight - feed.scrollTop < 12;
    renderTranscript(events, record, atBottom, feed.scrollTop);
    renderActivity(events);
    renderDetail(events);
    eventKey = key;
  }
  const live = record && record.id === state.active?.id && state.active.status === 'listening';
  byId('transcript-tag').textContent = live ? 'Live' : record ? 'Session #' + record.id : 'Latest session';
  byId('transcript-tag').dataset.tone = live ? 'live' : '';
  const exportLink = byId('export');
  exportLink.hidden = !record;
  if (record) exportLink.href = '/api/report?session=' + record.id;
  byId('phrase-count').textContent = report?.metrics.phrases ?? '—';
  const delay = report?.metrics.median_delay;
  const metric = byId('delay');
  metric.textContent = delay == null ? '—' : delay.toFixed(1);
  if (delay != null) metric.append(element('span', ' s', 'unit'));
  byId('report-note').textContent = record?.task === 'check' ? 'Audio check · ' + record.status :
    record?.settings.mode === 'transcribe' ? 'Microphone transcription · No dubbing' :
    record ? report.metrics.dubbed + ' dubs played · Delay from capture to playback' :
    'Start a session to see its report.';
  byId('full-log').hidden = !report?.truncated && !full;
  byId('full-log').textContent = full ? 'Show recent events' : 'Show full session';
}

async function refresh() {
  if (closed) return;
  const request = ++requestNumber;
  const query = new URLSearchParams();
  if (history.value) query.set('session', history.value);
  if (full) query.set('full', '1');
  try {
    const response = await fetch('/api/state?' + query);
    if (!response.ok) throw new Error();
    const next = await response.json();
    if (request !== requestNumber || closed) return;
    if (!connected) note('');
    connected = true;
    state = next;
    render();
  } catch {
    if (request !== requestNumber || closed) return;
    connected = false;
    note('Connection lost. Reopen the πlingual launcher if the board does not reconnect.');
    renderSession();
  }
}

async function action(name, body = {}) {
  if (busy) return;
  busy = true;
  note('');
  renderSession();
  try {
    const response = await fetch('/api/' + name, {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error);
    if (name === 'quit') {
      closed = true;
      connected = false;
      note('Control board closed. You can close this tab.');
    } else if (result.session) {
      history.value = '';
      full = false;
      selectedEvent = undefined;
    }
  } catch (error) {
    note(error.message);
  } finally {
    busy = false;
    renderSession();
    await refresh();
  }
}

function startTask(task) {
  if (!form.reportValidity()) return;
  action(task, {
    mode: fields.mode.value, language: fields.language.value, model: fields.model.value,
    pause: fields.pause.valueAsNumber, seconds: fields.seconds.valueAsNumber, denoise: fields.denoise.checked,
  });
}

form.addEventListener('submit', event => event.preventDefault());
fields.mode.addEventListener('change', updateLanguageOptions);
byId('start').addEventListener('click', () => startTask('start'));
byId('check').addEventListener('click', () => startTask('check'));
byId('stop').addEventListener('click', () => action('stop'));
byId('quit').addEventListener('click', () => action('quit'));
byId('jump-latest').addEventListener('click', () => { feed.scrollTop = feed.scrollHeight; });
byId('full-log').addEventListener('click', () => { full = !full; refresh(); });
history.addEventListener('change', () => {
  full = false;
  selectedEvent = undefined;
  feed.scrollTop = feed.scrollHeight;
  activity.scrollTop = 0;
  refresh();
});
byId('filter').addEventListener('change', () => { if (state) render(); });
activity.addEventListener('click', event => {
  const row = event.target.closest('.event');
  if (!row || !state.report) return;
  const id = Number(row.dataset.eventId);
  selectedEvent = selectedEvent === id ? undefined : id;
  renderDetail(state.report.events, true, event.detail > 0);
});
refresh();
setInterval(refresh, 800);

