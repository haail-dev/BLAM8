// Shared deliberate gesture. A completed hold consumes its subsequent click;
// close, focus loss, cancellation and hidden pages cannot fire a pending action.
// `armed` (optional) is asked each time a hold would start: while it says no, nothing starts, so a long
// press on Mist in hold-to-talk mode talks instead of ending the conversation.
export function installHoldAction(button, action, {armed} = {}) {
  let timer = 0, active = false, consumed = false;
  const cancel = () => { clearTimeout(timer); timer = 0; active = false; button.classList.remove('is-holding'); };
  const begin = () => {
    if (active) return;
    if (armed && !armed()) { consumed = false; return; }
    cancel(); active = true; consumed = false; button.classList.add('is-holding');
    timer = setTimeout(() => {
      timer = 0; active = false; consumed = true; button.classList.remove('is-holding'); action();
    }, 850);
  };
  button.addEventListener('pointerdown', event => { if (event.button === 0) begin(); });
  for (const type of ['pointerup','pointerleave','pointercancel','blur']) button.addEventListener(type, cancel);
  button.addEventListener('keydown', event => {
    if (event.key === 'Escape') { cancel(); return; }
    if (event.key !== ' ' && event.key !== 'Enter') return;
    event.preventDefault(); if (!event.repeat) begin();
  });
  button.addEventListener('keyup', event => { if (event.key === ' ' || event.key === 'Enter') {event.preventDefault();const tap=active && !consumed;cancel();if(tap)button.click();} });
  button.addEventListener('click', event => {
    if (consumed) { event.preventDefault(); event.stopImmediatePropagation(); }
  }, true);
  button.addEventListener('contextmenu', event => event.preventDefault());
  document.addEventListener('visibilitychange', () => {if(document.hidden)cancel();});
  window.addEventListener('blur', cancel);
  return cancel;
}

// Native GPT-Live: media tracks carry audio; captions never execute business work.
// Carry the app entry's deployment revision through the module graph. The
// installed service worker caches /static URLs by exact URL, including imports.
// An unversioned module would otherwise keep its old renderer across refreshes.
const revision = new URL(import.meta.url).search;
const [{ OVERLAY_CSS }, { createVoiceMeter }, { createGlassCore }, { BLAM8_MOTION_MARK }, { createTalkController, installTalkGestures, savedTalkMode },
  { createLineGate, savedLiveLine, LINE_BROKEN_REASONS, LINE_BROKEN_MS, LINE_BELOW_MS, LINE_PRESS_OPEN_MS }, { createPond }] = await Promise.all([
  import('./voice-overlay-css.js' + revision),
  import('./voice-meter.js' + revision),
  import('./glass-core.js' + revision),
  import('./blam8-mark-motion.js' + revision),
  import('./voice-talk.js' + revision),
  import('./voice-line.js' + revision),
  import('./voice-pond.js' + revision),
]);

// What the microphone is asked for. Echo cancellation and noise suppression stay on. Automatic gain stays
// on too, unless the person has set a volume line: the line is compared with the level, and a browser that
// keeps re-levelling the mic would move it. concierge_panel.js asks for the same thing when it opens the mic
// ahead of this screen (startPreMic), and a test pins the two equal.
export function captureConstraints(agcOff = false) {
  return {audio: {echoCancellation: true, noiseSuppression: true, autoGainControl: !agcOff}};
}

export function createNativeAudio() {
  // Keep remote media in the document for mobile WebKit playback/routing.
  const audio=document.createElement('audio');audio.autoplay=true;audio.playsInline=true;
  audio.setAttribute('playsinline','');audio.volume=1;
  audio.style.display='none';document.body.appendChild(audio);return audio;
}

export function createNativeTransport(deps) {
  let generation = 0, owner = null, microphoneMuted = false, visualActive = true;
  let inputConfig = {talkMode:'hands_free',line:null,agcOff:false};   // how this device has chosen to talk and what the room must reach; read by the screen when it opens
  let lastTimings = [];
  const now = () => globalThis.performance?.now?.() ?? Date.now();
  const timings = o => [...o.timings,{stage:o.stage,duration_ms:Math.max(0,Math.round((o.readyAt ?? now())-o.since))}];
  function stage(o, name) {
    const at=now();
    if(o.stage)o.timings.push({stage:o.stage,duration_ms:Math.max(0,Math.round(at-o.since))});
    o.stage=name;o.since=at;
    deps.status(({capture:'Waiting for microphone access…',offer:'Preparing voice connection…',ice:'Connecting microphone…',create:'Starting GPT-Live session…',answer:'Connecting GPT-Live audio…',awaiting_ready:'Waiting for GPT-Live audio…'})[name]);
  }
  const deadline = (promise, ms = 20000) => {
    let timer;
    return Promise.race([promise, new Promise((_, reject) => {timer = setTimeout(() => reject(Error('Voice request timed out')), ms);})])
      .finally(() => clearTimeout(timer));
  };
  // What the browser sends is the raw microphone track, switched on and off here and nowhere else: this is
  // the only place `enabled` is written. It is on unless the person muted explicitly, or the way they are
  // choosing to talk has the mic closed right now (sending(), which only ever adds reasons to stay silent).
  // Mute always wins. The flag is written every time, not only when it changes: WebKit has had a bug where
  // a track that was never explicitly set kept its old state. `refresh` writes a closed mic as open and then
  // closed once more, for the moment a connection comes up (Safari 18 published a disabled track until it was toggled).
  const sending = o => o.talk ? o.talk.talking() : o.gate ? o.gate.isOpen() : true;
  function applyInput(o, refresh=false) {
    if(!o?.media)return;
    const on=!microphoneMuted && sending(o);
    for (const track of o.media.getAudioTracks()) for (const value of refresh && !on ? [true,on] : [on]) track.enabled=value;
  }
  // What the screen needs to know about how the person is talking: told when it changes (and on a reason,
  // such as the conversation having just started), never on a timer.
  // `line` is "off" (no volume line), "on" (set and working) or "unavailable" (set but this phone cannot measure
  // the room right now, so everything is being sent); `below` is true once the line has been shutting sound out
  // for a moment; `near` counts sounds that were close to the line and not sent.
  function inputView(o) {
    const at=now(), gate=o?.gate;
    const line=!gate ? 'off' : o.brokenSince!==null && at-o.brokenSince>=LINE_BROKEN_MS ? 'unavailable' : 'on';
    return {mode:inputConfig.talkMode,talk:o?.talk?.state() || 'idle',line,below:Boolean(gate && gate.closedFor(at)>=LINE_BELOW_MS),near:gate ? gate.nearMisses() : 0};
  }
  function inputChanged(o, why='') {
    if(owner!==o)return;
    applyInput(o);
    const view=inputView(o), key=JSON.stringify(view);
    if(key===o.viewKey && !why)return;
    o.viewKey=key;deps.input?.(view,why);
  }
  const tapLive = o => Boolean(o.tap?.getAudioTracks?.().some(track=>track.readyState==='live' && track.muted!==true));
  // The caller's tick for anything that needs one: a closing tail, a press that has gone on for ever, and the volume
  // line, which reads the room through the copy of the track and shuts or opens the sent one. Mute is the person's
  // and wins, so the line does not run while muted.
  function tickInput(o) {
    if(owner!==o)return;
    const at=now();
    o.talk?.tick(at);
    if(o.gate && !microphoneMuted){
      const meter=o.meter;
      const verdict=o.gate.step({peak:meter?.peakLevel?.() ?? null,contextState:meter?.contextState?.() ?? null,hidden:Boolean(globalThis.document?.hidden),
        tapAvailable:Boolean(o.tap),tapLive:tapLive(o),started:Boolean(o.started)},at);
      if(LINE_BROKEN_REASONS.includes(verdict.reason)){if(o.brokenSince===null)o.brokenSince=at;}else o.brokenSince=null;
    }
    inputChanged(o);
  }
  // A copy of the microphone track for the meter to read. The sent track is disabled whenever the mic is
  // muted or closed (and a disabled track hands every listener silence), so measuring the sent track
  // would make the room look silent exactly when it has to be watched. A clone is not a second capture:
  // the browser opens the microphone once. No clone support, or any failure, means no copy (null) and the
  // meter falls back to the sent track. `copy.enabled=true` is set explicitly: a clone of a disabled track
  // does not always report the state it was given.
  function tapOf(media) {
    const copies=[];
    try {
      if(typeof MediaStream!=='function')return null;
      for (const track of media?.getAudioTracks?.() || []) {
        if(typeof track.clone!=='function'){for(const made of copies)made.stop();return null;}
        const copy=track.clone();copy.enabled=true;copies.push(copy);
      }
      return copies.length ? new MediaStream(copies) : null;
    } catch(_) {for(const made of copies)try{made.stop();}catch(__){}return null;}
  }
  function cleanup(o) {
    if (!o) return;
    clearTimeout(o.readyTimer);clearInterval(o.levelTimer);
    lastTimings=timings(o);
    o.abort.abort();
    o.meter?.destroy();
    for (const track of o.media?.getTracks() || []) track.stop();
    for (const track of o.tap?.getTracks() || []) track.stop();   // the copy holds the microphone open until it is stopped too
    clearInterval(o.inputTimer);
    o.channel?.close();
    o.peer?.close();
    o.audio.pause();o.audio.srcObject = null;o.audio.remove?.();
  }
  let closing = Promise.resolve(), acquiring = Promise.resolve(), pendingStart = null, pendingProvider = null, pendingProviderOwner = null, isClosing = false;
  const ready = o => Boolean(o?.started && o.peer?.connectionState==='connected' && o.channel?.readyState==='open' && o.playback==='playing');
  const rememberReady = o => {if(ready(o) && !o.reachedReady){o.reachedReady=true;clearTimeout(o.readyTimer);o.readyAt=now();}};
  const controlId = () => globalThis.crypto?.randomUUID?.() || '';
  function playbackEvent(o, action, reason='unknown') {
    o.playbackSequence=Math.min(65535,(o.playbackSequence || 0)+1);
    o.playbackEvents.push({sequence:o.playbackSequence,observed_at:new Date().toISOString(),action,reason,
      silence_reasons:[...o.silences],playback_state:o.playback || 'unknown',guard_seq:o.guardSeq || null});
    if(o.playbackEvents.length>32){o.playbackEvents.shift();o.playbackEventsOmitted=Math.min(65535,o.playbackEventsOmitted+1);}
  }
  // What the microphone was actually given, from the track the browser opened (the request is only
  // a wish). Booleans and small integers only, null when the browser does not say.
  function captureSettings(stream) {
    try {
      const track=stream?.getAudioTracks?.()[0], s=typeof track?.getSettings==='function' ? track.getSettings() : null;
      if(!s)return null;
      const flag=v=>typeof v==='boolean' ? v : null, count=(v,max)=>Number.isInteger(v) && v>=0 && v<=max ? v : null;
      return {echoCancellation:flag(s.echoCancellation),noiseSuppression:flag(s.noiseSuppression),autoGainControl:flag(s.autoGainControl),
        channelCount:count(s.channelCount,32),sampleRate:count(s.sampleRate,384000)};
    } catch(_) {return null;}
  }
  // The loudness shape of the room while the conversation is live and the mic is open: RMS of the
  // input, counted into eight buckets, plus a ninth count of samples that could not be read (a
  // suspended AudioContext). A count of levels, never audio; this histogram gates nothing (the volume line
  // reads its own statistic). It reads the copy of the track, so a muted or closed mic still has its room measured;
  // an explicit mute is skipped, as it always was.
  const LEVEL_EDGES = [.01,.02,.04,.08,.16,.32,.64];
  function sampleLevel(o) {
    if(!o.levels || !o.started || microphoneMuted)return;
    const rms=o.meter?.sampleLevel?.();
    let bucket=LEVEL_EDGES.length+1;
    if(typeof rms==='number' && Number.isFinite(rms)){bucket=0;while(bucket<LEVEL_EDGES.length && rms>=LEVEL_EDGES[bucket])bucket++;}
    o.levels[bucket]=Math.min(65535,o.levels[bucket]+1);
  }
  const closeMeta = (o, reason='unknown') => o.closeMeta ||= {close_reason:reason,
    startup_stage:ready(o)?'ready':(o.stage || 'unknown'), peer_state:o.peer?.connectionState || 'unknown', channel_state:o.channel?.readyState || 'unknown',
    reached_ready:Boolean(o.reachedReady), playback_state:o.playback || 'unknown',silence_reasons:[...o.silences],
    talk:o.talk ? o.talk.report() : null,line:o.gate ? o.gate.report({agcRequestedOff:inputConfig.agcOff}) : null,
    control_id:controlId(),playback_events:o.playbackEvents.slice(),playback_events_omitted:o.playbackEventsOmitted,
    capture:o.capture || null,levels:o.levels ? o.levels.slice() : null};
  function close(reason='unknown') {
    const acquisition=acquiring, pending=closeOwned(reason);
    const result=Promise.all([closing,pending,acquisition]);closing=result;isClosing=true;
    result.finally(()=>{if(closing===result){isClosing=false;deps.visualChange?.();}});
    return result;
  }
  function start(voiceSession) {
    if(pendingProvider){
      const retained=pendingProvider, previous=pendingProviderOwner, expected=generation;
      deps.status('Checking whether the previous connection has closed…');
      return deadline(deps.request('control',{session:previous.voiceSession,operation:'close',...previous.closeMeta}),6000)
        .then(async response=>{
          const result=await deadline(response.json(),6000);
          if(expected!==generation)return;
          if(response.ok && result.ok && result.retry_allowed===true && voiceSession!==previous.voiceSession){
            if(pendingProvider===retained)pendingProvider=null;
            return start(voiceSession);
          }
          deps.status('Previous connection is still closing. Hold Mist to check again; results remain in chat.');
        }).catch(()=>deps.status('Could not confirm the previous connection closed. Hold Mist to check again.'));
    }
    if(pendingStart && !isClosing)return Promise.resolve(owner?.session);
    const expected=generation, prior=acquiring;
    const pending=Promise.all([closing,prior]).then(()=>{if(expected===generation)return startOwned(voiceSession);});
    pendingStart=pending;acquiring=pending.catch(()=>{});
    pending.finally(()=>{if(pendingStart===pending)pendingStart=null;});return pending;
  }
  async function closeOwned(reason) {
    const o = owner;if(o)closeMeta(o,reason);if(o?.closeMeta.close_reason==='idle' && o.idleSeconds!==undefined)o.closeMeta.idle_seconds=o.idleSeconds;
    ++generation;owner = null;deps.playback?.('closed');
    if (o) deps.sessionEnded?.(o.voiceSession);
    deps.visualChange?.();
    cleanup(o); // local silence and mic release precede any network await
    if (o) {
      try { await deadline(deps.request('control', {session:o.session || o.voiceSession, operation:'close',...o.closeMeta}), 6000); }
      catch (_) { deps.status('Conversation closed locally; provider final usage is unconfirmed.'); }
      finally { deps.sessionClosed?.(o.voiceSession); }
    }
  }
  async function startOwned(voiceSession) {
    if (owner) return owner.session;
    const o = {generation:++generation, voiceSession, abort:new AbortController(), audio:deps.audio(), silences:new Set(), reachedReady:false,playbackEvents:[],playbackEventsOmitted:0,timings:[],since:now()};
    owner = o;
    // Hold to talk: the controller starts idle, so the sent track is disabled below, before it reaches the peer.
    if(inputConfig.talkMode==='hold')o.talk=createTalkController({now,onChange:(state,reason)=>inputChanged(o,reason==='watchdog' ? reason : '')});
    // The volume line: only when the owner set one on this device, and never together with hold to talk.
    o.gate=!o.talk && inputConfig.line ? createLineGate({line:inputConfig.line}) : null;o.brokenSince=null;
    o.meter=createVoiceMeter();o.meter.setActive(visualActive);
    const current = () => owner === o && generation === o.generation;
    const playback = state => {if(current()){o.playback=state;rememberReady(o);playbackEvent(o,'playback');deps.playback?.(state);deps.visualChange?.();}};
    playback('waiting');
    o.audio.addEventListener?.('pause',()=>{if(current() && !o.audio.muted){playback('blocked');deps.status('Audio paused. Tap Retry audio.');}});
    const play = () => {
      if(o.audio.muted)return Promise.resolve();
      const attemptId=o.playAttempt=(o.playAttempt || 0)+1;
      let attempt;
      try {attempt=o.audio.play();} catch(error) {attempt=Promise.reject(error);}
      return Promise.resolve(attempt).then(() => {
        if(current() && attemptId===o.playAttempt && !o.audio.muted){playback('playing');deps.status('Speaker enabled — GPT-Live');}
      }, error => {
        if(current() && attemptId===o.playAttempt && !o.audio.muted){
          playback('blocked');
          deps.status(error?.name==='NotAllowedError' ? 'Audio blocked by browser. Tap Retry audio.' : 'Audio could not play. Tap Retry audio.');
        }
      });
    };
    o.play = play;
    try {
      deps.timing?.('microphone-start');
      stage(o,'capture');const media = deps.media();
      media.then(stream => {if(!current())for(const track of stream.getTracks())track.stop();},()=>{});
      o.media = await deadline(media);
      if (current()) deps.timing?.('microphone-end');
      if (!current()) {cleanup(o);return;}
      o.tap = tapOf(o.media);o.tapAvailable = Boolean(o.tap);   // before the sent track can be disabled
      applyInput(o);deps.visualChange?.();
      o.meter?.input(o.tap || o.media); // Observe the already-owned mic (through a copy of its track); never capture a second stream.
      o.capture = captureSettings(o.media);
      o.levels = new Array(LEVEL_EDGES.length + 2).fill(0);
      o.levelTimer = setInterval(() => sampleLevel(o), 250);o.levelTimer?.unref?.();
      if(o.talk || o.gate){o.inputTimer = setInterval(() => tickInput(o), 25);o.inputTimer?.unref?.();}   // no timer unless an input control needs one
      o.peer = deps.peer();
      for (const track of o.media.getTracks()) o.peer.addTrack(track, o.media);
      o.peer.ontrack = event => {
        if (!current()) return;
        o.audio.srcObject = event.streams?.[0] || new MediaStream([event.track]);
        o.meter?.output(o.audio.srcObject);
        play();
      };
      o.peer.onconnectionstatechange = () => {
        if(current())rememberReady(o);
        if(current() && o.talk && o.peer.connectionState==='connected')applyInput(o,true);
        deps.visualChange?.();
        if (current() && ['failed','disconnected','closed'].includes(o.peer.connectionState)) {
          deps.failure?.();deps.status('GPT-Live disconnected. Hold Mist to reconnect; results remain in chat.');
          close('transport_terminal');
        }
      };
      o.channel = o.peer.createDataChannel('oai-events');
      const channelFailed=()=>{if(current()){deps.failure?.();deps.visualChange?.();deps.status('GPT-Live disconnected. Hold Mist to reconnect; results remain in chat.');close('transport_terminal');}};
      o.channel.onclose=channelFailed;o.channel.onerror=channelFailed;
      o.channel.onopen=()=>{if(current()){rememberReady(o);deps.visualChange?.();}};
      o.channel.onmessage = event => {
        if (!current()) return;
        let message;try {message = JSON.parse(event.data);} catch (_) {return;}
        if (message.type === 'session.started') {o.started = true;rememberReady(o);deps.sessionStarted?.(voiceSession, o.allowance, o.context, o.idleLimit);deps.visualChange?.();deps.status(o.playback==='blocked'?'Audio blocked. Tap Retry audio.':'Listening — GPT-Live');inputChanged(o,'started');}
        if (!o.started) return;
        if (message.type === 'session.closed') {deps.failure?.();deps.status('Conversation ended. Hold Mist to start again; results remain in chat.');close('provider_closed');return;}
        if (message.type === 'error') {deps.failure?.();deps.status('A voice update was not confirmed; check chat.');}
        if (message.type === 'session.input_transcript.delta' || message.type === 'session.output_transcript.delta') deps.caption(message);
        // No delegation handling and NO client commands: server permissions
        // explicitly disallow data-channel writes, even from a modified client.
      };
      stage(o,'offer');const offer = await deadline(o.peer.createOffer());
      if (!current()) {cleanup(o);return;}
      await deadline(o.peer.setLocalDescription(offer));
      if (!current()) {cleanup(o);return;}
      stage(o,'ice');if (o.peer.iceGatheringState !== 'complete') {
        await deadline(new Promise(resolve => {
          const done = () => {if(o.peer.iceGatheringState === 'complete') {o.peer.removeEventListener('icegatheringstatechange',done);resolve();}};
          o.peer.addEventListener('icegatheringstatechange',done);o.abort.signal.addEventListener('abort',resolve,{once:true});done();
        }), 10000);
      }
      if (!current()) {cleanup(o);return;}
      // Do not abort the create HTTP exchange on local close: retain its answer
      // solely to close any created session. The scoped close marker also covers
      // cancellation before the server returns a session ID.
      const contextFrom=deps.contextFrom?.() || null;
      stage(o,'create');o.creating=deps.request('session', {sdp:o.peer.localDescription.sdp, voice_session:voiceSession, context_from:contextFrom});
      const response = await deadline(o.creating);
      o.parsing=response.json();const result = await deadline(o.parsing);
      if (!response.ok || !result.session || !result.transport?.sdp) throw Error(result.detail || 'GPT-Live could not connect.');
      o.session = result.session;
      o.allowance = result.allowance || null;
      o.context = Array.isArray(result.context) ? result.context : [];
      o.idleLimit = Number.isInteger(result.idle_close_seconds) && result.idle_close_seconds > 0 ? Math.min(result.idle_close_seconds, 1800) : 0;
      if (!current()) {cleanup(o);await deadline(deps.request('control',{session:o.session,operation:'close',...closeMeta(o)}),6000);return;}
      stage(o,'answer');await deadline(o.peer.setRemoteDescription({type:'answer',sdp:result.transport.sdp}));
      if (!current()) {cleanup(o);return;}
      if(!o.reachedReady)stage(o,'awaiting_ready');
      if(!o.reachedReady)o.readyTimer=setTimeout(()=>{
        if(!current() || o.reachedReady)return;
        // Autoplay rejection has an explicit Retry audio action. It is only
        // recoverable here when every transport prerequisite and track exists.
        if(o.started && o.peer.connectionState==='connected' && o.channel.readyState==='open' && o.audio.srcObject && o.playback==='blocked')return;
        deps.failure?.();deps.status('GPT-Live audio did not become ready. Hold Mist to reconnect; results remain in chat.');
        close('start_failure');
      },20000);
      return o.session;
    } catch (error) {
      if (current() && o.stage === 'capture') deps.timing?.('microphone-error');
      if (current()) {deps.failure?.();deps.status(error.message==='Voice request timed out'
        ? (o.stage==='capture' ? 'Microphone permission timed out. Allow access, then hold Mist to retry.' : 'GPT-Live timed out during '+o.stage+'. Hold Mist to reconnect; results remain in chat.')
        : error.message || 'GPT-Live could not connect.');}
      closeMeta(o,'start_failure');const owned=current();
      if(owned){owner=null;++generation;deps.playback?.('closed');}cleanup(o);if(owned)deps.visualChange?.();
      // A timed-out exchange may still answer. Retain it for cleanup only,
      // never attach it or retry a business/provider request.
      if(o.creating && !o.session){
        const retained=Promise.resolve(o.parsing || o.creating.then(response=>response.json())).then(async result=>{
          if(!current() && result?.session && !o.session){o.session=result.session;await deadline(deps.request('control',{session:o.session,operation:'close',...o.closeMeta}),6000).catch(()=>{});}
        }).catch(()=>{}).finally(()=>{if(pendingProvider===retained){pendingProvider=null;deps.visualChange?.();}});
        pendingProvider=retained;
        pendingProviderOwner=o;
      }
      // Close by owned application UUID also covers a lost create response.
      await deadline(deps.request('control',{session:o.session || voiceSession,operation:'close',...o.closeMeta}),6000).catch(()=>{});
    }
  }
  return {start,close,
    // `muted` is the person's explicit mute, so the orb only goes red for that. The "you" level is zero
    // whenever nothing is being sent, whatever the reason.
    // `talking` (Mist held, or let go a moment ago) and `holdMode` are for the idle clock and its warning line. A
    // latched mic is not `talking`: it is open but nobody is holding anything, so the idle clock watches it
    // through the level, as it does a hands-free mic, and can still warn and close.
    signals() {
      const o=owner;
      const seen=o?.meter?.sample(o.audio,microphoneMuted || !sending(o),o.playback==='playing') || {agent:0,user:0,muted:microphoneMuted,agentPlaying:false,inputActive:false,analysisAvailable:false};
      return {...seen,muted:microphoneMuted,talking:Boolean(o?.talk?.holding()),holdMode:inputConfig.talkMode==='hold'};
    },
    setVisualActive(on){visualActive=Boolean(on);owner?.meter?.setActive(visualActive);},
    inputAvailable:()=>Boolean(owner?.media?.getAudioTracks().some(track=>track.readyState==='live')),
    // Muted means the person muted it: a mic that is closed for another reason (a released Mist, a line) is not.
    inputMuted(){const tracks=owner?.media?.getAudioTracks().filter(track=>track.readyState==='live') || [];return Boolean(microphoneMuted && tracks.length && tracks.every(track=>!track.enabled));},
    session:()=>owner?.session, started:()=>Boolean(owner?.started), active:()=>Boolean(owner), closing:()=>isClosing || Boolean(pendingProvider), settled:()=>Promise.all([closing,acquiring]),
    ready:()=>ready(owner),
    // Whether a hold-to-talk press can be taken: the session has started and the connection is up. Not whether the voice
    // is audible: a blocked or guard-silenced speaker does not stop the microphone, and hands-free keeps listening
    // through those states, so hold to talk must be able to open the mic in them too.
    inputReady:()=>Boolean(owner?.started && owner.peer?.connectionState==='connected' && owner.channel?.readyState==='open'),
    startupTimings:()=>owner ? timings(owner) : lastTimings.slice(),
    setMuted(on) {microphoneMuted=Boolean(on);if(microphoneMuted)owner?.talk?.cancel('mute');applyInput(owner);},
    // How the person talks and what the room must reach, per-device settings the screen reads when it opens.
    // talkMode "hold": the mic is open only while Mist is held, or latched by a tap; otherwise "hands_free"
    // (open all the time, the usual). `line` is the owner's volume line (0 to 1 exclusive) or none; it is not
    // used together with hold to talk. `agcOff` says automatic gain was asked off because a line is set.
    configureInput({talkMode,line,agcOff}={}) {
      const hold=talkMode==='hold', set=!hold && typeof line==='number' && line>0 && line<1 ? line : null;
      inputConfig={talkMode:hold ? 'hold' : 'hands_free',line:set,agcOff:Boolean(set && agcOff)};
    },
    // A press or tap on Mist holds the volume line open for a few seconds: the person is plainly using the screen.
    noteMist() {const o=owner;if(o?.gate){o.gate.forceOpen(LINE_PRESS_OPEN_MS,now(),'press');tickInput(o);}},
    inputMode:()=>inputConfig.talkMode,
    inputView:()=>inputView(owner),
    talkPress:(id,options)=>Boolean(!microphoneMuted && owner?.talk?.press(id,options)),   // a muted conversation takes no press
    talkRelease:id=>owner?.talk?.release(id) ?? null,
    talkCancel:reason=>Boolean(owner?.talk?.cancel(reason)),
    // The screen ended the conversation for idleness: the close report carries how many seconds.
    noteIdle(seconds) {if(owner)owner.idleSeconds=Math.max(0,Math.min(86400,Math.round(Number(seconds)) || 0));},
    controlId,
    silence(reason='user', guardSeq=null) {if(owner){const changed=!owner.silences.has(reason) || (guardSeq && guardSeq!==owner.guardSeq);owner.silences.add(reason);owner.audio.muted=true;owner.playAttempt=(owner.playAttempt || 0)+1;owner.playback='muted';if(guardSeq)owner.guardSeq=guardSeq;if(changed)playbackEvent(owner,'silence',reason);deps.playback?.('muted');deps.visualChange?.();}},
    recoverConnection() {if(owner && owner.silences.delete('connection')){playbackEvent(owner,'recover_connection','connection');if(!owner.silences.size){owner.audio.muted=false;return owner.play();}}},
    // With a volume line set, the room is measured whether or not the orb is being drawn, so a gesture resumes the
    // analysis even then; without one the visual flag still decides, as before.
    resumeAnalysis() {owner?.meter?.resume(Boolean(owner.gate));},
    resume() {if(owner){owner.meter?.resume();owner.silences.delete('user');owner.silences.delete('guard');playbackEvent(owner,'resume','user');if(!owner.silences.size){owner.audio.muted=false;return owner.play();}}},
  };
}

// ── Idle auto-close ──────────────────────────────────────────────────────────────────────────────
// A live conversation that nobody is using still bills live-voice minutes. This block is the whole
// idea: a conversation is idle when nobody is speaking, nothing is working and the page is not
// mid-start; a muted microphone is simply idle. Ten seconds before the cut a status line warns
// (a tap, a key, speech or any new activity starts the count again); at the limit the screen ends
// the conversation the way a hold on Mist does (paused, with a line saying why, close reason
// "idle"). The length comes from the server with the session (GPTLIVE_IDLE_CLOSE_SECONDS; 0 = off)
// and doubles while a card, draft or result is waiting for the person. Nothing here reads or
// stores speech: it counts seconds and looks at which things are busy.
export const IDLE_WARN_LEAD = 10;          // seconds of warning before the close
export const IDLE_AGENT_LEVEL = .05;       // output level (0-1, after the room-tone floor) that counts as the voice speaking
export const IDLE_USER_LEVEL = .05;        // input level (same scale) that counts as the person speaking, words not yet arrived
export const IDLE_LIMIT_CAP = 1800;        // seconds; the server's conversation lifetime, the most any idle allowance can be
export const IDLE_WAITING_FACTOR = 2;      // the allowance is multiplied by this while a card is waiting for the person
export const IDLE_STALL_SECONDS = 300;     // a turn or task "working" this long without finishing stops holding the clock (concierge_task_stall_minutes)
export const IDLE_SPEECH_CHARS_PER_SECOND = 15;   // how fast the voice is taken to speak, to estimate how long a reply keeps talking
export const IDLE_SPEECH_CAP_SECONDS = 120;       // the most speaking time that can be queued up ahead
export const idleWarning = (left, hold = false) => `Closing in ${left} seconds to save your minutes. ${hold ? 'Hold Mist to stay.' : 'Tap the screen to keep going.'}`;
export const idleEndedNote = 'Ended after a quiet spell to save your minutes. Hold Mist to continue; results remain in chat.';

// A task row the conversation is still waiting on: queued, working, or an approved change that is
// executing. A card waiting for the person's approval is not: that waiting is theirs (see
// taskAwaitsPerson), and the result stays in chat if the conversation ends. A stalled or expired
// row has stopped moving.
export function taskLive(task) {
  return task.proposal_state === 'executing' || (!taskSettled(task) && (task.status === 'queued' || task.status === 'working'));
}

// A card, draft or result waiting for the person to act (the "needs you" tier), not yet answered,
// expired or stalled. While one is up the idle allowance is longer.
export function taskAwaitsPerson(task) {
  return !taskSettled(task) && taskTier(task) === 0;
}

// Roughly how long the voice takes to say `text`, in seconds.
export function speechSeconds(text) {
  return typeof text === 'string' ? text.length / IDLE_SPEECH_CHARS_PER_SECOND : 0;
}

// The turns and tasks that are working right now, by id ("turn:..." / "task:...").
export function workingIds(activities, tasks) {
  const ids = [];
  for (const [id, item] of activities) if (item.state === 'running') ids.push('turn:' + id);
  for (const [id, task] of tasks) if (taskLive(task)) ids.push('task:' + id);
  return ids;
}

// True while any of `ids` has been working for less than `stallSeconds`. `seen` (id -> when it was
// first seen working, ms) is kept by the caller between calls; ids that are gone are forgotten, so
// a row that stops reporting finished cannot hold the clock for ever.
export function anyFresh(ids, seen, nowMs, stallSeconds = IDLE_STALL_SECONDS) {
  const current = new Set(ids);
  for (const id of [...seen.keys()]) if (!current.has(id)) seen.delete(id);
  let fresh = false;
  for (const id of current) {
    if (!seen.has(id)) seen.set(id, nowMs);
    if (nowMs - seen.get(id) < stallSeconds * 1000) fresh = true;
  }
  return fresh;
}

// True while something is going on that the idle clock must not count against the conversation.
// `eventMoved` is "the server sent a new event since last looked" (a question, result, status);
// `working` is anyFresh() over workingIds(); the levels are 0-1 from the screen's own analyser (0
// when it is not running); a muted microphone's level is not the person speaking. `talking` is hold to
// talk: Mist is being held (or was a moment ago). A mic left open by a tap is not "talking": it is idle-watched
// through the level like any open mic.
export function conversationBusy({started, eventMoved, working, agentLevel, userLevel, muted, talking}) {
  if (!started || eventMoved || working || talking) return true;
  if (Number(agentLevel) > IDLE_AGENT_LEVEL) return true;
  return !muted && Number(userLevel) > IDLE_USER_LEVEL;
}

// The clock. `seconds` <= 0 (or not a number) makes it inert. `busy` (a function, or an array of
// them, every one read each second) restarts the count while any is true. A tap on `target`, a key
// anywhere on `doc` and the page coming back to the front restart it too, as does hold(seconds)
// while that much speech is still to be heard. `extended()` true doubles the allowance (up to the
// cap); the count restarts whenever that changes. onWarn(secondsLeft) fires once when the warning
// starts (a status line, announced once, not a countdown); onClear() when it was showing and
// activity cancelled it; onClose(secondsIdle) once, after which the watch has stopped. warning() is
// true while the warning is showing.
export function createIdleWatch({seconds, busy = [], extended = null, onWarn, onClear, onClose, now = () => Date.now(), target = null, doc = null,
  timers = {set: (fn, ms) => setInterval(fn, ms), clear: id => clearInterval(id)}}) {
  const limit = Number.isFinite(seconds) && seconds > 0 ? Math.floor(seconds) : 0;
  const sources = Array.isArray(busy) ? busy : [busy];
  let last = 0, heldUntil = 0, handle = null, warned = false, longer = false;
  const touch = () => {
    if (handle === null) return;
    last = now();
    if (warned) {warned = false; onClear?.();}
  };
  const visible = () => {if (doc && !doc.hidden) touch();};
  function hold(seconds) {
    if (handle === null || !Number.isFinite(seconds) || seconds <= 0) return;
    const at = now();
    heldUntil = Math.min(Math.max(heldUntil, at) + seconds * 1000, at + IDLE_SPEECH_CAP_SECONDS * 1000);
  }
  function tick() {
    if (!limit || handle === null) return;
    const at = now();
    if (at < last) last = at;   // the clock stepped back: count from here
    const wide = Boolean(extended?.());
    if (wide !== longer) {longer = wide; touch(); return;}   // a card appeared or went: start counting again
    const working = sources.map(source => source()).some(Boolean);
    if (working || at < heldUntil) {touch(); return;}
    const allowed = longer ? Math.min(limit * IDLE_WAITING_FACTOR, IDLE_LIMIT_CAP) : limit;
    const idle = (at - last) / 1000;
    if (idle >= allowed) {stop(); onClose?.(Math.floor(idle)); return;}
    const left = Math.ceil(allowed - idle);
    if (left <= Math.min(IDLE_WARN_LEAD, Math.floor(allowed / 2)) && !warned) {warned = true; onWarn?.(left);}
  }
  function start() {
    if (!limit || handle !== null) return;
    last = now(); warned = false; longer = Boolean(extended?.());
    handle = timers.set(tick, 1000);
    target?.addEventListener('pointerdown', touch, true);
    doc?.addEventListener('keydown', touch, true);
    doc?.addEventListener('visibilitychange', visible);
  }
  function stop() {
    if (handle === null) return;
    timers.clear(handle); handle = null; warned = false; heldUntil = 0;
    target?.removeEventListener('pointerdown', touch, true);
    doc?.removeEventListener('keydown', touch, true);
    doc?.removeEventListener('visibilitychange', visible);
  }
  return {start, stop, touch, hold, tick, warning: () => warned, running: () => handle !== null};
}
// ── end Idle auto-close ──────────────────────────────────────────────────────────────────────────

export function sameTaskVersion(left, right) {
  return Boolean(left && right && ['id','voice_session','revision','row_version','proposal_id','result_kind','proposal_state'].every(key=>left[key]===right[key]));
}

export function taskPresentation(task) {
  if (task.stale === 'expired') return {icon:'–', label:'Expired — open in chat to reopen'};
  if (task.stale === 'stalled') return {icon:'!', label:'Stopped responding — open in chat to retry'};
  if (task.result_kind === 'action_completed') return {icon:'✓', label:'Completed — verified action'};
  if (task.result_kind === 'lookup_answer') return {icon:'✓', label:'Completed — read result ready'};
  if (task.proposal_state === 'executing') return {icon:'…', label:'Still running — result not confirmed'};
  if (task.result_kind === 'failed' || task.status === 'failed') return {icon:'✕', label:'Failed'};
  if (task.result_kind === 'cancelled') return {icon:'–', label:'Cancelled'};
  if (task.result_kind === 'waiting' || task.status === 'waiting') return {icon:'⏳', label:'Preparation complete · Waiting for approval'+(task.stale==='nudge'?' · '+taskAge(task):'')};
  if (task.status === 'queued') return {icon:'○', label:'Pending'};
  if (task.status === 'working') return {icon:'…', label:'Running'};
  if (task.result_kind === 'needs_input') return {icon:'?', label:'Needs clarification'};
  return {icon:'?', label:'Outcome unconfirmed — check saved result'};
}

// A notification is keyed by the durable record, not by a polling response.
// Row versions often change without a user-visible change, so only these
// material states can create a new notice.
export function taskNotice(task) {
  const kind=task.result_kind;
  if(!task.proposal_id)return null;
  if(kind==='action_completed')return {group:'Actions',state:'done',summary:task.title || 'Verified action'};
  if(kind==='waiting')return {group:'Pending',state:'pending',summary:task.title || 'Request awaiting approval'};
  return null;
}

export function taskAge(task) {
  const m=Math.floor(Number(task.age_seconds || 0)/60);
  return m>=120?'waiting '+Math.floor(m/60)+'h':'waiting '+Math.max(m,1)+'m';
}
// Expired and stalled rows have stopped moving: they are no longer "active", so
// they neither count in the banner nor keep the task list polling.
export function taskSettled(task) {
  return task.stale==='expired' || task.stale==='stalled' || ['action_completed','lookup_answer','cancelled','failed'].includes(task.result_kind) || ['failed','done','declined'].includes(task.status);
}
// Lower tier runs and shows first: needs you, quick reads, actions, long drafts.
// The server sets task.priority; this only covers rows that predate it.
export function taskTier(task) {
  if (Number.isInteger(task.priority) && task.priority >= 0 && task.priority <= 3) return task.priority;
  if (task.result_kind === 'waiting' || task.result_kind === 'needs_input' || task.status === 'waiting') return 0;
  if (task.result_kind === 'lookup_answer') return 1;
  if (task.result_kind === 'action_completed') return 2;
  return 3;
}
function compareTasks(a, b) { return taskTier(a) - taskTier(b); }
export function taskBannerText(attention, running, stopped = 0) {
  const parts = [];
  if (attention) parts.push(attention + ' needs you');
  if (running) parts.push(running + ' working');
  if (stopped) parts.push(stopped + ' stopped');
  return parts.join(' · ');
}

export function createVoiceLive(deps) {
  let open = false, root = null, session = null, generation = 0, timer = null, muted = false, after = 0;
  let polling = false, lastTurn = null, workingTurn = null, terminalTurn = false, fragments = [], captionIds = new Set(), closedPromise = Promise.resolve();
  let activities = new Map(), knownTurns = new Set(), finishedTurns = new Set(), activityFlash = '', flashTimer = null, notices = [], noticeTimer = null, noticeOpen = false, noticeHiddenAt = 0;
  const noticeSeen = new Map(), panelSeeded = new Set();
  const requests = new Set();
  let taskHint = true, tasksOpen = false, tasks = new Map(), tasksSeeded = false, tasksFresh = false, taskReading = false, tasksAt = 0;
  async function boundedRequest(url, body, g) {
    const controller = new AbortController();requests.add(controller);
    const timer = setTimeout(()=>controller.abort(), 10000);
    try {
      const response = body === null ? await fetch(url, {credentials:'same-origin',signal:controller.signal})
        : await deps.apiFetch('gptlive/' + url, body, controller.signal);
      const data = await response.json();
      if(!open || g!==generation)throw Error('Obsolete request');
      return {response,data};
    } finally {clearTimeout(timer);requests.delete(controller);}
  }
  let els = {}, glass = null, pond = null, paused = false;
  let allowanceSnapshot = null, allowanceSampledAt = 0, allowanceSession = null, allowanceBeat = 0, allowancePaintBeat = 0, allowanceMinuteShown = false, allowanceEndingShown = false;
  const holdCancels = [];
  let hintTimer = null, hintSince = 0, hintRemaining = 5000, hintDismissed = false;
  let micNote = '', micNoteUntil = 0, announceTimer = null, announceText = '', lineSaid = false, nearSeen = 0, nearNotedAt = 0;
  let inputNow = {talkMode:'hands_free',line:null,agcOff:false};
  const holdMode = () => transport.inputMode()==='hold';
  // What this device has chosen: how to talk, and the owner's volume line (not used with hold to talk). Automatic
  // gain is asked off when a line is set, because a fixed line cannot sit on a level the browser keeps moving.
  function inputSettings() {
    const talkMode=savedTalkMode(), line=talkMode==='hold' ? null : savedLiveLine();
    return {talkMode,line,agcOff:Boolean(line)};
  }
  function updateHint() {
    clearTimeout(hintTimer);hintTimer=null;
    if(hintSince){hintRemaining=Math.max(0,hintRemaining-(Date.now()-hintSince));hintSince=0;}
    if(!els.hint)return;
    els.hint.textContent=holdMode()?'Hold Mist to talk':'Tap to mute';
    const show=open && !document.hidden && transport.ready() && !hintDismissed && hintRemaining>0;
    els.hint.hidden=!show;
    if(show){hintSince=Date.now();hintTimer=setTimeout(updateHint,hintRemaining);}
    paintModeLine();
  }
  // The line under Mist that says how it is being used. Only one line shows at a time: the five-second
  // hint, then "Mic muted" (hands-free), then this.
  function modeLine() {
    if(!transport.active())return '';
    if(micNote && Date.now()<micNoteUntil)return micNote;
    const view=transport.inputView();
    if(view.mode==='hold')return view.talk==='pressed' ? 'Talking — release when done' : view.talk==='latched' ? 'Mic open — tap to close' : 'Hold to talk';
    return view.line==='on' ? 'Line on' : view.line==='unavailable' ? 'Line off on this phone — everything is sent' : '';
  }
  function paintModeLine() {
    if(!els.mode)return;
    const text=modeLine();
    els.mode.textContent=text;
    els.mode.hidden=!text || !els.hint.hidden || !els.micState.hidden;
  }
  // A line that is read aloud once and then goes: the status slot is a live region. While it is up, the plain
  // "Listening" and "Speaker enabled" lines (which arrive in either order around the start) do not wipe it. The idle
  // warning is the one line an announcement never replaces: it is time-limited and has to be seen, so the announcement
  // is dropped (false) and its caller may say it again at the next change.
  function announce(text) {
    if(idleWatch?.warning())return false;
    status(text);announceText=text;clearTimeout(announceTimer);
    announceTimer=setTimeout(()=>{const same=els.status?.textContent===text;announceText='';if(same)status('Listening — GPT-Live');},8000);
    return true;
  }
  // Told by the transport when how the person is talking changes, or on a reason: the conversation has
  // just started, or a press that went on too long was ended.
  function paintInput(view, why) {
    if(!root || !open)return;
    if(view.talk!=='idle' && !hintDismissed){hintDismissed=true;updateHint();}
    if(why==='started'){
      const said=view.mode==='hold' ? 'Hold Mist to talk. The End button ends the conversation.'
        : view.line!=='off' ? 'Volume line on — sound under your line is not sent.' : '';
      if(said)announce(said);
    }
    if(why==='watchdog')announce('Released the mic — hold Mist again to talk.');
    // The line cannot work on this phone: said once, and the persistent line under Mist says it too.
    // A tap on the screen (not Mist: that mutes) is what resumes a suspended analysis, so the note says so.
    if(view.line==='unavailable' && !lineSaid && announce('The volume line cannot work on this phone right now, so everything is being sent. Tap the screen to try again.'))lineSaid=true;
    // Something near the line was not sent. The owner is told, not left guessing: the first time, then at most once a minute.
    if(view.near>nearSeen){
      if(!nearNotedAt || Date.now()-nearNotedAt>=60000){
        if(announce('Something under your line was not sent. If that was you, lower the line in Settings.')){nearNotedAt=Date.now();nearSeen=view.near;}
      } else nearSeen=view.near;
    }
    paintMic();
  }
  function paintMic() {
    const button=root?.querySelector('.vl-orb');if(!button)return;
    const actuallyMuted=transport.inputMuted(), hold=holdMode(), talking=hold && ['pressed','latched'].includes(transport.inputView().talk);
    if(hold){
      button.setAttribute('aria-pressed',String(talking));
      button.setAttribute('aria-label',talking?'Talking; release to stop':'Hold to talk');
      button.title='Hold to talk; tap to keep the mic open';
    }else{
      button.setAttribute('aria-pressed',String(actuallyMuted));
      button.setAttribute('aria-label',actuallyMuted?'Unmute microphone; hold to end the conversation':'Mute microphone; hold to end the conversation');
      button.title=actuallyMuted?'Tap to unmute; hold to end the conversation':'Tap to mute; hold to end the conversation';
    }
    root.classList.toggle('is-muted',actuallyMuted);
    root.classList.toggle('is-talking',talking);
    root.classList.toggle('is-below-line',transport.inputView().below);
    els.mode.classList.toggle('is-warn',transport.inputView().line==='unavailable');
    els.micState.textContent=actuallyMuted?'Mic muted':'';els.micState.hidden=!actuallyMuted;
    paintModeLine();
    glass?.update();
  }
  const status = text => {if(root && open){const plain=/^(Listening|Speaker enabled) — GPT-Live$/.test(text);if(plain && announceText && els.status.textContent===announceText)return;els.status.textContent=plain?(transport.ready()?'':'Connecting — GPT-Live'):text;els.status.hidden=!els.status.textContent;}};
  const request = (action, body) => deps.apiFetch('gptlive/' + action, body);
  let readinessMarked = false;
  const timing = name => globalThis.window?.__blam8Timing?.('gptlive.' + name);
  const paintReady=()=>{
    if (transport.ready() && !readinessMarked) { readinessMarked = true; timing('provider-ready'); }
    if(root){
      paintMic();
      root.dataset.nativeReady=String(transport.ready());
      if(transport.ready() && els.status.textContent==='Connecting — GPT-Live'){els.status.textContent='';els.status.hidden=true;}
      root.querySelector('.vl-end').textContent=transport.session()?'End conversation':transport.active()?'Cancel connection':'Close voice';
      if(!transport.active()){
        root.querySelector('.vl-orb').setAttribute('aria-label',paused?'Hold to start conversation':'Hold to reconnect conversation');
        root.querySelector('.vl-orb').title=transport.closing()?'Closing previous conversation; please wait':paused?'Hold to start conversation':'Hold to reconnect conversation';
      }else paintMic();
    }
    updateHint();glass?.update();
  };
  function paintAllowance() {
    if (!els.allowance || !allowanceSnapshot) return;
    els.allowance.hidden = !allowanceSnapshot.enabled;
    if (!allowanceSnapshot.enabled) {
      els.allowance.classList.remove('is-expanded');
      els.allowance.setAttribute('aria-expanded', 'false');
      return;
    }
    if (allowanceSnapshot.unlimited) {
      els.allowanceText.textContent = 'Unlimited';
      els.allowanceProgress.setAttribute('aria-valuenow', '100');
      els.allowanceProgress.setAttribute('aria-valuetext', 'Unlimited live voice allowance');
      els.allowanceFill.style.width = '100%';
      root.classList.add('has-allowance');
      return;
    }
    const elapsed = allowanceSession ? Math.max(0, (Date.now() - allowanceSampledAt) / 1000) : 0;
    const remaining = Math.max(0, Number(allowanceSnapshot.remaining_seconds || 0) - elapsed);
    const limit = Math.max(1, Number(allowanceSnapshot.limit_seconds || 1));
    const ratio = Math.max(0, Math.min(100, remaining / limit * 100));
    const secs = Math.ceil(remaining);
    const label = secs >= 60 ? `${Math.floor(secs / 60)}m ${secs % 60 ? `${secs % 60}s` : ''}`.trim() : `${secs}s`;
    els.allowanceText.textContent = `${label} left`;
    els.allowanceProgress.setAttribute('aria-valuenow', String(Math.round(ratio)));
    els.allowanceProgress.setAttribute('aria-valuetext', `${label} of live voice time remaining today`);
    els.allowanceFill.style.width = `${ratio}%`;
    els.allowanceFill.style.background = ratio < 15 ? '#fda4af' : ratio < 35 ? '#fcd34d' : '#6ee7b7';
    root.classList.add('has-allowance');
    // A minute before the cut the bar opens by itself, once per time the screen opens
    // (a later tap to close it stays closed); in the last 20 seconds of a running
    // conversation the status line says it is about to end.
    if (remaining <= 60 && !allowanceMinuteShown) {
      allowanceMinuteShown = true;
      if (!els.allowance.classList.contains('is-expanded')) {
        els.allowance.classList.add('is-expanded');
        els.allowance.setAttribute('aria-expanded', 'true');
        els.allowance.removeAttribute('aria-label');
      }
    }
    if (remaining <= 20 && allowanceSession && !allowanceEndingShown) {
      allowanceEndingShown = true;
      status('20 seconds of live voice left — it will end soon.');
    }
  }
  async function refreshAllowance() {
    try {
      const response = await deps.apiFetch('allowance', {});
      const data = await response.json();
      if (!response.ok || !data.ok || !open) return;
      allowanceSnapshot = data; allowanceSampledAt = Date.now(); paintAllowance(); paintFeed();
    } catch (_) { /* allowance remains server-authoritative; a later read can recover */ }
  }
  function stopForAllowance(id, note) {
    if (!open || allowanceSession !== id) return;
    allowanceSession = null;
    if (allowanceBeat) { clearInterval(allowanceBeat); allowanceBeat = 0; }
    if (allowancePaintBeat) { clearInterval(allowancePaintBeat); allowancePaintBeat = 0; }
    paused = true;
    Promise.resolve(transport.close('allowance_exhausted')).finally(() => {
      if (open) { status(note); paintReady(); }
    });
  }
  function startAllowanceSession(id, snapshot) {
    if (!snapshot?.enabled || !id) return;
    allowanceSnapshot = Object.assign({}, allowanceSnapshot || {}, snapshot);
    allowanceSampledAt = Date.now();
    allowanceSession = id;
    paintAllowance(); paintFeed();
    if (allowanceBeat) clearInterval(allowanceBeat);
    const refresh = async () => {
      if (!open || allowanceSession !== id) return;
      await refreshAllowance();
    };
    refresh();
    allowanceBeat = setInterval(refresh, 15000);
    if (allowancePaintBeat) clearInterval(allowancePaintBeat);
    allowancePaintBeat = setInterval(paintAllowance, 1000);
  }
  function endAllowanceSession(id) {
    if (!id || allowanceSession !== id) return;
    const elapsed = Math.max(0, (Date.now() - allowanceSampledAt) / 1000);
    const remaining = Math.max(0, Number(allowanceSnapshot?.remaining_seconds || 0) - elapsed);
    allowanceSnapshot = Object.assign({}, allowanceSnapshot || {}, {remaining_seconds:remaining});
    allowanceSampledAt = Date.now();
    allowanceSession = null;
    if (allowanceBeat) { clearInterval(allowanceBeat); allowanceBeat = 0; }
    if (allowancePaintBeat) { clearInterval(allowancePaintBeat); allowancePaintBeat = 0; }
    paintAllowance();
  }
  function confirmAllowanceSessionEnded(id) {
    if (open && allowanceSession !== id) refreshAllowance();
  }
  function paintFeed() {
    if(!root || !open)return;
    const activeTasks=tasksFresh?[...tasks.values()].filter(task=>task.status==='queued' || task.status==='working' || task.proposal_state==='executing'):[];
    const activeTurns=[...activities.values()].filter(item=>item.state==='running');
    workingTurn=activeTurns.length?activeTurns.at(-1).id:null;
    let label='';
    if(activeTasks.length){const task=activeTasks.at(-1);label=task.status==='queued'?'Queued request':'Working on request';if(activeTasks.length>1)label+=' · '+activeTasks.length+' tasks';}
    else if(activeTurns.length){const turn=activeTurns.at(-1);label=['Request received','Checking business data'].includes(turn.text)?turn.text:'Working on request';if(activeTurns.length>1)label+=' · '+activeTurns.length+' requests';}
    else label=activityFlash;
    els.activity.textContent=label;els.activity.hidden=!label;
    const counts={Actions:0,Pending:0};for(const notice of notices)counts[notice.group]++;
    els.notice.hidden=!notices.length;
    if(notices.length){els.notice.textContent=Object.entries(counts).filter(([,n])=>n).map(([key,n])=>key+' '+n).join(' · ');els.notice.setAttribute('aria-label','Recent '+els.notice.textContent+' updates');els.notice.setAttribute('aria-expanded',String(noticeOpen));}
    else els.notice.setAttribute('aria-expanded','false');
    els.noticeDetail.hidden=!noticeOpen || !notices.length;
    if(noticeOpen){els.noticeDetail.replaceChildren();for(const item of notices){const row=document.createElement('p');row.textContent=item.group+' · '+item.summary;els.noticeDetail.appendChild(row);}}
    root.classList.toggle('has-live-strip',Boolean(label || notices.length));
    root.classList.toggle('has-notices',Boolean(notices.length));
    glass?.update();
  }
  function flash(text) {
    clearTimeout(flashTimer);activityFlash=text;paintFeed();
    flashTimer=setTimeout(()=>{activityFlash='';paintFeed();},3500);
  }
  function expireNotices() {
    clearTimeout(noticeTimer);noticeTimer=null;
    if(!open || !notices.length)return;
    if(document.hidden)return;
    if(noticeOpen || root?.contains(document.activeElement) && (document.activeElement===els.notice || els.noticeDetail.contains(document.activeElement))){root.classList.remove('notice-leaving');return;}
    const now=Date.now(),remaining=notices.filter(item=>item.until>now);
    if(!remaining.length && !window.matchMedia('(prefers-reduced-motion: reduce)').matches){
      root.classList.add('notice-leaving');noticeTimer=setTimeout(()=>{notices=[];root.classList.remove('notice-leaving');paintFeed();},250);return;
    }
    notices=remaining;paintFeed();
    if(!notices.length)return;
    noticeTimer=setTimeout(expireNotices,Math.max(50,Math.min(...notices.map(item=>item.until-now))));
  }
  function noticeVisibility() {
    if(document.hidden){noticeHiddenAt=Date.now();clearTimeout(noticeTimer);noticeTimer=null;return;}
    if(noticeHiddenAt){const delay=Date.now()-noticeHiddenAt;for(const item of notices)item.until+=delay;noticeHiddenAt=0;}
    expireNotices();
  }
  function addNotice(group, key, state, summary) {
    if(!open || !key || !summary)return;
    const material=group+':'+state;
    if(noticeSeen.get(key)===material)return;
    clearTimeout(noticeTimer);noticeTimer=null;
    noticeSeen.set(key,material);
    const now=Date.now();notices=notices.filter(item=>item.key!==key && item.until>now);
    root.classList.remove('notice-leaving');
    notices.push({group,key,summary:String(summary).slice(0,120),until:now+5000});
    notices=notices.slice(-4);paintFeed();expireNotices();
  }
  function notifyPanels(group, rows) {
    if(!open || !['Actions','Pending'].includes(group))return;
    const current=new Map();
    for(const row of rows || []){const key=row.proposal_id?'proposal:'+row.proposal_id:row.log_id?'draft:'+row.log_id:row.invoice_id?'invoice:'+row.invoice_id:row.id?'record:'+row.id:null;if(!key)continue;
      const state=group==='Actions'?'done':String(row.status || row.type || 'pending');
      current.set(key,{state,summary:row.action_summary || row.summary || row.subject || 'A change'});
    }
    if(!panelSeeded.has(group)){panelSeeded.add(group);for(const [key,item] of current)noticeSeen.set(key,group+':'+item.state);return;}
    for(const [key,item] of current)addNotice(group,key,item.state,item.summary);
  }
  const transport = createNativeTransport({request, timing, status:text=>{status(text);paintReady();}, failure:()=>{activities.clear();tasksFresh=false;flash('Connection lost — check chat');}, visualChange:paintReady,
    sessionStarted:(id,snapshot,context,idleLimit)=>{deps.onNativeContext?.(context || []);startAllowanceSession(id,snapshot);startIdleWatch(idleLimit);},
    sessionEnded:id=>{stopIdleWatch();endAllowanceSession(id);},sessionClosed:confirmAllowanceSessionEnded,
    playback:state=>{
      const button=root?.querySelector('[data-native="retry-audio"]');if(!button)return;
      button.hidden=state!=='blocked' && state!=='muted';
      button.textContent=state==='blocked'?'Retry audio':'Resume audio';
      button.setAttribute('aria-label',state==='blocked'?'Retry blocked audio playback':'Resume stopped audio playback');
      button.dataset.playback=state;
    },
    media:()=>{const pre=globalThis.window?.__blam8PreMic;if(pre){globalThis.window.__blam8PreMic=null;if(performance.now()-pre.at<60000)return pre.promise;pre.promise.then(stream=>stream.getTracks().forEach(track=>track.stop()),()=>{});}return navigator.mediaDevices.getUserMedia(captureConstraints(inputNow.agcOff));},
    peer:()=>new RTCPeerConnection(),audio:createNativeAudio,
    caption:message=>{
      idleWatch?.touch();
      if(captionIds.has(message.event_id))return;
      captionIds.add(message.event_id);if(captionIds.size>512)captionIds.delete(captionIds.values().next().value);
      if(message.type==='session.output_transcript.delta')idleWatch?.hold(speechSeconds(message.delta));
      fragments.push(message);fragments=fragments.slice(-80);
      const type=message.type;
      const text=fragments.filter(x=>x.type===type).sort((a,b)=>a.start_ms-b.start_ms).map(x=>x.delta).join('').slice(-1200);
      (type==='session.input_transcript.delta'?els.transcript:els.reply).textContent=text;
    },
    input:(view,why)=>paintInput(view,why),
  });
  function refreshStage() {
    if(!root || !open)return;
    const stage=deps.renderStage?.();els.stage.replaceChildren();els.links?.replaceChildren?.();
    if(stage?.node)els.stage.appendChild(stage.node);
    // The newest reply's page links stand in the orb column under the mic line, not at the bottom with the cards.
    const links=stage?.node?.querySelector?.('[data-cz-links]');if(links && els.links?.appendChild)els.links.appendChild(links);
  }
  function paintTasks() {
    els.tasks.replaceChildren();
    const ordered=[...tasks.values()].sort(compareTasks);
    let attention=0,running=0,stopped=0;
    for (const task of ordered) {
      // Finished and cancelled work is not shown here; its result is in the chat history.
      if (['action_completed','lookup_answer','cancelled'].includes(task.result_kind)) continue;
      const tier=taskTier(task);
      if(task.stale==='expired' || task.stale==='stalled' || task.result_kind==='failed' || task.status==='failed' || taskSettled(task))stopped++;
      else{if(tier===0)attention++;else running++;}
      const renderedGeneration=generation;
      const row=document.createElement('button');row.type='button';row.className='vl-task';
      row.dataset.task=task.id;row.dataset.tier=String(tier);
      const state=taskPresentation(task);
      const title=document.createElement('strong');title.textContent=state.icon+' '+(task.title || 'Request');
      const detail=document.createElement('span');detail.textContent=state.label;
      row.append(title,detail);
      if(task.outcome){const result=document.createElement('span');result.textContent=task.outcome;row.appendChild(result);}
      row.addEventListener('click',()=>{try{deps.onNativeTaskOpen?.(task,()=>open && renderedGeneration===generation && sameTaskVersion(tasks.get(task.id),task));}catch(_){status('Could not open this task. Check the saved conversation.');}});
      els.tasks.appendChild(row);
    }
    // One quiet line; the rows open on tap. Finished results stay in the chat history.
    const active=attention+running+stopped;
    if(!active)tasksOpen=false;
    els.taskHeading.hidden=!active;
    els.taskHeading.textContent=taskBannerText(attention,running,stopped);
    els.taskHeading.setAttribute('aria-expanded',String(tasksOpen));
    els.tasks.hidden=!active || !tasksOpen;
    root.classList.toggle('has-native-tasks',Boolean(active));
    paintFeed();
  }
  function onTaskEvent() {if(open){taskHint=true;tasksAt=0;readTasks(generation);}}
  async function readTasks(g) {
    if(!deps.taskRead || taskReading || Date.now()-tasksAt<3000)return;
    // Read when the stream says a task moved, or as a safety net while one is still live.
    if(!taskHint && ![...tasks.values()].some(task=>!taskSettled(task)))return;
    taskHint=false;taskReading=true;tasksAt=Date.now();
    const controller=new AbortController();requests.add(controller);
    const timeout=setTimeout(()=>controller.abort(),10000);
    try {
      const rows=await deps.taskRead(session,controller.signal);
      if(!open || g!==generation)return;
      const visible=new Set((rows || []).filter(task=>task.voice_session===session && task.id && task.revision).map(task=>task.id));
      for(const id of tasks.keys())if(!visible.has(id))tasks.delete(id);
      for(const task of rows || []) {
        if(task.voice_session!==session || !task.id || !task.revision)continue;
        const previous=tasks.get(task.id);
        if(previous && (task.revision<previous.revision || (task.revision===previous.revision && Number(task.row_version || 0)<Number(previous.row_version || 0))))continue;
        tasks.set(task.id,task);
        if(tasksSeeded && (!previous || previous.result_kind!==task.result_kind || previous.proposal_state!==task.proposal_state || previous.status!==task.status)){
          const notice=taskNotice(task);if(notice)addNotice(notice.group,task.proposal_id?'proposal:'+task.proposal_id:'task:'+task.id,notice.state,notice.summary);
          if(notice?.group==='Actions')flash('Action verified — see Actions');
          else if(task.result_kind==='lookup_answer')flash('Result ready in chat');
        }
      }
      tasksSeeded=true;tasksFresh=true;
      paintTasks();els.taskNote.textContent='';
      try{deps.onNativeTasks?.([...tasks.values()]);}catch(_){els.taskNote.textContent='Chat task display unavailable; current results are shown here.';}
    } catch(_) {if(open && g===generation){tasksFresh=false;els.taskNote.textContent='Task updates unavailable — showing last checked state. No action will be replayed.';flash('Task updates delayed — check chat');}}
    finally {clearTimeout(timeout);requests.delete(controller);if(g===generation)taskReading=false;}
  }
  async function poll(g) {
    const id=transport.session();if(!id || polling || !open || g!==generation)return;
    polling=true;
    try {
      const {response,data}=await boundedRequest('/concierge/voice/gptlive/events/'+encodeURIComponent(id)+'?after='+after,null,g);
      if(!open || g!==generation)return;
      if(!response.ok){activities.clear();tasksFresh=false;flash('Conversation lost — check chat');transport.silence('connection');status('Conversation unavailable. End and start again.');return;}
      for(const event of data.events || []) {
        if(event.seq>after+1){activities.clear();knownTurns.clear();flash('Updates missed — check chat');status('Some updates missed — check the saved conversation.');}
        if(event.seq<=after)continue;after=event.seq;
        if(event.kind==='question'){lastTurn=event.turn_id;terminalTurn=false;knownTurns.add(event.turn_id);activities.delete(event.turn_id);paintFeed();try{deps.onNativeQuestion?.(event.text,event.turn_id);}catch(_){status('Chat display could not update. Check the saved conversation.');}}
        if(event.kind==='result'){if(event.turn_id){activities.delete(event.turn_id);finishedTurns.add(event.turn_id);if(event.turn_id===lastTurn)terminalTurn=true;}paintFeed();try{deps.onAssistantTurn?.(event.payload);refreshStage();}catch(_){status('Chat display could not update. Check the saved conversation.');}}
        if(event.kind==='context'){try{deps.onNativeContext?.(event.turns || []);}catch(_){status('Context display could not update.');}}
        if(event.kind==='activity' && event.turn_id && knownTurns.has(event.turn_id) && !finishedTurns.has(event.turn_id)){if(event.state==='finished'){activities.delete(event.turn_id);finishedTurns.add(event.turn_id);if(event.turn_id===lastTurn)terminalTurn=true;}else if(event.state==='running'){activities.delete(event.turn_id);activities.set(event.turn_id,{id:event.turn_id,state:'running',text:String(event.text || '').slice(0,120)});}paintFeed();}
        if(event.kind==='task' || event.kind==='result')taskHint=true;
        if(event.kind==='status' || event.kind==='task'){status(event.text);}
        if(event.kind==='guard'){transport.silence('guard',event.seq);status(event.text);}
      }
      readTasks(g);
      transport.recoverConnection(); // only after a current authenticated successful response, preserving Stop/guard
    } catch (_) {if(open && g===generation){activities.clear();tasksFresh=false;flash('Connection lost — check chat');transport.silence('connection');status('Connection unavailable; results remain in chat.');}}
    finally {if(g===generation)polling=false;}
  }
  async function control(operation) {
    const g=generation, turn=lastTurn;
    transport.silence();
    const id=transport.session();
    if(id) {
      try {
        const {response,data}=await boundedRequest('control',{session:id,operation,turn_id:turn || '',control_id:transport.controlId?.() || ''},g);
        if(!open || g!==generation)return;
        const outcome=(data.turns || []).find(x=>x.turn_id===turn);
        if(response.ok && operation==='discard' && outcome?.status==='cancelled') {
          activities.delete(turn);finishedTurns.add(turn);if(turn===lastTurn)terminalTurn=true;paintFeed();
          deps.onDiscardTurn?.(turn);status('Request discarded before execution.');
        } else status(operation==='discard'?'Discard is unconfirmed or too late. Check the business result in chat.':data.status || 'Voice stopped.');
      }
      catch (_) {if(open && g===generation)status('Voice stopped locally. Check chat for business results.');}
    }
  }
  function ensureDom() {
    if(root)return;
    const style=document.createElement('style');style.textContent=OVERLAY_CSS.replaceAll('#voice-live-root','#voice-gptlive-root')+`
      #voice-gptlive-root .vl-transcript, #voice-gptlive-root .vl-reply { display:none; }
      #voice-gptlive-root .vl-live { flex:1 0 auto; gap:.35rem; padding:max(.5rem, env(safe-area-inset-top)) .75rem .5rem; }
      #voice-gptlive-root .vl-orb { width:clamp(14rem,82vw,24rem); height:clamp(14rem,82vw,24rem); padding:0; border:0; outline:none; appearance:none; font:inherit; cursor:pointer; touch-action:none; pointer-events:none; -webkit-tap-highlight-color:transparent; -webkit-user-select:none; user-select:none; -webkit-touch-callout:none; }
      #voice-gptlive-root .vl-orb *, #voice-gptlive-root .vl-ambient, #voice-gptlive-root .vl-mic-copy { -webkit-user-select:none; user-select:none; -webkit-touch-callout:none; }
      #voice-gptlive-root .vl-orb, #voice-gptlive-root[data-phase] .vl-orb { background:transparent; box-shadow:none; animation:none; filter:none; }
      #voice-gptlive-root .vl-orb > span { display:none; }
      #voice-gptlive-root .vl-orb > .vl-orb-hit-target { display:block; position:absolute; z-index:3; inset:19%; border-radius:50%; background:transparent; pointer-events:auto; touch-action:none; }
      #voice-gptlive-root .vl-orb { position:relative; isolation:isolate; overflow:visible; --mist-glow-cyan:rgba(34,211,238,.24); --mist-glow-blue:rgba(56,142,255,.22); --mist-glow-violet:rgba(124,77,255,.21); }
      #voice-gptlive-root .vl-orb[data-mist-mode="user"] { --mist-glow-cyan:rgba(34,211,238,.28); --mist-glow-blue:rgba(56,142,255,.22); --mist-glow-violet:rgba(124,77,255,.18); }
      #voice-gptlive-root .vl-orb[data-mist-mode="thinking"] { --mist-glow-cyan:rgba(34,211,238,.21); --mist-glow-blue:rgba(56,142,255,.25); --mist-glow-violet:rgba(124,77,255,.25); }
      #voice-gptlive-root .vl-orb[data-mist-mode="agent"] { --mist-glow-cyan:rgba(34,211,238,.2); --mist-glow-blue:rgba(56,142,255,.23); --mist-glow-violet:rgba(124,77,255,.28); }
      #voice-gptlive-root[data-native-ready="false"] .vl-orb { --mist-glow-cyan:rgba(34,211,238,.21); --mist-glow-blue:rgba(56,142,255,.2); --mist-glow-violet:rgba(124,77,255,.19); }
      #voice-gptlive-root[data-paused="true"] .vl-orb { --mist-glow-cyan:rgba(34,211,238,.19); --mist-glow-blue:rgba(56,142,255,.19); --mist-glow-violet:rgba(124,77,255,.18); }
      #voice-gptlive-root .vl-orb::before { content:""; position:absolute; z-index:0; inset:-150%; pointer-events:none; background:radial-gradient(ellipse 9% 8% at 47% 46%,var(--mist-glow-cyan),transparent 94%),radial-gradient(ellipse 11% 9% at 54% 55%,var(--mist-glow-blue),transparent 92%),radial-gradient(ellipse 8% 9% at 53% 45%,var(--mist-glow-violet),transparent 94%); filter:blur(14px); opacity:.64; transform-origin:50% 50%; transform:translate3d(-1.5%,-.75%,0) rotate(-8deg) scale(.96); animation:vl-mist-glow-field 29s ease-in-out infinite; animation-play-state:paused; }
      #voice-gptlive-root .vl-orb::after { content:""; position:absolute; z-index:0; inset:-150%; pointer-events:none; background:radial-gradient(ellipse 7% 6% at 53% 44%,var(--mist-glow-violet),transparent 93%),radial-gradient(ellipse 8% 7% at 46% 56%,var(--mist-glow-cyan),transparent 94%),radial-gradient(ellipse 10% 7% at 51% 49%,var(--mist-glow-blue),transparent 95%); filter:blur(12px); opacity:.56; transform-origin:50% 50%; transform:translate3d(2%,-1.5%,0) rotate(8deg) scale(.88,1.05); animation:vl-mist-glow-drift 17s ease-in-out infinite alternate; animation-play-state:paused; }
      #voice-gptlive-root.visual-active .vl-orb::before, #voice-gptlive-root.visual-active .vl-orb::after { animation-play-state:running; }
      #voice-gptlive-root .vl-glass-layer { position:absolute; z-index:1; inset:0; pointer-events:none; border-radius:50%; overflow:hidden; transform:translateZ(0); filter:none; }
      #voice-gptlive-root .vl-glass-layer canvas { position:absolute; z-index:2; inset:0; width:100%; height:100%; }
      @keyframes vl-mist-glow-field { 0%,100% { transform:translate3d(-1.5%,-.75%,0) rotate(-8deg) scale(.96); opacity:.56; } 24% { transform:translate3d(1.5%,2%,0) rotate(82deg) scale(1.12); opacity:.72; } 56% { transform:translate3d(2%,-1.75%,0) rotate(202deg) scale(.9,1.15); opacity:.48; } 82% { transform:translate3d(-1.25%,1.75%,0) rotate(302deg) scale(1.12,.88); opacity:.68; } }
      @keyframes vl-mist-glow-drift { 0% { transform:translate3d(2%,-1.5%,0) rotate(8deg) scale(.88,1.05); opacity:.48; } 29% { transform:translate3d(-2.5%,1.5%,0) rotate(-11deg) scale(1.18,.82); opacity:.68; } 63% { transform:translate3d(1.75%,2.25%,0) rotate(6deg) scale(.82,1.2); opacity:.5; } 100% { transform:translate3d(-2%,-1.25%,0) rotate(-7deg) scale(1.16,.84); opacity:.7; } }
      #voice-gptlive-root .vl-ghost-mark { position:absolute; z-index:1; width:62%; height:62%; left:19%; top:19%; color:#7182a2; opacity:.16; transition:opacity .65s ease; overflow:visible; pointer-events:none; }
      #voice-gptlive-root .vl-orb[data-mist-mode="user"] .vl-ghost-mark,
      #voice-gptlive-root .vl-orb[data-mist-mode="thinking"] .vl-ghost-mark { opacity:.3; }
      #voice-gptlive-root .vl-orb[data-mist-mode="user"] .vl-ghost-mark,
      #voice-gptlive-root .vl-orb[data-mist-mode="agent"] .vl-ghost-mark { opacity:.92; color:#b8f5e5; }
      #voice-gptlive-root .vl-orb[data-mist-mode="thinking"] .vl-ghost-mark { color:#c4b5fd; }
      #voice-gptlive-root .vl-orb[data-mist-mode="agent"] .vl-ghost-mark { color:#fde68a; }
      #voice-gptlive-root .vl-side-mark { position:absolute; z-index:2; width:min(34vw,22rem); height:min(34vw,22rem); left:150%; top:50%; transform:translate(-50%,-50%); color:#7182a2; opacity:.52; transition:opacity .45s ease,color .45s ease,filter .45s ease; overflow:visible; pointer-events:none; }
      #voice-gptlive-root:has(.vl-orb[data-mist-mode="user"]) .vl-side-mark { opacity:.96; color:#b8f5e5; filter:drop-shadow(0 0 1.2rem rgba(52,211,153,.38)); }
      #voice-gptlive-root:has(.vl-orb[data-mist-mode="thinking"]) .vl-side-mark { opacity:.72; color:#c4b5fd; filter:drop-shadow(0 0 1.2rem rgba(167,139,250,.35)); }
      #voice-gptlive-root:has(.vl-orb[data-mist-mode="agent"]) .vl-side-mark { opacity:.98; color:#fde68a; filter:drop-shadow(0 0 1.2rem rgba(251,191,36,.35)); }
      #voice-gptlive-root[data-native-ready="false"] .vl-side-mark { opacity:.28; }
      #voice-gptlive-root[data-paused="true"] .vl-side-mark { opacity:.18; }
      #voice-gptlive-root.is-muted .vl-side-mark { color:#fca5a5; }
      #voice-gptlive-root .vl-gear { transform-box:view-box; }
      #voice-gptlive-root .vl-gear-top { transform-origin:63.45px 33.8px; animation:vl-gear-top-turn 14s linear infinite; }
      #voice-gptlive-root .vl-gear-lower { transform-origin:56.8px 64.4px; animation:vl-gear-lower-turn 18s linear infinite; }
      #voice-gptlive-root .vl-speed-stroke { transform-box:fill-box; transform-origin:right center; animation:vl-stroke-reveal 5.8s ease-in-out infinite; }
      #voice-gptlive-root .vl-speed-stroke-1 { animation-delay:.7s; }
      #voice-gptlive-root .vl-speed-stroke-2 { animation-delay:1.4s; }
      @keyframes vl-gear-top-turn { to { transform:rotate(360deg); } }
      @keyframes vl-gear-lower-turn { to { transform:rotate(-360deg); } }
      @keyframes vl-stroke-reveal { 0%,100% { transform:scaleX(1); } 50% { transform:scaleX(1.12); } }
      #voice-gptlive-root .vl-ghost-mark .vl-gear, #voice-gptlive-root .vl-ghost-mark .vl-speed-stroke, #voice-gptlive-root .vl-side-mark .vl-gear, #voice-gptlive-root .vl-side-mark .vl-speed-stroke { animation-play-state:paused; }
      #voice-gptlive-root.visual-active .vl-ghost-mark .vl-gear, #voice-gptlive-root.visual-active .vl-ghost-mark .vl-speed-stroke, #voice-gptlive-root.visual-active .vl-side-mark .vl-gear, #voice-gptlive-root.visual-active .vl-side-mark .vl-speed-stroke { animation-play-state:running; }
      #voice-gptlive-root .vl-stage { isolation:isolate; }
      #voice-gptlive-root .vl-ambient { position:absolute; inset:0; overflow:hidden; pointer-events:none; z-index:0; }
      #voice-gptlive-root .vl-ambient span { position:absolute; width:48%; height:39%; border-radius:50%; filter:blur(25px); opacity:.36; animation:vl-ambient-drift 31s ease-in-out infinite alternate; animation-play-state:paused; }
      #voice-gptlive-root.visual-active .vl-ambient span { animation-play-state:running; }
      #voice-gptlive-root .vl-ambient span:nth-of-type(1) { left:-26%; top:-15%; background:radial-gradient(ellipse,rgba(241,217,83,.16),transparent 72%); animation-duration:29s; }
      #voice-gptlive-root .vl-ambient span:nth-of-type(2) { right:-27%; top:13%; background:radial-gradient(ellipse,rgba(87,205,136,.19),transparent 72%); animation-duration:37s; animation-delay:-11s; }
      #voice-gptlive-root .vl-ambient span:nth-of-type(3) { left:-29%; bottom:7%; background:radial-gradient(ellipse,rgba(171,117,220,.17),transparent 72%); animation-duration:41s; animation-delay:-19s; }
      #voice-gptlive-root .vl-ambient span:nth-of-type(4) { right:-26%; bottom:-15%; background:radial-gradient(ellipse,rgba(69,213,207,.18),transparent 72%); animation-duration:33s; animation-delay:-5s; }
      #voice-gptlive-root .vl-ambient span:nth-of-type(5) { left:25%; top:-27%; background:radial-gradient(ellipse,rgba(150,206,238,.13),transparent 72%); animation-duration:47s; animation-delay:-28s; }
      @keyframes vl-ambient-drift { 0% { transform:translate(-5%,2%) scale(.92,1.03); opacity:.25; } 47% { transform:translate(6%,-5%) scale(1.12,.88); opacity:.42; } 100% { transform:translate(-2%,7%) scale(.96,1.13); opacity:.28; } }
      #voice-gptlive-root .vl-stage > :not(.vl-ambient) { position:relative; z-index:1; }
      #voice-gptlive-root .vl-stage > .vl-side-mark { position:absolute; z-index:2; }
      #voice-gptlive-root .vl-stage > .vl-allowance { position:absolute; z-index:6; }
      #voice-gptlive-root .vl-mic-copy { position:relative; flex:0 0 1.1rem; width:100%; height:1.1rem; }
      #voice-gptlive-root .vl-mic-copy p { position:absolute; inset:0; text-align:center; line-height:1.1rem; }
      #voice-gptlive-root .vl-mic-state { font-size:.75rem; margin:0; color:#cbd5e1; }
      #voice-gptlive-root .vl-mic-mode { font-size:.75rem; margin:0; color:#94a3b8; }
      #voice-gptlive-root.is-talking .vl-mic-mode { color:#86efac; }
      #voice-gptlive-root.is-talking .vl-orb > .vl-orb-hit-target { box-shadow:0 0 0 2px rgba(134,239,172,.55); }
      #voice-gptlive-root .vl-mic-mode.is-warn { color:#fcd34d; }
      #voice-gptlive-root .vl-glass-layer { transition:opacity .3s ease; }
      #voice-gptlive-root.is-below-line .vl-glass-layer { opacity:.5; }
      #voice-gptlive-root .vl-top-strip { padding-left:.65rem; padding-right:.65rem; }
      #voice-gptlive-root .vl-stage > .vl-top-strip { position:absolute; z-index:4; left:0; right:0; top:calc(max(.9rem, env(safe-area-inset-top)) + 1.6rem); display:none; flex-wrap:wrap; align-items:flex-start; justify-content:center; gap:.35rem; margin:0; padding-top:0; padding-bottom:0; pointer-events:none; }
      #voice-gptlive-root .vl-top-strip > * { pointer-events:auto; }
      #voice-gptlive-root .vl-stage > .vl-top-strip > .vl-activity { pointer-events:none; }
      #voice-gptlive-root .vl-stage > .vl-allowance.is-expanded ~ .vl-top-strip { top:calc(max(.9rem, env(safe-area-inset-top)) + 2.5rem); }
      #voice-gptlive-root.has-live-strip .vl-top-strip { display:flex; }
      @media(max-height:500px) { #voice-gptlive-root .vl-stage > .vl-top-strip { justify-content:space-between; } }
      #voice-gptlive-root .vl-allowance { position:absolute; z-index:6; top:max(.9rem, env(safe-area-inset-top)); left:50%; transform:translateX(-50%); display:flex; flex:0 0 auto; align-items:center; justify-content:center; width:4.5rem; height:1.25rem; padding:0; border:0; border-radius:999px; background:transparent; color:#e2e8f0; font:600 .68rem/1 Inter,system-ui,sans-serif; cursor:pointer; -webkit-tap-highlight-color:transparent; }
      #voice-gptlive-root .vl-allowance[hidden] { display:none !important; }
      #voice-gptlive-root .vl-allowance-copy { display:none; }
      #voice-gptlive-root .vl-allowance-track { display:block; width:4.5rem; height:3px; margin:0; overflow:hidden; border-radius:99px; background:rgba(148,163,184,.35); }
      #voice-gptlive-root .vl-allowance-fill { display:block; height:100%; width:100%; background:#6ee7b7; transition:width .35s ease; }
      #voice-gptlive-root .vl-allowance.is-expanded { display:block; width:min(11rem,calc(100vw - 4rem)); height:auto; padding:.38rem .55rem; border:1px solid rgba(148,163,184,.25); border-radius:.7rem; background:rgba(15,23,42,.92); backdrop-filter:blur(8px); }
      #voice-gptlive-root .vl-allowance.is-expanded .vl-allowance-copy { display:flex; justify-content:space-between; gap:.4rem; font-weight:600; white-space:nowrap; }
      #voice-gptlive-root .vl-allowance.is-expanded .vl-allowance-track { width:100%; margin-top:.3rem; }
      #voice-gptlive-root .vl-allowance:focus-visible { outline:2px solid #7dd3fc; outline-offset:3px; }
      #voice-gptlive-root .vl-activity { flex:0 1 auto; width:max-content; min-width:0; max-width:100%; display:flex; align-items:center; margin:0; padding:var(--vl-pad-y) var(--vl-pad-x); border:1px solid rgba(125,211,252,.28); border-radius:999px; background:rgba(15,23,42,.72); color:#dbeafe; font-size:.75rem; line-height:1.2; white-space:normal; overflow-wrap:anywhere; }
      #voice-gptlive-root .vl-activity[hidden] { display:none; }
      #voice-gptlive-root.has-notices .vl-activity { max-width:calc(55% - .35rem); }
      #voice-gptlive-root .vl-notice { flex:0 1 auto; min-width:0; max-width:45%; border:1px solid rgba(251,191,36,.5); border-radius:999px; background:rgba(15,23,42,.85); color:#fde68a; font:inherit; font-size:.73rem; line-height:1.2; white-space:normal; overflow-wrap:anywhere; cursor:pointer; }
      #voice-gptlive-root .vl-notice { transition:opacity .25s ease; }
      #voice-gptlive-root.notice-leaving .vl-notice { opacity:0; }
      @media(max-width:400px) { #voice-gptlive-root .vl-notice { font-size:.65rem; } }
      #voice-gptlive-root .vl-notice-detail { flex:0 0 100%; max-height:20dvh; overflow:auto; padding:.35rem .55rem; border:1px solid rgba(251,191,36,.4); border-radius:.7rem; background:#0f172a; color:#fef3c7; font-size:.75rem; overflow-wrap:anywhere; }
      #voice-gptlive-root .vl-notice-detail p { margin:.3rem 0; }
      #voice-gptlive-root.is-muted .vl-mic-state { color:#f87171; }
      #voice-gptlive-root .vl-mic-hint { font-size:.75rem; margin:0; color:#94a3b8; }
      #voice-gptlive-root .vl-status, #voice-gptlive-root .vl-transcript, #voice-gptlive-root .vl-reply { margin:0; max-height:3em; overflow:auto; overflow-wrap:anywhere; font-size:.85rem; }
      #voice-gptlive-root .vl-status-slot { flex:0 0 3.4rem; width:100%; height:3.4rem; display:flex; align-items:center; justify-content:center; overflow:hidden; }
      #voice-gptlive-root .vl-status { width:100%; max-height:100%; line-height:1.2; text-align:center; }
      #voice-gptlive-root .vl-stage-scroll { flex:0 1 auto; max-height:36vh; max-height:36dvh; min-height:0; overflow:auto; overscroll-behavior:contain; width:calc(100% - 1rem); margin-bottom:.65rem; }
      #voice-gptlive-root .vl-controls { flex:none; padding:0; }
      #voice-gptlive-root .vl-cards [data-cz-tasks] { display:none; }
      #voice-gptlive-root .vl-task { position:relative; display:flex; flex-direction:column; gap:.2rem; width:100%; padding:.45rem .55rem; margin:.3rem 0; border:1px solid #64748b; border-radius:.75rem; background:#0f172a; color:#e2e8f0; text-align:left; overflow-wrap:anywhere; min-height:44px; }
      #voice-gptlive-root .vl-task span { font-size:.8rem; }
      #voice-gptlive-root .vl-task-note { font-size:.8rem; color:#bae6fd; margin:.25rem 0; }
      #voice-gptlive-root .vl-task-note:empty { display:none; }
      #voice-gptlive-root .vl-task-heading { display:block; margin:0 auto; border:1px solid #475569; border-radius:999px; background:#0f172a; color:#cbd5e1; font-size:.8rem; }
      #voice-gptlive-root .vl-task-heading[hidden], #voice-gptlive-root .vl-tasks[hidden] { display:none; }
      @media(max-height:600px) { #voice-gptlive-root .vl-orb { width:min(14rem,38vh); height:min(14rem,38vh); width:min(14rem,38dvh); height:min(14rem,38dvh); } #voice-gptlive-root .vl-transcript, #voice-gptlive-root .vl-reply { max-height:2.4em; } }
      @media(max-height:600px) { #voice-gptlive-root .vl-stage-scroll { max-height:28vh; max-height:28dvh; } }
      @media(max-height:500px) {
        #voice-gptlive-root .vl-live { flex:0 0 auto; gap:.1rem; padding:.25rem .75rem; }
        #voice-gptlive-root .vl-orb { width:20vh; height:20vh; width:20dvh; height:20dvh; }
        #voice-gptlive-root .vl-status { min-height:0; line-height:1.2; }
        #voice-gptlive-root .vl-stage-scroll { flex:1 1 auto; max-height:none; }
      }
      #voice-gptlive-root .vl-controls { flex-wrap:wrap; gap:0.5rem; margin:0; }
      #voice-gptlive-root .vl-controls:has(button[hidden]) { display:none; }
      #voice-gptlive-root .vl-end { margin:0 auto max(.5rem, env(safe-area-inset-bottom)); }
      @media(max-height:500px) { #voice-gptlive-root .vl-end { margin-top:1rem; } }
      #voice-gptlive-root .vl-glass-layer canvas, #voice-gptlive-root .vl-ghost-mark, #voice-gptlive-root .vl-side-mark { filter:grayscale(1); transition:filter .7s ease; }
      #voice-gptlive-root[data-native-ready="true"] .vl-glass-layer canvas, #voice-gptlive-root[data-native-ready="true"] .vl-ghost-mark, #voice-gptlive-root[data-native-ready="true"] .vl-side-mark { filter:grayscale(0); }
      @media(prefers-reduced-motion:reduce) { #voice-gptlive-root .vl-glass-layer, #voice-gptlive-root .vl-ghost-mark, #voice-gptlive-root .vl-side-mark, #voice-gptlive-root .vl-notice { transition:none; } #voice-gptlive-root .vl-orb::before, #voice-gptlive-root .vl-orb::after, #voice-gptlive-root .vl-ambient span, #voice-gptlive-root .vl-ghost-mark .vl-gear, #voice-gptlive-root .vl-ghost-mark .vl-speed-stroke, #voice-gptlive-root .vl-side-mark .vl-gear, #voice-gptlive-root .vl-side-mark .vl-speed-stroke { animation:none; } }
      #voice-gptlive-root .vl-controls button { width:auto; border:1px solid rgba(148,163,184,.4); border-radius:9999px; background:transparent; color:#cbd5e1; font-size:.85rem; }
      #voice-gptlive-root .vl-controls button:focus-visible, #voice-gptlive-root .vl-orb:focus-visible, #voice-gptlive-root .vl-notice:focus-visible { outline:2px solid #7dd3fc; outline-offset:2px; }
      #voice-gptlive-root .vl-controls button[aria-pressed="true"] { border-color:#86efac; color:#86efac; }
      /* The orb column reads: the orb, the mic line just under the B, the status line, then the page links of the newest reply stacked one above the other where the mic line used to sit (Kris 2026-10-16). Order only, so the DOM order stays as it was. */
      #voice-gptlive-root .vl-mic-copy { order:1; margin-top:-1.75rem; }
      #voice-gptlive-root .vl-status-slot, #voice-gptlive-root .vl-transcript, #voice-gptlive-root .vl-reply { order:2; }
      #voice-gptlive-root .vl-links { order:3; flex:0 0 auto; display:flex; flex-direction:column; align-items:center; gap:.4rem; width:100%; }
      #voice-gptlive-root .vl-links:empty { display:none; }
      #voice-gptlive-root .vl-links > div { display:flex; flex-direction:column; flex-wrap:nowrap; align-items:center; gap:.4rem; margin:0; max-width:100%; }
      #voice-gptlive-root .vl-links a { max-width:100%; text-align:center; text-decoration:none; background:rgba(226,232,240,.1); color:#e2e8f0; border:1px solid rgba(148,163,184,.35); border-radius:9999px; font-size:.85rem; }
      #voice-gptlive-root .vl-links a:hover { background:rgba(226,232,240,.18); }
      #voice-gptlive-root .vl-controls { order:4; }
      /* Buttons hug their text: padding only, never font-size. The visible face shrinks and a ::before keeps each tap target at 44px (a face already taller or wider gets no extra). Task rows are positioned so their own taps sit above the heading's hit area. The waiting chips are left as they are: their wrapper and label clip overflow, so a hit area drawn outside the face would be cut off. */
      #voice-gptlive-root { --vl-pad-y:.22rem; --vl-pad-x:.55rem; }
      #voice-gptlive-root .vl-notice, #voice-gptlive-root .vl-controls button, #voice-gptlive-root .vl-end, #voice-gptlive-root .vl-task-heading, #voice-gptlive-root .vl-stage-scroll a, #voice-gptlive-root .cz-draft-acts button:not(.cz-draft-btn) { position:relative; padding:var(--vl-pad-y) var(--vl-pad-x); min-height:0; height:auto; line-height:1.2; }
      #voice-gptlive-root .vl-notice::before, #voice-gptlive-root .vl-controls button::before, #voice-gptlive-root .vl-end::before, #voice-gptlive-root .vl-task-heading::before, #voice-gptlive-root .vl-stage-scroll a::before, #voice-gptlive-root .cz-draft-acts button:not(.cz-draft-btn)::before { content:""; position:absolute; top:min(0px, calc(50% - 22px)); bottom:min(0px, calc(50% - 22px)); left:min(0px, calc(50% - 22px)); right:min(0px, calc(50% - 22px)); }
      #voice-gptlive-root .vl-links a { position:relative; padding:var(--vl-pad-y) var(--vl-pad-x); line-height:1.2; }
      #voice-gptlive-root .vl-links a::before { content:""; position:absolute; top:min(0px, calc(50% - 22px)); bottom:min(0px, calc(50% - 22px)); left:min(0px, calc(50% - 22px)); right:min(0px, calc(50% - 22px)); }
    `;document.head.appendChild(style);
    root=document.createElement('div');root.id='voice-gptlive-root';
    root.innerHTML=`<div class="vl-stage"><button type="button" class="cz-voice-settings" aria-label="Concierge settings" title="Settings"><svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M19.14 12.94c.04-.31.06-.63.06-.94s-.02-.63-.06-.94l2.03-1.58a.5.5 0 0 0 .12-.64l-1.92-3.32a.5.5 0 0 0-.61-.22l-2.39.96a7.28 7.28 0 0 0-1.63-.95l-.36-2.54A.49.49 0 0 0 13.88 2h-3.76a.49.49 0 0 0-.49.42L9.27 4.96c-.6.23-1.15.55-1.63.95l-2.39-.96a.5.5 0 0 0-.61.22L2.72 8.49a.5.5 0 0 0 .12.64l2.03 1.58a7.28 7.28 0 0 0-.06 1.88L2.78 14.17a.5.5 0 0 0-.12.64l1.92 3.32a.5.5 0 0 0 .61.22l2.39-.96c.48.4 1.03.72 1.63.95l.36 2.54a.49.49 0 0 0 .49.42h3.76a.49.49 0 0 0 .49-.42l.36-2.54c.6-.23 1.15-.55 1.63-.95l2.39.96a.5.5 0 0 0 .61-.22l1.92-3.32a.5.5 0 0 0-.12-.64l-2.03-1.58ZM12 15.5a3.5 3.5 0 1 1 0-7 3.5 3.5 0 0 1 0 7Z"/></svg></button><div class="vl-ambient" aria-hidden="true"><span></span><span></span><span></span><span></span><span></span></div><button type="button" class="vl-allowance" hidden aria-label="Live voice allowance today. Tap to expand remaining time" aria-expanded="false"><span class="vl-allowance-copy"><span>Live voice today</span><span data-allowance-text></span></span><span class="vl-allowance-track" role="progressbar" aria-label="Live voice allowance remaining" aria-valuemin="0" aria-valuemax="100" aria-valuenow="100"><span class="vl-allowance-fill"></span></span></button><div class="vl-top-strip"><p class="vl-activity" role="status"></p><button type="button" class="vl-notice" aria-label="Recent Actions and Pending updates" aria-controls="vl-notice-detail" aria-expanded="false" hidden></button><div id="vl-notice-detail" class="vl-notice-detail" role="note" hidden></div></div><div class="vl-live"><button type="button" class="vl-orb" aria-label="Mute microphone; hold to stop speech" aria-pressed="false"><div class="vl-orb-hit-target" aria-hidden="true"></div></button>
      <div class="vl-status-slot"><p class="vl-status" role="status" tabindex="0"></p></div><p class="vl-transcript" aria-hidden="true"></p><p class="vl-reply" aria-hidden="true"></p>
      <div class="vl-mic-copy"><p class="vl-mic-hint" hidden>Tap to mute</p><p class="vl-mic-state" hidden></p><p class="vl-mic-mode" hidden></p></div><div class="vl-links"></div><div class="vl-controls"><button type="button" data-native="retry-audio" hidden>Retry audio</button></div></div>
      <div class="vl-stage-scroll"><button type="button" class="vl-task-heading" aria-expanded="false" hidden></button><p class="vl-task-note" role="status"></p><div class="vl-tasks"></div><div class="vl-cards"></div></div>
      <button type="button" class="vl-end">End conversation</button></div>`;
    root.querySelector('.vl-stage').insertAdjacentHTML('afterbegin', BLAM8_MOTION_MARK.replace('vl-ghost-mark','vl-side-mark').replaceAll('vl-motion-gear-mask','vl-motion-gear-mask-side'));
    document.body.appendChild(root);root.querySelector('.cz-voice-settings').addEventListener('click',event=>{event.stopPropagation();deps.onSettings?.();});els={hint:root.querySelector('.vl-mic-hint'),micState:root.querySelector('.vl-mic-state'),mode:root.querySelector('.vl-mic-mode'),status:root.querySelector('.vl-status'),transcript:root.querySelector('.vl-transcript'),reply:root.querySelector('.vl-reply'),stage:root.querySelector('.vl-cards'),activity:root.querySelector('.vl-activity'),notice:root.querySelector('.vl-notice'),noticeDetail:root.querySelector('.vl-notice-detail'),allowance:root.querySelector('.vl-allowance'),allowanceText:root.querySelector('[data-allowance-text]'),allowanceProgress:root.querySelector('.vl-allowance-track'),allowanceFill:root.querySelector('.vl-allowance-fill'),tasks:root.querySelector('.vl-tasks'),taskHeading:root.querySelector('.vl-task-heading'),taskNote:root.querySelector('.vl-task-note'),links:root.querySelector('.vl-links')};
    els.allowance.addEventListener('click',()=>{const expanded=els.allowance.classList.toggle('is-expanded');els.allowance.setAttribute('aria-expanded',String(expanded));if(expanded)els.allowance.removeAttribute('aria-label');else els.allowance.setAttribute('aria-label','Live voice allowance today. Tap to expand remaining time');if(allowanceSnapshot)paintAllowance();});
    els.taskHeading.addEventListener('click',()=>{tasksOpen=!tasksOpen;paintTasks();});
    els.notice.addEventListener('click',()=>{noticeOpen=!noticeOpen;paintFeed();if(!noticeOpen)expireNotices();});
    root.querySelector('.vl-top-strip').addEventListener('focusout',()=>setTimeout(expireNotices,0));
    const mist=root.querySelector('.vl-orb');
    // Resume only the owned analyser during a real gesture. Playback silences
    // remain latched; observer/timer activity cannot resume the context.
    // Any trusted tap on the screen is a real gesture (the idle clock listens on the overlay the same way), so it can
    // resume the analysis: on a phone that created the audio context suspended, the volume line's "cannot work" note
    // can be answered with a tap that does not also mute. Only a press on Mist is also "using Mist".
    root.addEventListener('pointerdown',event=>{if(event.isTrusted && event.button===0 && open && !paused)transport.resumeAnalysis();});
    mist.addEventListener('pointerdown',event=>{if(event.isTrusted && event.button===0 && open && !paused)transport.noteMist();});
    mist.addEventListener('keydown',event=>{if(event.isTrusted && !event.repeat && (event.key===' ' || event.key==='Enter') && open && !paused){transport.resumeAnalysis();transport.noteMist();}});
    // In hold-to-talk a long press talks: it does not end the conversation (the End button does), but a
    // paused or dropped conversation still restarts on a hold, since nothing is being said.
    holdCancels.push(installHoldAction(mist,()=>{
      if(!open)return;
      if(paused || !transport.active())restart();else pauseConversation();
    },{armed:()=>!(holdMode() && open && !paused && transport.active())}));
    mist.addEventListener('click',()=>{
      if(!open || paused || !transport.inputAvailable() || holdMode())return;   // hold to talk has no mute: the mic is shut whenever Mist is not held
      muted=!muted;hintDismissed=true;updateHint();transport.setMuted(muted);paintMic();
    });
    // Hold to talk (a per-device setting; nothing here acts in hands-free). Press and hold Mist, or Space or
    // Enter with it focused, to talk; a short tap keeps the mic open until the next tap. The remover it returns is
    // deliberately not kept in holdCancels: those run at every close, and this screen is built once and reopened, so
    // the gestures must outlive a conversation (every handler checks enabled(), so they do nothing in hands-free).
    installTalkGestures(mist,{
      talk:{press:(id,options)=>transport.talkPress(id,options),release:id=>transport.talkRelease(id),cancel:reason=>transport.talkCancel(reason)},
      enabled:()=>open && !paused && transport.active() && holdMode(),
      ready:()=>transport.inputReady(),
      notReady:()=>{micNote='One moment — still connecting';micNoteUntil=Date.now()+2500;paintModeLine();setTimeout(paintModeLine,2600);},
      warning:()=>Boolean(idleWatch?.warning()),
    });
    root.querySelector('[data-native="retry-audio"]').addEventListener('click',()=>transport.resume());
    root.querySelector('.vl-end').addEventListener('click',()=>api.close('end'));deps.onMount?.(root);
  }
  // ── idle auto-close wiring (the clock is createIdleWatch above; tests/replays/gptlive_idle.mjs slices this block) ──
  // Started when the provider session starts, stopped by every way a session ends (the transport's
  // sessionEnded). Speech, any new server event, a working turn or task (until it has been working
  // longer than the stall threshold), the voice being audible or still being said, the person's
  // unmuted voice, a tap or a key all count as activity; a muted microphone does not. A card
  // waiting for the person doubles the time allowed. The close is a pause, as with a hold on Mist.
  let idleWatch = null, idleSeq = 0, idleBefore = '';
  const idleSeen = new Map();
  function startIdleWatch(seconds) {
    stopIdleWatch();
    idleSeq = after;idleBefore = '';idleSeen.clear();
    idleWatch = createIdleWatch({seconds, target: root, doc: document,
      extended: () => [...tasks.values()].some(taskAwaitsPerson),
      busy: () => {
        const moved = after !== idleSeq;idleSeq = after;
        const signals = transport.signals();
        return conversationBusy({started: transport.started(), eventMoved: moved, working: anyFresh(workingIds(activities, tasks), idleSeen, Date.now(), IDLE_STALL_SECONDS),
          agentLevel: signals.agent, userLevel: signals.user, muted: signals.muted, talking: signals.talking});
      },
      // The line that was showing comes back when the warning is cleared, not a guessed one.
      onWarn: left => {idleBefore = els.status.textContent;status(idleWarning(left, transport.signals().holdMode));},
      onClear: () => {
        if(!/^Closing in /.test(els.status.textContent))return;
        if(idleBefore){els.status.textContent = idleBefore;els.status.hidden = false;}
        else status('Listening — GPT-Live');
      },
      onClose: spent => {transport.noteIdle(spent);pauseConversation('idle', idleEndedNote);}});
    idleWatch.start();
  }
  function stopIdleWatch() {idleWatch?.stop();idleWatch = null;}
  // ── end idle auto-close wiring ──
  // Hold ENDS the live connection (Kris 2026-10-14). The old hold only muted the
  // speaker and sent a soft "stop speaking"; the mic, peer and provider session
  // stayed up, so the model kept hearing and kept raising tasks nobody could hear
  // confirmed. Mute (a tap) is the one that leaves the connection open. This closes
  // the same way End does - mic tracks stopped, peer and data channel closed, the
  // provider session closed server-side - but the screen stays up, paused, and a
  // second hold reconnects. Results already produced remain in chat. An idle close
  // ends the same way (reason "idle", with a line saying why).
  function pauseConversation(reason='hold_stop', note='Conversation stopped. Hold Mist to reconnect; results remain in chat.') {
    if(!open || paused)return;
    paused=true;workingTurn=null;terminalTurn=true;activities.clear();
    root.dataset.paused='true';
    root.querySelector('[data-native="retry-audio"]').hidden=true;
    transport.close(reason);   // synchronous: the mic is released before any network await
    status(note);
    paintReady();paintFeed();
  }
  async function restart() {
    const closing=api.close('reconnect'), closedGeneration=generation;
    await Promise.all([closing,transport.settled()]);
    if(!open && generation===closedGeneration)api.open();
  }
  const api={
    open(opts) {
      if(open)return;inputNow=inputSettings();transport.configureInput(inputNow);micNote='';lineSaid=false;nearSeen=0;nearNotedAt=0;announceText='';clearTimeout(announceTimer);paused=Boolean(opts?.paused);open=true;const g=++generation;session=crypto.randomUUID();deps.onSessionStart?.(session);after=0;fragments=[];captionIds.clear();lastTurn=null;workingTurn=null;terminalTurn=false;polling=false;muted=false;activities=new Map();knownTurns=new Set();finishedTurns=new Set();activityFlash='';notices=[];noticeOpen=false;noticeHiddenAt=0;panelSeeded.clear();allowanceSession=null;allowanceSnapshot=null;allowanceSampledAt=Date.now();allowanceMinuteShown=false;allowanceEndingShown=false;if(allowanceBeat){clearInterval(allowanceBeat);allowanceBeat=0;}if(allowancePaintBeat){clearInterval(allowancePaintBeat);allowancePaintBeat=0;}
      readinessMarked=false;
      taskHint=true;tasksOpen=false;tasks=new Map();tasksSeeded=false;tasksFresh=false;taskReading=false;tasksAt=0;
      ensureDom();root.classList.remove('has-allowance');window.addEventListener('blam8:task',onTaskEvent);document.addEventListener('visibilitychange',updateHint);document.addEventListener('visibilitychange',noticeVisibility);hintRemaining=5000;hintSince=0;hintDismissed=false;root.dataset.nativeReady='false';root.dataset.paused=String(paused);root.querySelector('[data-native="retry-audio"]').hidden=true;updateHint();els.activity.textContent='';els.notice.hidden=true;els.noticeDetail.hidden=true;els.taskNote.textContent='';root.classList.remove('notice-leaving');root.classList.add('is-open');document.body.classList.add('voice-live-open');paintTasks();status('Connecting — GPT-Live');refreshAllowance();paintFeed();refreshStage();
      // Ripples under everything else: taps, the pointer and the person's voice move the water. Independent of the Mist: if it cannot start the screen is unchanged.
      try{pond=createPond(root.querySelector('.vl-stage'),root.querySelector('.vl-orb'));}catch(_){pond=null;}
      try{glass=createGlassCore(root.querySelector('.vl-orb'),()=>{const signals=transport.signals(),ready=transport.ready(),thinking=Boolean(open && ready && workingTurn);const seen=ready?{...signals,ready:true,muted:transport.inputMuted(),thinking}:{...signals,ready:false,muted:transport.inputMuted(),analysisAvailable:true,agentPlaying:false,thinking:false};pond?.hear(seen);return seen;},on=>{transport.setVisualActive(on);root.classList.toggle('visual-active',on);});}
      catch(_){glass=null;root.querySelector('.vl-orb').dataset.glass='fallback';transport.setVisualActive(false);}
      transport.setMuted(false);paintMic();
      // Native mode has no automatic reconnect or Standard TTS. New opening
      // owns a new provider/application session; old durable tasks stay in chat.
      const begin=()=>{if(open && g===generation)transport.start(session);};
      paintReady();
      if(opts?.paused){status('Hold Mist to start GPT-Live.');paintReady();}
      else {if(transport.closing())status('Closing previous conversation; please wait.');Promise.all([closedPromise,transport.settled()]).then(begin);}
      timing('screen-mount');
      timer=setInterval(()=>poll(g),500);
    },
    close(reason='unknown') {
      if(!open)return closedPromise;open=false;++generation;workingTurn=null;terminalTurn=true;activities.clear();clearInterval(timer);timer=null;clearTimeout(flashTimer);clearTimeout(noticeTimer);flashTimer=null;noticeTimer=null;notices=[];noticeOpen=false;allowanceSession=null;if(allowanceBeat){clearInterval(allowanceBeat);allowanceBeat=0;}if(allowancePaintBeat){clearInterval(allowancePaintBeat);allowancePaintBeat=0;}
      for(const controller of requests)controller.abort();requests.clear();
      for(const cancel of holdCancels)cancel();
      window.removeEventListener('blam8:task',onTaskEvent);document.removeEventListener('visibilitychange',updateHint);document.removeEventListener('visibilitychange',noticeVisibility);clearTimeout(hintTimer);hintTimer=null;hintSince=0;if(els.hint)els.hint.hidden=true;
      els.activity.textContent='';els.notice.hidden=true;els.noticeDetail.hidden=true;els.allowance.hidden=true;els.allowance.classList.remove('is-expanded');els.allowance.setAttribute('aria-expanded','false');els.allowance.setAttribute('aria-label','Live voice allowance today. Tap to expand remaining time');root.classList.remove('has-live-strip','has-allowance','notice-leaving');root.dataset.nativeReady='false';root.dataset.paused='false';
      root?.classList.remove('is-open','has-notices');document.body.classList.remove('voice-live-open');
      try{glass?.destroy();}catch(_){}glass=null;try{pond?.destroy();}catch(_){}pond=null;root.classList.remove('visual-active');
      closedPromise=transport.close(reason);deps.onClose?.(closedPromise);return closedPromise;
    },
    visualState:()=>glass?{...glass.snapshot(),workingTurn}:null,pondState:()=>pond?pond.snapshot():null,
    startupTimings:()=>transport.startupTimings(),
    isOpen:()=>open,isMuted:()=>transport.inputMuted(),setMuted(on){muted=Boolean(on);hintDismissed=true;updateHint();transport.setMuted(muted);paintMic();},refreshStage,
    clearScreen(line){api.close('context_clear');api.open();if(els.transcript)els.transcript.textContent='';if(els.reply)els.reply.textContent=line || 'Cleared context; reconnecting.';},
    // The Debrief button. The data channel takes no client commands, so the browser
    // asks the server (control → debrief), which runs the request and hands the
    // verified reply to the voice. Unlike control(), it never silences anything.
    async startDebrief(step){
      if(!open || paused || !transport.ready() || !transport.session()){status(transport.active()?'One moment — still connecting.':'Hold Mist to start the conversation first.');return false;}
      const g=generation;
      try {
        const {response,data}=await boundedRequest('control',{session:transport.session(),operation:'debrief',step:step==='next'?'next':'start',control_id:transport.controlId?.() || ''},g);
        if(!open || g!==generation)return false;
        if(!response.ok){status(typeof data?.detail==='string'?data.detail:'The debrief could not start.');return false;}
        return true;
      } catch (_) {if(open && g===generation)status('The debrief could not start. Check chat.');return false;}
    },
    notify(text){status(text);refreshStage();},notifyRecords:notifyPanels,startTour(){status('GPT-Live: speak naturally; confirm changes using the visible card.');},
  };
  return api;
}
