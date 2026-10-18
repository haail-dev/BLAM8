// Read-only analysis of already-owned streams. Never captures or routes audible output.
// The input stream the native transport hands over is a clone of the sent microphone track, kept enabled,
// so the room is still measured while the sent track is disabled (muted, or closed by hold to talk or by
// the volume line): a disabled track reads as silence to everything listening to it.
export function createVoiceMeter() {
  let context=null, input=null, output=null, dead=false, active=true;
  const silence=new Float32Array(64),samples=new Float32Array(1024),wave=new Float32Array(64);
  try {const Context=globalThis.AudioContext || globalThis.webkitAudioContext;if(Context)context=new Context();}catch(_){}
  function disconnect(binding){for(const node of [binding?.source,binding?.analyser])try{node?.disconnect();}catch(_){}}
  function attach(stream) {
    if(!context || !stream)return null;
    let source,analyser;
    try{source=context.createMediaStreamSource(stream);analyser=context.createAnalyser();analyser.fftSize=1024;source.connect(analyser);return {stream,source,analyser};}catch(_){disconnect({source,analyser});return null;}
  }
  // `force` (the volume line is set) resumes a suspended context even while the visual is paused: the line's
  // measurement does not depend on the orb being drawn. Everything else is as before.
  function resume(force=false){if(context && !dead && (active || force) && context.state!=='running')try{Promise.resolve(context.resume()).catch(()=>{});}catch(_){}}
  function read(binding,fillWave=false) {
    if(!binding || context?.state!=='running')return 0;
    if(!binding.stream.getAudioTracks().some(track=>track.enabled!==false && track.muted!==true && track.readyState!=='ended'))return 0;
    try{binding.analyser.getFloatTimeDomainData(samples);}catch(_){return 0;}
    let sum=0;for(const value of samples)sum+=value*value;
    if(fillWave)for(let i=0;i<64;i++)wave[i]=Math.max(-1,Math.min(1,samples[i*16]*3));
    return Math.min(1,Math.max(0,Math.sqrt(sum/samples.length)-.009)*12);
  }
  resume();
  return {
    input(stream){disconnect(input);input=attach(stream);},
    output(stream){disconnect(output);output=attach(stream);},
    resume,
    // Raw RMS of the owned input right now (0 to 1), or null when it cannot be read: no input,
    // no analyser, or a context that is not running (iOS may keep it suspended). Unlike sample(),
    // it ignores the visual flag, so a hidden orb does not stop the measurement. It only measures;
    // nothing is gated on it (the volume line reads peakLevel() below).
    sampleLevel(){
      if(dead || !input || context?.state!=='running')return null;
      try{input.analyser.getFloatTimeDomainData(samples);}catch(_){return null;}
      let sum=0;for(const value of samples)sum+=value*value;
      return Math.sqrt(sum/samples.length);
    },
    // The largest absolute sample of the owned input right now (0 to 1), from the same analyser window
    // and under the same rules as sampleLevel: null when it cannot be read. This is the statistic the
    // volume line compares with its threshold (the settings sheet's own meter reads a peak too, so the
    // numbers carry over). Like the other reads it ignores the visual flag.
    peakLevel(){
      if(dead || !input || context?.state!=='running')return null;
      try{input.analyser.getFloatTimeDomainData(samples);}catch(_){return null;}
      let peak=0;for(const value of samples){const level=Math.abs(value);if(level>peak)peak=level;}
      return peak;
    },
    // The audio context's state ('running', 'suspended', 'interrupted', 'closed'), or null when there is none.
    contextState(){return context ? context.state : null;},
    setActive(on){active=Boolean(on);},
    sample(audio,muted=false,playConfirmed=false){
      if(dead || !active)return {agent:0,user:0,wave:silence,muted,agentPlaying:false,inputActive:false,analysisAvailable:Boolean(context && context.state==='running')};
      wave.fill(0);
      const inputActive=!muted && Boolean(input?.stream.getAudioTracks().some(track=>track.enabled!==false && track.muted!==true && track.readyState!=='ended'));
      const user=inputActive?read(input,true):0;
      // Raw remote RTP can contain sound while HTML playback is blocked or
      // silenced. Only this owned element's active audible playback admits it.
      const playing=Boolean(playConfirmed && audio && !audio.paused && !audio.muted && audio.volume>0 && audio.readyState>=2);
      const agent=playing?read(output):0;
      return {agent,user,wave:user?wave:silence,muted,agentPlaying:playing,inputActive,analysisAvailable:context?.state==='running'};
    },
    destroy(){if(dead)return;dead=true;disconnect(input);disconnect(output);input=output=null;if(context){try{Promise.resolve(context.close()).catch(()=>{});}catch(_){}context=null;}},
  };
}
