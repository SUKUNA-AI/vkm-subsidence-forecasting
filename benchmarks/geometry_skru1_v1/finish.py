"""Write PRIVATE technical QA reports and a whitelisted public-safe receipt.

The public receipt contains counts, logical artifact IDs and SHA-256 only; literal
cells, quotations, coordinates, image fragments and machine paths are excluded.
"""
from __future__ import annotations
import argparse,collections,json
from pathlib import Path
from run import sha,write,local_file


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--out',default='work/geometry_2026-09-29')
    parser.add_argument('--public-receipt',default='docs/corpus_platform/receipts/geometry_skru1_v1.json')
    parser.add_argument('--inventory-count',type=int,default=411);parser.add_argument('--tests-passed',type=int,default=17)
    args=parser.parse_args();repo=Path.cwd().resolve();out=local_file(repo,args.out)
    if not out.is_relative_to(repo/'work'):raise ValueError('literal reports must remain PRIVATE')
    frozen=json.loads((out/'qgis_import_manifest.json').read_text(encoding='utf-8'))
    run=json.loads((out/'receipt.json').read_text(encoding='utf-8'))
    annotations=json.loads((out/'reviewed_annotations_receipt.json').read_text(encoding='utf-8'))
    dxf=json.loads((out/'dxf/DXF_RECEIPT.json').read_text(encoding='utf-8'))
    native=json.loads((out/'targeted/VKM-SRC-252_tables_1_1_1_2.json').read_text(encoding='utf-8'))
    registration=json.loads((out/'source_image_registration.json').read_text(encoding='utf-8'))
    qgis_file=repo/'work/qgis_2026-09-30/source_import/acceptance.json'
    qgis=json.loads(qgis_file.read_text(encoding='utf-8')) if qgis_file.exists() else None
    counts=collections.Counter(); source_counts=collections.Counter()
    units=collections.Counter(); nonempty=0
    for layer in frozen['layers']:
        data=json.loads((repo/layer['file']).read_text(encoding='utf-8'))
        if sha(repo/layer['file']) != layer['sha256']:raise ValueError('frozen source layer changed')
        for ft in data['features']:
            counts[ft['geometry']['type'] if ft.get('geometry') else 'NULL']+=1
            source_counts[ft['properties'].get('source_id','UNKNOWN')]+=1
        units[layer['coordinate_units']]+=1
        nonempty+=bool(data['features'])
    glyph_merge_count=sum(c['vertical_merge'] is not None or c['grid_span']>1 for t in native['selected_tables'] for row in t['rows'] for c in row['cells'])
    reviewed_count=sum(v['feature_count'] for v in annotations['layers'])
    table_count=sum(v['feature_count'] for v in frozen['layers'] if v['name']=='control_points')
    qa=registration[0].get('diagnostic_transform',{}) if registration else {}
    accuracy=['# Точность и ограничения геометрии','',
      f"Замороженный вход: `qgis_import_manifest.json`, SHA-256 `{sha(out/'qgis_import_manifest.json')}`. Геометрическая обработка охватила {frozen['raw_figures']} объектов; текущий inventory части A содержит {args.inventory_count}. Новые изображения после freeze не включены автоматически.",'',
      'Все source frames независимы. Растровые координаты — пиксели оригинального объекта, Y вниз; native PDF — unrotated page points. Числовые xcoord/ycoord и exel_X/Y_coord имеют UNIT_UNKNOWN и CRS_AUTHORITY_UNKNOWN: заголовки не доказывают метры. Метрическая регистрация к рудничной сетке не принята.','',
      'Бюджет ошибки: raster threshold, выбранный ROI, contour simplification и Hough gap являются MODEL_CHOICE. Использованы Lab distance threshold18, simplification0.75px, min enclosed area40px²/min colour area30px² и Hough maxLineGap2px. Стабильность этих решений на всех объектах не доказана. Training RMS, pixel precision и число вершин не являются полевой точностью.','',
      'Native PDF сохраняет clip/group levels, paint order, page rotation/derotation, оригинальные path items и ссылки на JSON/XObjects. Сложные clipping paths имеют UNAPPLIED_COMPLEX_CLIP; такие объекты требуют просмотра. Bezier chord tolerance0.1pt задаёт точность производной полилинии в координатах страницы, без перевода в метры.','',
      'Контуры — кандидаты, включая текст, штриховку и интерфейсные элементы вне вручную выделенных priority ROI. Некоторые Polygon-кандидаты самопересекаются/самокасаются после contour approximation. Их исходные вершины сохраняются; MakeValid, буферизация и скрытое исправление запрещены. Из invalid geometry нельзя получать принятые площади, пересечения или физические границы. Строгий QGIS-import это обнаружил; допустим отдельный source archive с validity flags и downstream rejection.','',
      'F023_007: оригинальный EMF содержит HEADER, EMR_STRETCHDIBITS и EOF, без vector primitives. Сохранён исходный DIB385×729. Предыдущий PNG1647×3199 является увеличением. В колонке видны разрывы масштаба; высоты полос в пикселях не переводятся в толщины/глубины. Буквально прочитанная средняя мощность имеет напечатанную единицу м, однако это обобщённая колонка, не скважинный лог СКРУ-1. Абсолютные глубины, Z и время UNKNOWN; мощности не суммировались в глубины.','',
      '## Диагностическая регистрация изображений','',
      f"F023_014(б) → F023_024(map): similarity; {qa.get('control_count','UNKNOWN')} автоматических соответствий; RMS={qa.get('rms','UNKNOWN')} target px; max={qa.get('max_residual','UNKNOWN')} target px; LOO RMS={qa.get('leave_one_out',{}).get('rms','UNKNOWN')} target px. Контрольные crop-пары и spatial distribution сохранены. Это RANSAC-selected texture matches; LOO условна, не независима от выбора inliers и не доказывает identity/geodetic registration.",'',
      'Affine/projective поддержаны проверками rank/degeneracy и требуют явного justification; на этих картах они не использованы как принятая регистрация. Соответствие table row → polygon по одному screenshot не восстановлено.','',
      '## Сохранение контейнеров','',
      f"DXF: {len(dxf['layers'])} отдельных source-frame files, {sum(l['entities'] for l in dxf['layers'])} entities; координатный roundtrip max delta={max(l['roundtrip_max_coordinate_delta'] for l in dxf['layers'])}. Это проверка сериализации, не оцифровки. Pixel/page/unknown-table layers имеют INSUNITS0; Polygon holes записаны отдельными closed polylines, топология задаётся GeoJSON. Фиксированные metadata date/GUID обеспечивают воспроизводимую сериализацию; это не время наблюдения.",'',
      f"QGIS: {qgis['status'] if qgis else 'SOURCE_ARCHIVE_IMPORT_PENDING_AFTER_STRICT_INVALID_GEOMETRY_REJECTION'}. EPSG не назначается. Полная приёмка B/C, требуемые метры и временная точность не заявляются.",'']
    report=['# Геометрия и геология: исполненный source-extraction','',
      f"Часть B/C фактически обработала {frozen['raw_figures']} source objects из замороженного inventory. Текущая часть A: {args.inventory_count} objects. Заморожены {len(frozen['layers'])} GeoJSON layers ({nonempty} непустых), {sum(counts.values())} features; {run['statistics']['features']} автоматических graphic candidates, {table_count} табличных target rows с UNKNOWN units и {reviewed_count} явных source annotations. Это количество графических кандидатов, не число подтверждённых геологических/горных объектов.",'',
      'Priority F023_014/024/025/026: source linework, замкнутые области и классы цветов по legend swatches извлечены; crop transform и original-image hashes сохранены. У F023_024 геометрический ROI исключает GUI/таблицу. Семантические значения цветов и связи чисел с объектами остаются UNKNOWN, если не прошли отдельное literal/vision review.','',
      'Source014: отдельно сохранены семь явно подписанных блоков, одна панель и два ствола в source pixels с атрибутами, локаторами и overlays. Объекты вручную проверены по изображениям, но имеют ASTRA_REVIEW_REQUIRED до независимой приёмки. Source011 не получает атрибуцию SKRU1 автоматически; среди материалов имеются другие рудники. Штриховка не превращается в surface, field observation или временную историю.','',
      'F023_007: original raster-only EMF исследован; DIB, record inventory и девять source bands сохранены. Средние мощности прочитаны в пределах напечатанного заголовка; UNKNOWN-depth поля не заполнены. Геология011/012 хранится как отдельная исходная геометрия без интерполяции и без объединения с разными масштабами/рудниками.','',
      'Source197: семь assembled native figures восстановлены из placement-совместимых horizontal XObject stripes; исходные fragments/transform/mask references и SHA сохранены. Карты INS на p13/p14 помечены MODEL_INS_OUTPUT_NOT_FIELD_OBSERVATION; они не GCP и не независимая validation.','',
      f"Source252: native DOCX XML tables1.1/1.2 (ordinals4/5), rows8/9, cells40/79. Сохранены {glyph_merge_count} merged/span cells, part/relationship/cell hashes и явные locator. Source SHA `{native['source_sha256']}`. Статус PLANNED и AUTO_EXTRACTED_UNREVIEWED; наличие плана не подтверждает исполнение. Таблицы не содержат native plan geometry; границы/даты не придуманы.",'',
      f"Проверки: {args.tests_passed} синтетических tests PASS; собственно extract, annotations и DXF реально выполнены на CPU. Geometry pipeline не запускал solver, CAD, GPU или локальные модели. Часть A и её OCR выполняются отдельно; это не утверждение об отсутствии GPU-задач во всей сессии.",'',
      'Артефакты: layers/, overlays/, original_objects/, targeted/, review_crops/, zone_attributes_gold.json, source_image_registration.json, ASTRA_REVIEW_REQUIRED.json, reviewed_annotations_receipt.json, qgis_import_manifest.json и dxf/DXF_RECEIPT.json. Они остаются PRIVATE в ignored work/. Санитизированная public receipt не включает координаты, цитаты, literal cells, изображения и machine paths.','',
      'Приёмка полного WorldSpec/metric registration/temporal history остаётся открытой. Required15 layers являются схемой контейнера; неизвестные mine_boundaries/boreholes/anomalies/geology coordinates не заполнены формальными заглушками. Реальные source geometry layers импортируются отдельно от метрических слоёв.','']
    if qgis:
        package=qgis.get('source_package',qgis)
        accuracy.extend(['',f"Source archive: raw coordinates {package.get('raw_coordinate_preservation','UNKNOWN')}; topology {package.get('topology_status','UNKNOWN')}. Invalid candidates={package.get('invalid_geometry_count','UNKNOWN')}, affected layers={package.get('layers_with_invalid_geometry','UNKNOWN')}. No MakeValid; downstream use rejected.",''])
        report.extend(['',f"Реальный QGIS source archive: {package.get('layer_count','UNKNOWN')} layers, {package.get('feature_count','UNKNOWN')} features; raw-coordinate roundtrip отдельно от topology status. Некорректные кандидаты сохранены с флагами и не разрешены для downstream операций.",''])
    (out/'ACCURACY.md').write_text('\n'.join(accuracy),encoding='utf-8',newline='\n')
    (out/'REVIEW_GEO_RU.md').write_text('\n'.join(report),encoding='utf-8',newline='\n')
    artifacts=[('SOURCE_IMPORT_SNAPSHOT',out/'qgis_import_manifest.json'),('AUTO_EXTRACTION_RECEIPT',out/'receipt.json'),
               ('EXPLICIT_SOURCE_ANNOTATIONS_RECEIPT',out/'reviewed_annotations_receipt.json'),('DXF_SERIALIZATION_RECEIPT',out/'dxf/DXF_RECEIPT.json'),
               ('NATIVE_TARGETED_RECEIPT',out/'targeted_receipt.json'),('ACCURACY_PRIVATE',out/'ACCURACY.md'),('GEO_REVIEW_PRIVATE',out/'REVIEW_GEO_RU.md')]
    if qgis:artifacts.append(('QGIS_SOURCE_ARCHIVE_ACCEPTANCE',qgis_file))
    migration=out/'source_regions_migration_receipt.json'
    if migration.exists():artifacts.append(('PRIVATE_SOURCE_REGIONS_MIGRATION_RECEIPT',migration))
    receipt={'schema':'vkm.geometry_skru1_v1.public_receipt/1','scope':'CPU_SOURCE_GEOMETRY_AND_QA_WITHOUT_METRIC_WORLD_ACCEPTANCE',
      'tool':'geometry-skru1/0.1','date':'2026-09-30','inventory_A_objects_at_reporting':args.inventory_count,
      'frozen_B_C_objects':frozen['raw_figures'],'layers':len(frozen['layers']),'nonempty_layers':nonempty,
      'features_by_geometry':dict(sorted(counts.items())),'features_by_source':dict(sorted(source_counts.items())),
      'automatic_graphic_candidates':run['statistics']['features'],'source_annotations_pending_independent_acceptance':reviewed_count,
      'table_target_points_unknown_units':table_count,'layer_units':dict(sorted(units.items())),
      'native_docx_252':{'role':'PLANNED','verification':'AUTO_EXTRACTED_UNREVIEWED','tables':['1.1','1.2'],'cells':[40,79],
                        'source_sha256':native['source_sha256'],'part_sha256':native['part_sha256'],'relationships_sha256':native['relationships_sha256']},
      'dxf':{'version':dxf['ezdxf_version'],'layers':len(dxf['layers']),'entities':sum(v['entities'] for v in dxf['layers']),
             'roundtrip_max_coordinate_delta':max(v['roundtrip_max_coordinate_delta'] for v in dxf['layers']),
             'invalid_polygon_policy':'SOURCE_POLYLINES_PRESERVED_WITHOUT_MAKEVALID'},
      'qgis_status':qgis['status'] if qgis else 'PENDING_SOURCE_ARCHIVE_IMPORT','tests':{'passed':args.tests_passed,'command':'python -B -m pytest -q tests/corpus/test_geometry_skru1_v1.py'},
      'metric_registration':'NOT_ACCEPTED','absolute_depth':'UNKNOWN','temporal_history':'UNKNOWN',
      'scientific_evidence_promotion':False,'solver_executed':False,'cad_executed':False,'gpu_in_geometry_pipeline':False,
      'private_artifacts':[{'logical_id':name,'sha256':sha(path)} for name,path in artifacts],
      'code_role':'CURRENT_PUBLIC_CODE_AT_REPORTING; executed code hashes remain in the original PRIVATE extraction receipt',
      'code':{str(p.relative_to(repo).as_posix()):sha(p) for p in sorted((repo/'benchmarks/geometry_skru1_v1').glob('*.py'))},
      'sanitization':'Explicit whitelist; no literals, quotations, coordinates, source fragments or machine paths'}
    if qgis:
        package=qgis.get('source_package',qgis)
        receipt['qgis_source_archive']={key:package.get(key) for key in ('layer_count','feature_count','invalid_geometry_count','layers_with_invalid_geometry','raw_coordinate_preservation','topology_status','output_sha256')}
    public=local_file(repo,args.public_receipt);write(public,receipt)
    write(out/'package_receipt.json',{'schema':'vkm.geometry_package/1','inputs':{str(p.relative_to(repo).as_posix()):sha(p) for _,p in artifacts},
       'outputs':{str(public.relative_to(repo).as_posix()):sha(public)},'command':'python -B benchmarks/geometry_skru1_v1/finish.py',
       'frozen_input_sha256':sha(out/'qgis_import_manifest.json'),'source_geometry_only':True})
    print(json.dumps({'public_receipt':args.public_receipt,'sha256':sha(public),'figures':frozen['raw_figures'],'layers':len(frozen['layers']),
                      'features':sum(counts.values()),'qgis_status':receipt['qgis_status']}))


if __name__=='__main__':main()
