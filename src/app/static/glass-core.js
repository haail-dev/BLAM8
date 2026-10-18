// Mist-only smoke-flow geometry. Visuals consume agent signals; they own no media.
const revision = new URL(import.meta.url).search;
const [{ BLAM8_MOTION_MARK }, { BLAM8_PARTICLE_MAP, BLAM8_GEAR_MASK }] = await Promise.all([
  import('./blam8-mark-motion.js' + revision),
  import('./blam8-particle-map.js' + revision),
]);
const vs=`precision highp float;attribute vec4 aSeed;attribute vec4 aMark;uniform float uTime,uCameraTime,uWorkTime,uThinking,uSpeech,uInput,uActivity,uScatter,uSize,uRibbon,uArrival;uniform vec2 uMouse;uniform vec3 uStrokes;varying float vAlpha,vColor,vFine,vRibbon,vPointSize;varying vec2 vPlane;
vec3 path(float a,float t){float fold=.5+.5*sin(t*.42);float winding=a+fold*.48*sin(2.*a-t*.2);return vec3((.53+.10*cos(3.*a-t*.34))*cos(winding),(.38+.10*cos(3.*a-t*.34))*sin(winding),.32*sin(2.*a+t*.36)+.07*sin(3.*a-t*.41));}
// Preserve the accepted loose user ribbon. Its travelling contraction
// pulls one local section inward and along its tangent, crossing its loose
// fold in depth before release. No wholesale path/topology replacement occurs.
vec3 foldedPath(float a,float s,float t,float travelTime,float force){vec3 base=path(a,t);float center=.75-1.5*abs(2.*fract(travelTime*.35)-1.);float local=(s-center)/.33;float fold=exp(-local*local*.72);float curl=2.55*local;float pull=(.14+.33*force)*fold;
vec2 tangent=normalize(path(a+.006,t).xy-path(a-.006,t).xy);vec2 inward=-normalize(base.xy);
base.xy+=tangent*pull*sin(curl)+inward*pull*.86*cos(curl*.84);base.z+=pull*1.05*sin(curl+.4);return base;}
// Thinking alone uses a compact evolving 3D knot. Material coordinates and
// the accumulated phase never restart; the accepted user ribbon stays intact.
vec3 knotPath(float a,float t){
float warp=.34*sin(3.*a+t*.71)+.19*sin(5.*a-t*.43)+.12*cos(2.*a+t*.293);
float around=2.*a+warp+.24*sin(t*.227);
float coil=3.*a+.43*sin(2.*a-t*.593)+t*.379;
float d0=sin((a-t*.413-.73*sin(t*.281))*.5);
float d1=sin((a+t*.197-2.1-.51*cos(t*.367))*.5);
float d2=sin((a-t*.139-4.3-.62*sin(t*.419))*.5);
float f0=exp(-d0*d0*(11.+3.*sin(t*.313)));
float f1=exp(-d1*d1*(15.+4.*cos(t*.239)));
float f2=exp(-d2*d2*(13.+3.*sin(t*.457)));
float contraction=.31*f0*(.7+.3*sin(t*.839))+.25*f1*(.65+.35*cos(t*.617));
float radius=.175+.035*sin(2.*a-t*.331);
vec3 p=vec3((radius+.105*cos(coil))*cos(around),(radius+.105*cos(coil))*sin(around),.16*sin(coil)+.055*sin(4.*a+t*.487));
p*=1.-contraction;
p+=vec3(-sin(around),cos(around),.65*cos(coil))*(.065*f2*sin(t*.947+coil)+.035*f0*cos(coil+t*.563));
float yaw=.19*t+.27*sin(t*.173),pitch=.37*sin(t*.211)+.16*cos(t*.307);
p=vec3(cos(yaw)*p.x+sin(yaw)*p.z,p.y,-sin(yaw)*p.x+cos(yaw)*p.z);
return vec3(p.x,cos(pitch)*p.y-sin(pitch)*p.z,sin(pitch)*p.y+cos(pitch)*p.z);}
void main(){float t=uTime;float a=aMark.w*6.2831853+t*.32;float spread=uScatter;
float s=aMark.w*2.-1.;float activity=uActivity;float force=clamp(max(uThinking*.82,uSpeech*1.7),0.,1.);float foldTime=mix(t,uWorkTime,uThinking);vec3 p=mix(path(a,t),foldedPath(a,s,t,foldTime,force),activity);vec3 tangent=normalize(mix(path(a+.005,t)-path(a-.005,t),foldedPath(a+.005,s+.0016,t,foldTime,force)-foldedPath(a-.005,s-.0016,t,foldTime,force),activity));
vec3 side=normalize(cross(tangent,vec3(.2,.1,1.)));vec3 up=normalize(cross(tangent,side));
float q=(aSeed.y-.5)*2.;float w=(aSeed.z-.5)*2.;float swirl=a*2.+t*.5;
float f1=q*cos(swirl)-w*sin(swirl),f2=q*sin(swirl)+w*cos(swirl);
vec3 work=vec3(0.);
if(uThinking>.0001){work=knotPath(a,t*1.7);vec3 kt=normalize(knotPath(a+.005,t*1.7)-knotPath(a-.005,t*1.7));vec3 ks=normalize(cross(kt,vec3(.2,.1,1.))),ku=normalize(cross(kt,ks));work+=ks*f1*(.085+spread*.045)+ku*f2*(.03+spread*.045);}

p+=side*f1*mix(.14+spread*.14,.085+spread*.045,activity)+up*f2*mix(.035+spread*.14,.03+spread*.045,activity);
p+=side*sin(a*6.3+t*1.8)*uInput*.075+up*cos(a*4.7-t)*uInput*.035;
p+=vec3(sin(aSeed.w*67.+t*.41),cos(aSeed.w*53.+t*.37),sin(aSeed.w*79.-t*.31))*.035*spread;
p.x+=uMouse.x*.04*sin(a);p.y+=uMouse.y*.04*cos(a);
// Camera time freezes during active folds and resumes from that orientation.
// A long idle can never unwind its accumulated angle on speech onset/release.
float yaw=uCameraTime*.19+.25;p=vec3(cos(yaw)*p.x+sin(yaw)*p.z,p.y,-sin(yaw)*p.x+cos(yaw)*p.z);float pitch=.48*sin(uCameraTime*.23);p=vec3(p.x,cos(pitch)*p.y-sin(pitch)*p.z,sin(pitch)*p.y+cos(pitch)*p.z);
// One continuous particle field travels between exact-logo samples and the ribbon.
// Each point orbits only as far as its precomputed clearance from every real
// contour edge permits. The B stays filled and its gear cutouts stay open.
float orbit=t*(6.7+.6*aSeed.z)+aMark.w*18.849556+aSeed.w*.28;
vec2 markFlow=vec2(cos(orbit),sin(orbit))*aMark.z*mix(.94,.78,uArrival);
vec3 mark=vec3(aMark.xy+markFlow,0.);
// Original speed strokes move on their own fixed mechanical clock.
vec2 logo=vec2(aMark.x*50./.62+50.,50.-aMark.y*50./.62);
if(logo.x<39. && logo.y>30. && logo.y<35.)mark.x=-.148676+(mark.x+.148676)*uStrokes.x;
if(logo.x<35. && logo.y>47. && logo.y<53.)mark.x=-.194928+(mark.x+.194928)*uStrokes.y;
if(logo.x<31.5 && logo.y>65. && logo.y<70.)mark.x=-.24118+(mark.x+.24118)*uStrokes.z;
// Arc each particle outwards rather than draw a chord across the centre;
// a straight mix crushes the B into a bright central blob halfway through.
float fromAngle=atan(mark.y,mark.x),toAngle=atan(p.y,p.x);
float angleDelta=mod(toAngle-fromAngle+3.14159265,6.2831853)-3.14159265;
float angle=fromAngle+angleDelta*uRibbon;
float radius=mix(length(mark.xy),length(p.xy),uRibbon);
p=vec3(vec2(cos(angle),sin(angle))*radius,p.z*uRibbon);
// The knot contracts in Cartesian space after the accepted user arc. Its
// depth-crossing target cannot flip a shortest-angle branch during entry.
p=mix(p,work,uThinking);
// The smoke should wrap around the faint mark, while staying inside the
// circular tap target. Scale the projected particles, not the canvas box.
float z=3.2-p.z;gl_Position=vec4(p.x*3.2,p.y*3.2,1.02*z-.202,z);
vPlane=gl_Position.xy/gl_Position.w;vRibbon=uRibbon;vFine=step(.955,aSeed.w);gl_PointSize=mix(mix(16.,(44.+spread*48.)*(.7+aSeed.w*.6),uRibbon),6.,vFine)*uSize/z;
vPointSize=gl_PointSize;float flow=.5+.5*sin(aMark.w*18.849556-t*8.);
vAlpha=mix(mix(.74,.16,uRibbon),.65,vFine)*(.55+.45*sin(aSeed.w*50.+t*.35)*sin(aSeed.w*50.+t*.35))*mix(.35+1.15*flow,1.,uRibbon);vAlpha*=mix(1.,.42+.58*smoothstep(-.35,.35,p.z),uThinking);vColor=.5+.5*sin(a+.4*sin(t*.2));}`;
const fs=`precision highp float;uniform float uMuted,uPixel,uGearTop,uGearLower;uniform sampler2D uGearMask;varying float vAlpha,vColor,vFine,vRibbon,vPointSize;varying vec2 vPlane;
vec2 rotatePoint(vec2 p,float a){return mat2(cos(a),sin(a),-sin(a),cos(a))*p;}
float gearCut(vec2 point,vec2 center,float angle,bool top){
vec2 local=(point-center)/.3968,rotated=rotatePoint(local,angle);
if(max(abs(rotated.x),abs(rotated.y))>=.5)return 0.;
vec4 gear=texture2D(uGearMask,rotated+.5);float hub=0.;
if(max(abs(local.x),abs(local.y))<.5){vec4 eyes=texture2D(uGearMask,local+.5);hub=top?eyes.b:eyes.a;}
return (top?gear.r:gear.g)*(1.-hub);}
void main(){vec2 p=gl_PointCoord*2.-1.;float r=dot(p,p);if(r>1.)discard;float a=exp(-r*4.5)*(1.-smoothstep(.55,1.,r))*vAlpha;
if(vRibbon<.2){vec2 point=vPlane+vec2(gl_PointCoord.x-.5,.5-gl_PointCoord.y)*vPointSize*uPixel;
float cut=max(gearCut(point,vec2(.16678,.20088),uGearTop,true),gearCut(point,vec2(.08432,-.17856),uGearLower,false));a*=1.-cut*(1.-smoothstep(0.,.2,vRibbon));}
vec3 c=mix(vec3(.12,.68,1.),vec3(.65,.34,1.),vColor);c=mix(c,vec3(.73,.9,1.),vFine*.6);vec3 warm=mix(vec3(1.,.12,.035),vec3(1.,.65,.06),vColor);warm=mix(warm,vec3(1.,.92,.28),vFine*.75);c=mix(c,warm,uMuted);gl_FragColor=vec4(c,a);}`;
// Pre-sampled from those eight genuine contours at build time. Decoding this
// twelve-KB map avoids path sampling on the critical voice-open path.
function markSamples() {
  const bytes=atob(BLAM8_PARTICLE_MAP),points=new Float32Array(3000*4);
  if(bytes.length!==points.length)throw Error('Logo particle map unavailable');
  for(let i=0;i<points.length;i+=4){
    const x=18+bytes.charCodeAt(i)*64/255,y=17+bytes.charCodeAt(i+1)*66/255;
    points[i]=(x-50)/50*.62;points[i+1]=(50-y)/50*.62;
    points[i+2]=bytes.charCodeAt(i+2)/255*5.5/50*.62;
    points[i+3]=bytes.charCodeAt(i+3)/255;
  }
  return points;
}
export function createGlassCore(host, readSignals, onActive=()=>{}) {
  let dead=false, failed=false, visible=true, frame=0, last=0, lastDraw=0;
  let elapsed=0, cameraElapsed=0, workElapsed=0, thinking=0, agentEnvelope=0, inputEnvelope=0, shapeActivity=0, speed=.3, scatter=.15, draws=0;
  let mechanicalElapsed=0,markAnimations=null,mechanicalPose={top:0,lower:0,strokes:[1,1,1]};
  let phase='connecting',mode='connecting',ribbon=0,arrival=0,readySeen=false;
  let userOnset=0,userQuiet=0,userActive=false,agentOnset=0,agentQuiet=0,agentActive=false;
  const reduced=typeof matchMedia==='function'?matchMedia('(prefers-reduced-motion: reduce)'):{matches:true};
  const layer=document.createElement('div');layer.className='vl-glass-layer';layer.setAttribute('aria-hidden','true');
  const core=document.createElement('canvas');
  layer.innerHTML=BLAM8_MOTION_MARK;layer.append(core);host.prepend(layer);
  const resources=[];
  let c,ro,io;
  function morph(signals,dt) {
    if(!signals.ready){
      if(readySeen)arrival=0;
      readySeen=false;mode=phase='connecting';
      userOnset=userQuiet=agentOnset=agentQuiet=0;userActive=agentActive=false;
      agentEnvelope=inputEnvelope=0;
      ribbon=Math.max(0,ribbon-dt/.7);arrival=Math.min(1,arrival+dt/.85);
      return;
    }
    readySeen=true;arrival=Math.min(1,arrival+dt/.85);
    // The microphone analyser supplies normalized RMS, not a VAD flag.
    // Require sustained energy to enter speech and a quieter release window
    // to bridge syllables. Muted, absent, or blocked input cannot arm it.
    const validInput=signals.inputActive===true && !signals.muted && signals.analysisAvailable!==false;
    const user=validInput?Math.max(0,Math.min(1,signals.user||0)):0;
    if(!validInput){userOnset=userQuiet=0;userActive=false;}
    else if(user>.07){userOnset=Math.min(.2,userOnset+dt);userQuiet=0;if(userOnset>=.12)userActive=true;}
    else {userOnset=Math.max(0,userOnset-dt*3);if(user<.035)userQuiet+=dt;else userQuiet=0;if(userQuiet>=.6)userActive=false;}
    // The decoded output meter is owned by actual audible playback. Mere
    // transport readiness or a playing-but-silent element never arms speech.
    const decoded=signals.agentPlaying && signals.analysisAvailable!==false?Math.max(0,Math.min(1,signals.agent||0)):0;
    if(decoded>.075){agentOnset=Math.min(.16,agentOnset+dt);agentQuiet=0;if(agentOnset>=.08)agentActive=true;}
    else {agentOnset=Math.max(0,agentOnset-dt*3);if(agentEnvelope<.04)agentQuiet+=dt;else agentQuiet=0;if(agentQuiet>.45)agentActive=false;}
    mode=userActive?'user':agentActive?'agent':signals.thinking?'thinking':'quiet';
    if(mode==='user'){
      ribbon=Math.min(1,ribbon+dt/1.15);
      phase=ribbon===1?'listening':'user-unfold';
    }else if(mode==='thinking'){
      ribbon=Math.min(1,ribbon+dt/.9);
      phase=ribbon===1?'thinking':'work-unfold';
    }else{
      ribbon=Math.max(0,ribbon-dt/1.05);
      phase=ribbon===0?(mode==='agent'?'speaking-b':'quiet-b'):(mode==='agent'?'agent-gather':'quiet-gather');
    }
  }
  function release() {
    for(const resource of resources.splice(0)) {
      const {gl,program,shaders,buffers,textures}=resource;
      for(const texture of textures)gl.deleteTexture(texture);
      for(const buffer of buffers)gl.deleteBuffer(buffer);
      for(const shader of shaders)gl.deleteShader(shader);
      gl.deleteProgram(program);
      gl.getExtension('WEBGL_lose_context')?.loseContext();
    }
  }
  function fallback() {
    if(dead || failed)return;failed=true;cancelAnimationFrame(frame);frame=0;
    host.classList.remove('has-glass');host.dataset.glass='fallback';onActive(false);ro?.disconnect();io?.disconnect();document.removeEventListener('visibilitychange',sync);reduced.removeEventListener?.('change',sync);release();layer.remove();
  }
  function make(canvas,vertex,fragment) {
    const gl=canvas.getContext('webgl',{alpha:true,antialias:false,premultipliedAlpha:false,preserveDrawingBuffer:true});
    if(!gl)throw Error('Graphics unavailable');
    const program=gl.createProgram(),record={gl,program,shaders:[],buffers:[],textures:[]};resources.push(record);
    for(const [type,source] of [[gl.VERTEX_SHADER,vertex],[gl.FRAGMENT_SHADER,fragment]]) {
      const shader=gl.createShader(type);record.shaders.push(shader);gl.shaderSource(shader,source);gl.compileShader(shader);
      if(!gl.getShaderParameter(shader,gl.COMPILE_STATUS))throw Error('Shader unavailable');gl.attachShader(program,shader);
    }
    gl.linkProgram(program);if(!gl.getProgramParameter(program,gl.LINK_STATUS))throw Error('Graphics unavailable');gl.useProgram(program);
    const locations=new Map();
    return {...record,uniform(name){if(!locations.has(name))locations.set(name,gl.getUniformLocation(program,name));return locations.get(name);}};
  }
  function bind(renderer,data,name,size) {
    const gl=renderer.gl,buffer=gl.createBuffer();renderer.buffers.push(buffer);gl.bindBuffer(gl.ARRAY_BUFFER,buffer);gl.bufferData(gl.ARRAY_BUFFER,data,gl.STATIC_DRAW);
    const at=gl.getAttribLocation(renderer.program,name);gl.enableVertexAttribArray(at);gl.vertexAttribPointer(at,size,gl.FLOAT,false,0,0);return buffer;
  }
  function render(signals={}) {
    if(dead || failed || !c)return;
    host.dataset.mistMode=mode;
    if(!markAnimations)markAnimations=['.vl-gear-top','.vl-gear-lower','.vl-speed-stroke-0','.vl-speed-stroke-1','.vl-speed-stroke-2'].map(selector=>layer.querySelector(selector)?.getAnimations?.()[0] || null);
    const mechanicalTime=index=>reduced.matches?0:typeof markAnimations[index]?.currentTime==='number'?markAnimations[index].currentTime/1000:mechanicalElapsed;
    mechanicalPose={top:mechanicalTime(0)*Math.PI*2/14,lower:-mechanicalTime(1)*Math.PI*2/18,
      strokes:[0,1,2].map(i=>1+.12*(.5-.5*Math.cos(Math.max(0,mechanicalTime(i+2)-i*.7)*Math.PI*2/5.8)))};
    const muted=Boolean(signals.muted),cg=c.gl;
    cg.uniform1f(c.uniform('uPixel'),2/core.width);cg.uniform1f(c.uniform('uGearTop'),mechanicalPose.top);cg.uniform1f(c.uniform('uGearLower'),mechanicalPose.lower);cg.uniform3f(c.uniform('uStrokes'),...mechanicalPose.strokes);
    cg.clear(cg.COLOR_BUFFER_BIT);cg.uniform1f(c.uniform('uTime'),elapsed);cg.uniform1f(c.uniform('uCameraTime'),cameraElapsed);cg.uniform1f(c.uniform('uWorkTime'),workElapsed);cg.uniform1f(c.uniform('uThinking'),thinking*thinking*(3-2*thinking));cg.uniform1f(c.uniform('uSpeech'),mode==='agent'?0:agentEnvelope);cg.uniform1f(c.uniform('uInput'),inputEnvelope);cg.uniform1f(c.uniform('uActivity'),shapeActivity*shapeActivity*(3-2*shapeActivity));cg.uniform1f(c.uniform('uScatter'),scatter);cg.uniform1f(c.uniform('uRibbon'),ribbon*ribbon*(3-2*ribbon));cg.uniform1f(c.uniform('uArrival'),arrival*arrival*(3-2*arrival));
    cg.uniform1f(c.uniform('uMuted'),muted?1:0);cg.uniform1f(c.uniform('uSize'),core.width/430);cg.uniform2f(c.uniform('uMouse'),0,0);cg.drawArrays(cg.POINTS,0,3000);
    draws++;
  }
  // Keep bounded backing stores stable: WebKit can lose composited canvases after reallocation.
  function resize(){if(dead || failed)return;const size=256;for(const [canvas,renderer] of [[core,c]]){if(canvas.width!==size)canvas.width=size;if(canvas.height!==size)canvas.height=size;renderer.gl.viewport(0,0,size,size);}render(readSignals());}
  function active(){return !dead && !failed && visible && !document.hidden && !reduced.matches;}
  function tick(now) {
    frame=0;if(!active())return;
    if(now-lastDraw>=1000/30) {
      const dt=last?Math.min((now-last)/1000,.1):0;last=now;lastDraw=now;
      const signals=readSignals(),agent=signals.ready && signals.agentPlaying && signals.analysisAvailable!==false?Math.max(0,Math.min(1,signals.agent || 0)):0;
      if(!signals.ready || !signals.agentPlaying || signals.analysisAvailable===false)agentEnvelope=0;
      agentEnvelope+=(agent-agentEnvelope)*(1-Math.exp(-dt*(agent>agentEnvelope?10:3)));
      mechanicalElapsed+=dt;morph(signals,dt);
      const validInput=signals.ready && signals.inputActive===true && !signals.muted && signals.analysisAvailable!==false;
      let wavePeak=0;
      if(validInput && mode==='user' && signals.wave)for(const sample of signals.wave)wavePeak=Math.max(wavePeak,Math.abs(sample));
      const input=mode==='user'?Math.min(1,Math.max(signals.user||0,wavePeak*.35)):0;
      inputEnvelope+=(input-inputEnvelope)*(1-Math.exp(-dt*(input>inputEnvelope?12:5)));
      // Work folds the expanded ribbon. Audible output is a separate B pose.
      const working=mode==='thinking';
      thinking=Math.max(0,Math.min(1,thinking+(working?dt/.75:-dt/.68)));
      if(working)workElapsed+=dt;
      const folding=working || mode==='user';
      shapeActivity=Math.max(0,Math.min(1,shapeActivity+(folding?dt/.55:-dt/.85)));
      speed=.3+.9*agentEnvelope+.55*inputEnvelope;scatter=.15+.5*agentEnvelope+.35*inputEnvelope;elapsed+=dt*speed;
      if(!folding && shapeActivity===0)cameraElapsed+=dt*speed;
      render(signals);
    }
    frame=requestAnimationFrame(tick);
  }
  function sync(){if(dead || failed)return;markAnimations=null;cancelAnimationFrame(frame);frame=0;last=0;lastDraw=0;onActive(active());if(active()){if(phase.startsWith('reduced-'))phase=readySeen?'quiet-b':'connecting';frame=requestAnimationFrame(tick);}else{agentEnvelope=inputEnvelope=0;speed=.3;scatter=.15;const signals=readSignals();if(reduced.matches){thinking=shapeActivity=0;ribbon=0;arrival=1;readySeen=Boolean(signals.ready);mode=signals.ready?'quiet':'connecting';phase=signals.ready?'reduced-b':'reduced-mark';userOnset=userQuiet=agentOnset=agentQuiet=0;userActive=agentActive=false;}render(signals);}}
  const lost=event=>{event.preventDefault();fallback();};
  try {
    c=make(core,vs,fs);
    let seedState=971;const seed=new Float32Array(3000*4);for(let i=0;i<seed.length;i++){seedState=(seedState*1664525+1013904223)>>>0;seed[i]=seedState/4294967296;}
    const packed=atob(BLAM8_GEAR_MASK),pixels=new Uint8Array(128*128*4);
    if(packed.length!==8192)throw Error('Mechanical mask unavailable');
    for(let i=0;i<128*128;i++){const value=(packed.charCodeAt(i>>1)>>((i%2)*4))&15;for(let j=0;j<4;j++)pixels[i*4+j]=(value&(1<<j))?255:0;}
    const texture=c.gl.createTexture();if(!texture)throw Error('Mechanical mask unavailable');c.textures.push(texture);c.gl.activeTexture(c.gl.TEXTURE0);c.gl.bindTexture(c.gl.TEXTURE_2D,texture);
    c.gl.texImage2D(c.gl.TEXTURE_2D,0,c.gl.RGBA,128,128,0,c.gl.RGBA,c.gl.UNSIGNED_BYTE,pixels);
    for(const name of [c.gl.TEXTURE_MIN_FILTER,c.gl.TEXTURE_MAG_FILTER])c.gl.texParameteri(c.gl.TEXTURE_2D,name,c.gl.LINEAR);
    for(const name of [c.gl.TEXTURE_WRAP_S,c.gl.TEXTURE_WRAP_T])c.gl.texParameteri(c.gl.TEXTURE_2D,name,c.gl.CLAMP_TO_EDGE);
    c.gl.uniform1i(c.uniform('uGearMask'),0);
    bind(c,seed,'aSeed',4);bind(c,markSamples(),'aMark',4);c.gl.enable(c.gl.BLEND);c.gl.blendFuncSeparate(c.gl.SRC_ALPHA,c.gl.ONE_MINUS_SRC_ALPHA,c.gl.ONE,c.gl.ONE_MINUS_SRC_ALPHA);c.gl.clearColor(0,0,0,0);
    core.addEventListener('webglcontextlost',lost);
    ro=new ResizeObserver(resize);ro.observe(host);io=new IntersectionObserver(entries=>{visible=entries[0].isIntersecting;sync();});io.observe(host);
    document.addEventListener('visibilitychange',sync);reduced.addEventListener?.('change',sync);
    host.classList.add('has-glass');host.dataset.glass='ready';resize();sync();
  } catch(_){fallback();}
  return {update(){if(!dead && !failed){const signals=readSignals();if(!signals.ready || !signals.agentPlaying || signals.analysisAvailable===false){agentEnvelope=0;speed=.3;scatter=.15;}if(reduced.matches){thinking=shapeActivity=0;ribbon=0;arrival=1;readySeen=Boolean(signals.ready);mode=signals.ready?'quiet':'connecting';phase=signals.ready?'reduced-b':'reduced-mark';userOnset=userQuiet=agentOnset=agentQuiet=0;userActive=agentActive=false;}else if(!signals.ready){if(readySeen)arrival=0;readySeen=false;mode=phase='connecting';agentEnvelope=inputEnvelope=0;userOnset=userQuiet=agentOnset=agentQuiet=0;userActive=agentActive=false;}render(signals);}},
    snapshot:()=>({maskTextures:resources.reduce((total,resource)=>total+resource.textures.length,0),mechanicalElapsed,mechanicalPose,elapsed,cameraElapsed,workElapsed,thinking,shapeActivity,envelope:0,agentEnvelope,inputEnvelope,speed,scatter,draws,active:active(),failed,pixels:core.width,particles:3000,phase,mode,ribbon,arrival,userOnset,userQuiet,userActive,agentOnset,agentQuiet,agentActive,readySeen}),
    destroy(){if(dead)return;dead=true;cancelAnimationFrame(frame);frame=0;ro?.disconnect();io?.disconnect();document.removeEventListener('visibilitychange',sync);reduced.removeEventListener?.('change',sync);core.removeEventListener('webglcontextlost',lost);onActive(false);release();layer.remove();host.classList.remove('has-glass');delete host.dataset.glass;delete host.dataset.mistMode;},
  };
}
