"""CAD capability detector on a fake Windows environment (CAD-01…CAD-04): no Autodesk software, registry or COM
needed; licence values are never requested; no machine path leaves the report."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from vkm_cad.detect import AUTOCAD_KEY, HKCR, HKCU, HKLM, DetectEnv, SafeRegistry, detect, progids

INSTALL = Path("Z:/Fake/AutoCAD 2026")


class FakeRegistry:
    """Dict-backed registry (case-insensitive paths); records every value name asked for."""

    def __init__(self, keys: dict[tuple[str, str], dict[str, str]]):
        self.keys = keys
        self.asked: list[str] = []

    def subkeys(self, hive: str, path: str) -> list[str]:
        prefix = path.lower() + "\\"
        out: list[str] = []
        for h, p in self.keys:
            if h == hive and p.lower().startswith(prefix):
                name = p[len(prefix):].split("\\")[0]
                if name not in out:
                    out.append(name)
        return sorted(out)

    def value(self, hive: str, path: str, name: str) -> str | None:
        self.asked.append(name)
        for (h, p), values in self.keys.items():
            if h == hive and p.lower() == path.lower():
                return values.get(name)
        return None


def _registry(products: dict[str, dict[str, str]], *, progid_ok: bool = True, cur: tuple[str, str] | None = None,
              civil: bool = False) -> FakeRegistry:
    keys: dict[tuple[str, str], dict[str, str]] = {}
    for path, values in products.items():
        keys[(HKLM, rf"{AUTOCAD_KEY}\{path}")] = values
    keys[(HKCR, r"AutoCAD.Application.25.1\CLSID")] = {"": "{ACAD}"}
    keys[(HKCR, r"CLSID\{ACAD}\LocalServer32")] = {"": f'"{INSTALL / "acad.exe"}" /Automation' if progid_ok
                                                   else '"Z:/missing/acad.exe" /Automation'}
    if civil:
        keys[(HKCR, r"AeccXUiLand.AeccApplication.13.8\CLSID")] = {"": "{C3D}"}
        keys[(HKCR, r"CLSID\{C3D}\InprocServer32")] = {"": str(INSTALL / "C3D" / "AeccXUiLand.dll")}
    if cur:
        keys[(HKCU, AUTOCAD_KEY)] = {"CurVer": cur[0]}
        keys[(HKCU, rf"{AUTOCAD_KEY}\{cur[0]}")] = {"CurVer": cur[1]}
    return FakeRegistry(keys)


ACAD = {"ProductName": "AutoCAD 2026", "Release": "25.1.60.0", "AcadLocation": str(INSTALL), "LangAbbrev": "rus",
        "LocaleID": "419", "UPIRELEASE": "2026", "SerialNumber": "must-never-be-read"}
CIVIL = {"ProductName": "Civil 3D 2026", "Release": "13.8.280.0", "AcadLocation": str(INSTALL), "UPIRELEASE": "2026",
         "AeccXVersion": "138", "ProductNameShort": "C3D 2026", "NetSupport": "x"}
FILES = {INSTALL, INSTALL / "acad.exe", INSTALL / "accoreconsole.exe", INSTALL / "AcCoreMgd.dll",
         INSTALL / "AcDbMgd.dll",
         INSTALL / "AcMgd.dll", INSTALL / "C3D" / "AeccDbMgd.dll", INSTALL / "C3D" / "AeccXUiLand.dll"}


def _env(reg: FakeRegistry, *, files=FILES, running: str = "", sdks: str = "6.0.201 [x]\n",
         ezdxf: str | None = "1.4.4", pywin32: str | None = "312") -> DetectEnv:
    outputs = {"dotnet": sdks, "tasklist": running}
    return DetectEnv(registry=reg, exists=lambda p: Path(p) in files, file_version=lambda p: "25.1.164.0",
                     read_text=lambda p: '{"runtimeOptions": {"tfm": "net8.0"}}' if p.name.endswith(".json") else None,
                     run=lambda cmd: outputs.get(cmd[0]), program_files="Q:/PFiles", platform="win32",
                     module_version=lambda name: {"ezdxf": ezdxf, "pywin32": pywin32}.get(name))


def test_not_installed():
    report = detect(_env(_registry({})))
    assert report["overall"] == "NOT_INSTALLED" and report["products"] == []
    assert report["capabilities"]["read_open_documents"] == "UNAVAILABLE:AUTOCAD_NOT_INSTALLED"
    assert report["capabilities"]["scratch_ezdxf"] == "AVAILABLE"      # ezdxf works without Autodesk


def test_autocad_and_civil_with_current_version_and_redaction():
    reg = _registry({r"R25.1\ACAD-9101:419": ACAD, r"R25.1\ACAD-9100:419": CIVIL},
                    cur=("R25.1", "ACAD-9101:419"), civil=True)
    report = detect(_env(reg, running='"acad.exe","1","Console","1","1 K"\n'))
    kinds = {p["product_key"]: p for p in report["products"]}
    assert kinds["ACAD-9101:419"]["product"] == "AUTOCAD" and kinds["ACAD-9101:419"]["current"] is True
    assert kinds["ACAD-9100:419"]["product"] == "CIVIL3D" and kinds["ACAD-9100:419"]["current"] is False
    assert kinds["ACAD-9101:419"]["locale"] == "ru-RU" and kinds["ACAD-9101:419"]["year"] == 2026
    assert kinds["ACAD-9101:419"]["dotnet_api"]["target_framework"] == "net8.0"
    assert kinds["ACAD-9100:419"]["com"]["progid"] == "AeccXUiLand.AeccApplication.13.8"
    assert report["overall"] == "AVAILABLE" and report["running"]["acad"] == 1
    assert report["capabilities"]["read_open_documents"] == "AVAILABLE"
    assert report["capabilities"]["civil3d_api"] == "AVAILABLE_WHEN_CIVIL3D_RUNNING"
    assert "INSTALL_DIR_NON_DEFAULT" in report["warnings"]
    text = json.dumps(report)
    assert "Fake" not in text and "must-never-be-read" not in text                     # CAD-03 redaction
    assert not any("serial" in n.lower() or "netsupport" in n.lower() for n in reg.asked)   # CAD-02


def test_two_releases_and_progid_order():
    older = {**ACAD, "Release": "25.0.1.0"}
    reg = _registry({r"R25.0\ACAD-9001:409": older, r"R25.1\ACAD-9101:419": ACAD}, cur=("R25.1", "ACAD-9101:419"))
    report = detect(_env(reg))
    assert [p["release_key"] for p in report["products"]] == ["R25.0", "R25.1"]
    assert progids(report)[0] == "AutoCAD.Application.25.1" and progids(report)[-1] == "AutoCAD.Application"
    assert report["capabilities"]["read_open_documents"] == "AVAILABLE_WHEN_USER_STARTS_AUTOCAD"


def test_broken_registration_and_missing_pieces():
    reg = _registry({r"R25.1\ACAD-9101:419": ACAD}, progid_ok=False)
    report = detect(_env(reg))
    assert report["overall"] == "BROKEN_REGISTRATION"
    assert report["products"][0]["com"] == {"progid": "AutoCAD.Application.25.1", "registered": True,
                                            "server_kind": "LOCAL_SERVER", "server_exists": False,
                                            "create_starts_process": True}
    partial = detect(_env(_registry({r"R25.1\ACAD-9101:419": ACAD}), ezdxf=None))
    assert partial["overall"] == "PARTIAL" and partial["capabilities"]["scratch_ezdxf"] == "UNAVAILABLE:EZDXF_MISSING"
    no_pywin = detect(_env(_registry({r"R25.1\ACAD-9101:419": ACAD}), pywin32=None))
    assert no_pywin["capabilities"]["read_open_documents"] == "UNAVAILABLE:PYWIN32_MISSING"


def test_dotnet_sdks_and_stub_keys():
    reg = _registry({r"R25.1\ACAD-9101:419": ACAD, r"R25.1\ACAD-9104:419": {}})
    report = detect(_env(reg, sdks="6.0.201 [a]\n8.0.100 [b]\n"))
    assert report["toolchain"]["dotnet_sdks"] == ["6.0.201", "8.0.100"] and report["toolchain"]["net8_sdk"] is True
    assert report["stub_product_keys"] == ["R25.1/ACAD-9104:419"] and len(report["products"]) == 1
    assert report["capabilities"]["dotnet_plugin"] == "NOT_IN_V0"


@pytest.mark.parametrize("name", ["SerialNumber", "NetSupport", "StandaloneNetworkType", "ADLMInfoPath",
                                  "LicenseType", "Anything"])
def test_safe_registry_refuses_licence_values(name):
    with pytest.raises(PermissionError):
        SafeRegistry(_registry({})).value(HKLM, AUTOCAD_KEY, name)
