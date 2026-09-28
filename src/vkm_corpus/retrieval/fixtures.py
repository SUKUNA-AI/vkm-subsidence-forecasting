"""Deterministic synthetic fixtures for rerank parity and acceptance (no text or images of real sources).

* Six page-like images (карта-схема, таблица, график, разрез, текстовая страница, пустой лист) plus two figures that
  share the caption «Рис. 1» with different content — drawn with Pillow from fixed coordinates and a seeded RNG, so the
  same font file gives the same pixels. The font is a TrueType font with Cyrillic glyphs, found on the host (its name
  and sha256 go into the manifest).
* Five Russian visual queries (one expected image each) and 24 short synthetic Russian passages with five text
  queries (one expected passage each).

Used by ``vkm-corpus rerank fixtures`` (writes PNG files + ``manifest.json``); the acceptance run on EDGE reads that
directory, so the parity reference and the deployment see identical bytes.
"""
from __future__ import annotations

import hashlib
import json
import random
from pathlib import Path
from typing import Any

FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/noto/NotoSans-Regular.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "C:/Windows/Fonts/arial.ttf",  # host-path-ok (font lookup, not data)
)
PAGE = (1400, 1000)

VISUAL_QUERIES: list[dict[str, str]] = [
    {"id": "vq-map", "query": "схема шахтного поля со стволами и наблюдательной линией реперов", "expect": "map_scheme"},
    {"id": "vq-table", "query": "таблица результатов нивелирования реперов, оседание в миллиметрах", "expect": "table"},
    {"id": "vq-plot", "query": "график оседания земной поверхности во времени", "expect": "plot"},
    {"id": "vq-section", "query": "геологический разрез соляной толщи с калийными пластами", "expect": "section"},
    {"id": "vq-text", "query": "текст о методике маркшейдерских наблюдений за сдвижением горных пород",
     "expect": "text_page"},
]

TEXT_DOCS: list[dict[str, str]] = [
    {"id": "td-01", "text": "Нивелирование реперов наблюдательной станции выполняют два раза в год; по разности отметок "
                            "вычисляют оседание земной поверхности над выработанным пространством."},
    {"id": "td-02", "text": "Закладка выработанного пространства галитовыми отходами снижает скорость оседания и "
                            "уменьшает деформации земной поверхности."},
    {"id": "td-03", "text": "Каменная соль обладает выраженной ползучестью: при постоянной нагрузке деформация растёт "
                            "во времени, что учитывают реологические модели."},
    {"id": "td-04", "text": "Спутниковая радиолокационная интерферометрия позволяет получать карты смещений земной "
                            "поверхности с миллиметровой точностью по серии снимков."},
    {"id": "td-05", "text": "Спутниковые GNSS-наблюдения дают координаты пунктов в трёх измерениях и дополняют "
                            "геометрическое нивелирование на горных отводах."},
    {"id": "td-06", "text": "Водозащитная толща над калийными пластами должна сохранять сплошность, иначе возможен "
                            "прорыв рассолов в горные выработки."},
    {"id": "td-07", "text": "Междукамерные целики воспринимают нагрузку от вышележащих пород; их размеры определяют "
                            "по прочности соли и глубине разработки."},
    {"id": "td-08", "text": "Геологический разрез составляют по данным скважин: выделяют покровные отложения, "
                            "соляно-мергельную толщу и продуктивные пласты."},
    {"id": "td-09", "text": "Мульда сдвижения характеризуется максимальным оседанием, наклонами и горизонтальными "
                            "деформациями, которые сравнивают с допустимыми значениями."},
    {"id": "td-10", "text": "Камерная система разработки предусматривает оставление целиков и отработку пласта "
                            "камерами заданной ширины."},
    {"id": "td-11", "text": "Шахтный ствол проходят через водоносные горизонты с применением замораживания пород и "
                            "тюбинговой крепи."},
    {"id": "td-12", "text": "Сильвинитовые пласты отличаются от карналлитовых минеральным составом и прочностными "
                            "свойствами."},
    {"id": "td-13", "text": "Прогноз оседаний строят по результатам многолетних маркшейдерских измерений и "
                            "сравнивают с наблюдениями на опорных линиях."},
    {"id": "td-14", "text": "Сейсмическая разведка помогает выявлять зоны нарушения соляной толщи до начала "
                            "очистных работ."},
    {"id": "td-15", "text": "Георадарное зондирование применяют для изучения приконтурного массива и выявления "
                            "трещин в кровле выработок."},
    {"id": "td-16", "text": "Температура в глубоких выработках калийного рудника зависит от глубины и режима "
                            "проветривания."},
    {"id": "td-17", "text": "Методика маркшейдерских наблюдений за сдвижением включает закладку реперов, "
                            "периодическое нивелирование и обработку рядов измерений."},
    {"id": "td-18", "text": "Флотационное обогащение сильвинита позволяет получать хлористый калий заданного "
                            "качества."},
    {"id": "td-19", "text": "Провалы земной поверхности над рудниками связаны с нарушением водозащитной толщи и "
                            "растворением солей."},
    {"id": "td-20", "text": "Метод конечных элементов используют для расчёта напряжённо-деформированного состояния "
                            "соляного массива."},
    {"id": "td-21", "text": "Высокоточное нивелирование II класса обеспечивает среднюю квадратическую ошибку "
                            "превышения на станции около полумиллиметра."},
    {"id": "td-22", "text": "Мониторинг зданий на подрабатываемых территориях включает наблюдения за трещинами и "
                            "кренами."},
    {"id": "td-23", "text": "Горное давление в соляных породах проявляется конвергенцией выработок, которую измеряют "
                            "контурными реперами."},
    {"id": "td-24", "text": "Гидрогеологические скважины контролируют уровень и минерализацию подземных вод над "
                            "шахтным полем."},
]
TEXT_QUERIES: list[dict[str, str]] = [
    {"id": "tq-1", "query": "как измеряют оседание поверхности нивелированием реперов", "expect": "td-01"},
    {"id": "tq-2", "query": "ползучесть каменной соли и реологические модели", "expect": "td-03"},
    {"id": "tq-3", "query": "радарная интерферометрия для карт смещений", "expect": "td-04"},
    {"id": "tq-4", "query": "опасность прорыва рассолов через водозащитную толщу", "expect": "td-06"},
    {"id": "tq-5", "query": "закладка выработанного пространства и её влияние на оседание", "expect": "td-02"},
]


def find_font(explicit: str | None = None) -> Path:
    for candidate in ([explicit] if explicit else []) + list(FONT_CANDIDATES):
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    raise FileNotFoundError("no TrueType font with Cyrillic glyphs found; pass --font")


def _fonts(path: Path):
    from PIL import ImageFont

    return {size: ImageFont.truetype(str(path), size) for size in (18, 22, 26, 30, 36)}


def _canvas():
    from PIL import Image, ImageDraw

    img = Image.new("RGB", PAGE, (255, 255, 255))
    return img, ImageDraw.Draw(img)


def draw_map_scheme(f, title: str = "Схема шахтного поля и наблюдательной линии"):
    img, d = _canvas()
    d.text((60, 30), title, fill=(0, 0, 0), font=f[36])
    d.rectangle((100, 120, 1300, 900), outline=(0, 0, 0), width=4)
    for i, (x0, y0) in enumerate([(160, 180), (560, 180), (160, 540), (560, 540)], 1):
        d.rectangle((x0, y0, x0 + 340, y0 + 300), outline=(90, 60, 30), width=3)
        for k in range(0, 340, 24):
            d.line((x0 + k, y0, x0 + min(k + 150, 340), y0 + 150), fill=(170, 140, 110), width=1)
        d.text((x0 + 110, y0 + 200), f"Панель {i}", fill=(90, 60, 30), font=f[26])
    for name, (cx, cy) in (("Ствол №1", (1060, 300)), ("Ствол №2", (1060, 640))):
        d.ellipse((cx - 30, cy - 30, cx + 30, cy + 30), outline=(0, 0, 160), width=5)
        d.line((cx - 30, cy, cx + 30, cy), fill=(0, 0, 160), width=3)
        d.text((cx + 40, cy - 14), name, fill=(0, 0, 160), font=f[26])
    for k, x in enumerate(range(130, 1290, 96), 1):
        d.line((x, 480, min(x + 60, 1290), 480), fill=(200, 0, 0), width=4)
        d.ellipse((x - 7, 473, x + 7, 487), fill=(200, 0, 0))
        d.text((x - 12, 492), f"Rp{k}", fill=(200, 0, 0), font=f[18])
    d.text((140, 430), "Наблюдательная линия I", fill=(200, 0, 0), font=f[22])
    d.polygon([(1230, 150), (1215, 200), (1245, 200)], fill=(0, 0, 0))
    d.text((1222, 205), "С", fill=(0, 0, 0), font=f[26])
    d.text((110, 930), "Масштаб 1:10 000   — граница шахтного поля", fill=(0, 0, 0), font=f[22])
    return img


def draw_table(f, rng: random.Random, title: str = "Таблица 1 — Результаты нивелирования реперов"):
    img, d = _canvas()
    d.text((60, 30), title, fill=(0, 0, 0), font=f[36])
    cols = [100, 330, 620, 900, 1300]
    header = ["№ репера", "Отметка, м", "Оседание, мм", "Дата"]
    top, row_h, rows = 110, 52, 15
    for r in range(rows + 2):
        d.line((cols[0], top + r * row_h, cols[-1], top + r * row_h), fill=(0, 0, 0), width=3 if r < 2 else 1)
    for c in cols:
        d.line((c, top, c, top + (rows + 1) * row_h), fill=(0, 0, 0), width=2)
    for i, h in enumerate(header):
        d.text((cols[i] + 14, top + 12), h, fill=(0, 0, 0), font=f[26])
    for r in range(rows):
        y = top + (r + 1) * row_h + 12
        values = [f"Rp{r + 1}", f"{120 + rng.uniform(-5, 5):.3f}", f"{rng.uniform(5, 180):.1f}",
                  f"{rng.randint(1, 28):02d}.0{rng.randint(4, 9)}.2025"]
        for i, v in enumerate(values):
            d.text((cols[i] + 14, y), v, fill=(0, 0, 0), font=f[22])
    return img


def draw_plot(f, rng: random.Random, title: str = "Рис. 2. График оседания земной поверхности"):
    import math

    img, d = _canvas()
    d.text((60, 30), title, fill=(0, 0, 0), font=f[36])
    x0, y0, x1, y1 = 180, 120, 1320, 860
    for k in range(0, 11):
        x = x0 + (x1 - x0) * k // 10
        y = y0 + (y1 - y0) * k // 10
        d.line((x, y0, x, y1), fill=(220, 220, 220), width=1)
        d.line((x0, y, x1, y), fill=(220, 220, 220), width=1)
        d.text((x - 10, y1 + 12), str(2015 + k), fill=(0, 0, 0), font=f[18])
        d.text((x0 - 70, y - 10), str(-k * 20), fill=(0, 0, 0), font=f[18])
    d.line((x0, y0, x0, y1), fill=(0, 0, 0), width=3)
    d.line((x0, y1, x1, y1), fill=(0, 0, 0), width=3)
    for curve, color in ((1.0, (200, 0, 0)), (0.65, (0, 90, 200))):
        pts = []
        for k in range(0, 101):
            t = k / 100
            s = curve * (1 - math.exp(-3.2 * t)) + rng.uniform(-0.01, 0.01)
            pts.append((x0 + (x1 - x0) * t, y0 + (y1 - y0) * min(1.0, s)))
        d.line(pts, fill=color, width=4)
    d.text((x0 + 20, y1 - 60), "Время, годы", fill=(0, 0, 0), font=f[26])
    d.text((x0 + 20, y0 + 10), "Оседание, мм", fill=(0, 0, 0), font=f[26])
    d.text((x1 - 330, y0 + 40), "— Rp7   — Rp12", fill=(0, 0, 0), font=f[22])
    return img


def draw_section(f, title: str = "Геологический разрез по линии I–I"):
    img, d = _canvas()
    d.text((60, 30), title, fill=(0, 0, 0), font=f[36])
    layers = [("Покровные отложения", (205, 180, 120)), ("Соляно-мергельная толща", (170, 170, 150)),
              ("Покровная каменная соль", (220, 220, 235)), ("Сильвинитовый пласт АБ", (235, 150, 150)),
              ("Сильвинитовый пласт Кр-II", (230, 120, 120)), ("Подстилающая каменная соль", (200, 205, 225))]
    x0, x1, top = 100, 1300, 120
    y = top
    for i, (name, color) in enumerate(layers):
        h = 120 if i != 3 else 90
        wave = [(x, y + 12 * ((x // 150) % 2)) for x in range(x0, x1 + 1, 150)]
        bottom = [(x, y + h + 12 * ((x // 150) % 2)) for x in range(x1, x0 - 1, -150)]
        d.polygon(wave + bottom, fill=color, outline=(60, 60, 60))
        d.text((x0 + 20, y + h // 2 - 12), name, fill=(0, 0, 0), font=f[26])
        y += h
    for x in range(420, 1240, 170):
        d.rectangle((x, 560, x + 90, 600), fill=(40, 40, 40))
    d.text((980, 520), "Камеры", fill=(0, 0, 0), font=f[22])
    for x, name in ((300, "Скв. 1"), (1150, "Скв. 2")):
        d.line((x, top - 30, x, 880), fill=(0, 0, 0), width=4)
        d.text((x - 40, top - 60), name, fill=(0, 0, 0), font=f[22])
    return img


def draw_text_page(f):
    img, d = _canvas()
    d.text((60, 30), "Методика маркшейдерских наблюдений за сдвижением", fill=(0, 0, 0), font=f[36])
    lines = [
        "Наблюдения за сдвижением горных пород и земной поверхности выполняют",
        "на наблюдательных станциях, заложенных до начала очистных работ.",
        "Реперы располагают по профильным линиям вкрест и по простиранию",
        "выработок; расстояния между реперами выбирают по глубине разработки.",
        "Начальные наблюдения включают два независимых цикла нивелирования",
        "и определение планового положения реперов.",
        "Периодические наблюдения повторяют по графику, а при активизации",
        "процесса сдвижения — чаще. По результатам вычисляют оседания,",
        "наклоны, кривизну и горизонтальные деформации.",
        "Результаты сопоставляют с прогнозом и допустимыми деформациями",
        "для охраняемых объектов на земной поверхности.",
    ]
    for i, line in enumerate(lines):
        d.text((80, 130 + i * 62), line, fill=(20, 20, 20), font=f[30])
    d.text((650, 930), "— 12 —", fill=(0, 0, 0), font=f[22])
    return img


def draw_blank(_f=None):
    img, _ = _canvas()
    return img


def render_all(font_path: Path, seed: int = 20260928) -> dict[str, Any]:
    """name → PIL image (deterministic for a given font file and seed)."""
    f = _fonts(font_path)
    rng = random.Random(seed)
    return {
        "map_scheme": draw_map_scheme(f),
        "table": draw_table(f, rng),
        "plot": draw_plot(f, rng),
        "section": draw_section(f),
        "text_page": draw_text_page(f),
        "blank": draw_blank(),
        # «visual means visual» D: same caption, different content
        "fig1_plot": draw_plot(f, random.Random(seed + 1), title="Рис. 1"),
        "fig1_section": draw_section(f, title="Рис. 1"),
    }


def write_fixtures(out_dir: Path, font: str | None = None, seed: int = 20260928) -> dict[str, Any]:
    font_path = find_font(font)
    out_dir.mkdir(parents=True, exist_ok=True)
    images = {}
    for name, img in render_all(font_path, seed).items():
        path = out_dir / f"{name}.png"
        img.save(path, format="PNG", optimize=False, compress_level=6)
        data = path.read_bytes()
        images[name] = {"file": path.name, "sha256": hashlib.sha256(data).hexdigest(), "size_px": list(img.size)}
    manifest = {
        "fixture_set": "vkm-rerank-synthetic/1", "seed": seed, "font_file": font_path.name,
        "font_sha256": hashlib.sha256(font_path.read_bytes()).hexdigest(), "images": images,
        "visual_queries": VISUAL_QUERIES, "text_docs": TEXT_DOCS, "text_queries": TEXT_QUERIES,
        "note": "synthetic; no text or images of real sources",
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest
