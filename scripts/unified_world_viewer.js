/* Offline generic viewer. Native inputs stay unchanged; display heights use the
 * supplied per-column correction. No LOD, decimation or interpolated well axes. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const state = window.VKM_UNIFIED_STATE = {ready: false, error: null};
  const D = window.VKM_SCENE, J = window.VKM_UNIFIED, O = window.VKM_SPATIAL_OBJECTS;
  const host = $('viewport'), status = $('status'), loading = $('loading');
  function fail(error) {
    state.error = String(error && error.stack || error);
    if (loading) loading.textContent = 'Ошибка загрузки: ' + (error.message || error);
    if (status) status.textContent = 'Просмотр не готов. ' + (error.message || error);
    console.error(error);
  }
  if (!host || !D || !J || !O || !window.THREE) {fail(new Error('Не загружены локальные данные или Three.js')); return;}
  const T = window.THREE;
  function decode(spec) {
    if (!spec || typeof spec.data !== 'string') throw new Error('Отсутствует массив геометрии');
    const binary = atob(spec.data), bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
    if (spec.dtype === 'f4') return new Float32Array(bytes.buffer);
    if (spec.dtype === 'i4') return new Uint32Array(bytes.buffer);
    throw new Error('Неизвестный тип массива: ' + spec.dtype);
  }
  const nextFrame = () => new Promise(resolve => requestAnimationFrame(resolve));
  function note(message) {if (loading) loading.textContent = message; if (status) status.textContent = message;}
  function geometry(positions, indices, normals = true) {
    const g = new T.BufferGeometry();
    g.setAttribute('position', new T.BufferAttribute(positions, 3));
    if (indices) g.setIndex(new T.BufferAttribute(indices, 1));
    if (normals) g.computeVertexNormals();
    g.computeBoundingSphere();
    return g;
  }
  function escape(s) {return String(s == null ? 'UNKNOWN' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
  async function start() {
    note('Декодирование полной сетки…'); await nextFrame();
    const xy = decode(D.xy), triangles = decode(D.triangles), heights = decode(D.levels), delta = decode(J.delta), edges = decode(D.edges);
    const N = xy.length / 2;
    if (heights.length !== 10 * N || delta.length !== N || D.units.length !== 9) throw new Error('Нужны 10 контактов, 9 толщ и одна поправка на колонку');
    const bounds = D.meta.bounds, cx = (bounds[0] + bounds[2]) / 2, cy = (bounds[1] + bounds[3]) / 2;
    const span = Math.max(bounds[2] - bounds[0], bounds[3] - bounds[1]), scale = 20 / span;
    let minZ = Infinity, maxZ = -Infinity;
    for (let i = 0; i < N; i++) {minZ = Math.min(minZ, heights[i] + delta[i]); maxZ = Math.max(maxZ, heights[9*N+i] + delta[i]);}
    const cz = (minZ + maxZ) / 2;
    const nx = x => (x-cx)*scale, ny = y => (y-cy)*scale, nz = z => (z-cz)*scale;
    const world = new T.Scene(); world.background = new T.Color('#e6edf1');
    const root = new T.Group(); world.add(root);
    const groups = {};
    for (const name of ['shell','cutsX','cutsY','terrain','room','local_seam','local_strata','plans','wells','recent']) {
      groups[name] = new T.Group(); root.add(groups[name]);
    }
    const renderer = new T.WebGLRenderer({antialias:true, alpha:false, preserveDrawingBuffer:true, powerPreference:'high-performance'});
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.5));
    renderer.localClippingEnabled = true;
    renderer.outputColorSpace = T.SRGBColorSpace;
    host.appendChild(renderer.domElement);
    const camera = new T.PerspectiveCamera(42,1,.005,1000); camera.up.set(0,0,1);
    const ambient = new T.HemisphereLight('#ffffff','#7c8990',2.1); world.add(ambient);
    const sun = new T.DirectionalLight('#ffffff',2.2); sun.position.set(10,-15,25); world.add(sun);
    const sideLight = new T.DirectionalLight('#ffffff',.7); sideLight.position.set(-15,4,5);world.add(sideLight);
    let target = new T.Vector3(), radius = 31, theta = -.90, phi = 1.03;
    let needsRender = true, raf = 0;
    function cameraUpdate() {
      camera.position.set(target.x+radius*Math.sin(phi)*Math.cos(theta),target.y+radius*Math.sin(phi)*Math.sin(theta),target.z+radius*Math.cos(phi));
      camera.lookAt(target); requestRender();
    }
    function draw() {raf=0; if (!needsRender) return; needsRender=false; renderer.render(world,camera);}
    function requestRender() {needsRender=true; if (!raf) raf=requestAnimationFrame(draw);}
    function resize() {const w=host.clientWidth,h=host.clientHeight;renderer.setSize(w,h,false);camera.aspect=w/h;camera.updateProjectionMatrix();requestRender();}
    new ResizeObserver(resize).observe(host); resize();
    let drag = null;
    const canvas=renderer.domElement;
    canvas.style.touchAction='none';canvas.addEventListener('contextmenu',event=>event.preventDefault());
    canvas.addEventListener('pointerdown',event=>{drag={x:event.clientX,y:event.clientY,startX:event.clientX,startY:event.clientY,pan:event.button===2||event.shiftKey};canvas.setPointerCapture(event.pointerId);});
    canvas.addEventListener('pointermove',event=>{
      if (!drag) return;
      const dx=event.clientX-drag.x,dy=event.clientY-drag.y;drag.x=event.clientX;drag.y=event.clientY;
      if(drag.pan){const right=new T.Vector3().setFromMatrixColumn(camera.matrix,0),up=new T.Vector3().setFromMatrixColumn(camera.matrix,1);const k=radius/host.clientHeight*.8;target.addScaledVector(right,-dx*k).addScaledVector(up,dy*k);}
      else {theta-=dx*.006;phi=Math.max(.025,Math.min(Math.PI-.025,phi+dy*.006));}
      cameraUpdate();
    });
    canvas.addEventListener('wheel',event=>{event.preventDefault();radius=Math.max(.03,Math.min(300,radius*Math.exp(event.deltaY*.001)));cameraUpdate();},{passive:false});
    const raycaster=new T.Raycaster(),clickable=[];
    raycaster.params.Points.threshold=.055;
    function pick(event) {
      const rect=canvas.getBoundingClientRect();raycaster.setFromCamera(new T.Vector2(2*(event.clientX-rect.left)/rect.width-1,1-2*(event.clientY-rect.top)/rect.height),camera);
      const hits=raycaster.intersectObjects(clickable.filter(o=>{let p=o;while(p){if(!p.visible)return false;p=p.parent;}return true;}),false);
      if(!hits.length)return;
      const hit=hits[0],data=hit.object.userData.records?hit.object.userData.records[hit.index]:hit.object.userData;
      const meta=data.metadata||{},native=hit.point.clone();native.z/=root.scale.z;
      const rows=[['Объект',data.name||data.label||data.id],['Статус',Array.isArray(data.status)?data.status.join(', '):data.status],['Источник',meta.source_id||data.source_id],['Участок',meta.region_label||meta.region_id],['Высотное представление',data.joint_status||data.z_status||data.display_anchor_status||data.height_datum||data.z_frame],['План / факт',meta.planned_or_actual||data.planned_or_actual]];
      if(data.date)rows.push(['Эпоха',data.date],['h, м',data.native_h],['N, м',data.geoid_N_m],['H = h − N, м',data.orthometric_H_m],['Качество',data.quality_status||data.quality_flags||'UNKNOWN']);
      if(data.object_kind==='GRAPHICAL_PLAN_SYMBOL')rows.push(['Глубина','UNKNOWN — показан только знак']);
      const eventRows=(O.events||[]).filter(e=>(e.target_region_ids||[]).includes(meta.region_id));
      for(const e of eventRows)rows.push(['Календарь',`${e.planned_or_actual} · ${e.event_start_min} — ${e.event_end_max} · ${e.event_type}`]);
      $('info').innerHTML=rows.filter(r=>r[1]!=null).map(r=>`<b>${escape(r[0])}:</b> ${escape(r[1])}`).join('<br>');
    }
    canvas.addEventListener('pointerup',event=>{const click=drag&&Math.hypot(event.clientX-drag.startX,event.clientY-drag.startY)<4;drag=null;if(click)pick(event);});
    canvas.addEventListener('pointercancel',()=>{drag=null;});
    const px=new T.Plane(new T.Vector3(-1,0,0),0),py=new T.Plane(new T.Vector3(0,1,0),0);
    const cutXLimit=new T.Plane(new T.Vector3(0,-1,0),0),cutYLimit=new T.Plane(new T.Vector3(1,0,0),0);
    const shellMaterials=[],terrainMaterials=[];
    function material(color,extra={}) {return new T.MeshLambertMaterial({color,side:T.DoubleSide,...extra});}
    function cap(level,unit) {
      const pos=new Float32Array(N*3);
      for(let i=0;i<N;i++){pos[i*3]=nx(xy[i*2]);pos[i*3+1]=ny(xy[i*2+1]);pos[i*3+2]=nz(heights[level*N+i]+delta[i]);}
      const mat=material(D.units[unit].color);shellMaterials.push(mat);
      const mesh=new T.Mesh(geometry(pos,triangles),mat);groups.shell.add(mesh);return mesh;
    }
    note('Полная оболочка: нижняя граница, верх и девять боковых толщ…');await nextFrame();
    const bottom=cap(0,0),top=cap(9,8);
    for(let u=0;u<9;u++){
      const pos=new Float32Array(edges.length*2*3),ind=new Uint32Array(edges.length*3);
      for(let e=0;e<edges.length;e+=2){const a=edges[e],b=edges[e+1],v=e*2;
        for(let k=0;k<4;k++){const id=k===0||k===3?a:b,l=k<2?u:u+1;pos[(v+k)*3]=nx(xy[id*2]);pos[(v+k)*3+1]=ny(xy[id*2+1]);pos[(v+k)*3+2]=nz(heights[l*N+id]+delta[id]);}
        ind.set([v,v+1,v+2,v,v+2,v+3],e*3);
      }
      const mat=material(D.units[u].color);shellMaterials.push(mat);groups.shell.add(new T.Mesh(geometry(pos,ind),mat));
    }
    await nextFrame();note('Поверхность DSM и пространственные объекты…');
    const txy=decode(D.terrain_xy),tindices=decode(D.terrain_triangles),raw=decode(D.terrain_raw_z),smooth=decode(D.terrain_smooth_z);
    function terrain(z) {
      const n=z.length,pos=new Float32Array(n*3),colors=new Float32Array(n*3);let low=Infinity,high=-Infinity;
      for(let i=0;i<n;i++){low=Math.min(low,z[i]);high=Math.max(high,z[i]);}
      const a=new T.Color('#557c58'),b=new T.Color('#e3d1aa'),c=new T.Color();
      for(let i=0;i<n;i++){pos.set([nx(txy[i*2]),ny(txy[i*2+1]),nz(z[i])],i*3);c.copy(a).lerp(b,(z[i]-low)/(high-low||1));c.toArray(colors,i*3);}
      const g=geometry(pos,tindices);g.setAttribute('color',new T.BufferAttribute(colors,3));const mat=material('#ffffff',{vertexColors:true});terrainMaterials.push(mat);const mesh=new T.Mesh(g,mat);groups.terrain.add(mesh);return mesh;
    }
    const terrainSmooth=terrain(smooth),terrainRaw=terrain(raw);
    const regionIds=[];
    for(const m of O.meshes){const id=m.metadata&&m.metadata.region_id;if(id&&!regionIds.includes(id))regionIds.push(id);}
    const meshes=new Map(),objectMaterials=[];
    function objectMesh(data) {
      if(meshes.has(data.id))return meshes.get(data.id);
      const points=decode(data.points),d=decode(data.delta),pos=new Float32Array(points.length);
      for(let i=0;i<d.length;i++)pos.set([nx(points[i*3]),ny(points[i*3+1]),nz(points[i*3+2]+d[i])],i*3);
      const unit=D.units.find(u=>u.id===(data.metadata||{}).unit);
      const room=data.group==='room',mat=material(room?'#d98728':data.group==='local_seam'?'#a32c5e':unit?unit.color:'#8a94a2',{polygonOffset:true,polygonOffsetFactor:room?-1:1,polygonOffsetUnits:1});
      const mesh=new T.Mesh(geometry(pos,decode(data.triangles)),mat);mesh.userData=data;groups[data.group].add(mesh);meshes.set(data.id,mesh);objectMaterials.push(mat);
      if(room)clickable.push(mesh);return mesh;
    }
    for(const data of O.meshes)if(data.group!=='local_strata')objectMesh(data);
    for(const data of O.lines||[])for(const path of data.paths||[]){
      const points=decode(path.points),d=decode(path.delta),pos=new Float32Array(points.length);
      for(let i=0;i<d.length;i++)pos.set([nx(points[i*3]),ny(points[i*3+1]),nz(points[i*3+2]+d[i])+.0004],i*3);
      const line=new T.Line(geometry(pos,null,false),new T.LineBasicMaterial({color:data.group==='plan_outline'?'#273e4c':'#315c75'}));line.userData=data;groups.plans.add(line);
    }
    function markers(records,group,color,size) {
      const rows=records.filter(p=>Number.isFinite(p.x)&&Number.isFinite(p.y)&&Number.isFinite(p.display_z)),pos=new Float32Array(rows.length*3);
      rows.forEach((p,i)=>pos.set([nx(p.x),ny(p.y),nz(p.display_z)+.006],i*3));
      const points=new T.Points(geometry(pos,null,false),new T.PointsMaterial({color,size,sizeAttenuation:false,depthTest:false,transparent:true,opacity:.95}));points.renderOrder=15;points.userData.records=rows;group.add(points);clickable.push(points);return points;
    }
    markers(O.boreholes||[],groups.wells,'#352066',7);
    const dates=[...new Set((D.recent_points||[]).map(p=>p.date))].sort(),datePoints=[];
    dates.forEach((date,i)=>{const m=markers(D.recent_points.filter(p=>p.date===date),groups.recent,['#1456da','#ce7b05','#ce3056'][i%3],5);m.userData.date=date;datePoints.push(m);});
    const axis=new T.AxesHelper(1.2);axis.position.set(nx(bounds[0])-.8,ny(bounds[1])-.8,nz(minZ));root.add(axis);
    const legend=$('legend');if(legend)legend.innerHTML=[...D.units].reverse().map(u=>`<div><span class="swatch" style="background:${escape(u.color)}"></span>${escape(u.name)}</div>`).join('');
    let currentCutX=NaN,currentCutY=NaN,sectionOnly=null,cutBusy=false,pendingCut=null;
    function clearGroup(group){for(const child of [...group.children]){group.remove(child);child.geometry.dispose();child.material.dispose();}}
    function section(axisIndex,value,group) {
      clearGroup(group);const segments=[];
      for(let f=0;f<triangles.length;f+=3){
        const a=triangles[f],b=triangles[f+1],c=triangles[f+2],av=xy[a*2+axisIndex],bv=xy[b*2+axisIndex],cv=xy[c*2+axisIndex];
        if(Math.min(av,bv,cv)>value||Math.max(av,bv,cv)<value)continue;
        const ids=[a,b,c],hits=[];
        for(let e=0;e<3;e++){const i=ids[e],j=ids[(e+1)%3],v=xy[i*2+axisIndex]-value,w=xy[j*2+axisIndex]-value;if((v<0&&w>=0)||(w<0&&v>=0)){const t=v/(v-w);hits.push([i,j,t]);}}
        if(hits.length!==2)continue;
        const p=hits.map(([i,j,t])=>[xy[i*2]+(xy[j*2]-xy[i*2])*t,xy[i*2+1]+(xy[j*2+1]-xy[i*2+1])*t]);
        if(Math.hypot(p[0][0]-p[1][0],p[0][1]-p[1][1])<1e-6)continue;
        segments.push({hits,p});
      }
      for(let u=0;u<9;u++){
        const pos=new Float32Array(segments.length*18);let at=0;
        for(const seg of segments){
          const vertices=[];
          for(const [endpoint,l] of [[0,u],[1,u],[1,u+1],[0,u+1]]){const [i,j,t]=seg.hits[endpoint],z0=heights[l*N+i]+delta[i],z1=heights[l*N+j]+delta[j];vertices.push([nx(seg.p[endpoint][0]),ny(seg.p[endpoint][1]),nz(z0+(z1-z0)*t)]);}
          for(const k of [0,1,2,0,2,3]){pos.set(vertices[k],at);at+=3;}
        }
        const mat=material(D.units[u].color,{polygonOffset:true,polygonOffsetFactor:-1,polygonOffsetUnits:-1});group.add(new T.Mesh(geometry(pos,null),mat));
      }
      state[axisIndex===0?'cutSegmentsX':'cutSegmentsY']=segments.length;
    }
    function values() {return {mode:$('mode').value,zscale:Number($('zscale').value),cutx:Number($('cutx').value),cuty:Number($('cuty').value),terrain:$('terrain').value,opacity:Number($('opacity').value)/100,region:$('region').value};}
    function clipMaterials(mats,planes,intersection=false){for(const mat of mats){if(mat.clippingPlanes!==planes||mat.clipIntersection!==intersection){mat.clippingPlanes=planes;mat.clipIntersection=intersection;mat.needsUpdate=true;}}}
    const noClip=[],quarterClip=[px,py],xClip=[px],yClip=[py],cxClip=[cutXLimit],cyClip=[cutYLimit];
    function apply() {
      const v=values(),mode=v.mode,sections=mode==='sections',mine=mode==='mine';root.scale.z=v.zscale;
      px.constant=nx(bounds[0]+(bounds[2]-bounds[0])*v.cutx/100);py.constant=-ny(bounds[1]+(bounds[3]-bounds[1])*v.cuty/100);
      cutXLimit.constant=-py.constant;cutYLimit.constant=-px.constant;
      const clipping=mode==='quarter'?quarterClip:mode==='halfX'?xClip:mode==='halfY'?yClip:noClip;
      clipMaterials(shellMaterials,clipping,mode==='quarter');clipMaterials(terrainMaterials,clipping,mode==='quarter');
      groups.shell.visible=$('geology').checked&&!sections&&!mine;top.visible=v.terrain==='off';
      groups.cutsX.visible=$('geology').checked&&(sections&&sectionOnly!=='Y'||mode==='quarter'||mode==='halfX');
      groups.cutsY.visible=$('geology').checked&&(sections&&sectionOnly!=='X'||mode==='quarter'||mode==='halfY');
      clipMaterials(groups.cutsX.children.map(m=>m.material),mode==='quarter'?cxClip:noClip);
      clipMaterials(groups.cutsY.children.map(m=>m.material),mode==='quarter'?cyClip:noClip);
      for(const mat of shellMaterials.concat(groups.cutsX.children.map(m=>m.material),groups.cutsY.children.map(m=>m.material))){mat.opacity=v.opacity;mat.transparent=v.opacity<1;mat.depthWrite=v.opacity>=1;}
      groups.terrain.visible=v.terrain!=='off';terrainRaw.visible=v.terrain==='raw';terrainSmooth.visible=v.terrain==='smooth';
      for(const mat of terrainMaterials){mat.transparent=sections||mine;mat.opacity=sections?.16:mine?.12:1;mat.depthWrite=!sections&&!mine;}
      groups.room.visible=$('chambers').checked;groups.local_seam.visible=$('seams').checked;groups.local_strata.visible=$('localstrata').checked;
      groups.plans.visible=$('plans').checked;groups.wells.visible=$('wells').checked;groups.recent.visible=$('recent').checked;
      const selected=v.region==='all'?null:regionIds[Number(v.region)];
      for(const [id,mesh] of meshes){const m=mesh.userData.metadata||{};mesh.visible=!selected||m.region_id===selected;}
      for(const p of datePoints)p.visible=$('epoch').value==='all'||p.userData.date===$('epoch').value;
      const recentVisible=groups.recent.visible?datePoints.filter(p=>p.visible).reduce((s,p)=>s+p.userData.records.length,0):0;
      Object.assign(state,{mode,zscale:v.zscale,cutx:v.cutx,cuty:v.cuty,units:9,rooms:(O.meshes||[]).filter(m=>m.group==='room').length,wellSymbols:(O.boreholes||[]).length,recentVisible,fullTrianglesPerContact:triangles.length/3,heightMode:J.meta.height_mode,localStrataVisible:groups.local_strata.visible,region:v.region});
      if(status)status.textContent=`9 толщ · ${state.fullTrianglesPerContact.toLocaleString('ru-RU')} треугольника на контакт · ${state.rooms} сценариев камер · ${state.wellSymbols} знаков скважин · ICESat-2: ${recentVisible} · Z × ${v.zscale} · ${mode} · высотная увязка — сценарий`;
      requestRender();
    }
    async function rebuildCuts() {
      const v=values();pendingCut=v;if(cutBusy)return;cutBusy=true;
      try {while(pendingCut){const next=pendingCut;pendingCut=null;const x=bounds[0]+(bounds[2]-bounds[0])*next.cutx/100,y=bounds[1]+(bounds[3]-bounds[1])*next.cuty/100;
        if(currentCutX!==x){section(0,x,groups.cutsX);currentCutX=x;await nextFrame();}
        if(currentCutY!==y){section(1,y,groups.cutsY);currentCutY=y;await nextFrame();}apply();
      }}finally{cutBusy=false;}
    }
    function reset(){target.set(0,0,0);radius=31;theta=-.90;phi=1.03;cameraUpdate();}
    function fitRegion(){const v=values(),selected=v.region==='all'?null:regionIds[Number(v.region)];if(!selected){reset();return;}const box=new T.Box3();for(const mesh of meshes.values())if((mesh.userData.metadata||{}).region_id===selected&&mesh.userData.group==='local_seam'){mesh.updateWorldMatrix(true,false);box.expandByObject(mesh);}if(!box.isEmpty()){box.getCenter(target);const size=box.getSize(new T.Vector3());radius=Math.max(1.1,size.length()*1.8);cameraUpdate();}}
    for(const id of ['mode','zscale','terrain','geology','chambers','seams','plans','wells','recent','epoch','opacity'])$(id).addEventListener('change',()=>{if(id==='mode')sectionOnly=null;apply();});
    for(const id of ['cutx','cuty'])$(id).addEventListener('change',()=>rebuildCuts().catch(fail));
    $('localstrata').addEventListener('change',async()=>{if($('localstrata').checked){note('Загрузка 63 полных локальных альтернативных толщ…');await nextFrame();for(const d of O.meshes)if(d.group==='local_strata')objectMesh(d);loading.style.display='none';}apply();});
    $('region').addEventListener('change',()=>{apply();fitRegion();});
    $('reset').addEventListener('click',()=>{ $('region').value='all';apply();reset();});
    $('top').addEventListener('click',()=>{phi=.025;theta=-Math.PI/2;cameraUpdate();});
    for(const axisName of ['X','Y'])$('section'+axisName).addEventListener('click',()=>{
      $('mode').value='sections';sectionOnly=axisName;target.set(axisName==='X'?px.constant:0,axisName==='Y'?-py.constant:0,0);
      radius=31;theta=axisName==='X'?0:-Math.PI/2;phi=Math.PI/2;apply();cameraUpdate();
    });
    $('snapshot').addEventListener('click',()=>{renderer.render(world,camera);const a=document.createElement('a');a.download='geology-view.png';a.href=canvas.toDataURL('image/png');a.click();});
    window.VKM_UNIFIED_API={setMode(mode){$('mode').value=mode;sectionOnly=null;apply();},render:apply,reset,getRenderer:()=>renderer,getScene:()=>world,getCamera:()=>camera};
    note('Строю два полных разреза по исходным треугольникам…');await nextFrame();await rebuildCuts();reset();
    state.ready=true;state.objectMeshes=meshes.size;state.sourceVertices=N;state.threeRevision=T.REVISION;state.fullResolution=true;
    loading.style.display='none';apply();
  }
  start().catch(fail);
})();
