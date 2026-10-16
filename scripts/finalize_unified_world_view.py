"""Assemble the portable local view after the geometry and viewer are ready."""
from pathlib import Path
import argparse
import hashlib
import json
import shutil


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(4*1024*1024), b''):
            h.update(block)
    return h.hexdigest()


PAGE = '''<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>СКРУ-1 Пространственная геологическая модель</title>
<style>
*{box-sizing:border-box}body{margin:0;font:14px Arial,sans-serif;color:#223746;background:#f1f4f6}
header{padding:14px 22px;background:white;border-bottom:1px solid #ccd5dc;display:flex;gap:24px;align-items:center;justify-content:space-between}h1{font-size:23px;margin:0 0 5px}header p{margin:0;color:#5b6c77;line-height:1.4;max-width:1100px}header a{color:#2b586f;white-space:nowrap}
main{display:grid;grid-template-columns:310px 1fr;height:calc(100vh - 105px);min-height:700px}aside{padding:16px;background:#fff;overflow:auto;border-right:1px solid #d0d8df}h2{font-size:15px;margin:0 0 12px}fieldset{border:0;padding:0;margin:0 0 21px}legend{font-weight:bold;margin-bottom:10px}label{display:block;margin:9px 0}label.row{display:flex;justify-content:space-between;gap:8px;align-items:center}select{width:100%;border:1px solid #acbac4;border-radius:4px;padding:7px;background:white;color:#223746;margin-top:5px}input[type=range]{width:100%}input[type=checkbox]{accent-color:#326b83}button{border:1px solid #afbec9;background:#f4f7f8;color:#264558;border-radius:4px;padding:8px 10px;cursor:pointer}button:hover{background:#e3edf1}.buttons{display:flex;gap:7px;flex-wrap:wrap}small{display:block;color:#62737e;line-height:1.45;margin-top:7px}.notice{background:#eff3f5;padding:10px;line-height:1.45;font-size:12px;margin-bottom:16px}.links a{display:block;margin:8px 0;color:#2b586f}.checkgrid{display:grid;grid-template-columns:1fr 1fr;gap:0 9px}#stage{position:relative;min-width:0;overflow:hidden;background:#e6edf1}#viewport{width:100%;height:100%;min-height:700px}#viewport canvas{display:block;width:100%;height:100%}#loading{position:absolute;left:24px;top:20px;background:rgba(255,255,255,.94);padding:12px 16px;border-radius:4px;max-width:600px;pointer-events:none}#info{position:absolute;right:18px;bottom:22px;max-width:390px;font-size:12px;line-height:1.5;background:rgba(255,255,255,.94);padding:12px;color:#2d4555;pointer-events:none}#status{padding:8px 18px;font-size:12px;background:#dae5eb;line-height:1.5;min-height:30px}#legend{font-size:12px;line-height:1.7}.swatch{display:inline-block;width:12px;height:12px;margin-right:7px;vertical-align:middle}.axis-help{position:absolute;left:18px;bottom:22px;font-size:12px;color:#345267;background:rgba(255,255,255,.85);padding:8px;pointer-events:none}
@media(max-width:1000px){main{grid-template-columns:245px 1fr}aside{padding:12px}header{padding:10px 14px}h1{font-size:19px}header p{font-size:12px}}@media(max-width:700px){main{display:block;height:auto}aside{max-height:350px}#viewport{height:650px}header{display:block}.checkgrid{grid-template-columns:repeat(3,1fr)}}
</style></head><body>
<header><div><h1>СКРУ-1 Пространственная геологическая модель</h1><p>Рельеф, девять геологических толщ, план горных работ, скважины и размерные сценарии камер в одной системе просмотра.</p></div><a href="unified_world.vtm">Открыть геометрию в ParaView</a></header>
<main><aside>
<div class="notice">Сценарное совмещение с поверхностью. Единую высотную привязку источники пока не устанавливают. Исходные отметки сохранены; верх массива замкнут на DSM с сохранением вертикальных мощностей.</div>
<fieldset><legend>Вид модели</legend><label>Представление<select id="mode"><option value="quarter">Массив с вырезом четверти</option><option value="full">Полный массив</option><option value="halfX">Половина массива по X</option><option value="halfY">Половина массива по Y</option><option value="sections">Пересекающиеся разрезы</option><option value="mine">Выработки внутри массива</option></select></label>
<label>Участок<select id="region"><option value="all">Вся область</option><option value="0">1ЮЗП</option><option value="1">2ЮЗП</option><option value="2">Блок 27</option><option value="3">Блок 53</option><option value="4">Блок 49</option><option value="5">Блок 51</option><option value="6">Блок 11</option></select></label>
<label>Вертикальный масштаб<select id="zscale"><option value="1">1 : 1 по всем направлениям</option><option value="3" selected>Вертикальное преувеличение в 3 раза</option><option value="5">Вертикальное преувеличение в 5 раз</option></select></label><small>При масштабе 1 : 1 сохраняются метрические пропорции.</small>
<div class="buttons"><button id="reset">Общий вид</button><button id="top">Сверху</button><button id="snapshot">Сохранить PNG</button></div></fieldset>
<fieldset><legend>Положение разрезов</legend><label>Сечение по X<input id="cutx" type="range" min="5" max="95" step="1" value="50"></label><label>Сечение по Y<input id="cuty" type="range" min="5" max="95" step="1" value="50"></label><div class="buttons"><button id="sectionX">Разрез по X</button><button id="sectionY">Разрез по Y</button></div><small>Срезы следуют исходной треугольной сетке. Вырез меняет только видимость модели.</small></fieldset>
<fieldset><legend>Состав сцены</legend><label>Поверхность<select id="terrain"><option value="smooth">Сглаженная DSM</option><option value="raw">Исходная DSM</option><option value="off">Скрыть поверхность</option></select></label>
<div class="checkgrid"><label><input id="geology" type="checkbox" checked>Толщи</label><label><input id="chambers" type="checkbox" checked>Камеры</label><label><input id="seams" type="checkbox" checked>КрII</label><label><input id="plans" type="checkbox" checked>План</label><label><input id="wells" type="checkbox" checked>Скважины</label><label><input id="recent" type="checkbox" checked>ICESat-2</label></div>
<label><input id="localstrata" type="checkbox">Локальные складчатые варианты</label><small>Камеры показывают размерные сценарии, а локальные толщи — альтернативные интерпретации. Они не устанавливают фактическую выемку или единственное строение массива.</small>
<label>Непрозрачность геологии<input id="opacity" type="range" min="10" max="100" step="5" value="100"></label>
<label>Спутниковые высоты<select id="epoch"><option value="all">Все 419 точек</option><option value="2024-07-20">20 июля 2024 года — 41 точка</option><option value="2025-07-19">19 июля 2025 года — 376 точек</option><option value="2026-01-16">16 января 2026 года — 2 точки</option></select></label></fieldset>
<fieldset><legend>Стратиграфия</legend><div id="legend"></div></fieldset>
<div class="links"><a href="data/joint_manifest.json">Основания и параметры сборки</a><a href="evidence/HEIGHT_FRAME_REVIEW_RU.md">Увязка высот и ограничения</a><a href="RESULT_RU.md">Состав модели и проверки</a></div>
</aside><section id="stage"><div id="viewport"></div><div id="loading">Загрузка полной геометрии и построение разрезов</div><div class="axis-help">Вращение — левая кнопка мыши · перенос — правая · масштаб — колесо</div><div id="info">Выберите объект в модели, чтобы увидеть его происхождение и статус.</div></section></main><div id="status">Подготовка сцены</div>
<script src="vendor/three.min.js"></script><script src="data/scene.js"></script><script src="data/supplement.js"></script><script src="data/unified.js"></script><script src="data/spatial_objects.js"></script><script src="unified_world_viewer.js"></script>
</body></html>'''


def run(out):
    repo = Path(__file__).resolve().parents[1]
    out = out.resolve()
    if not out.is_relative_to((repo/'work').resolve()):
        raise ValueError('real-data deliverable belongs in ignored work/')
    receipt = json.loads((out/'data_receipt.json').read_text(encoding='utf-8'))
    for filename, digest in receipt['outputs_sha256'].items():
        if sha(out/filename) != digest:
            raise ValueError(filename)
    shutil.copyfile(repo/'scripts/unified_world_viewer.js', out/'unified_world_viewer.js')
    (out/'index.html').write_text(PAGE, encoding='utf-8')
    files = ['index.html','unified_world_viewer.js','vendor/three.min.js']
    final = {'schema':'vkm.unified_view_receipt/1','builder_sha256':sha(Path(__file__)),
             'data_receipt_sha256':sha(out/'data_receipt.json'),
             'outputs_sha256':{name:sha(out/name) for name in files}}
    (out/'presentation_receipt.json').write_text(json.dumps(final,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'status':'READY','html':'index.html','files':len(files)}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,required=True)
    run(parser.parse_args().output)
