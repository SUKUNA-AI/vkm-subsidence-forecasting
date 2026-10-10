"""Evidence anchor rebase onto a new snapshot: synthetic in-memory canons, no private corpus reads."""
from __future__ import annotations

import hashlib
import json
import random
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pytest

from vkm_corpus.api.canon import CanonStore
from vkm_corpus.contracts.access import AccessContext, ResourcePolicy
from vkm_evidence import rebase as rb
from vkm_evidence.contracts import Claim, EvidenceBatch, ObjectRef, ReviewDecision
from vkm_evidence.journal import EvidenceJournal, JournalConflict, ZERO
from vkm_evidence.objects import canonical_resolver
from vkm_world.core.provenance import EpistemicStatus, EvidenceType, Provenance, Scale, Scope, SourceRef
from vkm_world.governance.leakage import scan

NOW = datetime(2026, 10, 6, 12, tzinfo=timezone.utc)
SID, SRC_SHA = "VKM-SRC-001", "a" * 64
OLD, NEW = "snap-20260929T175107Z-0000000a", "snap-20261006T000000Z-0000000b"
OLD_COMMIT, NEW_COMMIT = "CMT-000000000000000a", "CMT-000000000000000b"
POLICY = ResourcePolicy(access_class="PRIVATE_CLOUD_ALLOWED", experimental_role="INPUT", policy_version="p1",
                        authority="owner")
CTX = AccessContext(principal="rebaser", execution="LOCAL", granted_classes={"PRIVATE_CLOUD_ALLOWED"})

# Synthetic sentences (not corpus text). Old layer = GLM-like OCR, new primary = PaddleOCR-like.
OLD_PAGES = {
    1: "Предел прочности соли на сжатие 25,4 МПа при скорости нагружения 0,1 мм/мин.",
    2: "Модуль деформа-\nции каменной соли составляет 12,5 ГПа по данным испытаний.",
    3: "Коэффициент Пуассона принят равным 0,32 для сильвинита пласта КрII.",
    4: "Длительная прочность сильвинита оценена величиной 8,7 МПа на базе 100 суток.",
    5: "Ползучесть соли описывается степенным законом.",
    6: "Глубина залегания пласта АБ составляет 315 м.",
    7: "Влажность образцов 2,5 % по массе.",
    8: "Страница, отсутствующая в новом снимке.",
}
NEW_PAGES = {
    1: OLD_PAGES[1],
    2: "Модуль  деформации каменной\nсоли составляет 12,5 ГПа по данным испытаний образцов.",
    3: "Таблица 3. Характеристики пород",
    4: "Длительная прочнесть сильвинита оценена величиной 8,7 МПа на базе 100 суток.",
    5: "Ползучесть соли описывается степенным законом Нортона.",
    6: "Глубина залегания пласта АБ составляет 318 м.",
    7: "Влажность образцов 2.5 % по массе.",
}
QUOTES = {
    "R-UNCH": (1, "Прочность соли около 25 МПа (пересказ)."),
    "R-QP": (2, "Модуль деформации каменной соли составляет 12,5 ГПа"),
    "R-SEC": (3, "Коэффициент Пуассона принят равным 0,32"),
    "R-FZ": (4, "Длительная прочность сильвинита оценена величиной 8,7 МПа"),
    "R-NF": (5, "Авторы считают реологию соли нелинейной и зависящей от температуры"),
    "R-DIG": (6, "Глубина залегания пласта АБ составляет 315 м"),
    "R-SEP": (7, "Влажность образцов 2,5 % по массе"),
    "R-ABS": (8, "Страница, отсутствующая в новом снимке"),
}
GLM_BLOCK = SID + ":p0003:b0000000000a3"
PADDLE_BLOCK = SID + ":p0003:b0000000000b3"


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def pid(n: int) -> str:
    return f"{SID}:p{n:04d}"


def make_canon(snapshot: str, commit: str, pages: dict[int, str], blocks: list[tuple], path: Path | None = None):
    con = duckdb.connect(str(path) if path else ":memory:")
    con.execute("CREATE SCHEMA meta")
    con.execute("CREATE TABLE meta.snapshot AS SELECT ? snapshot_id, ? manifest_sha256, "
                "TIMESTAMPTZ '2026-10-01 00:00:00+00' built_at, 'synthetic' duckdb_version", [snapshot, sha(snapshot)])
    con.execute("CREATE TABLE meta.commits(commit_key VARCHAR, commit_id VARCHAR)")
    con.execute("INSERT INTO meta.commits VALUES (?, ?)", [SID, commit])
    con.execute("CREATE TABLE pages(object_id VARCHAR, object_kind VARCHAR, page_id VARCHAR, source_id VARCHAR, "
                "source_sha256 VARCHAR, content_sha256 VARCHAR, extraction_generation SMALLINT, "
                "extraction_signature VARCHAR, normalized_text VARCHAR)")
    for n, text in pages.items():
        con.execute("INSERT INTO pages VALUES (?, 'PAGE', ?, ?, ?, ?, 1, NULL, ?)",
                    [pid(n), pid(n), SID, SRC_SHA, sha(snapshot[:5] + text), text])
    con.execute('CREATE TABLE blocks(object_id VARCHAR, object_kind VARCHAR, page_id VARCHAR, source_id VARCHAR, '
                'source_sha256 VARCHAR, content_sha256 VARCHAR, extraction_generation SMALLINT, '
                'extraction_signature VARCHAR, raw_locator VARCHAR, docx_paragraph_path VARCHAR, text_layer VARCHAR, '
                'origin VARCHAR, is_primary_layer BOOLEAN, reading_order INTEGER, native_order INTEGER, "text" VARCHAR)')
    for oid, layer, primary, text in blocks:
        con.execute("INSERT INTO blocks VALUES (?, 'BLOCK', ?, ?, ?, ?, 1, NULL, NULL, NULL, ?, 'OCR', ?, 1, 1, ?)",
                    [oid, oid.rsplit(":", 1)[0], SID, SRC_SHA, sha(f"{layer}{primary}{text}"), layer, primary, text])
    if path:
        con.close()
        return path
    return CanonStore(connection=con)


def source_policy(sid):
    return POLICY


def page_ref(canon: CanonStore, n: int, span: tuple[int, int] | None = None) -> ObjectRef:
    row = canon.row("PAGE", pid(n))
    extra = {}
    if span:
        extra = {"char_start": span[0], "char_end": span[1],
                 "fragment_sha256": sha(row["normalized_text"][span[0]:span[1]])}
    return ObjectRef(source_id=SID, source_sha256=SRC_SHA, snapshot_id=canon.snapshot_id(), object_id=pid(n),
                     object_version=f"{row['content_sha256']}@{canon.commit_of(SID)}",
                     content_sha256=row["content_sha256"], locator=pid(n), extraction_generation="1", **extra)


def claim(rid: str, ref: ObjectRef, quote: str, depends_on=()) -> Claim:
    return Claim(record_id=rid, recorded_at=NOW, actor=CTX.principal, policy=POLICY, supports=(ref,),
                 depends_on=depends_on, proposition=quote, attribution="synthetic", polarity="UNCERTAIN",
                 modality="REPORTED", qualifiers=("UNREVIEWED_HISTORICAL_CATALOGUE", "historical_quote_field:PARAPHRASE"),
                 provenance=Provenance(status=EpistemicStatus.FACT, scope=Scope("SKRU1"), scale=Scale("LAB"),
                                       evidence_type=EvidenceType("LAB_TEST"),
                                       sources=(SourceRef(source_id=SID, locator=ref.locator),)))


class Env:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.old = make_canon(OLD, OLD_COMMIT, OLD_PAGES, [(GLM_BLOCK, "GLM_OCR", True, OLD_PAGES[3])])
        new_pages = dict(NEW_PAGES)
        self.new = make_canon(NEW, NEW_COMMIT, new_pages, [(GLM_BLOCK, "GLM_OCR", False, OLD_PAGES[3]),
                                                           (PADDLE_BLOCK, "PADDLEOCR_VL", True, NEW_PAGES[3])])
        self.root = tmp / "private-runtime" / "evidence"
        journal = EvidenceJournal(self.root, object_validator=canonical_resolver(self.old, source_policy),
                                  reviewers=frozenset({CTX.principal}))
        records = {rid: claim(rid, page_ref(self.old, n), q) for rid, (n, q) in QUOTES.items()}
        records["R-DEP"] = claim("R-DEP", page_ref(self.old, 1), "Зависимая запись (пересказ).",
                                 depends_on=(records["R-UNCH"].version_ref,))
        span_text = "каменной соли составляет 12,5 ГПа"
        at = OLD_PAGES[2].index("каменной")
        records["R-SPAN"] = claim("R-SPAN", page_ref(self.old, 2, (at, at + len(span_text))), span_text)
        self.records = records
        first = journal.publish("seed-1", ZERO, EvidenceBatch(records=tuple(records.values())), CTX)
        review = ReviewDecision(record_id="REV-0001", recorded_at=NOW, actor=CTX.principal, policy=POLICY,
                                supports=(page_ref(self.old, 2),), target=records["R-QP"].version_ref,
                                decision="CONFLICT", rationale="synthetic conflict", reviewer_authority=CTX.principal)
        journal.publish("seed-2", first["revision"], EvidenceBatch(records=(review,)), CTX)

    def journal(self, canon=None):
        return EvidenceJournal(self.root, object_validator=canonical_resolver(canon or self.new, source_policy),
                               reviewers=frozenset({CTX.principal}))

    def plan(self, policy=None, *, target=None, canons=None, revision=None, recorded_at=NOW):
        target = target or self.new
        return rb.plan_rebase(self.journal(target), canons=canons if canons is not None else [self.old],
                              target=target, policy=policy or rb.RebasePolicy(), source_policy=source_policy,
                              context=CTX, recorded_at=recorded_at, target_snapshot_id=target.snapshot_id(),
                              revision=revision)

    def approval(self, plan, **kw):
        values = dict(plan_sha256=plan.sha256, base_revision=plan.base_revision,
                      to_snapshot_id=plan.to_snapshot.snapshot_id, publisher=CTX.principal, authority="owner",
                      acknowledged_held=rb.held_count(plan), acknowledged_not_found_repointed=rb.not_found_repointed(plan))
        values.update(kw)
        return rb.RebaseApproval(**values)

    def publish(self, plan, approval=None, **kw):
        policy = plan.policy
        return rb.publish_rebase(self.journal(), plan, approval or self.approval(plan), owners=frozenset({"owner"}),
                                 context=CTX, replan=lambda: self.plan(policy, revision=plan.base_revision), **kw)


@pytest.fixture
def env(tmp_path):
    if tmp_path.resolve().is_relative_to(Path(__file__).resolve().parents[2]):
        tmp_path = Path(tempfile.mkdtemp(prefix="vkm-evidence-rebase-"))
    return Env(tmp_path)


def entries(plan):
    return {e.record_id: e for e in plan.entries}


# ------------------------------------------------------------------------------------------------ matching


def test_normalization_joins_line_break_hyphenation_and_keeps_offsets():
    text = "Модуль деформа-\nции  каменной­соли"
    key, starts, ends = rb.normalize(text)
    assert key == "Модуль деформации каменнойсоли"
    at = key.index("деформации")
    assert text[starts[at]:ends[at + len("деформации") - 1]] == "деформа-\nции"
    assert rb.normalize("10 - 12")[0] == "10 - 12"            # a spaced dash is not hyphenation


def test_numbers_must_match_whole_tokens_and_separators():
    policy = rb.RebasePolicy()
    assert rb.match_quote("15 МПа", [("PRIMARY", "величина равна 315 МПа")], policy)["reason"] == "NUMERIC_MISMATCH"
    assert rb.match_quote("равна 0,5", [("PRIMARY", "равна 0,55 МПа")], policy)["reason"] == "NUMERIC_MISMATCH"
    found = rb.match_quote("15 МПа", [("PRIMARY", "равна 315 МПа; равна 15 МПа")], policy)
    assert found["kind"] == "EXACT" and found["span"][0] > 10
    assert rb.numbers_at("x 1,25 y", 4, 6) == ["1,25"]
    long = "Влажность образцов составила 2,5 % по массе в среднем"
    assert rb.match_quote(long, [("PRIMARY", long.replace("2,5", "2.5"))], policy)["reason"] == "NUMERIC_MISMATCH"


def test_best_substring_matches_brute_force():
    rng = random.Random(7)
    for _ in range(60):
        text = "".join(rng.choice("абвг ") for _ in range(rng.randint(1, 30)))
        pattern = "".join(rng.choice("абвг") for _ in range(rng.randint(1, 8)))
        distance, start, end = rb.best_substring(pattern, text)

        def lev(a, b):
            row = list(range(len(b) + 1))
            for i, ca in enumerate(a, 1):
                prev, row[0] = row[0], i
                for j, cb in enumerate(b, 1):
                    prev, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, prev + (ca != cb))
            return row[-1]

        best = min(lev(pattern, text[i:j]) for i in range(len(text) + 1) for j in range(i, len(text) + 1))
        assert distance == best == lev(pattern, text[start:end])


# ------------------------------------------------------------------------------------------------ planning


def test_each_status_is_decided_and_recorded(env):
    plan = env.plan()
    e = entries(plan)
    assert (e["R-UNCH"].status, e["R-UNCH"].action) == ("UNCHANGED", "REBASED")
    assert (e["R-QP"].status, e["R-QP"].action) == ("QUOTE_FOUND_PRIMARY", "REBASED")
    assert e["R-QP"].supports[0].baseline == "EXACT"
    sec = e["R-SEC"]
    assert (sec.status, sec.action, sec.supports[0].layer) == ("QUOTE_FOUND_SECONDARY_LAYER", "REBASED", "GLM_OCR")
    assert [r.object_id for r in sec.supports[0].new] == [pid(3), GLM_BLOCK]
    fz = e["R-FZ"].supports[0]
    assert (e["R-FZ"].status, e["R-FZ"].action) == ("FUZZY", "REBASED") and 90 <= fz.score < 100
    assert (e["R-NF"].status, e["R-NF"].action, e["R-NF"].reasons) == ("NOT_FOUND", "HELD", ("BELOW_THRESHOLD",))
    assert e["R-NF"].supports[0].baseline == "ABSENT"
    for rid in ("R-DIG", "R-SEP"):
        assert (e[rid].status, e[rid].action, e[rid].reasons) == ("NOT_FOUND", "HELD", ("NUMERIC_MISMATCH",))
    assert (e["R-ABS"].status, e["R-ABS"].action) == ("BLOCKED", "HELD")
    assert "OBJECT_ABSENT_IN_TARGET" in e["R-ABS"].reasons
    assert (e["REV-0001"].action, e["REV-0001"].reasons[0]) == ("HELD", "KIND_REQUIRES_OWNER_DECISION")
    assert [(s.record_id, s.pinned) for s in plan.stale_dependents] == [("REV-0001", env.records["R-QP"].version_ref)]
    assert {r.record_id for r in plan.batch} == {"R-UNCH", "R-QP", "R-SEC", "R-FZ", "R-DEP", "R-SPAN"}
    assert plan == env.plan() and plan.sha256 == env.plan().sha256       # deterministic, read-only


def test_rebased_records_are_revision_plus_one_and_change_only_anchors(env):
    plan = env.plan()
    current = env.journal().records()
    for record in plan.batch:
        previous = current[record.record_id]
        assert record.revision == previous.revision + 1 and record.supersedes == previous.version_ref
        assert all(ref.snapshot_id == NEW for ref in record.supports)
        changed = {"revision", "supersedes", "supports", "depends_on", "actor", "recorded_at"}
        assert ({k: v for k, v in record.model_dump(mode="json").items() if k not in changed}
                == {k: v for k, v in previous.model_dump(mode="json").items() if k not in changed})
    batch = {r.record_id: r for r in plan.batch}
    assert batch["R-DEP"].depends_on == (batch["R-UNCH"].version_ref,)       # re-pinned inside the batch
    span = batch["R-SPAN"].supports[0]
    new_text = NEW_PAGES[2]
    assert new_text[span.char_start:span.char_end] == "каменной\nсоли составляет 12,5 ГПа"
    assert span.fragment_sha256 == sha(new_text[span.char_start:span.char_end])


def test_resolver_accepts_rebased_anchors_on_new_and_rejects_old(env):
    plan = env.plan()
    new_resolve = canonical_resolver(env.new, source_policy)
    old_resolve = canonical_resolver(env.old, source_policy)
    for record in plan.batch:
        for ref in record.supports:
            new_resolve(ref)
            with pytest.raises(ValueError, match="original snapshot is not available"):
                old_resolve(ref)
        for ref in env.records[record.record_id].supports:
            old_resolve(ref)
            with pytest.raises(ValueError, match="original snapshot is not available"):
                new_resolve(ref)


def test_not_found_policy_repoints_only_when_allowed(env):
    plan = env.plan(rb.RebasePolicy(not_found="REPOINT_IF_BASELINE_ABSENT"))
    e = entries(plan)
    assert e["R-NF"].action == "REBASED" and e["R-NF"].status == "NOT_FOUND"
    assert e["R-DIG"].action == e["R-SEP"].action == "HELD"             # numeric mismatch is never auto-moved
    assert rb.not_found_repointed(plan) == 1
    everything = entries(env.plan(rb.RebasePolicy(not_found="REPOINT")))
    assert everything["R-DIG"].action == "REBASED" and everything["R-ABS"].action == "HELD"


def test_identity_rebase_is_a_noop(env):
    plan = env.plan(target=env.old, canons=[env.old])
    assert {e.status for e in plan.entries} == {"UNCHANGED"} and {e.action for e in plan.entries} == {"NOOP"}
    assert plan.batch == () and rb.public_receipt(plan)["status"] == "NOOP"


def test_publish_requires_exact_owner_approval_and_commits_revision_chain(env):
    plan = env.plan()
    with pytest.raises(rb.RebaseBlocked, match="REBASE_OWNER_APPROVAL_REQUIRED"):
        env.publish(plan, env.approval(plan, authority="someone"))
    with pytest.raises(rb.RebaseBlocked, match="REBASE_APPROVAL_COUNTS_MISMATCH"):
        env.publish(plan, env.approval(plan, acknowledged_held=0))
    partitions = sorted(p.name for p in (env.root / "partitions").iterdir())
    hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (env.root / "partitions").iterdir()}
    receipt = env.publish(plan)
    assert receipt["status"] == "COMMITTED" and receipt["records"] == len(plan.batch)
    journal = env.journal()
    records = journal.records()                          # validates the supersedes chain
    for record in plan.batch:
        assert records[record.record_id] == record
    history = [r for r in journal.iter_records() if r.record_id == "R-QP"]
    assert [r.revision for r in history] == [1, 2] and history[0] == env.records["R-QP"]
    assert {name: hashlib.sha256((env.root / "partitions" / name).read_bytes()).hexdigest()
            for name in partitions} == hashes          # existing partitions are never rewritten
    # A lost ACK replays the same request; no second commit.
    assert env.publish(plan)["journal"]["revision"] == receipt["journal"]["revision"]
    assert len(journal.commits()) == 3


def test_second_rebase_emits_no_new_revisions(env):
    env.publish(env.plan())
    again = env.plan()
    assert again.batch == ()
    e = entries(again)
    assert {e[r].action for r in ("R-UNCH", "R-QP", "R-SEC", "R-FZ", "R-DEP", "R-SPAN")} == {"NOOP"}
    assert {e[r].action for r in ("R-NF", "R-DIG", "R-SEP", "R-ABS", "REV-0001")} == {"HELD"}
    with pytest.raises(rb.RebaseBlocked, match="REBASE_NOTHING_TO_PUBLISH"):
        env.publish(again)


def test_existing_records_are_never_edited(env):
    plan = env.plan()
    env.publish(plan)
    journal = env.journal()
    # The same revision+1 records under another request cannot overwrite the committed chain.
    with pytest.raises(JournalConflict):
        journal.publish("forged", journal.revision, EvidenceBatch(records=plan.batch), CTX)
    # A record that claims revision 1 again for an existing ID is refused as well.
    forged = claim("R-QP", plan.batch[0].supports[0], "подмена")
    with pytest.raises(JournalConflict):
        journal.publish("forged-2", journal.revision, EvidenceBatch(records=(forged,)), CTX)


def test_stale_plan_is_not_reproducible_after_inputs_change(env):
    plan = env.plan()
    other = env.plan(recorded_at=datetime(2026, 10, 7, tzinfo=timezone.utc))
    with pytest.raises(rb.RebaseBlocked, match="REBASE_PLAN_NO_LONGER_REPRODUCIBLE"):
        rb.publish_rebase(env.journal(), plan, env.approval(plan), owners=frozenset({"owner"}), context=CTX,
                          replan=lambda: other)


def test_outputs_are_private_and_public_reports_carry_no_text(env, tmp_path):
    plan = env.plan()
    private = env.tmp / "private-runtime" / "rebase"
    public = env.tmp / "public-copy"
    result = rb.write_outputs(plan, private, public_dir=public, inputs_sha256={"target_canon_duckdb": "0" * 64})
    assert result["counts"]["records_by_action"] == {"HELD": 5, "REBASED": 6}
    assert set(p.name for p in private.iterdir() if not p.name.startswith(".")) == {
        "plan.json", "map.json", "batch.json", "receipt.json", "review.json"}
    for name in ("receipt.json", "review.json"):
        raw = (public / name).read_bytes()
        assert raw == (private / name).read_bytes()
        text = raw.decode("utf-8")
        assert not any(q in text for _, q in QUOTES.values()) and "Предел" not in text
        assert "quote" not in json.loads(text) and str(env.tmp) not in text
    assert scan(public, files=[public / "receipt.json", public / "review.json"]) == []
    review = json.loads((public / "review.json").read_text(encoding="utf-8"))
    assert {i["record_id"] for i in review["items"]} >= {"R-NF", "R-DIG", "R-SEP", "R-ABS", "R-SEC", "R-FZ"}
    rb.write_outputs(plan, private, inputs_sha256={"target_canon_duckdb": "0" * 64})   # identical retry is accepted
    with pytest.raises(ValueError, match="IMMUTABLE"):
        rb.write_outputs(plan, private)                  # different bytes never replace an existing output
    inside = Path(rb.__file__).resolve().parents[2] / "work" / "rebase-test-refused"
    with pytest.raises(rb.RebaseBlocked, match="PRIVATE_REBASE_STORAGE_REQUIRED"):
        rb.write_outputs(plan, inside)
    assert not inside.exists()


def test_wrong_target_snapshot_and_denied_policy_fail_closed(env):
    with pytest.raises(rb.RebaseBlocked, match="TARGET_SNAPSHOT_MISMATCH"):
        rb.plan_rebase(env.journal(), canons=[env.old], target=env.new, policy=rb.RebasePolicy(),
                       source_policy=source_policy, context=CTX, recorded_at=NOW, target_snapshot_id=OLD)
    denied = AccessContext(principal="rebaser", execution="CLOUD", granted_classes={"PUBLIC"})
    with pytest.raises(PermissionError):
        rb.plan_rebase(env.journal(), canons=[env.old], target=env.new, policy=rb.RebasePolicy(),
                       source_policy=source_policy, context=denied, recorded_at=NOW, target_snapshot_id=NEW)
    missing = env.plan(canons=[])
    assert {m.reasons for m in entries(missing)["R-QP"].supports} == {("SNAPSHOT_NOT_PROVIDED",)}


def test_cli_plans_privately_and_publishes_only_with_pinned_owner_approval(env, capsys):
    from vkm_evidence import cli

    files = env.tmp / "files"
    files.mkdir()
    old = make_canon(OLD, OLD_COMMIT, OLD_PAGES, [(GLM_BLOCK, "GLM_OCR", True, OLD_PAGES[3])], files / "old.duckdb")
    new = make_canon(NEW, NEW_COMMIT, NEW_PAGES, [(GLM_BLOCK, "GLM_OCR", False, OLD_PAGES[3]),
                                                  (PADDLE_BLOCK, "PADDLEOCR_VL", True, NEW_PAGES[3])],
                     files / "new.duckdb")
    (files / "policy.json").write_text(json.dumps({"schema": "vkm-source-policy/1",
                                                   "policies": {SID: POLICY.model_dump(mode="json")}}))
    (files / "context.json").write_text(CTX.model_dump_json())
    common = ["--journal-root", str(env.root), "--old-canon", str(old), "--new-canon", str(new),
              "--new-snapshot-id", NEW, "--policy", str(files / "policy.json"), "--context", str(files / "context.json")]
    package = env.tmp / "private-runtime" / "rebase-cli"
    assert cli.main(["rebase-plan", *common, "--output-dir", str(package), "--recorded-at", NOW.isoformat()]) == 0
    printed = capsys.readouterr().out
    summary = json.loads(printed)
    assert summary["status"] == "PLANNED" and summary["held_to_acknowledge"] == 5
    assert not any(q in printed for _, q in QUOTES.values()) and str(env.tmp) not in printed
    assert summary["counts"]["supports_by_status"]["UNCHANGED"] == 2
    assert {"target_canon_duckdb", "source_canon_duckdb:" + OLD, "source_policy_file"} <= set(
        json.loads((package / "receipt.json").read_text(encoding="utf-8"))["inputs_sha256"])
    plan = rb.RebasePlan.model_validate_json((package / "plan.json").read_bytes())
    approval = env.approval(plan)
    (files / "approval.json").write_bytes(approval.model_dump_json().encode())
    (files / "owners.json").write_text(json.dumps(["owner"]))
    publish = ["rebase-publish", *common, "--plan", str(package / "plan.json"), "--approval",
               str(files / "approval.json"), "--owners", str(files / "owners.json")]
    assert cli.main([*publish, "--approval-sha256", "0" * 64]) == 1          # unpinned approval bytes
    capsys.readouterr()
    pin = hashlib.sha256((files / "approval.json").read_bytes()).hexdigest()
    assert cli.main([*publish, "--approval-sha256", pin]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "COMMITTED" and result["records"] == 6
    assert (package / "publish-receipt.json").exists()
    assert EvidenceJournal(env.root).records()["R-QP"].revision == 2
