"""Label an existing source section without extending its geological geometry."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.patches import Patch, Polygon


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def build(features_path, config_path, resources_root, out):
    features = json.loads(features_path.read_text(encoding="utf-8"))
    config = json.loads(config_path.read_text(encoding="utf-8"))
    image_path = resources_root / config["image"]
    inputs = {"features": features_path, "config": config_path, "image": image_path,
              "code": Path(__file__)}
    pins = {name: sha(path) for name, path in inputs.items()}
    if pins["image"] != config["source"]["image_sha256"]:
        raise ValueError("source image hash does not match the section configuration")
    out.mkdir(parents=True, exist_ok=False)
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 11,
                         "svg.hashsalt": "vkm-skru1-source-section"})
    units = [f for f in features["features"] if f["layer"] == "section_units"]
    controls = [f for f in features["features"] if f["layer"] == "section_controls"]
    names = {"Q": "Q — четвертичные отложения", "PCT": "ПЦТ — пестроцветная толща",
             "TKT": "ТКТ — терригенно-карбонатная толща", "SMT": "СМТ — соляно-мергельная толща",
             "PKS": "ПКС — покровная каменная соль", "KP": "КП — обозначение рисунка",
             "SP": "СП — обозначение рисунка"}
    legend = [Patch(facecolor=np.array(json.loads(f["properties"]["color_rgb"]))/255,
                    edgecolor="#555", label=names.get(f["properties"]["unit_id"],
                                                      f["properties"]["unit_as_printed"])) for f in units]

    def draw(ax, exaggeration=1):
        ax.set_facecolor("#e5e7e9")
        for item in units:
            p = item["properties"]
            ax.add_patch(Polygon(np.asarray(item["coordinates"]), closed=True,
                         facecolor=np.array(json.loads(p["color_rgb"]))/255,
                         edgecolor="#656565", linewidth=.7))
        for item in controls:
            p = item["properties"]; a = np.asarray(item["coordinates"])
            key = p["unit_id"]
            color = "#1250ad" if key == "SALT_MIRROR_TRACE" else "#cb3025" if key == "STRUCTURE_98" else "#333"
            ax.plot(a[:, 0], a[:, 1], color=color, linewidth=1.7,
                    linestyle="-" if key == "SALT_MIRROR_TRACE" else "--")
            if key in {"BH128", "BH_128", "BOREHOLE_128"} or p["unit_as_printed"] == "скв.128":
                ax.annotate("скв. 128\nустье +174,40 м", (a[0, 0], 174.4), xytext=(0, 20),
                            textcoords="offset points", ha="center", fontsize=10)
            elif key == "VIRTUAL_129_220":
                ax.annotate("129–220 · виртуальная опора\n+160,77 м по рисунку", a[0], xytext=(10, 20),
                            textcoords="offset points", fontsize=10)
            elif key == "STRUCTURE_98":
                ax.annotate("98 · линия рисунка", a.mean(axis=0), xytext=(-8, 0),
                            textcoords="offset points", ha="right", fontsize=10, color=color)
            elif key == "SALT_MIRROR_TRACE":
                ax.annotate("Соляное зеркало", a[-1], xytext=(-8, 10),
                            textcoords="offset points", ha="right", color=color, fontsize=10)
        ax.set(xlim=(0, 1060), ylim=(-220, 220),
               xlabel="Расстояние по исходному разрезу с запада на восток, м",
               ylabel="Отметка по оси источника, м\nГеодезическая система высот UNKNOWN")
        ax.set_xticks(np.arange(0, 1100, 100)); ax.set_yticks(np.arange(-200, 201, 50))
        ax.set_aspect(exaggeration)
        ax.grid(alpha=.2, linewidth=.6)
        ax.text(.005, .985, "З", transform=ax.transAxes, va="top", fontweight="bold")
        ax.text(.995, .985, "В", transform=ax.transAxes, va="top", ha="right", fontweight="bold")

    foot = ("Источник: VKM-SRC-011, Кудряшов, 2013, PDF с. 131, рис. 4.7а. Графическая оцифровка цвета; DERIVATION.\n"
            "Серые полосы — невыделенные пиксельные области, не установленные геологические пустоты. КП/СП сохранены как напечатано.\n"
            "129–220 не является отдельной скважиной. Соляное зеркало сохранено отдельно от границы СМТ/ПКС. Полевая проверка NOT_RUN.")
    for exaggeration in (1, 2):
        fig, ax = plt.subplots(figsize=(17, 9 if exaggeration == 1 else 12))
        draw(ax, exaggeration)
        ax.set_title(f"СКРУ-1 · исходный геологический разрез · вертикальный масштаб ×{exaggeration}", pad=38, fontsize=16)
        fig.legend(handles=legend, loc="lower center", bbox_to_anchor=(.5, .075), ncols=3, fontsize=10)
        fig.text(.06, .015, foot, fontsize=9, va="bottom")
        fig.subplots_adjust(left=.09, right=.97, top=.85, bottom=.22)
        for extension in ("png", "svg"):
            fig.savefig(out/f"section_x{exaggeration}.{extension}", dpi=200,
                        metadata={"Date": None} if extension == "svg" else None)
        plt.close(fig)

    fig, axes = plt.subplots(1, 2, figsize=(18, 9))
    image = plt.imread(image_path)
    axes[0].imshow(image); axes[0].axis("off")
    axes[0].set_title("Исходное изображение · PDF с. 131, рис. 4.7а")
    draw(axes[1], 2)
    axes[1].set_title("Сохранённые границы и подписи · вертикаль ×2", pad=38)
    fig.legend(handles=legend, loc="lower center", bbox_to_anchor=(.5, .085), ncols=3, fontsize=10)
    fig.text(.04, .015, foot, fontsize=9, va="bottom")
    fig.subplots_adjust(left=.025, right=.98, top=.86, bottom=.23, wspace=.2)
    fig.savefig(out/"source_comparison.png", dpi=200); plt.close(fig)
    if pins != {name: sha(path) for name, path in inputs.items()}:
        raise RuntimeError("input changed during rendering")
    receipt = {"schema": "vkm.labelled_source_section/1", "inputs_sha256": pins,
               "source_document_sha256": config["source"]["document_sha256"],
               "source_pdf_page": 131, "figure": "4.7a", "unit_count": len(units),
               "geometry_changed": False, "unit_correlation": "NOT_PROMOTED_FROM_CANONICAL_GUESS",
               "map_trace": "Separate tracefit MODEL_CHOICE; original frame unchanged",
               "outputs_sha256": {p.name: sha(p) for p in sorted(out.iterdir())},
               "scientific_admission": "NOT_CHECKED", "input_integrity": "PASS"}
    (out/"receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    return receipt


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--features", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--resources-root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(build(args.features, args.config, args.resources_root, args.out), ensure_ascii=False))
