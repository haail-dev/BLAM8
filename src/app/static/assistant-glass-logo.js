/* Landing-page Assistant glass B. Self-contained, transparent cog openings.
 * Approved mist, slow cog-driven swells and rounded glass; no voice/Pond hooks.
 * The still remains usable without JS, with reduced motion or with data saver.
 */
(()=>{
 'use strict';
 const root=document.querySelector('[data-assistant-glass-logo]');
 if(!root)return;
 const still=root.querySelector('img'),motion=matchMedia('(prefers-reduced-motion: reduce)');
 const connection=navigator.connection;
 let animator=null,failed=false;
 function syncMotion(){
   if(motion.matches||(connection&&connection.saveData)){if(animator)animator.pause();return;}
   if(failed)return;
   try{if(!animator)animator=mount();animator.resume();}
   catch(error){failed=true;still.hidden=false;root.querySelectorAll('canvas').forEach(el=>el.remove());console.warn('Assistant logo using still image',error);}
 }
 motion.addEventListener('change',syncMotion);
 if(connection&&connection.addEventListener)connection.addEventListener('change',syncMotion);
 if(window.requestIdleCallback)requestIdleCallback(syncMotion,{timeout:1500});else setTimeout(syncMotion,150);
 function mount(){
 const canvas=document.createElement('canvas');canvas.width=canvas.height=128;
 canvas.setAttribute('aria-hidden','true');canvas.className='bc-glass-canvas';
 const source=document.createElement('canvas');source.width=600;source.height=600;
 const c=source.getContext('2d');
 const outputs=[{el:canvas,ctx:canvas.getContext('2d')}];
 const brand=new Path2D('M35.42 15.8 L59.64 15.8 L62.47 16.04 L63.6 16.27 L64.45 16.51 L65.14 16.75 L65.74 16.99 L66.27 17.23 L66.75 17.46 L67.19 17.7 L67.59 17.94 L67.96 18.18 L68.31 18.41 L68.63 18.65 L68.94 18.89 L69.22 19.13 L69.5 19.36 L69.75 19.6 L69.99 19.84 L70.22 20.08 L70.44 20.31 L70.65 20.55 L70.85 20.79 L71.04 21.03 L71.22 21.26 L71.39 21.5 L71.55 21.74 L71.71 21.98 L71.86 22.21 L72 22.45 L72.13 22.69 L72.26 22.93 L72.38 23.16 L72.5 23.4 L72.61 23.64 L72.71 23.88 L72.81 24.11 L72.91 24.35 L73 24.59 L73.08 24.83 L73.16 25.06 L73.24 25.3 L73.31 25.54 L73.37 25.78 L73.43 26.01 L73.49 26.25 L73.54 26.49 L73.59 26.73 L73.64 26.96 L73.68 27.2 L73.71 27.44 L73.75 27.68 L73.77 27.91 L73.8 28.15 L73.82 28.39 L73.84 28.63 L73.85 28.86 L73.86 29.1 L73.87 29.34 L73.88 29.57 L73.88 29.81 L73.87 30.05 L73.87 30.29 L73.86 30.53 L73.84 30.76 L73.83 31 L73.81 31.24 L73.78 31.48 L73.76 31.71 L73.73 31.95 L73.7 32.19 L73.66 32.42 L73.62 32.66 L73.58 32.9 L73.53 33.14 L73.48 33.38 L73.43 33.61 L73.38 33.85 L73.32 34.09 L73.26 34.33 L73.19 34.56 L73.12 34.8 L73.05 35.04 L72.98 35.27 L72.9 35.51 L72.82 35.75 L72.73 35.99 L72.65 36.23 L72.55 36.46 L72.46 36.7 L72.36 36.94 L72.26 37.17 L72.16 37.41 L72.05 37.65 L71.94 37.89 L71.82 38.13 L71.7 38.36 L71.58 38.6 L71.45 38.84 L71.32 39.08 L71.19 39.31 L71.05 39.55 L70.91 39.79 L70.76 40.02 L70.61 40.26 L70.46 40.5 L70.3 40.74 L70.14 40.98 L69.97 41.21 L69.8 41.45 L69.63 41.69 L69.45 41.92 L69.26 42.16 L69.07 42.4 L68.88 42.64 L68.68 42.88 L68.47 43.11 L68.26 43.35 L68.04 43.59 L67.82 43.83 L67.59 44.06 L67.35 44.3 L67.11 44.54 L66.86 44.77 L66.61 45.01 L66.34 45.25 L66.07 45.49 L65.79 45.73 L65.5 45.96 L65.2 46.2 L64.89 46.44 L64.57 46.67 L64.24 46.91 L63.9 47.15 L63.54 47.39 L63.17 47.63 L62.78 47.86 L62.37 48.1 L61.95 48.34 L61.5 48.58 L61.25 48.81 L61.7 49.05 L62.12 49.29 L62.5 49.52 L62.86 49.76 L63.2 50 L63.51 50.24 L63.81 50.48 L64.1 50.71 L64.36 50.95 L64.62 51.19 L64.86 51.42 L65.09 51.66 L65.31 51.9 L65.52 52.14 L65.72 52.38 L65.91 52.61 L66.09 52.85 L66.26 53.09 L66.43 53.33 L66.58 53.56 L66.74 53.8 L66.88 54.04 L67.02 54.27 L67.15 54.51 L67.28 54.75 L67.4 54.99 L67.51 55.23 L67.62 55.46 L67.72 55.7 L67.82 55.94 L67.92 56.17 L68.01 56.41 L68.09 56.65 L68.17 56.89 L68.24 57.13 L68.32 57.36 L68.38 57.6 L68.44 57.84 L68.5 58.08 L68.56 58.31 L68.61 58.55 L68.65 58.79 L68.7 59.02 L68.74 59.26 L68.77 59.5 L68.8 59.74 L68.83 59.98 L68.86 60.21 L68.88 60.45 L68.89 60.69 L68.91 60.92 L68.92 61.16 L68.93 61.4 L68.93 61.64 L68.93 61.88 L68.93 62.11 L68.92 62.35 L68.92 62.59 L68.9 62.83 L68.89 63.06 L68.87 63.3 L68.85 63.54 L68.82 63.77 L68.8 64.01 L68.77 64.25 L68.73 64.49 L68.7 64.72 L68.66 64.96 L68.61 65.2 L68.57 65.44 L68.52 65.67 L68.47 65.91 L68.41 66.15 L68.35 66.39 L68.29 66.63 L68.23 66.86 L68.16 67.1 L68.09 67.34 L68.02 67.58 L67.94 67.81 L67.86 68.05 L67.78 68.29 L67.69 68.53 L67.6 68.76 L67.51 69 L67.42 69.24 L67.32 69.47 L67.21 69.71 L67.11 69.95 L67 70.19 L66.89 70.42 L66.77 70.66 L66.66 70.9 L66.53 71.14 L66.41 71.38 L66.28 71.61 L66.15 71.85 L66.01 72.09 L65.87 72.33 L65.73 72.56 L65.58 72.8 L65.43 73.04 L65.27 73.28 L65.11 73.51 L64.95 73.75 L64.78 73.99 L64.61 74.22 L64.43 74.46 L64.25 74.7 L64.07 74.94 L63.88 75.17 L63.68 75.41 L63.48 75.65 L63.28 75.89 L63.07 76.13 L62.85 76.36 L62.63 76.6 L62.4 76.84 L62.17 77.08 L61.93 77.31 L61.69 77.55 L61.44 77.79 L61.18 78.03 L60.91 78.26 L60.63 78.5 L60.35 78.74 L60.06 78.97 L59.76 79.21 L59.45 79.45 L59.13 79.69 L58.8 79.92 L58.46 80.16 L58.1 80.4 L57.74 80.64 L57.35 80.88 L56.95 81.11 L56.53 81.35 L56.1 81.59 L55.64 81.83 L55.15 82.06 L54.63 82.3 L54.08 82.54 L53.48 82.78 L52.83 83.01 L52.11 83.25 L51.3 83.49 L50.32 83.72 L49.06 83.96 L46.06 84.2 L20.88 84.2 Z');
 const transform=new DOMMatrix([4.5,0,0,4.5,0,-32]);
 const body=new Path2D();body.addPath(brand,transform);
 let time=0,last=0,visible=true,active=false,raf=0;
 const parameters={flow:1,smoke:1.35};
 const tau=Math.PI*2;
 let seed=82731;
 function random(){seed=(Math.imul(seed,1664525)+1013904223)>>>0;return seed/4294967296;}
 const clamp=(x,a,b)=>Math.max(a,Math.min(b,x));
 const holes=[{x:239,y:117,r:39,spin:.62},{x:211,y:259,r:44,spin:-.55}];
 function gear(x,y,r,angle){
   c.save();c.translate(x,y);c.rotate(angle);c.beginPath();
   for(let i=0;i<48;i++){const a=i/48*tau,rr=(i%4===1||i%4===2)?r:r*.80;const px=Math.cos(a)*rr,py=Math.sin(a)*rr;if(i===0)c.moveTo(px,py);else c.lineTo(px,py);}
   c.closePath();c.fill();c.restore();
 }
 // Geometry-aware flow field. A distance transform supplies the outer wall
 // normal and an estimate of channel width between each gear and the B wall.
 const n=134,cell=3,dist=new Float32Array(n*n),inside=new Uint8Array(n*n);
 for(let y=0;y<n;y++)for(let x=0;x<n;x++){
   const i=y*n+x;inside[i]=c.isPointInPath(body,x*cell,y*cell)?1:0;dist[i]=inside[i]?1e4:0;
 }
 for(let y=1;y<n-1;y++)for(let x=1;x<n-1;x++){
   const i=y*n+x;dist[i]=Math.min(dist[i],dist[i-1]+cell,dist[i-n]+cell,dist[i-n-1]+cell*Math.SQRT2,dist[i-n+1]+cell*Math.SQRT2);
 }
 for(let y=n-2;y>0;y--)for(let x=n-2;x>0;x--){
   const i=y*n+x;dist[i]=Math.min(dist[i],dist[i+1]+cell,dist[i+n]+cell,dist[i+n+1]+cell*Math.SQRT2,dist[i+n-1]+cell*Math.SQRT2);
 }
 function distance(x,y){
   const xx=clamp(x/cell,1,n-2.01),yy=clamp(y/cell,1,n-2.01),ix=xx|0,iy=yy|0,fx=xx-ix,fy=yy-iy,i=iy*n+ix;
   return (dist[i]*(1-fx)+dist[i+1]*fx)*(1-fy)+(dist[i+n]*(1-fx)+dist[i+n+1]*fx)*fy;
 }
 function field(x,y,t){
   let vx=0,vy=-3;
   // Finite-core vortices circulate mist around both transparent openings.
   for(let k=0;k<2;k++){
     const h=holes[k],dx=x-h.x,dy=y-h.y,r2=dx*dx+dy*dy+950;
     const strength=k===0?1900:-2000;
     vx+=-dy*strength/r2;vy+=dx*strength/r2;
   }
   // Curl of a smooth travelling streamfunction, not independent random jitter.
   // curl(psi)=(d psi/dy,-d psi/dx) creates locally swirling wisps.
   const a=x*.052+t*.36,b=y*.046-t*.28;
   vx+=5.5*Math.sin(a)*Math.cos(b)+2.3*Math.cos(x*.11-y*.075+t*.3);
   vy+=-6.2*Math.cos(a)*Math.sin(b)+3.3*Math.cos(x*.11-y*.075+t*.3);
   const outer=distance(x,y),gx=(distance(x+cell,y)-distance(x-cell,y))/(2*cell),gy=(distance(x,y+cell)-distance(x,y-cell))/(2*cell);
   const gl=Math.hypot(gx,gy)||1,nx=gx/gl,ny=gy/gl;
   const near=clamp((11-outer)/10,0,1),dot=vx*nx+vy*ny;
   vx-=dot*nx*near;vy-=dot*ny*near;
   vx+=nx*Math.max(0,3-outer)*6;vy+=ny*Math.max(0,3-outer)*6;
   let passage=90;
   for(const h of holes){
     const dx=x-h.x,dy=y-h.y,r=Math.hypot(dx,dy)||1,gap=r-h.r;
     passage=Math.min(passage,Math.max(5,outer+gap));
     const w=clamp((10-gap)/10,0,1),normal=(vx*dx+vy*dy)/r;
     vx-=normal*dx/r*w;vy-=normal*dy/r*w;
     vx+=dx/r*Math.max(0,3-gap)*3;vy+=dy/r*Math.max(0,3-gap)*3;
   }
   // A bounded v~Q/A approximation: tight passages stretch wisps, broad
   // sections relax them. This is an artistic flow model, not a CFD solver.
   const acceleration=clamp(54/passage,.60,3.40),speed=Math.hypot(vx,vy)||1;
   const factor=Math.min(42,speed*acceleration)/speed;
   return [vx*factor,vy*factor];
 }
 function spawn(p){
   for(let k=0;k<500;k++){
     const x=94+random()*240,y=39+random()*309;
     if(distance(x,y)>2 && (!p.outer || distance(x,y)<24) && holes.every(h=>Math.hypot(x-h.x,y-h.y)>h.r+(p.outer?18:1))){
       p.x=x;p.y=y;p.px=x;p.py=y;p.age=p.outer?0:random()*8;p.life=p.outer?18+random()*15:7+random()*11;p.size=.35+Math.pow(random(),3)*1.8;p.seed=random()*tau;return p;
     }
   }
   p.x=170;p.y=180;p.px=170;p.py=180;p.age=0;p.life=10;p.size=.5;p.seed=0;return p;
 }
 const particles=[...Array.from({length:5600},()=>spawn({})),...Array.from({length:4000},()=>{const p=spawn({outer:true});p.age=2+random()*10;return p;})];
 const dust=Array.from({length:3600},()=>spawn({}));
 const palettes=Array.from({length:16},(_,i)=>{
   const u=i/15;return `rgb(${Math.round(187-116*u)},${Math.round(105+104*u)},${Math.round(247+8*u)})`;
 });
 const color=x=>palettes[Math.round(clamp((x-110)/205,0,1)*15)];
 // Cache two smooth divergence-free curl states plus wall-guided circulation.
 // Interpolating the field produces continuously evolving motion, without
 // evaluating expensive geometry and transcendental functions per particle.
 const fields=[new Float32Array(n*n*2),new Float32Array(n*n*2)];
 for(let y=1;y<n-1;y++)for(let x=1;x<n-1;x++){
   if(!inside[y*n+x])continue;
   for(let k=0;k<2;k++){const v=field(x*cell,y*cell,k*8);fields[k][(y*n+x)*2]=v[0];fields[k][(y*n+x)*2+1]=v[1];}
 }
 let velocityX=0,velocityY=0;
 function sample(x,y,mix){
   const xx=clamp(x/cell,1,n-2.01),yy=clamp(y/cell,1,n-2.01),ix=xx|0,iy=yy|0,fx=xx-ix,fy=yy-iy,i=(iy*n+ix)*2;
   const w0=(1-fx)*(1-fy),w1=fx*(1-fy),w2=(1-fx)*fy,w3=fx*fy;
   const a=fields[0],b=fields[1],j=i+n*2;
   velocityX=(a[i]*w0+a[i+2]*w1+a[j]*w2+a[j+2]*w3)*(1-mix)+(b[i]*w0+b[i+2]*w1+b[j]*w2+b[j+2]*w3)*mix;
   velocityY=(a[i+1]*w0+a[i+3]*w1+a[j+1]*w2+a[j+3]*w3)*(1-mix)+(b[i+1]*w0+b[i+3]*w1+b[j+1]*w2+b[j+3]*w3)*mix;
   const wallPosition=clamp(distance(x,y)/30,0,1),wallDrag=.30+.70*wallPosition*wallPosition*(3-2*wallPosition); velocityX*=wallDrag;velocityY*=wallDrag;
   // No-slip entrainment: mist beside each tooth shares its angular speed.
   // v = omega x (position - cog centre). Blend into the wider flow only
   // beyond the near-cog band, so turbulence cannot make the contact slip.
   let nearest=holes[0],gap=Infinity;
   for(const h of holes){const g=Math.hypot(x-h.x,y-h.y)-h.r;if(g<gap){gap=g;nearest=h;}}
   const q=clamp((gap-10)/23,0,1),weight=1-q*q*(3-2*q);
   if(weight>0){
     const dx=x-nearest.x,dy=y-nearest.y;
     velocityX=velocityX*(1-weight)-nearest.spin*dy*weight;
     velocityY=velocityY*(1-weight)+nearest.spin*dx*weight;
   }

 }
 function advance(dt,t){
   const step=dt*parameters.flow,mix=.5+.5*Math.sin(t*.32);
   for(const p of particles){
     p.px=p.x;p.py=p.y;
     sample(p.x,p.y,mix);const mx=p.x+velocityX*step*.5,my=p.y+velocityY*step*.5;
     sample(mx,my,mix);p.vx=velocityX;p.vy=velocityY;
     p.x+=velocityX*step;p.y+=velocityY*step;p.age+=step;
     if(p.age>p.life||distance(p.x,p.y)<1||(p.outer&&distance(p.x,p.y)>30))spawn(p);
   }
 }
 const backdrop=document.createElement('canvas');backdrop.width=600;backdrop.height=600;
 const bc=backdrop.getContext('2d');bc.scale(1.5,1.5);bc.save();bc.clip(body);
 const haze=bc.createLinearGradient(90,290,330,80);
 haze.addColorStop(0,'rgba(152,91,215,.45)');haze.addColorStop(.48,'rgba(85,140,206,.38)');haze.addColorStop(1,'rgba(49,166,215,.46)');
 bc.fillStyle=haze;bc.fill(body);
 for(const p of dust){bc.fillStyle=color(p.x);bc.globalAlpha=.16+p.seed*.024;bc.fillRect(p.x,p.y,p.size*1.25,p.size);}
 bc.restore();
 const bars=[{x:65,y:100,w:57,h:17},{x:34,y:184,w:72,h:17},{x:49,y:267,w:40,h:16}];
 const barDust=bars.map(()=>Array.from({length:160},()=>({u:random(),v:random(),s:.5+random()*1.5,a:random(),phase:random()*tau})));
 // Ambient brand motion only; the landing chat has no live voice signal.
 let activityClock=0,drive=.72,energy=.45;
 function updateActivity(dt){
   activityClock+=dt;
   const smoothing=1-Math.exp(-dt*7);
   drive+=(.68+.09*Math.sin(activityClock*1.8)-drive)*smoothing;
   energy+=(.43-energy)*smoothing;
 }
 // Bounded wave equation adapted from the shared water-core model:
 // next = h + damp * (h - previous) + c^2 * discreteLaplacian(h).
 // This renderer extends a reflecting rectangle to a B-shaped domain
 // with moving toothed obstacles. No preset rings, drop cadence or ring pool.
 const waterN=134,waterCell=3,waterCount=waterN*waterN;
 let height=new Float32Array(waterCount),previous=new Float32Array(waterCount),nextHeight=new Float32Array(waterCount);
 const wet=new Uint8Array(waterCount),oldWet=new Uint8Array(waterCount);
 const wall=new Uint8Array(waterCount),waterCells=[];
 const cogOwner=new Int8Array(waterCount).fill(-1),cogDistance=new Float32Array(waterCount),cogAngle=new Float32Array(waterCount);
 const currentRadius=new Float32Array(waterCount),priorRadius=new Float32Array(waterCount);
 let simDebt=0,waterSteps=0,lakeMax=0,pointerDisturbed=false;
 const toothLookup=new Float32Array(720);
 function perimeterRadius(angle,r){
   const sector=tau/48,u=((angle%tau)+tau)%tau/sector,i=Math.floor(u),local=(u-i)*sector;
   const vertex=index=>(index%4===1||index%4===2)?r:r*.80;
   const r0=vertex(i),r1=vertex((i+1)%48);
   return r0*r1*Math.sin(sector)/(r1*Math.sin(sector-local)+r0*Math.sin(local));
 }
 for(let i=0;i<720;i++)toothLookup[i]=perimeterRadius(i/720*tau,1);
 function toothRadius(angle,r){
   const u=((angle/tau%1)+1)%1*720,i=u|0,f=u-i;
   return r*(toothLookup[i]*(1-f)+toothLookup[(i+1)%720]*f);
 }
 for(let y=1;y<waterN-1;y++)for(let x=1;x<waterN-1;x++){
   const i=y*waterN+x,px=x*waterCell,py=y*waterCell;
   if(!c.isPointInPath(body,px,py))continue;
   wall[i]=1;wet[i]=1;waterCells.push(i);
   for(let k=0;k<holes.length;k++){
     const h=holes[k],dx=px-h.x,dy=py-h.y,r=Math.hypot(dx,dy);
     if(r<h.r+18){cogOwner[i]=k;cogDistance[i]=r;cogAngle[i]=Math.atan2(dy,dx);break;}
   }
 }
 function updateWaterBoundary(driveTime,previousTime){
   oldWet.set(wet);
   for(const i of waterCells){
     const k=cogOwner[i];if(k<0)continue;
     const h=holes[k],radius=toothRadius(cogAngle[i]-h.spin*driveTime,h.r);
     currentRadius[i]=radius;priorRadius[i]=toothRadius(cogAngle[i]-h.spin*previousTime,h.r);
     wet[i]=cogDistance[i]>=radius?1:0;
   }
   for(const i of waterCells){
     if(!wet[i]){height[i]=previous[i]=nextHeight[i]=0;continue;}
     if(!oldWet[i]){
       let sum=0,v=0,count=0;
       for(const j of [i-1,i+1,i-waterN,i+waterN])if(oldWet[j]&&wet[j]){sum+=height[j];v+=previous[j];count++;}
       height[i]=count?sum/count:0;previous[i]=count?v/count:0;
     }
   }
 }
 // A gradual absorbing margin reduces repeated echoes while retaining the wall.
 const waterEdge=new Float32Array(waterCount);
 for(let i=0;i<waterCount;i++)waterEdge[i]=wall[i]?1000:0;
 for(let y=1;y<waterN-1;y++)for(let x=1;x<waterN-1;x++){
   const i=y*waterN+x;waterEdge[i]=Math.min(waterEdge[i],waterEdge[i-1]+1,waterEdge[i-waterN]+1,waterEdge[i-waterN-1]+Math.SQRT2,waterEdge[i-waterN+1]+Math.SQRT2);
 }
 for(let y=waterN-2;y>0;y--)for(let x=waterN-2;x>0;x--){
   const i=y*waterN+x;waterEdge[i]=Math.min(waterEdge[i],waterEdge[i+1]+1,waterEdge[i+waterN]+1,waterEdge[i+waterN+1]+Math.SQRT2,waterEdge[i+waterN-1]+Math.SQRT2);
 }
 updateWaterBoundary(0,0);
 function stepWater(driveTime,previousTime,forceEnabled=true){
   updateWaterBoundary(driveTime,previousTime);
   // Moving tooth flanks displace the adjacent water continuously. Pressure
   // changes sign on leading/trailing flanks: a rotating impeller, not drops.
   if(forceEnabled&&driveTime!==previousTime){
     for(const i of waterCells){
       if(!wet[i]||cogOwner[i]<0)continue;
       const gap=cogDistance[i]-currentRadius[i];if(gap>16)continue;
       const cog=holes[cogOwner[i]],a=cogAngle[i]-cog.spin*driveTime,b=cogAngle[i]-cog.spin*previousTime;
       // Coherent pressure over groups of teeth: broad rotating swells plus a
       // small amount of tooth-scale detail. Rotation drives both continuously.
       // This is a stylized impeller pressure model, not emitted ring sprites.
       const pressure=angle=>2.6*Math.cos(5*angle)+1.2*Math.cos(3*angle+.8)+.5*Math.cos(7*angle-1.2);
       const displacement=.12*(currentRadius[i]-priorRadius[i])+.88*(pressure(a)-pressure(b));
       const flank=Math.max(0,1-gap/16);
       previous[i]-=displacement*(.12/9)*flank*flank*(.45+.55*energy);
     }
   }
   const waterRate=1/3; const c2=(.6*waterRate)**2,damping=Math.pow(drive>.002?.9935:.975,waterRate);
   for(const i of waterCells){
     if(!wet[i])continue;
     const centre=height[i];
     // Neumann boundary: a solid neighbour contributes the centre value.
     // Waves therefore reflect at the B wall AND the rotating cog perimeter.
     const left=wet[i-1]?height[i-1]:centre,right=wet[i+1]?height[i+1]:centre;
     const up=wet[i-waterN]?height[i-waterN]:centre,down=wet[i+waterN]?height[i+waterN]:centre;
     const edgeLoss=.025*Math.pow(Math.max(0,1-waterEdge[i]/7),2);
     nextHeight[i]=centre+(centre-previous[i])*(damping-edgeLoss)+c2*(left+right+up+down-4*centre);
   }
   const swap=previous;previous=height;height=nextHeight;nextHeight=swap;
   waterSteps++;
   if(waterSteps%6===0){
     let sum=0,count=0;for(const i of waterCells)if(wet[i]){sum+=height[i];count++;}
     const mean=count?sum/count:0;lakeMax=0;
     for(const i of waterCells)if(wet[i]){height[i]-=mean;previous[i]-=mean;lakeMax=Math.max(lakeMax,Math.abs(height[i]));}
     if(drive===0&&!pointerDisturbed&&lakeMax<.00018){height.fill(0);previous.fill(0);nextHeight.fill(0);lakeMax=0;}
     pointerDisturbed=false;
   }
 }
 function advanceWater(dt,previousDriveTime,nextDriveTime){
   simDebt+=dt*60;
   const steps=Math.min(5,Math.floor(simDebt));simDebt-=steps;
   if(!steps)return;
   for(let k=0;k<steps;k++){
     const a=previousDriveTime+(nextDriveTime-previousDriveTime)*k/steps,b=previousDriveTime+(nextDriveTime-previousDriveTime)*(k+1)/steps;
     stepWater(b,a);
   }
 }
 const waterCanvas=document.createElement('canvas');waterCanvas.width=waterN;waterCanvas.height=waterN;
 const waterContext=waterCanvas.getContext('2d'),waterImage=waterContext.createImageData(waterN,waterN);
 const waterRGB=new Uint8Array(waterCount*3);
 for(const i of waterCells){
   const x=(i%waterN)*waterCell,u=clamp((x-110)/205,0,1);
   waterRGB[i*3]=Math.round(190-102*u);waterRGB[i*3+1]=Math.round(139+92*u);waterRGB[i*3+2]=255;
 }
 function drawWater(){
   const data=waterImage.data;data.fill(0);
   for(const i of waterCells){
     if(!wet[i])continue;
     const h=height[i],l=wet[i-1]?height[i-1]:h,r=wet[i+1]?height[i+1]:h,u=wet[i-waterN]?height[i-waterN]:h,d=wet[i+waterN]?height[i+waterN]:h;
     // Signed surface lighting gives each crest a bright face and a shaded
     // back, so the travelling curve reads through the dense mist.
     const tilt=(l-r)*.65+(u-d)*.76;
     const signed=tilt*4.8,amount=Math.pow(1-Math.exp(-Math.abs(signed)*22),1.8);
     if(amount<.008)continue;
     const p=i*4,colour=i*3,lit=signed>=0;
     data[p]=lit?Math.round(58+waterRGB[colour]*.78):24;
     data[p+1]=lit?Math.round(60+waterRGB[colour+1]*.76):52;
     data[p+2]=lit?255:96;
     data[p+3]=Math.round(Math.min(lit?.53:.33,amount*(lit?.63:.43))*255);
   }
   // Owned low-resolution simulation pixels only; never read back the large
   // particle canvas. Upscale this field underneath the unchanged particles.
   waterContext.putImageData(waterImage,0,0);
   c.save();c.globalAlpha=1;c.imageSmoothingEnabled=true;c.drawImage(waterCanvas,0,0,waterN*waterCell,waterN*waterCell);c.restore();
 }
 // A shallow rounded rim around a mostly flat face: a pressed glass bubble.
 // The optical shell is a separate layer; fluid/particle motion is unchanged.
 const glassSettings={depth:10,bevel:17};
 const glassBack=document.createElement('canvas'),glassFace=document.createElement('canvas');
 glassBack.width=glassBack.height=glassFace.width=glassFace.height=600;
 const gb=glassBack.getContext('2d'),gf=glassFace.getContext('2d');
 const shapeCanvas=document.createElement('canvas');shapeCanvas.width=shapeCanvas.height=600;
 const maskContext=shapeCanvas.getContext('2d',{willReadFrequently:true});
 maskContext.setTransform(1.5,0,0,1.5,0,0);maskContext.fillStyle='#fff';maskContext.fill(body);
 const shapePixels=maskContext.getImageData(0,0,600,600).data;
 const glassDistance=new Float32Array(600*600),glassInside=new Uint8Array(600*600);
 for(let i=0;i<glassInside.length;i++){glassInside[i]=shapePixels[i*4+3]>128?1:0;glassDistance[i]=glassInside[i]?1e4:0;}
 for(let y=1;y<599;y++)for(let x=1;x<599;x++){
   const i=y*600+x;glassDistance[i]=Math.min(glassDistance[i],glassDistance[i-1]+1,glassDistance[i-600]+1,glassDistance[i-601]+Math.SQRT2,glassDistance[i-599]+Math.SQRT2);
 }
 for(let y=598;y>0;y--)for(let x=598;x>0;x--){
   const i=y*600+x;glassDistance[i]=Math.min(glassDistance[i],glassDistance[i+1]+1,glassDistance[i+600]+1,glassDistance[i+601]+Math.SQRT2,glassDistance[i+599]+Math.SQRT2);
 }
 function buildGlass(){
   gb.setTransform(1,0,0,1,0,0);gb.clearRect(0,0,600,600);gb.setTransform(1.5,0,0,1.5,0,0);
   // Only a diffuse shadow outside the silhouette: no offset glass plate.
   gb.save();gb.filter='blur(7px)';gb.translate(1.2,4);
   gb.fillStyle='rgba(12,30,54,.23)';gb.fill(body);gb.restore();
   gb.globalCompositeOperation='destination-out';gb.fill(body);gb.globalCompositeOperation='source-over';
   gf.setTransform(1,0,0,1,0,0);gf.clearRect(0,0,600,600);
   const pixels=gf.createImageData(600,600),data=pixels.data;
   const h=glassSettings.depth,b=glassSettings.bevel;
   const lx=-.48,ly=-.62,lz=.62,hl=Math.hypot(lx,ly,lz+1),hx=lx/hl,hy=ly/hl,hz=(lz+1)/hl;
   for(let y=3;y<597;y++)for(let x=3;x<597;x++){
     const i=y*600+x;if(!glassInside[i])continue;
     const d=glassDistance[i]/1.5;
     const gx=(glassDistance[i+3]-glassDistance[i-3])/6,gy=(glassDistance[i+1800]-glassDistance[i-1800])/6,gn=Math.hypot(gx,gy)||1;
     const q=clamp(1-d/b,0,.995),slope=h/b*q/Math.sqrt(Math.max(.01,1-q*q));
     // Low curvature across the wide front makes the bubble look compressed.
     let nx=-slope*gx/gn+(x/1.5-220)*.00022,ny=-slope*gy/gn+(y/1.5-190)*.00018,nz=1;
     const length=Math.hypot(nx,ny,nz);nx/=length;ny/=length;nz/=length;
     const light=Math.max(0,nx*lx+ny*ly+nz*lz),spec=Math.pow(Math.max(0,nx*hx+ny*hy+nz*hz),30);
     const fresnel=Math.pow(1-nz,2),rim=Math.max(0,1-d/b),shade=(1-light)*rim;
     // A broad reflected fill light reveals the curved lower/right shoulder.
     const fillSpec=Math.pow(Math.max(0,nx*.28+ny*.38+nz*.881),24)*rim;
     const alpha=clamp(.018+fresnel*.48+spec*.73*(.40+.60*rim)+fillSpec*.44+shade*.30,0,.84);
     const bright=clamp(spec*1.5+fillSpec*1.3+light*.4+fresnel*.46,0,1),j=i*4;
     data[j]=Math.round(65+190*bright);data[j+1]=Math.round(117+138*bright);data[j+2]=Math.round(170+85*bright);data[j+3]=Math.round(alpha*255);
   }
   gf.putImageData(pixels,0,0);gf.setTransform(1.5,0,0,1.5,0,0);gf.save();gf.clip(body);
   // Broad, soft studio reflection across the flattened front face.
   const sheen=gf.createLinearGradient(90,10,320,350);
   sheen.addColorStop(0,'rgba(237,249,255,.12)');sheen.addColorStop(.20,'rgba(237,249,255,.035)');sheen.addColorStop(.28,'rgba(237,249,255,0)');
   sheen.addColorStop(.58,'rgba(237,249,255,0)');sheen.addColorStop(.66,'rgba(237,249,255,.065)');sheen.addColorStop(.74,'rgba(237,249,255,0)');sheen.addColorStop(1,'rgba(237,249,255,.02)');
   gf.fillStyle=sheen;gf.fillRect(0,0,400,400);
   // The curved normal field provides the edge; no hard perimeter stroke.
   gf.restore();
 }
 function cogGlassPath(h,angle){
   const path=new Path2D();
   for(let j=0;j<48;j++){
     const a=j/48*tau+angle,r=(j%4===1||j%4===2)?h.r:h.r*.8;
     const x=h.x+Math.cos(a)*r,y=h.y+Math.sin(a)*r;if(j===0)path.moveTo(x,y);else path.lineTo(x,y);
   }
   path.closePath();return path;
 }
 function drawGlass(t){
   c.save();c.setTransform(1,0,0,1,0,0);c.drawImage(glassFace,0,0);c.setTransform(1.5,0,0,1.5,0,0);c.clip(body);
   // An inner glass lip makes the holes look cut through the shallow vessel.
   // Every stroke portion inside a cog is erased by the existing final mask.
   for(const h of holes){
     const path=cogGlassPath(h,t*h.spin);
     const grad=c.createLinearGradient(h.x-h.r,h.y-h.r,h.x+h.r,h.y+h.r);
     grad.addColorStop(0,'rgba(62,110,166,.54)');grad.addColorStop(.42,'rgba(129,191,234,.24)');grad.addColorStop(.72,'rgba(221,244,255,.66)');grad.addColorStop(1,'rgba(153,216,250,.50)');
     c.strokeStyle='rgba(37,76,131,.18)';c.lineWidth=7;c.lineJoin='round';c.stroke(path);
     c.strokeStyle=grad;c.lineWidth=4.5;c.stroke(path);
     c.strokeStyle='rgba(221,246,255,.40)';c.lineWidth=1.2;c.stroke(path);
   }
   c.restore();
 }
 buildGlass();

 // Shed a small stream from actual perimeter particles. The escape pool uses
 // its own RNG, so the approved interior particle paths remain unchanged.
 const escapePool=Array.from({length:1200},()=>({alive:false}));
 let escapeCursor=0,escapeSeed=57243;
 function escapeRandom(){escapeSeed=(Math.imul(escapeSeed,1664525)+1013904223)>>>0;return escapeSeed/4294967296;}
 const flatEdgeX=y=>159.39+(y-39.1)*(93.96-159.39)/(346.9-39.1);
 // Full-height emission, with the longest drift halfway down the flat edge.
 const escapeReach=y=>.16+1.10*Math.pow(Math.sin(Math.PI*clamp((y-39.1)/307.8,0,1)),1.5);
 const escapeClip=new Path2D();escapeClip.rect(0,0,400,400);escapeClip.addPath(body);
 const escapeGlow=document.createElement('canvas');escapeGlow.width=escapeGlow.height=600;
 const eg=escapeGlow.getContext('2d');
 function advanceEscape(dt,t,emission=1){
   // Preserve the tangent of the mist approaching the sloped left wall, then
   // allow a gentle outward velocity through that otherwise closed boundary.
   if(emission>0)for(const p of particles){
     if(p.y<39.1||p.y>346.9)continue;
     const gap=p.x-flatEdgeX(p.y);if(gap<0||gap>5.5)continue;
     if(escapeRandom()>dt*.12*emission)continue;
     const out=escapePool[escapeCursor++%escapePool.length],reach=escapeReach(p.y),speed=(10+escapeRandom()*9)*reach;
     Object.assign(out,{alive:true,x:flatEdgeX(p.y)+.8,y:p.y,age:0,life:3.8+escapeRandom()*2.7,
       reach,vx:-speed,vy:(p.vy||0)*.22*reach-speed*.21,size:.42+escapeRandom()*.95,phase:escapeRandom()*tau});
   }
   for(const p of escapePool){
     if(!p.alive)continue;
     p.age+=dt;if(p.age>=p.life||p.x<4){p.alive=false;continue;}
     const curl=Math.sin(p.x*.043+p.y*.037-t*.58);
     p.vx+=(-11*p.reach-p.vx)*dt*.30;
     p.vy+=(curl*4*p.reach-p.vy)*dt*.48;
     p.x+=p.vx*dt;p.y+=p.vy*dt;
   }
 }
 function drawEscape(){
   c.save();c.clip(escapeClip,'evenodd');
   const dots=Array.from({length:12},()=>new Path2D());
   const glows=Array.from({length:12},()=>new Path2D());
   for(const p of escapePool){
     if(!p.alive)continue;
     const life=p.age/p.life,edgeFade=clamp((p.x-5)/22,0,1);
     const reach=escapeReach(p.y),travel=Math.max(0,flatEdgeX(p.y)-p.x);
     const taper=clamp(1-travel/(76*reach),0,1),heightFade=clamp(Math.min(p.y-38,348-p.y)/5,0,1);
     const opacity=Math.min(1,p.age/.12)*Math.pow(1-life,1.7)*edgeFade*Math.sqrt(taper)*heightFade;
     if(opacity<.018)continue;
     // Slowly expanding soft wisps with crisp pink grains near their origin.
     const bucket=Math.min(11,Math.floor(opacity*12)),size=p.size*(1+.18*p.age);
     const spread=2+p.age*.9*Math.min(1,p.reach);
     glows[bucket].moveTo(p.x+spread,p.y);glows[bucket].arc(p.x,p.y,spread,0,tau);
     dots[bucket].moveTo(p.x+size/2,p.y);dots[bucket].arc(p.x,p.y,size/2,0,tau);
   }
   // Batch the haze, then blur once; hundreds of individual sprite draws
   // would compete with the existing fluid simulation for the frame budget.
   eg.setTransform(1,0,0,1,0,0);eg.clearRect(0,0,600,600);eg.setTransform(1.5,0,0,1.5,0,0);
   eg.fillStyle='rgb(215,102,225)';
   for(let i=0;i<12;i++){eg.globalAlpha=(i+.5)/12*.10;eg.fill(glows[i]);}
   c.save();c.filter='blur(2px)';c.drawImage(escapeGlow,0,0,400,400);c.restore();
   c.fillStyle='rgb(229,113,228)';
   for(let i=0;i<12;i++){c.globalAlpha=(i+.5)/12*.83;c.fill(dots[i]);}
   c.restore();
 }

 function render(t){
   c.setTransform(1,0,0,1,0,0);c.clearRect(0,0,600,600);c.drawImage(glassBack,0,0);c.drawImage(backdrop,0,0);c.setTransform(1.5,0,0,1.5,0,0);
   c.save();c.clip(body);
   // Batch thousands of particles into 64 draws, preserving subpixel motion.
   const buckets=Array.from({length:64},()=>new Path2D());
   for(const p of particles){
     const concentration=.5+.5*Math.sin(p.x*.071+p.y*.044+Math.sin(p.y*.052-t*.48)*1.8+t*.3);
     const fade=clamp(Math.min(p.age,p.life-p.age),0,1);
     const opacity=clamp(((p.outer?.35:.20)+.64*concentration*concentration)*fade*parameters.smoke,0,.96);
     if(opacity<.06)continue;
     const col=Math.round(clamp((p.x-110)/205,0,1)*15),bucket=buckets[col*4+Math.min(3,Math.floor(opacity*4))];
     const size=p.size*1.65;
     bucket.rect(p.x,p.y,size,size*.8);
     // Velocity-aligned threads grow in fast constrictions; length is time-
     // based, not dependent on the display frame rate.
     if(p.size>.95){
       const dx=(p.vx||0)*.055,dy=(p.vy||0)*.055;
       bucket.moveTo(p.x,p.y);bucket.lineTo(p.x-dx,p.y-dy);bucket.lineTo(p.x-dx+size*.55,p.y-dy+.4);bucket.lineTo(p.x+size*.55,p.y+.4);bucket.closePath();
     }
   }
   for(let i=0;i<64;i++){c.fillStyle=palettes[i>>2];c.globalAlpha=(.14+(i%4)*.25);c.fill(buckets[i]);}
   c.restore();c.globalAlpha=1;
   c.save();c.clip(body);drawWater();c.restore();
   drawGlass(t);c.globalCompositeOperation='destination-out';c.fillStyle='#000';holes.forEach(h=>gear(h.x,h.y,h.r,t*h.spin));c.globalCompositeOperation='source-over';
   drawEscape();
   bars.forEach((bar,i)=>{
     c.save();c.beginPath();c.moveTo(bar.x+4,bar.y);c.lineTo(bar.x+bar.w,bar.y);c.lineTo(bar.x+bar.w-4,bar.y+bar.h);c.lineTo(bar.x,bar.y+bar.h);c.closePath();c.clip();
     c.fillStyle='rgba(151,91,215,.20)';c.fillRect(bar.x,bar.y,bar.w,bar.h);
     const grains=[new Path2D(),new Path2D(),new Path2D()];
     for(const p of barDust[i]){
       const u=((p.u-t*.29*parameters.flow)%1+1)%1,x=bar.x+u*bar.w,y=bar.y+p.v*bar.h+Math.sin(p.phase+t)*1.5;
       const beat=.5+.5*Math.sin(t*2.5-i*1.6+u*3.5);
       grains[Math.min(2,Math.floor(beat*3))].rect(x,y,p.s*2,p.s);
     }
     c.fillStyle=palettes[2];for(let j=0;j<3;j++){c.globalAlpha=.15+j*.28;c.fill(grains[j]);}c.restore();
   });
   c.globalAlpha=1;
   // Never read back the full particle canvas during animation. Only the small
   // wave field uploads pixels. A second alpha pass increases chip density.
   outputs.forEach(({el,ctx})=>{ctx.clearRect(0,0,el.width,el.height);ctx.drawImage(source,0,0,el.width,el.height);if(el.width<200){ctx.globalAlpha=.65;ctx.drawImage(source,0,0,el.width,el.height);ctx.globalAlpha=1;}});
 }
 for(let i=0;i<70;i++){advance(1/30,i/30);advanceEscape(1/30,i/30);}

 function syncVisibility(){
   if(active&&visible&&!document.hidden&&root.isConnected){
     if(!raf){last=performance.now();raf=requestAnimationFrame(tick);}
   }else if(raf){cancelAnimationFrame(raf);raf=0;}
 }
 function tick(now){
   raf=0;
   if(active&&visible&&!document.hidden&&root.isConnected){
     const dt=Math.min((now-last)/1000,.05);last=now;
     updateActivity(dt);
     const before=time,step=dt*drive;time+=step;
     advance(step,time);advanceEscape(dt*.68,time,drive/.68);advanceWater(dt,before,time);render(time);
   }
   syncVisibility();
 }
 render(0);root.appendChild(canvas);
 const observer=new IntersectionObserver(entries=>{visible=entries[0].isIntersecting;syncVisibility();});
 observer.observe(root);
 document.addEventListener('visibilitychange',syncVisibility);
 return {
   resume(){active=true;canvas.hidden=false;still.hidden=true;syncVisibility();},
   pause(){active=false;syncVisibility();canvas.hidden=true;still.hidden=false;}
 };

 }
})();
