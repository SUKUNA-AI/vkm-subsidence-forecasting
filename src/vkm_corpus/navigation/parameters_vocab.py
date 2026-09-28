"""Vocabulary of the parameter-candidate part of NAV (``parameters``): properties, materials, sites, scale cues, units.

Built from the project catalogues — the required parameters of the 72 processes
(``catalogues/physics/physics_coverage_and_execution_matrix.csv``), the variables of the mechanics catalogue
(``evidence/materials/mechanics_evidence_catalog.csv``) and of the evidence records of kind ``parameter`` — plus the
reports in ``docs/science`` and the terms of the concept graph (N3). It is our own wording of the project scope: no
corpus text.

Patterns are regular expressions over lower-case text with «ё» folded to «е» (Russian stems cover the case forms);
``abbr`` patterns are case-sensitive. ``dims`` lists the unit dimensions a value of the property may carry
(``none`` — a bare number, ``any`` — the unit is not checked). ``vrange`` is a plausibility gate in SI units: a parse
outside it is not a value of that property (a page number, an equation number, a relative change) and is dropped.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# ------------------------------------------------------------------------------------------------------ dimensions
PRESSURE, DENSITY, WEIGHT, ANGLE, LENGTH, TIME = "pressure", "density", "weight_density", "angle", "length", "time"
VELOCITY, STRAIN_RATE, RATIO, STRAIN, NONE = "velocity", "strain_rate", "ratio", "strain", "none"
VISCOSITY, MOLAR_ENERGY, PERMEABILITY, AREA, CURVATURE, ANY = (
    "viscosity", "molar_energy", "permeability", "area", "curvature", "any")

SI_UNIT: dict[str, str] = {
    PRESSURE: "Pa", DENSITY: "kg/m3", WEIGHT: "N/m3", ANGLE: "deg", LENGTH: "m", TIME: "s", VELOCITY: "m/s",
    STRAIN_RATE: "1/s", RATIO: "1", STRAIN: "1", NONE: "1", VISCOSITY: "Pa*s", MOLAR_ENERGY: "J/mol",
    PERMEABILITY: "m2", AREA: "m2", CURVATURE: "1/m",
}
DIMENSIONLESS = (RATIO, STRAIN, NONE)

_DAY = 86400.0
_YEAR = 365.25 * _DAY
_MONTH = _YEAR / 12.0

# ------------------------------------------------------------------------------------------------------ properties


@dataclass(frozen=True)
class PropertyDef:
    key: str
    label_ru: str
    label_en: str
    group: str
    dims: tuple[str, ...]
    patterns: tuple[str, ...]
    prefilter: tuple[str, ...]
    symbols: tuple[str, ...] = ()
    vrange: tuple[float, float] | dict[str, tuple[float, float]] | None = None
    synonyms: tuple[str, ...] = ()
    # the property words themselves: a «foreign quantity» noun of this list is not foreign for this property
    own_nouns: tuple[str, ...] = ()


_ANG = r"уг(?:ол|л[аеуыо]\w*)"          # угол, угла, углу, углом, углы, углов, …
_MOD = r"модул\w*"
_STR = r"(?:предел\w* )?прочност\w*"
_GAP2 = r"(?: [а-яё]+){0,2}"               # «предел прочности карналлита на растяжение»


def P(key, label_ru, label_en, group, dims, patterns, prefilter, **kw) -> PropertyDef:  # noqa: N802 - table helper
    return PropertyDef(key, label_ru, label_en, group, tuple(dims), tuple(patterns), tuple(prefilter), **kw)


PROPERTIES: tuple[PropertyDef, ...] = (
    # ---------------------------------------------------------------- elasticity and deformability
    P("youngs_modulus", "модуль упругости", "Young's modulus", "mechanics", [PRESSURE],
      [_MOD + r" (?:статическ\w* |динамическ\w* |мгновенн\w* )?(?:упругост\w*|юнга)",
       r"young'?s? modul\w*", r"modul\w* of elasticity", r"elastic(?:ity)? modul\w*"],
      ["модул", "modul"], symbols=("E", "Eу", "Ey"), vrange=(1e4, 3e11), own_nouns=("упругост", "модул")),
    P("deformation_modulus", "модуль деформации", "deformation modulus", "mechanics", [PRESSURE],
      [_MOD + r" (?:общ\w* |полн\w* |секущ\w* |касательн\w* |предельн\w* |статическ\w* |динамическ\w* )?"
              r"деформаци\w*",
       r"(?:секущ|касательн|предельн)\w* " + _MOD + r"(?! упругост| сдвиг| спада)",
       r"deformation modul\w*", r"modul\w* of deformation"],
      ["модул", "modul"], symbols=("E0", "Eд", "D", "Dпр", "Dу"), vrange=(1e4, 3e11),
      own_nouns=("деформаци", "модул")),
    P("softening_modulus", "модуль спада", "post-peak (softening) modulus", "mechanics", [PRESSURE],
      [_MOD + r" спада", _MOD + r" разупрочнени\w*", r"post[- ]peak modul\w*", r"softening modul\w*",
       r"drop modul\w*"],
      ["модул", "modul"], symbols=("M",), vrange=(1e4, 3e11), own_nouns=("спад", "модул")),
    P("shear_modulus", "модуль сдвига", "shear modulus", "mechanics", [PRESSURE],
      [_MOD + r" сдвиг\w*", r"shear modul\w*", r"modul\w* of rigidity"], ["модул", "modul"],
      symbols=("G",), vrange=(1e4, 3e11), own_nouns=("сдвиг", "модул")),
    P("bulk_modulus", "модуль объёмного сжатия", "bulk modulus", "mechanics", [PRESSURE],
      [_MOD + r" объемн\w* (?:сжати|деформаци|упругост)\w*", r"объемн\w* " + _MOD, r"bulk modul\w*"],
      ["модул", "modul"], symbols=("K",), vrange=(1e4, 3e11), own_nouns=("объемн", "модул")),
    P("poisson_ratio", "коэффициент Пуассона", "Poisson's ratio", "mechanics", [NONE],
      [r"коэффициент\w* пуассона", r"poisson'?s? ratio", r"коэффициент\w* поперечн\w* деформаци\w*"],
      ["пуассон", "poisson", "поперечн"], symbols=("ν", "μ"), vrange=(0.0, 0.5),
      own_nouns=("пуассон", "деформаци")),
    # ---------------------------------------------------------------- strength
    P("ucs", "предел прочности на одноосное сжатие", "uniaxial compressive strength", "mechanics", [PRESSURE],
      [_STR + _GAP2 + r" (?:на|при) (?:одноосн\w* |осев\w* )?сжати\w*",
       r"(?:временн\w* )?сопротивлени\w* (?:одноосному )?сжатию",
       r"(?:uniaxial )?compressive strength", r"uniaxial strength", r"\bucs\b"],
      ["прочност", "сжати", "compressive", "ucs"], symbols=("σсж", "σ_сж", "Rс"), vrange=(1e4, 1e9),
      own_nouns=("прочност", "сжати")),
    P("tensile_strength", "предел прочности на растяжение", "tensile strength", "mechanics", [PRESSURE],
      [_STR + _GAP2 + r" (?:на|при) (?:одноосн\w* )?растяжени\w*", r"сопротивлени\w* растяжению",
       r"(?:uniaxial )?tensile strength"],
      ["прочност", "растяж", "tensile"], symbols=("σр", "σ_р"), vrange=(1e3, 3e8),
      own_nouns=("прочност", "растяжени")),
    P("flexural_strength", "предел прочности при изгибе", "flexural strength", "mechanics", [PRESSURE],
      [_STR + _GAP2 + r" (?:на|при) изгиб\w*", r"(?:flexural|bending) strength"], ["изгиб", "flexural", "bending"],
      symbols=("σизг",), vrange=(1e3, 3e8), own_nouns=("прочност", "изгиб")),
    P("shear_strength", "прочность на сдвиг (срез)", "shear strength", "mechanics", [PRESSURE],
      [_STR + _GAP2 + r" (?:на|при) (?:сдвиг|срез)\w*", r"сопротивлени\w* (?:сдвигу|срезу)", r"shear strength"],
      ["сдвиг", "срез", "shear"], symbols=("τ",), vrange=(1e3, 1e9), own_nouns=("прочност", "сдвиг", "срез")),
    P("residual_strength", "остаточная прочность", "residual strength", "mechanics", [PRESSURE],
      [r"остаточн\w* прочност\w*", r"residual strength"], ["остаточн", "residual"], vrange=(1e3, 1e9),
      own_nouns=("прочност",)),
    P("standard_strength", "стандартная (нормативная) прочность", "standard (normative) strength", "mechanics",
      [PRESSURE], [r"(?:стандартн|нормативн)\w* прочност\w*"], ["стандартн", "нормативн"], vrange=(1e4, 1e9),
      own_nouns=("прочност",)),
    P("strength_unspecified", "предел прочности (вид не указан)", "strength (type not stated)", "mechanics",
      [PRESSURE], [r"предел\w* прочност\w*", r"прочност\w*", r"\bstrength\b"], ["прочност", "strength"],
      vrange=(1e4, 1e9), own_nouns=("прочност",)),
    P("cohesion", "сцепление", "cohesion", "mechanics", [PRESSURE],
      [r"(?:коэффициент\w* )?сцеплени\w*", r"\bcohesion\b"], ["сцеплен", "cohesion"], symbols=("C", "c", "k"),
      vrange=(0.0, 1e9), own_nouns=("сцеплени",)),
    P("friction_angle", "угол внутреннего трения", "friction angle", "mechanics", [ANGLE],
      [_ANG + r" (?:внутренн\w* )?трени\w*", r"(?:internal )?friction angle", r"angle of (?:internal )?friction"],
      ["трени", "friction"], symbols=("φ", "ρ"), vrange=(0.0, 90.0), own_nouns=("трени", "угол", "угл")),
    P("friction_coefficient", "коэффициент (внутреннего) трения", "friction coefficient", "mechanics", [NONE],
      [r"коэффициент\w* (?:внутренн\w* )?трени\w*", r"friction coefficient", r"coefficient of friction"],
      ["трени", "friction"], symbols=("f", "tgφ"), vrange=(0.0, 3.0), own_nouns=("трени",)),
    P("limit_strain", "предельная деформация", "strain at failure", "mechanics", [RATIO, STRAIN, NONE],
      [r"(?:относительн\w* )?(?:предельн|критическ|разрушающ)\w* (?:продольн\w* |поперечн\w* )?деформаци\w*",
       r"деформаци\w* (?:при|на пределе) (?:разрушени|прочност)\w*", r"strain at (?:failure|peak)",
       r"(?:failure|peak) strain"],
      ["деформаци", "strain"], symbols=("εпр", "ε_кр"), vrange=(0.0, 1.0), own_nouns=("деформаци",)),
    # ---------------------------------------------------------------- density and weight
    P("density", "плотность", "density", "physical", [DENSITY, WEIGHT],
      [r"(?:объемн\w* |средн\w* )?плотност\w*", r"объемн\w* масс\w*", r"(?:bulk |rock |grain )?density"],
      ["плотност", "масс", "density"], symbols=("ρ",), vrange={DENSITY: (500.0, 6000.0), WEIGHT: (5e3, 6e4)},
      own_nouns=("плотност", "масс")),
    P("unit_weight", "объёмный (удельный) вес", "unit weight", "physical", [WEIGHT, DENSITY],
      [r"(?:объемн|удельн)\w* вес\w*", r"(?:unit|specific) weight"], ["вес", "weight"], symbols=("γ",),
      vrange={DENSITY: (500.0, 6000.0), WEIGHT: (5e3, 6e4)}, own_nouns=("вес",)),
    P("moisture_content", "влажность", "moisture content", "physical", [RATIO],
      [r"(?<!относительная )(?<!относительной )(?:естественн\w* |весов\w* |начальн\w* )?влажност\w*(?! воздух)",
       r"moisture content", r"water content"], ["влажност", "moisture", "water content"], symbols=("W",),
      vrange=(0.0, 1.0),
      own_nouns=("влажност",)),
    P("porosity", "пористость", "porosity", "hydro", [RATIO, NONE],
      [r"(?:коэффициент\w* )?(?:общ\w* |эффективн\w* |открыт\w* )?пористост\w*", r"porosity"],
      ["пористост", "porosity"], symbols=("n",), vrange=(0.0, 1.0), own_nouns=("пористост",)),
    P("permeability", "проницаемость", "permeability", "hydro", [PERMEABILITY, AREA],
      [r"(?:коэффициент\w* )?(?:газо)?проницаемост\w*", r"(?<!magnetic )(?<!dielectric )permeability"],
      ["проницаем", "permeab"], symbols=("k",), vrange=(1e-30, 1e-6), own_nouns=("проницаемост",)),
    P("hydraulic_conductivity", "коэффициент фильтрации", "hydraulic conductivity", "hydro", [VELOCITY],
      [r"коэффициент\w* фильтраци\w*", r"hydraulic conductivity"], ["фильтрац", "conductivity"],
      symbols=("Kф",), own_nouns=("фильтраци",)),
    # ---------------------------------------------------------------- rheology
    P("long_term_strength", "предел длительной прочности", "long-term strength", "rheology", [PRESSURE],
      [r"(?:предел\w* )?длительн\w* прочност\w*", r"long[- ]term strength"], ["длительн", "long"],
      symbols=("σ∞", "σдл"), vrange=(1e4, 1e9), own_nouns=("прочност", "длительн")),
    P("long_term_strength_ratio", "коэффициент длительной прочности", "long-term strength ratio", "rheology",
      [NONE, RATIO], [r"коэффициент\w* длительн\w* прочност\w*", r"long[- ]term strength (?:ratio|coefficient)"],
      ["длительн", "long"], symbols=("Kдл",), vrange=(0.0, 1.2), own_nouns=("прочност", "длительн")),
    P("creep_rate", "скорость ползучести", "creep rate", "rheology", [STRAIN_RATE],
      [r"скорост\w* (?:установивш\w* |стационарн\w* |неустановивш\w* |деформаци\w* )?ползучест\w*",
       r"скорост\w* деформаци\w* ползучест\w*", r"(?:steady[- ]state |secondary |minimum )?creep (?:strain )?rate"],
      ["ползуч", "creep"], symbols=("ε̇",), own_nouns=("скорост", "ползучест", "деформаци")),
    P("creep_exponent", "показатель ползучести (степени)", "creep (stress) exponent", "rheology", [NONE],
      [r"показател\w* (?:степени )?ползучест\w*", r"(?:stress|creep|norton) exponent"],
      ["показател", "exponent"], symbols=("n",), vrange=(0.5, 30.0), own_nouns=("ползучест", "показател")),
    P("creep_coefficient", "коэффициент ползучести", "creep coefficient", "rheology", [ANY],
      [r"коэффициент\w* ползучест\w*", r"creep coefficient"], ["ползуч", "creep"], symbols=("A", "k"),
      own_nouns=("ползучест",)),
    P("rheological_constant", "параметр реологической модели (ядра ползучести)", "rheological constant",
      "rheology", [ANY],
      [r"реологическ\w* (?:параметр|констант|характеристик|коэффициент)\w*",
       r"параметр\w* (?:ядра|ядер|функци\w*) (?:ползучест|релаксаци)\w*", r"параметр\w* ползучест\w*",
       r"параметр\w* релаксаци\w*", r"creep (?:parameter|constant)s?", r"rheological (?:parameter|constant)s?"],
      ["реологич", "ползуч", "релаксац", "creep", "rheolog"], symbols=("α", "δ", "β"),
      own_nouns=("ползучест", "релаксаци", "параметр")),
    P("viscosity", "вязкость", "viscosity", "rheology", [VISCOSITY],
      [r"(?:коэффициент\w* )?(?:динамическ\w* |эффективн\w* )?вязкост\w*(?! разрушени)", r"viscosity"],
      ["вязкост", "viscosity"], symbols=("η",), own_nouns=("вязкост",)),
    P("activation_energy", "энергия активации", "activation energy", "rheology", [MOLAR_ENERGY],
      [r"энерги\w* активаци\w*", r"activation energy"], ["активац", "activation"], symbols=("Q",),
      own_nouns=("энерги", "активаци")),
    # ---------------------------------------------------------------- initial stress
    P("vertical_stress", "вертикальное напряжение", "vertical stress", "stress", [PRESSURE],
      [r"вертикальн\w* (?:составляющ\w* )?(?:напряжени\w*|давлени\w*)", r"(?:лито|гео)статическ\w* "
       r"(?:давлени|напряжени)\w*", r"vertical stress", r"(?:overburden|lithostatic) (?:stress|pressure)"],
      ["вертикальн", "статическ", "vertical", "overburden", "lithostatic"], symbols=("σv", "σz", "γH"),
      vrange=(1e3, 1e9), own_nouns=("напряжени", "давлени")),
    P("horizontal_stress", "горизонтальное напряжение", "horizontal stress", "stress", [PRESSURE],
      [r"горизонтальн\w* (?:составляющ\w* )?(?:напряжени\w*|давлени\w*)",
       r"horizontal (?:principal )?stress"], ["горизонтальн", "horizontal"], symbols=("σh", "σH", "σx"),
      vrange=(1e3, 1e9), own_nouns=("напряжени", "давлени")),
    P("lateral_pressure_coefficient", "коэффициент бокового распора", "lateral pressure coefficient", "stress",
      [NONE], [r"коэффициент\w* бокового (?:распора|давления)", r"бокового распора", r"боков\w* распор\w*",
               r"lateral (?:earth )?pressure coefficient", r"horizontal[- ]to[- ]vertical stress ratio"],
      ["распор", "бокового давлен", "lateral", "horizontal-to"], symbols=("λ", "K0"), vrange=(0.05, 5.0),
      own_nouns=("распор", "давлени")),
    # ---------------------------------------------------------------- mining geometry
    P("depth", "глубина (разработки, залегания)", "depth", "geometry", [LENGTH],
      [r"глубин\w* (?:залегани\w*|разработк\w*|отработк\w*|выемк\w*|заложени\w*|ведени\w* (?:горн\w* |очистн\w* )?"
       r"работ|горн\w* работ|очистн\w* работ|расположени\w* (?:выработ|пласт|камер|горизонт)\w*|"
       r"от (?:дневн\w* )?поверхност\w*)",
       r"на глубин(?:е|ах)", r"(?:mining|excavation|working) depths?",
       r"depths? of (?:mining|cover|the (?:mine|workings|seam|deposit|excavation))", r"at (?:a )?depths? of"],
      ["глубин", "depth"], symbols=("H",), vrange=(1.0, 10000.0), own_nouns=("глубин",)),
    P("seam_thickness", "мощность пласта (вынимаемая)", "seam (extracted) thickness", "geometry", [LENGTH],
      [r"(?:вынимаем\w* |извлекаем\w* |геологическ\w* |средн\w* |полезн\w* |суммарн\w* |общ\w* )?мощност\w* "
       r"(?:пласт\w*|залеж\w*|рудн\w* тел\w*|вынимаем\w* сло\w*)",
       r"(?:вынимаем|извлекаем)\w* мощност\w*", r"seam thickness", r"thickness of (?:the )?seams?",
       r"(?:mining|extraction) height", r"пласт\w* (?:[а-я]+ )?мощност(?:ью|ь)\b"],
      ["мощност", "thickness", "height"], symbols=("m", "m0"), vrange=(0.05, 300.0),
      own_nouns=("мощност",)),
    P("layer_thickness", "мощность толщи (слоя, пачки)", "layer thickness", "geometry", [LENGTH],
      [r"мощност\w* (?:водозащитн\w* толщ\w*|толщ\w*|сло[яеёи]\w*|слоев|пачк\w*|горизонт\w*|прослоя|прослоев|"
       r"прослойк\w*|прослои|междупласть\w*|покровн\w*|подстилающ\w*|наносов|отложени\w*|надсол\w*|соляной|"
       r"соленосн\w*|вышележащ\w*|налегающ\w*|взт|пкс|смт|пцт|ткт)",
       r"thickness of (?:the )?(?:layer|stratum|overburden|salt|cover|interlayer)s?", r"layer thickness"],
      ["мощност", "thickness"], symbols=("h",), vrange=(0.001, 3000.0), own_nouns=("мощност",)),
    P("thickness_unspecified", "мощность (объект не указан)", "thickness (body not stated)", "geometry", [LENGTH],
      [r"(?:средн\w* |общ\w* |суммарн\w* |максимальн\w* |минимальн\w* |истинн\w* |нормальн\w* )?мощност\w*",
       r"\bthickness\b"], ["мощност", "thickness"], symbols=("m", "h"), vrange=(0.001, 3000.0),
      own_nouns=("мощност",)),
    P("depth_unspecified", "глубина (объект не указан)", "depth (object not stated)", "geometry", [LENGTH],
      [r"(?:средн\w* |максимальн\w* |минимальн\w* )?глубин\w*", r"\bdepths?\b"], ["глубин", "depth"],
      symbols=("H",), vrange=(0.01, 10000.0), own_nouns=("глубин",)),
    P("room_width", "ширина камеры", "room (chamber) width", "geometry", [LENGTH],
      [r"ширин\w* (?:очистн\w* )?камер\w*", r"пролет\w* (?:очистн\w* )?камер\w*", r"(?:room|chamber) (?:width|span)",
       r"width of (?:the )?(?:rooms?|chambers?|stopes?)", r"камер\w* (?:[а-я]+ )?ширин(?:ой|ою)"],
      ["ширин", "пролет", "width", "span"], symbols=("a",), vrange=(0.3, 200.0), own_nouns=("ширин", "пролет")),
    P("pillar_width", "ширина целика", "pillar width", "geometry", [LENGTH],
      [r"ширин\w* (?:междукамерн\w* |ленточн\w* |барьерн\w* |предохранительн\w* |охранн\w* |межпанельн\w* |"
       r"межходов\w* |опорн\w* |междушахтн\w* )?целик\w*", r"pillar width", r"width of (?:the )?pillars?",
       r"целик\w* (?:[а-я]+ )?ширин(?:ой|ою)"],
      ["ширин", "width"], symbols=("b",), vrange=(0.3, 3000.0), own_nouns=("ширин",)),
    P("excavation_height", "высота камеры (целика)", "room (pillar) height", "geometry", [LENGTH],
      [r"высот\w* (?:очистн\w* )?камер\w*", r"высот\w* (?:междукамерн\w* )?целик\w*",
       r"(?:room|pillar|chamber|stope) height", r"(?:камер|целик)\w* (?:[а-я]+ )?высот(?:ой|ою)"],
      ["высот", "height"], symbols=("h",), vrange=(0.3, 200.0),
      own_nouns=("высот",)),
    P("loading_degree", "степень нагружения целиков", "pillar loading degree", "geometry", [NONE, RATIO],
      [r"(?:расчетн\w* |допустим\w* |фактическ\w* |средн\w* )?степен\w* нагружени\w*", r"коэффициент\w* нагружени\w*",
       r"(?:pillar )?loading degree", r"degree of loading"],
      ["нагружени", "loading"], symbols=("C",), vrange=(0.0, 2.0), own_nouns=("нагружени",)),
    P("extraction_ratio", "коэффициент извлечения", "extraction ratio", "geometry", [NONE, RATIO],
      [r"коэффициент\w* извлечени\w*", r"(?:степен|уровн|уровен)\w* извлечени\w*", r"(?:extraction|recovery) ratio"],
      ["извлечени", "extraction", "recovery"], vrange=(0.0, 1.0), own_nouns=("извлечени",)),
    # ---------------------------------------------------------------- backfill
    P("backfill_ratio", "коэффициент закладки (заполнения камер)", "backfill ratio", "backfill", [NONE, RATIO],
      [r"коэффициент\w* (?:закладки|заполнения|закладывания)", r"степен\w* (?:закладки|заполнения)",
       r"полнот\w* закладки", r"back ?fill(?:ing)? ratio", r"fill(?:ing)? ratio"],
      ["заклад", "заполнен", "fill"], symbols=("A", "kзап"), vrange=(0.0, 1.0),
      own_nouns=("закладк", "заполнени")),
    P("backfill_shrinkage", "усадка закладки", "backfill shrinkage", "backfill", [NONE, RATIO, LENGTH],
      [r"коэффициент\w* усадки", r"усадк\w* (?:закладк\w*|закладочн\w* (?:массив|материал|смес)\w*)",
       r"back ?fill shrinkage"], ["усадк", "shrinkage"], symbols=("B", "kу"), vrange=(0.0, 2.0),
      own_nouns=("усадк", "закладк")),
    # ---------------------------------------------------------------- subsidence
    P("max_subsidence", "максимальное оседание", "maximum subsidence", "subsidence", [LENGTH],
      [r"максимальн\w* (?:величин\w* )?(?:оседани\w*|опускани\w*)(?: земной поверхности)?",
       r"(?:оседани|опускани)\w* максимальн\w*", r"max(?:imum)?\.? subsidence"],
      ["оседан", "опускан", "subsidence"], symbols=("ηmax", "η0", "ηm"), vrange=(1e-4, 100.0),
      own_nouns=("оседани", "опускани")),
    P("subsidence", "оседание (величина)", "subsidence (value)", "subsidence", [LENGTH],
      [r"оседани\w*(?: земной поверхности)?", r"(?:ground |surface |land )?subsidence"], ["оседан", "subsidence"],
      symbols=("η",), vrange=(1e-4, 100.0), own_nouns=("оседани",)),
    P("subsidence_rate", "скорость оседания", "subsidence rate", "subsidence", [VELOCITY],
      [r"скорост\w* (?:нарастани\w* |развити\w* )?(?:оседани|опускани)\w*(?: земной поверхности)?",
       r"(?:оседани|опускани)\w*(?:\s+[^\s.;]+){0,6}?\s+со\s+скорост\w*", r"subsidence (?:rate|velocity)",
       r"rate of subsidence"], ["скорост", "rate", "velocity"], symbols=("v", "η̇"),
      own_nouns=("скорост", "оседани", "опускани")),
    P("boundary_angle", "граничный угол", "boundary (limit) angle", "subsidence", [ANGLE],
      [r"граничн\w* " + _ANG, r"(?:boundary|limit) angles?", r"angles? of draw"], ["граничн", "angle"],
      symbols=("δ0", "β0", "γ0"), vrange=(0.0, 90.0), own_nouns=("угол", "угл")),
    P("displacement_angle", "угол сдвижения", "angle of displacement", "subsidence", [ANGLE],
      [_ANG + r" сдвижени\w*", r"angles? of (?:critical )?displacement"], ["сдвижени", "displacement"],
      symbols=("δ", "β", "γ"), vrange=(0.0, 90.0), own_nouns=("угол", "угл", "сдвижени")),
    P("break_angle", "угол разрывов", "angle of breaks (cracks)", "subsidence", [ANGLE],
      [_ANG + r" (?:разрыв|трещин)\w*", r"angles? of (?:break|crack)s?"], ["разрыв", "трещин", "break", "crack"],
      vrange=(0.0, 90.0), own_nouns=("угол", "угл")),
    P("full_subsidence_angle", "угол полных сдвижений", "angle of full subsidence", "subsidence", [ANGLE],
      [_ANG + r" полн\w* (?:сдвижени|оседани)\w*", r"angles? of full subsidence"], ["полн", "full"],
      symbols=("ψ",), vrange=(0.0, 90.0), own_nouns=("угол", "угл", "сдвижени")),
    P("max_subsidence_angle", "угол максимального оседания", "angle of maximum subsidence", "subsidence", [ANGLE],
      [_ANG + r" максимальн\w* оседани\w*", r"angles? of maximum subsidence"], ["максимальн", "maximum"],
      symbols=("θ",), vrange=(0.0, 90.0), own_nouns=("угол", "угл", "оседани")),
    P("subsidence_duration", "продолжительность процесса сдвижения", "duration of subsidence", "subsidence", [TIME],
      [r"(?:общ\w* )?(?:продолжительност|длительност|врем|срок)\w* (?:процесса )?(?:сдвижени|оседани)\w*",
       r"duration of (?:the )?(?:subsidence|movement)"], ["сдвижени", "оседани", "duration"],
      symbols=("T",), vrange=(3600.0, 2e10), own_nouns=("врем", "продолжительност", "срок", "длительност")),
    P("horizontal_strain", "горизонтальная деформация", "horizontal strain", "subsidence", [STRAIN, NONE],
      [r"(?:относительн\w* )?горизонтальн\w* деформаци\w*(?: растяжени\w*| сжати\w*)?",
       r"horizontal strains?"], ["горизонтальн", "horizontal"], symbols=("ε",), vrange=(0.0, 0.1),
      own_nouns=("деформаци",)),
    P("horizontal_displacement", "горизонтальное сдвижение", "horizontal displacement", "subsidence", [LENGTH],
      [r"горизонтальн\w* (?:сдвижени|смещени|перемещени)\w*", r"horizontal displacements?"],
      ["горизонтальн", "horizontal"], symbols=("ξ",), vrange=(1e-5, 50.0), own_nouns=("сдвижени", "смещени")),
    P("tilt", "наклон земной поверхности", "tilt", "subsidence", [STRAIN],
      [r"наклон\w*(?: земной| дневной)?(?: поверхности)?", r"\btilts?\b"], ["наклон", "tilt"], symbols=("i",),
      vrange=(0.0, 0.5), own_nouns=("наклон",)),
    P("curvature", "кривизна земной поверхности", "curvature", "subsidence", [CURVATURE],
      [r"кривизн\w*(?: земной| дневной)?(?: поверхности)?", r"\bcurvatures?\b"], ["кривизн", "curvature"],
      symbols=("K", "k"), vrange=(0.0, 1.0), own_nouns=("кривизн",)),
)

PROPERTY_BY_KEY: dict[str, PropertyDef] = {p.key: p for p in PROPERTIES}
# the longest (most specific) phrase wins when two properties match overlapping text
PROPERTY_GROUPS = sorted({p.group for p in PROPERTIES})

# «foreign quantity» words: a value right after one of them belongs to that quantity, not to the property (regex
# fragments matched at a word start; the words of other property phrases are masked before the check)
FOREIGN_NOUNS: tuple[str, ...] = (
    r"давлени\w*", r"нагрузк\w*", r"напряжени\w*", r"нагружени\w*", r"температур\w*", r"глубин\w*", r"скорост\w*",
    r"влажност\w*", r"времен\w*", r"время\b", r"продолжительност\w*", r"длительност\w*", r"ширин\w*", r"высот\w*",
    r"мощност\w*", r"размер\w*", r"диаметр\w*", r"длин(?:а|ы|е|у|ой)\b", r"расстояни\w*", r"возраст\w*",
    r"объем(?:а|е|у|ом|ы|ов)?\b", r"толщин\w*", r"шаг(?:а|е|ом|у)?\b", r"пролет\w*", r"площад\w*", r"частот\w*",
    r"плотност\w*", r"деформаци\w*", r"уровн\w*", r"содержани\w*", r"концентраци\w*", r"отметк\w*",
    r"интервал\w*", r"количеств\w*", r"числ(?:о|а|е|у|ом)\b", r"радиус\w*", r"прочност\w*", r"модул\w*",
    r"угл(?:а|е|у|ом|ы|ов)\b", r"угол\b", r"масс(?:а|ы|е|у|ой)\b", r"вес(?:а|е|у|ом)?\b", r"коэффициент\w*",
    r"показател\w*", r"сил(?:а|ы|е|у|ой)\b", r"глубок\w*", r"pressures?\b", r"loads?\b", r"temperatures?\b", r"depths?\b",
    r"rates?\b", r"velocit\w*", r"times?\b", r"widths?\b", r"heights?\b", r"thickness\w*", r"lengths?\b",
    r"diameters?\b", r"distances?\b", r"sizes?\b", r"confining\b", r"stress\w*", r"strains?\b", r"contents?\b",
    r"levels?\b", r"durations?\b", r"frequenc\w*", r"areas?\b", r"volumes?\b", r"radius\b", r"coefficients?\b",
)
# ------------------------------------------------------------------------------------------------------ materials


@dataclass(frozen=True)
class MaterialDef:
    label: str
    group: str
    patterns: tuple[str, ...]
    abbr: tuple[str, ...] = ()
    synonyms: tuple[str, ...] = field(default_factory=tuple)


SALT, OVERBURDEN, BACKFILL, FLUID, OTHER = "соляные породы", "надсолевые породы", "закладка", "флюиды", "прочие"

MATERIALS: tuple[MaterialDef, ...] = (
    MaterialDef("покровная каменная соль", SALT, (r"покровн\w* (?:каменн\w* )?сол[ьиея]\w*",), ("ПКС",)),
    MaterialDef("подстилающая каменная соль", SALT, (r"подстилающ\w* (?:каменн\w* )?сол[ьиея]\w*",), ("ПдКС",)),
    MaterialDef("междупластовая каменная соль", SALT,
                (r"межд?у?пластов\w* (?:каменн\w* )?сол[ьиея]\w*", r"каменн\w* сол[ьиея]\w* междупласть\w*")),
    MaterialDef("каменная соль", SALT,
                (r"каменн\w* сол[ьиея]\w*", r"галит(?!ов\w* отход)\w*", r"rock[- ]salt\w*", r"halit\w*"),
                synonyms=("галит", "rock salt", "halite")),
    MaterialDef("сильвинит", SALT, (r"сильвинит\w*", r"sylvinit\w*"), synonyms=("sylvinite",)),
    MaterialDef("карналлит", SALT, (r"карнал+ит\w*", r"carnallit\w*"), synonyms=("carnallite",)),
    MaterialDef("калийные соли (руда)", SALT, (r"калийн\w* (?:сол[ьиея]\w*|руд(?:а|ы|е|у|ой)\b|пород\w*)",
                                               r"potash (?:ore|rock)s?"), synonyms=("калийная руда", "potash")),
    MaterialDef("соляные породы", SALT, (r"солян\w* пород\w*", r"соленосн\w* пород\w*", r"salt rocks?",
                                         r"\bsalt\b(?! (?:mine|mines|mining|dome|domes|marsh|water|lake|cavern|"
                                         r"caverns|deposit|deposits|basin|industry|production|content))"),
                synonyms=("соль", "salt")),
    MaterialDef("ангидрит", OTHER, (r"ангидрит\w*", r"anhydrit\w*")),
    MaterialDef("доломит", OTHER, (r"доломит\w*", r"dolomit\w*")),
    MaterialDef("глинисто-мергелистая толща", OVERBURDEN, (r"глинисто-мергелист\w* (?:толщ|пород)\w*",)),
    MaterialDef("соляно-мергельная толща", OVERBURDEN, (r"соляно-мергел\w* толщ\w*",), ("СМТ",)),
    MaterialDef("пестроцветная толща", OVERBURDEN, (r"пестроцветн\w* толщ\w*",), ("ПЦТ",)),
    MaterialDef("терригенно-карбонатная толща", OVERBURDEN, (r"терригенно-карбонатн\w* толщ\w*",), ("ТКТ",)),
    MaterialDef("переходная пачка", OVERBURDEN, (r"переходн\w* пачк\w*",)),
    MaterialDef("четвертичные отложения", OVERBURDEN, (r"четвертичн\w* отложени\w*",)),
    MaterialDef("водозащитная толща", OVERBURDEN, (r"водозащитн\w* толщ\w*",), ("ВЗТ",)),
    MaterialDef("надсолевая толща", OVERBURDEN, (r"надсол[еья]\w* (?:толщ|пород)\w*",)),
    MaterialDef("налегающая толща", OVERBURDEN, (r"(?:налегающ|вышележащ)\w* (?:толщ|пород)\w*", r"overburden")),
    MaterialDef("мергель", OVERBURDEN, (r"мерг[е]?л(?:ь|я|ю|ем|е|и|ей|ям|ями|ях)\b", r"мергелист\w*", r"marls?\b")),
    MaterialDef("глина", OVERBURDEN, (r"глин(?:а|ы|е|у|ой|ам|ами|ах)\b", r"глинист\w* (?:прослой|прослоя|прослоев|прослои|"
                                      r"пород|контакт)\w*", r"clays?\b", r"claystones?")),
    MaterialDef("аргиллит", OVERBURDEN, (r"аргил+ит\w*", r"argillit\w*", r"mudstones?")),
    MaterialDef("алевролит", OVERBURDEN, (r"алевролит\w*", r"siltstones?")),
    MaterialDef("песчаник", OVERBURDEN, (r"песчаник\w*", r"sandstones?")),
    MaterialDef("известняк", OVERBURDEN, (r"известняк\w*", r"limestones?")),
    MaterialDef("гипс", OTHER, (r"гипс(?:а|ом|е|ы|ов)?\b", r"gypsum")),
    MaterialDef("закладочный материал", BACKFILL,
                (r"закладочн\w* (?:материал|массив|смес|пульп)\w*", r"заклад(?:ка|ки|ке|ку|кой|ок)\b", r"гидрозакладк\w*",
                 r"галитов\w* отход\w*", r"глинисто-солев\w* шлам\w*", r"гидросмес\w*", r"back ?fill\w*"),
                synonyms=("закладка", "backfill")),
    MaterialDef("рассол", FLUID, (r"рассол\w*", r"brines?\b")),
    MaterialDef("уголь", OTHER, (r"\b(?:уголь|угля|углей|углю|углем|углём|угольн\w*)\b", r"\bcoal\b")),
    MaterialDef("руда (вид не указан)", OTHER, (r"\bруд(?:а|ы|е|у|ой)\b", r"\bores?\b")),
)
MATERIAL_BY_LABEL: dict[str, MaterialDef] = {m.label: m for m in MATERIALS}
MATERIAL_GROUPS = sorted({m.group for m in MATERIALS})
# «ОАО „Сильвинит“», «ПАО Уралкалий»: company names are not materials
COMPANY_BEFORE = re.compile(r"(?:(?:пао|оао|ао|зао|ооо|компани\w*|предприяти\w*|объединени\w*)\s*)?[«\"„“]\s*$", re.I)

# ------------------------------------------------------------------------------------------------------ sites
VKM_FAMILY = ("ВКМ", "СКРУ", "СКРУ-1", "СКРУ-2", "СКРУ-3", "БКПРУ", "БКПРУ-1", "БКПРУ-2", "БКПРУ-3", "БКПРУ-4",
              "Усть-Яйвинский", "Палашерский", "Талицкий", "Половодовский", "Ново-Соликамский", "Балахонцевский",
              "Дурыманский")
# (label, pattern) — a pattern with a group (\d) forms the label with that number (label must end with «-»)
SITES: tuple[tuple[str, str], ...] = (
    ("СКРУ-", r"\bскру\s*[-–—№]?\s*([123])(?!\d)"),
    ("СКРУ", r"\bскру\b(?!\s*[-–—№]?\s*\d)|соликамск\w* калийн\w*|(?:пао|оао|ао)\s*[«\"„“]?\s*сильвинит"),
    ("БКПРУ-", r"\bбк(?:п)?ру\s*[-–—№]?\s*([1-4])(?!\d)"),
    ("БКПРУ", r"\bбк(?:п)?ру\b(?!\s*[-–—№]?\s*\d)|березниковск\w* калийн\w*"),
    ("Усть-Яйвинский", r"усть[-\s]?яйв\w*"),
    ("Палашерский", r"палашер\w*"),
    ("Талицкий", r"талицк\w*"),
    ("Половодовский", r"половодов\w*"),
    ("Ново-Соликамский", r"ново[-\s]?соликамск\w*"),
    ("Балахонцевский", r"балахонцев\w*"),
    ("Дурыманский", r"дурыман\w*"),
    ("ВКМ", r"верхнекамск\w*|\bвкм(?:кс)?\b|verkhnekamsk\w*|upper kama|уралкали\w*"),
    ("ANALOGUE:Старобинское", r"старобинск\w*|солигорск\w*|беларуськали\w*|starobin\w*|soligorsk"),
    ("ANALOGUE:Гремячинское", r"гремячинск\w*|gremyachin\w*"),
    ("ANALOGUE:Непское", r"непск\w* (?:месторожд|калийн)\w*"),
    ("ANALOGUE:Тюбегатанское", r"тюбегатан\w*"),
    ("ANALOGUE:Карлюкское", r"карлюк\w*"),
    ("ANALOGUE:Гаурдакское", r"гаурдак\w*"),
    ("ANALOGUE:Калуш-Стебник", r"калуш\w*|стебник\w*|kalush|stebnik"),
    ("ANALOGUE:Соль-Илецк", r"илецк\w*"),
    ("ANALOGUE:Артёмовск (Соледар)", r"артемовск\w*|соледар\w*|бахмут\w*"),
    ("ANALOGUE:Солотвино", r"солотвин\w*"),
    ("ANALOGUE:Саскачеван", r"саскачеван\w*|saskatchewan|esterhazy|rocanville|lanigan"),
    ("ANALOGUE:Германия (соль, калий)", r"gorleben|\basse\b|morsleben|hattorf|werra|zielitz|bernburg|sondershausen|"
                                        r"горлебен\w*|цехштейн\w*|zechstein|stassfurt|staßfurt|вера[- ]фульда"),
    ("ANALOGUE:WIPP", r"\bwipp\b|carlsbad|waste isolation pilot"),
    ("ANALOGUE:Boulby", r"boulby"),
    ("ANALOGUE:Польша (соль)", r"wieliczka|величк\w*|kłodawa|klodawa|bochnia"),
    ("ANALOGUE:Донбасс", r"донбасс\w*|донецк\w* (?:угольн\w* )?бассейн\w*"),
    ("ANALOGUE:Кузбасс", r"кузбасс\w*|кузнецк\w* (?:угольн\w* )?бассейн\w*"),
    ("ANALOGUE:Карагандинский бассейн", r"карагандинск\w*"),
    ("ANALOGUE:Печорский бассейн", r"печорск\w* (?:угольн\w* )?бассейн\w*|воркут\w*"),
    ("ANALOGUE:Подмосковный бассейн", r"подмосковн\w* (?:угольн\w* )?бассейн\w*"),
    ("ANALOGUE:Кривбасс", r"кривбасс\w*|криворожск\w*"),
    ("ANALOGUE:Норильск", r"норильск\w*|талнах\w*"),
    ("ANALOGUE:Хибины", r"хибин\w*|апатитов\w* рудник\w*"),
    ("ANALOGUE:Жезказган", r"[дж]?жезказган\w*"),
)

# ------------------------------------------------------------------------------------------------------ scale cues
LAB, MASSIF, NORMATIVE, MODEL, UNKNOWN = "LAB", "MASSIF", "NORMATIVE", "MODEL", "UNKNOWN"
SCALE_HINTS = (LAB, MASSIF, NORMATIVE, MODEL, UNKNOWN)
SCALE_CUES: tuple[tuple[str, str, str], ...] = (
    # (category, cue key, pattern) — the cue key is a vocabulary word, never corpus text
    (LAB, "образец", r"образц\w*|образ(?:ец|це)\b"),
    (LAB, "лабораторн", r"лаборатор\w*"),
    (LAB, "керн", r"\bкерн\w*"),
    (LAB, "specimen", r"specimens?|\bsamples?\b|laborator\w*|lab\.? tests?|core samples?"),
    (MASSIF, "в массиве", r"\bв (?:горном |породном |соляном |нетронутом |ненарушенном )?массиве\b|"
                          r"(?:модул\w*|прочност\w*|деформируемост\w*)(?: [а-я]+){0,2} массива\b|"
                          r"массива в целом|масштаб\w* массива"),
    (MASSIF, "натурн", r"натурн\w*|в натуре\b"),
    (MASSIF, "фактическ", r"фактическ\w*"),
    (MASSIF, "шахтн", r"шахтн\w* (?:измерени|наблюдени|исследовани|испытани|услови|эксперимент)\w*"),
    (MASSIF, "маркшейдерск", r"маркшейдерск\w* (?:наблюдени|измерени|данн)\w*|инструментальн\w* наблюдени\w*"),
    (MASSIF, "in situ", r"in[- ]situ|rock mass\b|field (?:measurement|test|observation|data)s?|underground measurement"),
    (NORMATIVE, "нормативн", r"норматив\w*"),
    (NORMATIVE, "указания", r"указани(?:я|й|ям|ях|ями)\b"),
    (NORMATIVE, "правила", r"правил\w* (?:охраны|безопасности)|\bснип\b|регламент\w*"),
    (NORMATIVE, "рекомендуем", r"рекомендуем\w*|рекомендует\w*|рекомендован\w*"),
    (NORMATIVE, "допустим", r"допустим\w*|предельно допустим\w*"),
    (NORMATIVE, "проектн", r"проектн\w*|по проекту"),
    (NORMATIVE, "standard", r"code of practice|guidelines?|regulations?"),
    (MODEL, "расчётн", r"расчетн\w*|по расчету|расчеты|расчетах"),
    (MODEL, "модел", r"моделировани\w*|модел(?:и|ь|ью|ей|ях|ям)\b|численн\w*"),
    (MODEL, "принят", r"принят\w*|принима\w*|задава\w*|задан(?:о|ы|а|ное|ные)\b"),
    (MODEL, "прогноз", r"прогноз\w*"),
    (MODEL, "калибровк", r"калибр\w*|обратн\w* анализ\w*"),
    (MODEL, "model", r"numerical|simulat\w*|\bmodel\w*|back[- ]analys\w*|calibrat\w*|assumed|adopted"),
)

# ------------------------------------------------------------------------------------------------------ units
# (pattern, dim, factor to SI, canonical spelling); tried longest first at a position; case-insensitive
_S2 = r"\s?(?:2|²|\^2)"
_S3 = r"\s?(?:3|³|\^3)"
_M1 = r"\s?(?:-1|−1|⁻¹|\^-1|\^\(-1\))"
_TIME_UNITS: tuple[tuple[str, float, str], ...] = (
    (r"(?:сут(?:ок|ки|кам|\.)?|сутк\w*|дн(?:ей|я|\.)?|days?|d(?![a-z]))", _DAY, "сут"),
    (r"(?:год(?:а|у)?|лет|гг?\.?|yrs?|years?|a(?![a-z]))", _YEAR, "год"),
    (r"(?:мес(?:яц(?:а|ев)?|\.)?|months?|mo\.?)", _MONTH, "мес"),
    (r"(?:ч(?:ас(?:а|ов)?|\.)?|h(?:rs?|ours?)?(?![a-z]))", 3600.0, "ч"),
    (r"(?:мин(?:ут(?:ы)?|\.)?|min)", 60.0, "мин"),
    (r"(?:с(?:ек\.?)?|s(?:ec)?)(?![a-zа-я])", 1.0, "с"),
)
_LENGTH_UNITS: tuple[tuple[str, float, str], ...] = (
    (r"(?:мм|mm)", 1e-3, "мм"), (r"(?:см|cm)", 1e-2, "см"), (r"(?:км|km)", 1e3, "км"), (r"(?:м|m)", 1.0, "м"),
)


def _build_units() -> list[tuple[str, str, float, str]]:
    u: list[tuple[str, str, float, str]] = []
    # viscosity (before pressure: «МПа·с»)
    for pre, f in (("г", 1e9), ("м", 1e6), ("к", 1e3), ("", 1.0)):
        lat = {"г": "g", "м": "m", "к": "k", "": ""}[pre]
        for tp, tf, tn in (_TIME_UNITS[0], _TIME_UNITS[1], _TIME_UNITS[3], _TIME_UNITS[5]):
            cls = f"[{pre}{lat}]" if pre else ""
            u.append((rf"{cls}[пp][аa]\s?[·⋅∙*×.x]?\s?{tp}", VISCOSITY, f * tf, f"{pre}Па·{tn}"))
    u += [(r"(?:пуаз\w*|poise)", VISCOSITY, 0.1, "П")]
    # strain rate (before length and pressure)
    for tp, tf, tn in _TIME_UNITS:
        u.append((rf"1\s?/\s?{tp}", STRAIN_RATE, 1.0 / tf, f"1/{tn}"))
        u.append((rf"%\s?/\s?{tp}", STRAIN_RATE, 0.01 / tf, f"%/{tn}"))
        u.append((rf"{tp}{_M1}", STRAIN_RATE, 1.0 / tf, f"{tn}⁻¹"))
    # velocity: length per time («мм/год», «мм в год», «мм за сутки»)
    for lp, lf, ln in _LENGTH_UNITS:
        for tp, tf, tn in _TIME_UNITS:
            u.append((rf"{lp}\s?(?:/|в|за|per)\s?{tp}", VELOCITY, lf / tf, f"{ln}/{tn}"))
    # molar energy
    u += [(r"(?:кдж|kj)\s?/\s?(?:моль|mol)", MOLAR_ENERGY, 1e3, "кДж/моль"),
          (r"(?:ккал|kcal)\s?/\s?(?:моль|mol)", MOLAR_ENERGY, 4184.0, "ккал/моль"),
          (r"(?:дж|j)\s?/\s?(?:моль|mol)", MOLAR_ENERGY, 1.0, "Дж/моль")]
    # weight density
    u += [(rf"(?:мн|mn|mh)\s?/\s?(?:м|m){_S3}", WEIGHT, 1e6, "МН/м³"),
          (rf"(?:кн|kn|kh)\s?/\s?(?:м|m){_S3}", WEIGHT, 1e3, "кН/м³"),
          (rf"(?:н|n)\s?/\s?(?:м|m){_S3}", WEIGHT, 1.0, "Н/м³"),
          (rf"(?:тс|tf)\s?/\s?(?:м|m){_S3}", WEIGHT, 9806.65, "тс/м³"),
          (rf"(?:гс|gf)\s?/\s?(?:см|cm){_S3}", WEIGHT, 9806.65, "гс/см³")]
    # density
    u += [(rf"(?:кг|kg)\s?/\s?(?:м|m){_S3}", DENSITY, 1.0, "кг/м³"),
          (rf"(?:гр?|g)\s?/\s?(?:см|cm){_S3}", DENSITY, 1000.0, "г/см³"),
          (rf"(?:т|t)\s?/\s?(?:м|m){_S3}", DENSITY, 1000.0, "т/м³")]
    # pressure
    u += [(rf"(?:гн|gn)\s?/\s?(?:м|m){_S2}", PRESSURE, 1e9, "ГН/м²"),
          (rf"(?:мн|mn|mh)\s?/\s?(?:м|m){_S2}", PRESSURE, 1e6, "МН/м²"),
          (rf"(?:н|n)\s?/\s?(?:мм|mm){_S2}", PRESSURE, 1e6, "Н/мм²"),
          (rf"(?:кн|kn|kh)\s?/\s?(?:м|m){_S2}", PRESSURE, 1e3, "кН/м²"),
          (rf"(?:н|n)\s?/\s?(?:м|m){_S2}", PRESSURE, 1.0, "Н/м²"),
          (rf"(?:кгс|кг|kgf|kg)\s?/\s?(?:см|cm){_S2}", PRESSURE, 98066.5, "кгс/см²"),
          (rf"(?:тс|т|tf|t)\s?/\s?(?:м|m){_S2}", PRESSURE, 9806.65, "тс/м²"),
          (r"[гg][пp][аa]", PRESSURE, 1e9, "ГПа"), (r"(?:[мm][пp][аa]|m[i1]pa)", PRESSURE, 1e6, "МПа"),
          (r"[кk][пp][аa]", PRESSURE, 1e3, "кПа"), (r"[пp][аa]", PRESSURE, 1.0, "Па"),
          (r"(?:бар|bar)", PRESSURE, 1e5, "бар"), (r"(?:атм|atm)", PRESSURE, 101325.0, "атм"),
          (r"ksi", PRESSURE, 6.894757e6, "ksi"), (r"psi", PRESSURE, 6894.757, "psi")]
    # permeability, area
    u += [(r"(?:мд|md|мдарси|millidarcys?)", PERMEABILITY, 9.869233e-16, "мД"),
          (r"(?:дарси|darcys?)", PERMEABILITY, 9.869233e-13, "Д"),
          (rf"(?:мкм|µm|μm){_S2}", PERMEABILITY, 1e-12, "мкм²"),
          (rf"(?:м|m){_S2}", AREA, 1.0, "м²")]
    # curvature
    u += [(rf"1\s?/\s?(?:км|km)|(?:км|km){_M1}", CURVATURE, 1e-3, "1/км"),
          (rf"1\s?/\s?(?:м|m)(?![a-zа-я23²³])|(?:м|m){_M1}", CURVATURE, 1.0, "1/м")]
    # strain-like ratios, ratios
    u += [(r"(?:мм|mm)\s?/\s?(?:м|m)(?![a-zа-я23²³])", STRAIN, 1e-3, "мм/м"), (r"‰", STRAIN, 1e-3, "‰"),
          (r"%|per ?cent|процент(?:а|ов)?", RATIO, 0.01, "%"),
          (r"(?:доли|долей|долях)\s+(?:ед(?:иниц[ыа]?|\.)?)", RATIO, 1.0, "доли ед."),
          (r"д\.\s?ед\.?", RATIO, 1.0, "д. ед."), (r"отн\.\s?ед\.?", RATIO, 1.0, "отн. ед.")]
    # angle (minutes/seconds are parsed with the number)
    u += [(r"(?:°|º|˚|град(?:ус(?:а|ов)?)?\.?|deg(?:rees?)?)", ANGLE, 1.0, "°")]
    # length and time (last: the shortest spellings)
    for lp, lf, ln in (((r"(?:мкм|µm|μm)"), 1e-6, "мкм"), (r"(?:дм|dm)", 0.1, "дм"), *_LENGTH_UNITS):
        u.append((lp + r"(?![a-zа-я0-9²³/])", LENGTH, lf, ln))
    for tp, tf, tn in _TIME_UNITS:
        u.append((tp, TIME, tf, tn))
    return u


UNITS: tuple[tuple[str, str, float, str], ...] = tuple(_build_units())
REJECT_AFTER = re.compile(r"\s*(?:раз(?:а)?\b|times\b|fold\b|-?кратн\w*|x\b(?!\s*\d))", re.I)
