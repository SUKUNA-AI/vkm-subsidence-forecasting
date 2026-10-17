/* Generic complete-shell viewer. Source data are provided by window.VKM_SCENE.
 * All source triangles are retained; only shared interior layer faces are culled.
 */
(() => {
  'use strict';
  const scene = window.VKM_SCENE;
  const plot = document.getElementById('plot');
  const status = document.getElementById('status');
  const controls = {
    mode: document.getElementById('mode'),
    zscale: document.getElementById('zscale'),
    terrain: document.getElementById('terrain'),
    recent: document.getElementById('recent'),
    epoch: document.getElementById('epoch'),
  };
  if (!scene || !plot || !window.Plotly) {
    if (status) status.textContent = 'Не удалось загрузить данные сцены или Plotly.';
    return;
  }

  function decode(spec) {
    if (!spec || !Array.isArray(spec.shape)) throw new Error('Missing typed array shape');
    const binary = atob(spec.data);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
    const kind = spec.dtype === 'f4' ? Float32Array : spec.dtype === 'i4' ? Int32Array : null;
    if (!kind) throw new Error('Unsupported array type: ' + spec.dtype);
    const array = new kind(bytes.buffer);
    if (array.length !== spec.shape.reduce((a, b) => a * b, 1)) throw new Error('Invalid array length');
    return array;
  }

  function splitXY(spec) {
    const array = decode(spec), count = spec.shape[0];
    if (spec.shape.length !== 2 || spec.shape[1] !== 2) throw new Error('XY must have two components');
    const x = new Float32Array(count), y = new Float32Array(count);
    for (let i = 0; i < count; i++) { x[i] = array[2 * i]; y[i] = array[2 * i + 1]; }
    return { x, y, count };
  }

  function splitTriangles(spec) {
    const array = decode(spec), count = spec.shape[0];
    if (spec.shape.length !== 2 || spec.shape[1] !== 3) throw new Error('Triangles must have three components');
    const i = new Int32Array(count), j = new Int32Array(count), k = new Int32Array(count);
    for (let f = 0; f < count; f++) { i[f] = array[3 * f]; j[f] = array[3 * f + 1]; k[f] = array[3 * f + 2]; }
    return { i, j, k, count };
  }

  function range(array) {
    let min = Infinity, max = -Infinity;
    for (let i = 0; i < array.length; i++) { const v = array[i]; if (v < min) min = v; if (v > max) max = v; }
    return [min, max];
  }

  try {
    if (status) status.textContent = 'Загружаю полную сетку и непрерывную оболочку…';
    const xy = splitXY(scene.xy), faces = splitTriangles(scene.triangles);
    const heights = decode(scene.levels), count = xy.count;
    if (scene.levels.shape[0] !== 10 || scene.levels.shape[1] !== count || scene.units.length !== 9) {
      throw new Error('Nine layers and ten contact levels required');
    }
    const levels = Array.from({ length: 10 }, (_, i) => heights.subarray(i * count, (i + 1) * count));
    const edges = decode(scene.edges);
    const terrainXY = splitXY(scene.terrain_xy);
    const terrainFaces = splitTriangles(scene.terrain_triangles);
    const rawZ = decode(scene.terrain_raw_z), smoothZ = decode(scene.terrain_smooth_z);
    if (rawZ.length !== terrainXY.count || smoothZ.length !== terrainXY.count) throw new Error('Terrain height count mismatch');
    const boundaryIds = Array.from(new Set(edges));
    const boundaryLookup = new Map(boundaryIds.map((id, i) => [id, i]));
    const boundaryCount = boundaryIds.length;
    const wallX = new Float32Array(2 * boundaryCount), wallY = new Float32Array(2 * boundaryCount);
    for (let v = 0; v < boundaryCount; v++) {
      wallX[v] = wallX[v + boundaryCount] = xy.x[boundaryIds[v]];
      wallY[v] = wallY[v + boundaryCount] = xy.y[boundaryIds[v]];
    }
    const wallI = new Int32Array(edges.length), wallJ = new Int32Array(edges.length), wallK = new Int32Array(edges.length);
    for (let e = 0; e < edges.length; e += 2) {
      const a = boundaryLookup.get(edges[e]), b = boundaryLookup.get(edges[e + 1]);
      wallI[e] = a; wallJ[e] = b; wallK[e] = b + boundaryCount;
      wallI[e + 1] = a; wallJ[e + 1] = b + boundaryCount; wallK[e + 1] = a + boundaryCount;
    }
    const lighting = { ambient: .62, diffuse: .7, specular: .16, roughness: .8, fresnel: .05 };
    const baseMesh = { type: 'mesh3d', flatshading: false, lighting,
      lightposition: { x: 1000, y: -2000, z: 3000 }, opacity: 1, showscale: false };
    const walls = scene.units.map((unit, unitIndex) => {
      const z = new Float32Array(2 * boundaryCount);
      for (let v = 0; v < boundaryCount; v++) {
        z[v] = levels[unitIndex][boundaryIds[v]];
        z[v + boundaryCount] = levels[unitIndex + 1][boundaryIds[v]];
      }
      return { ...baseMesh, name: unit.name, legendgroup: 'unit-' + unit.id,
        x: wallX, y: wallY, z, i: wallI, j: wallJ, k: wallK, color: unit.color,
        showlegend: true, hovertemplate: unit.name + '<br>X %{x:.1f} м<br>Y %{y:.1f} м<br>Z %{z:.2f} м<extra>Интерпретация</extra>' };
    });
    function cap(levelIndex, unitIndex, floor) {
      const unit = scene.units[unitIndex];
      return { ...baseMesh, name: unit.name, legendgroup: 'unit-' + unit.id, showlegend: false,
        x: xy.x, y: xy.y, z: levels[levelIndex], i: faces.i,
        j: floor ? faces.k : faces.j, k: floor ? faces.j : faces.k, color: unit.color,
        hovertemplate: unit.name + '<br>X %{x:.1f} м<br>Y %{y:.1f} м<br>Z %{z:.2f} м<extra>Интерполированная граница</extra>' };
    }
    const caps = { full: [cap(0, 0, true), cap(9, 8, false)], productive: [cap(1, 1, true), cap(3, 2, false)] };
    const sectionTraces = [];
    for (const [sectionIndex, section] of (scene.sections || []).entries()) {
      const sectionXY = splitXY(section.xy), sectionN = sectionXY.count;
      const sectionHeights = decode(section.levels);
      if (sectionN < 2 || section.levels.shape[0] !== 10 || section.levels.shape[1] !== sectionN) {
        throw new Error('Section requires two or more XY samples and ten contact levels');
      }
      const x = new Float32Array(2 * sectionN), y = new Float32Array(2 * sectionN);
      x.set(sectionXY.x); x.set(sectionXY.x, sectionN);
      y.set(sectionXY.y); y.set(sectionXY.y, sectionN);
      const i = new Int32Array(2 * (sectionN - 1)), j = new Int32Array(i.length), k = new Int32Array(i.length);
      for (let n = 0; n < sectionN - 1; n++) {
        i[2 * n] = n; j[2 * n] = n + 1; k[2 * n] = n + 1 + sectionN;
        i[2 * n + 1] = n; j[2 * n + 1] = n + 1 + sectionN; k[2 * n + 1] = n + sectionN;
      }
      for (let unitIndex = 0; unitIndex < 9; unitIndex++) {
        const unit = scene.units[unitIndex], z = new Float32Array(2 * sectionN);
        z.set(sectionHeights.subarray(unitIndex * sectionN, (unitIndex + 1) * sectionN));
        z.set(sectionHeights.subarray((unitIndex + 1) * sectionN, (unitIndex + 2) * sectionN), sectionN);
        sectionTraces.push({ ...baseMesh, name: unit.name, legendgroup: 'unit-' + unit.id,
          showlegend: sectionIndex === 0, x, y, z, i, j, k, color: unit.color,
          hovertemplate: (section.name || 'Разрез') + '<br>' + unit.name
            + '<br>X %{x:.1f} м<br>Y %{y:.1f} м<br>Z %{z:.2f} м<extra>Интерпретация · производный разрез</extra>' });
      }
    }
    const palette = [[0, '#3d6c56'], [.3, '#7f9b65'], [.6, '#b7b486'], [.85, '#c4ad8c'], [1, '#e3d4bc']];
    function terrainTrace(isRaw, terrainOnly) {
      const z = isRaw ? rawZ : smoothZ;
      return { ...baseMesh, name: isRaw ? 'Исходная DSM' : 'Сглаженная DSM · производное представление',
        x: terrainXY.x, y: terrainXY.y, z,
        i: terrainFaces.i, j: terrainFaces.j, k: terrainFaces.k,
        intensity: z, colorscale: palette, opacity: terrainOnly ? 1 : .75, showlegend: true,
        hovertemplate: 'Поверхность DSM<br>X %{x:.1f} м<br>Y %{y:.1f} м<br>Высота %{z:.2f} м EGM2008<extra>' + (isRaw ? 'Исходная DSM' : 'Сглажено для просмотра') + '</extra>' };
    }
    const plans = (scene.plan_lines || []).map((line, i) => ({
      type: 'scatter3d', mode: 'lines', x: line.x, y: line.y, z: line.z,
      name: line.name || 'Контуры плана', legendgroup: 'plans', showlegend: i === 0,
      line: { color: line.color || '#293b4b', width: 2 }, opacity: .8,
      hovertemplate: (line.name || 'Контуры плана') + '<br>X %{x:.1f} м<br>Y %{y:.1f} м<br>Z %{z:.2f} м<extra>Производная привязка</extra>',
    }));
    const recent = (scene.recent_points || []).filter(p => Number.isFinite(p.x) && Number.isFinite(p.y) && Number.isFinite(p.display_z));
    const dates = [...new Set(recent.map(p => p.date))].sort();
    const dateColors = ['#174dd9', '#e6a011', '#cb234a', '#7e39bf', '#04a2a2'];
    const recentTraces = dates.map((date, dateIndex) => {
      const rows = recent.filter(p => p.date === date);
      return { type: 'scatter3d', mode: 'markers', name: 'ICESat-2 · ' + date,
        legendgroup: 'recent-' + date, x: rows.map(p => p.x), y: rows.map(p => p.y), z: rows.map(p => p.display_z),
        marker: { color: dateColors[dateIndex % dateColors.length], size: dateIndex === dates.length - 1 ? 5 : 3,
          line: { color: '#ffffff', width: .5 }, opacity: .95 },
        meta: { date, pointCount: rows.length },
        customdata: rows.map(p => {
          const conditional = Number.isFinite(p.geoid_N_m) && Number.isFinite(p.orthometric_H_m);
          return [p.date, p.native_h, p.vertical_datum || p.height_datum || 'UNKNOWN', p.uncertainty_note || 'UNKNOWN',
            Number.isFinite(p.geoid_N_m) ? p.geoid_N_m.toFixed(3) : 'UNKNOWN',
            Number.isFinite(p.orthometric_H_m) ? p.orthometric_H_m.toFixed(3) : 'UNKNOWN',
            conditional ? 'Условная высота EGM2008: H = h − N' : 'Проекция маркера для просмотра; высотная связь UNKNOWN',
            p.quality_status || 'UNKNOWN'];
        }),
        hovertemplate: 'ICESat-2 %{customdata[0]}<br>Исходная эллипсоидальная h: %{customdata[1]:.3f} м<br>Отсчёт h: %{customdata[2]}'
          + '<br>Геоид N: %{customdata[4]} м<br>H = h − N: %{customdata[5]} м'
          + '<br>%{customdata[6]}<br>X %{x:.1f} м · Y %{y:.1f} м<br>Маркер Z: %{z:.2f} м'
          + '<br>Качество: %{customdata[7]}<br>%{customdata[3]}<extra>Условная высотная привязка</extra>',
      };
    });

    const query = new URLSearchParams(location.search);
    if (controls.mode && query.get('view') === 'terrain') controls.mode.value = 'terrain';
    if (controls.mode && query.get('view') === 'geology') {
      controls.mode.value = 'full'; if (controls.terrain) controls.terrain.value = 'off';
    }
    let rendering = false, rerender = false;
    async function render() {
      if (rendering) { rerender = true; return; }
      rendering = true;
      try {
        const mode = controls.mode ? controls.mode.value : 'full';
        const terrainMode = controls.terrain ? controls.terrain.value : 'smooth';
        const factor = controls.zscale ? Number(controls.zscale.value) : 1;
        const epoch = controls.epoch ? controls.epoch.value : 'all';
        const visibleRecentTraces = recentTraces.filter(trace => epoch === 'all' || trace.meta.date === epoch);
        const showRecent = !controls.recent || controls.recent.checked;
        const visibleRecentPoints = showRecent ? visibleRecentTraces.reduce((sum, trace) => sum + trace.meta.pointCount, 0) : 0;
        const separateHeightFrames = mode !== 'terrain';
        let geologyTraces = [];
        if (mode === 'full') geologyTraces = [...walls, ...caps.full];
        if (mode === 'productive') geologyTraces = [walls[1], walls[2], ...caps.productive];
        if (mode === 'sections') geologyTraces = [...sectionTraces];
        if (separateHeightFrames) geologyTraces.push(...plans);
        const surfaceTraces = [];
        if (terrainMode !== 'off') surfaceTraces.push(terrainTrace(terrainMode === 'raw', true));
        if (showRecent) surfaceTraces.push(...visibleRecentTraces);
        const surfaceSceneName = separateHeightFrames ? 'scene2' : 'scene';
        const traces = [...geologyTraces.map(trace => ({ ...trace, scene: 'scene' })),
          ...surfaceTraces.map(trace => ({ ...trace, scene: surfaceSceneName }))];
        const meta = scene.meta || {}, bounds = meta.bounds || [range(xy.x)[0], range(xy.y)[0], range(xy.x)[1], range(xy.y)[1]];
        let geologyRange = range(heights);
        if (mode === 'productive') geologyRange = [range(levels[1])[0], range(levels[3])[1]];
        const terrainXRange = range(terrainXY.x), terrainYRange = range(terrainXY.y);
        const terrainBounds = [terrainXRange[0], terrainYRange[0], terrainXRange[1], terrainYRange[1]];
        const terrainZRange = range(terrainMode === 'raw' ? rawZ : smoothZ);
        function makeView(viewBounds, zrange, domain, ztitle) {
          const spans = [Math.max(viewBounds[2] - viewBounds[0], 1), Math.max(viewBounds[3] - viewBounds[1], 1), Math.max(zrange[1] - zrange[0], 1)];
          const normalizer = Math.max(...spans);
          // The data aspect and camera fit are computed independently for each
          // height frame; no offset or union of incompatible Z ranges is used.
          const cameraDistance = factor === 1 ? Math.max(1, normalizer / Math.cbrt(spans[0] * spans[1] * spans[2]) / 1.25) : 1;
          const view = { domain: { x: domain, y: [0, 1] },
            xaxis: { title: { text: 'X, м · локальная привязка' } },
            yaxis: { title: { text: 'Y, м · локальная привязка' } },
            zaxis: { title: { text: ztitle } },
            aspectmode: factor === 1 ? 'data' : 'manual',
            camera: { eye: { x: 1.4 * cameraDistance, y: -1.6 * cameraDistance, z: .95 * cameraDistance } },
            bgcolor: '#f3f6f8' };
          if (factor !== 1) view.aspectratio = { x: spans[0] / normalizer, y: spans[1] / normalizer, z: spans[2] / normalizer * factor };
          return view;
        }
        const geologyTitle = mode === 'full' ? 'Геология · все 9 толщ' : mode === 'productive' ? 'Геология · продуктивные зоны' : 'Геология · два полных разреза';
        const surfaceTitle = 'Рельеф и ICESat-2 · EGM2008 условно';
        const layout = { margin: { l: 0, r: 0, b: 0, t: 55 }, paper_bgcolor: '#f3f6f8',
          scene: separateHeightFrames
            ? makeView(bounds, geologyRange, [0, .49], 'Z геологии, м · связь с EGM2008 UNKNOWN')
            : makeView(terrainBounds, terrainZRange, [0, 1], 'H, м · EGM2008; ICESat-2 условно'),
          annotations: separateHeightFrames
            ? [{ text: geologyTitle, x: .245, y: 1.02, xref: 'paper', yref: 'paper', showarrow: false, font: { size: 14 } },
              { text: surfaceTitle, x: .755, y: 1.02, xref: 'paper', yref: 'paper', showarrow: false, font: { size: 14 } }]
            : [{ text: surfaceTitle, x: .5, y: 1.02, xref: 'paper', yref: 'paper', showarrow: false, font: { size: 14 } }],
          uirevision: 'complete-shell-' + mode + '-' + factor,
          legend: { x: .01, y: .99, bgcolor: 'rgba(255,255,255,.88)', font: { size: 11 }, groupclick: 'togglegroup' },
          hoverlabel: { font: { size: 12 } }, showlegend: true };
        if (separateHeightFrames) layout.scene2 = makeView(terrainBounds, terrainZRange, [.51, 1], 'H, м · EGM2008; ICESat-2 условно');
        await Plotly.react(plot, traces, layout,
          { responsive: true, displaylogo: false, scrollZoom: true, toImageButtonOptions: { format: 'png', scale: 2 } });
        const conditionalRecent = recent.some(p => Number.isFinite(p.geoid_N_m) && Number.isFinite(p.orthometric_H_m));
        if (status) status.textContent = (mode === 'full' ? 'Все 9 толщ · непрерывный массив без выреза' : mode === 'productive' ? 'Продуктивные толщи · замкнутая наружная оболочка' : mode === 'sections' ? 'Все 9 толщ · два производных разреза интерпретации' : 'Поверхность · весь исходный охват')
          + ' | Z ×' + factor + (factor === 1 ? ' · 1 м по Z = 1 м по XY' : ' · вертикальное преувеличение')
          + (separateHeightFrames ? ' | Два независимых высотных вида: геология слева, поверхность справа' : '')
          + ' | DSM: ' + (terrainMode === 'raw' ? 'исходная' : terrainMode === 'off' ? 'скрыта' : 'сглаженная, σ=' + (meta.smoothing_sigma_m || 'UNKNOWN') + ' м')
          + ' | Показано свежих точек: ' + visibleRecentPoints + ' из ' + recent.length + (epoch === 'all' ? '' : ' · ' + epoch) + '. ' + (meta.source_note || '')
          + ' Геология не увязана с DSM; ' + (conditionalRecent ? 'высоты ICESat-2 условно приведены к EGM2008 по H=h−N, качество исходных оценок UNKNOWN.' : 'исходные ICESat-2 высоты и проекция маркеров разделены.')
          + ' Исходная DSM не является съёмкой 2026 года или DTM.';
        window.VKM_VIEWER_STATE = { mode, zscale: factor, terrain: terrainMode,
          fullGeologyVertices: count, fullGeologyTrianglesPerContact: faces.count,
          exteriorWallTrianglesPerUnit: edges.length, terrainVertices: terrainXY.count,
          terrainTriangles: terrainFaces.count, recentPointCount: recent.length,
          epoch, visibleRecentPoints, representedUnits: mode === 'full' || mode === 'sections' ? 9 : mode === 'productive' ? 2 : 0,
          sectionCount: (scene.sections || []).length, separateHeightFrames,
          geologyScene: separateHeightFrames ? 'scene' : null, surfaceScene: surfaceSceneName,
          internalFacesCulled: true, sourceTriangleDecimation: false, cutout: false };
      } catch (error) {
        if (status) status.textContent = 'Ошибка отображения: ' + error.message;
        console.error(error);
      } finally {
        rendering = false; if (rerender) { rerender = false; render(); }
      }
    }
    for (const control of Object.values(controls)) if (control) control.addEventListener('change', render);
    render();
  } catch (error) {
    if (status) status.textContent = 'Ошибка данных сцены: ' + error.message;
    console.error(error);
  }
})();
