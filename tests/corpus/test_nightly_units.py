"""Static checks of the nightly jobs (agent OPS): systemd user units and timers (04:00 / 02:00 / 02:30 Moscow time,
resource caps, paths only through the env files), env templates in systemd EnvironmentFile syntax, the capped
read-only ``vkm-nightly`` compose service, shell scripts (dry-run option, strict mode, LF, ``bash -n``) and the
forced command of the backup key. Runs everywhere; ``bash -n`` only where bash exists."""
from __future__ import annotations

import configparser
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CORE = ROOT / "infra" / "core" / "nightly"
EDGE = ROOT / "infra" / "edge" / "backup"
UNITS = {CORE / "systemd" / n: CORE for n in ("vkm-nightly.service", "vkm-nightly.timer", "vkm-backup-prepare.service",
                                             "vkm-backup-prepare.timer")}
UNITS.update({EDGE / "systemd" / n: EDGE for n in ("vkm-backup.service", "vkm-backup.timer")})
SCRIPTS = [CORE / n for n in ("nightly_checks.sh", "backup_prepare.sh", "install.sh", "vkm_backup_gate.sh")] + \
    [EDGE / n for n in ("vkm_backup.sh", "vkm_restore.sh", "install.sh")]


def unit(path: Path) -> configparser.ConfigParser:
    cp = configparser.ConfigParser(interpolation=None, strict=False)
    cp.optionxform = str
    cp.read_string(path.read_text(encoding="utf-8"))
    return cp


def minutes(calendar: str) -> int:
    m = re.fullmatch(r"\*-\*-\* (\d\d):(\d\d):00 Europe/Moscow", calendar)
    assert m, calendar
    return int(m.group(1)) * 60 + int(m.group(2))


def test_timers_run_in_moscow_time_in_the_right_order():
    cal = {p.name: unit(p)["Timer"]["OnCalendar"] for p in UNITS if p.suffix == ".timer"}
    assert cal["vkm-nightly.timer"] == "*-*-* 04:00:00 Europe/Moscow"
    assert minutes(cal["vkm-backup-prepare.timer"]) < minutes(cal["vkm-backup.timer"]) < minutes(cal["vkm-nightly.timer"])
    for p in UNITS:
        if p.suffix == ".timer":
            t = unit(p)
            assert t["Timer"]["Persistent"] == "true" and t["Install"]["WantedBy"] == "timers.target"
            assert (p.parent / t["Timer"]["Unit"]).is_file()


def test_services_are_capped_and_take_paths_from_env_files():
    for p, home in UNITS.items():
        if p.suffix != ".service":
            continue
        s = unit(p)["Service"]
        assert s["Type"] == "oneshot"
        env_file = s["EnvironmentFile"]
        assert env_file.startswith("%h/.config/vkm/") and env_file.endswith(".env")
        m = re.fullmatch(r"/bin/bash \$\{(VKM_[A-Z_]+)\}/([a-z_]+\.sh)", s["ExecStart"])
        assert m, s["ExecStart"]
        var, script = m.groups()
        assert (home / script).is_file()
        example = (home / ("nightly.env.example" if home == CORE else "backup.env.example")).read_text(encoding="utf-8")
        assert re.search(rf"^{var}=", example, re.M)
        quota = int(s["CPUQuota"].rstrip("%"))
        assert 100 <= quota <= 1200 and s["MemoryMax"][-1] in "GM" and int(s["Nice"]) >= 10
        assert s["IOSchedulingClass"] in ("best-effort", "idle")
    assert unit(CORE / "systemd" / "vkm-nightly.service")["Service"]["CPUQuota"] == "1200%"


def test_env_templates_have_no_inline_comments():
    for p in (CORE / "nightly.env.example", EDGE / "backup.env.example"):
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if line.strip() and not line.startswith("#"):
                assert re.fullmatch(r"[A-Z][A-Z0-9_]*=[^#\s]*", line), f"{p.name}:{n}: {line}"


def test_nightly_compose_service_is_read_only_and_capped():
    yaml = pytest.importorskip("yaml")
    compose = yaml.safe_load((ROOT / "infra" / "core" / "compose.yml").read_text(encoding="utf-8"))
    svc = compose["services"]["vkm-nightly"]
    job = compose["services"]["vkm-job"]
    assert svc["profiles"] == ["nightly"] and svc["image"] == job["image"] and svc["user"] == job["user"]
    assert all(v.get("read_only") is True for v in svc["volumes"]) and svc["read_only"] is True
    assert float(svc["cpus"]) <= 12 and int(svc["cpu_shares"]) < 1024 and svc["mem_limit"]
    assert svc["secrets"] == ["neo4j_password"] and svc["networks"] == ["vkm_internal"]
    assert "ports" not in svc


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_scripts_are_strict_lf_and_have_a_dry_run(script):
    raw = script.read_bytes()
    assert b"\r\n" not in raw and raw.startswith(b"#!/")
    text = raw.decode("utf-8")
    if script.name == "vkm_backup_gate.sh":
        assert "set -eu" in text
    else:
        assert "set -Eeuo pipefail" in text and "--dry-run)" in text


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_scripts_parse(script):
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is not available")
    r = subprocess.run([bash, "-n", str(script)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr


def test_backup_gate_is_rsync_only_read_only_pull_and_write_only_status_push():
    text = (CORE / "vkm_backup_gate.sh").read_text(encoding="utf-8")
    assert '"rsync --server --sender "*)' in text and 'exec "$RRSYNC" -ro "$DATA"' in text
    assert 'exec "$RRSYNC" -wo -no-del "$DATA/receipts/backup/edge"' in text
    assert "only rsync is allowed" in text and text.count("exec ") == 2


def test_python_parts_use_only_the_standard_library_on_the_hosts():
    stdlib_only = [CORE / "nightly_summary.py", CORE / "dossier_store.py", CORE / "dossiers.py",
                   EDGE / "vkm_manifest.py"]
    allowed = {"argparse", "concurrent", "datetime", "fnmatch", "gzip", "hashlib", "json", "os", "re", "shutil",
               "stat", "sys", "time", "pathlib", "typing", "urllib", "__future__", "io"}
    for p in stdlib_only:
        mods = set(re.findall(r"^(?:import|from) ([a-z_]+)", p.read_text(encoding="utf-8"), re.M))
        assert mods <= allowed, (p.name, mods - allowed)
