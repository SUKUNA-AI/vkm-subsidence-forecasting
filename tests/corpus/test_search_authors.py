"""Authors of the work registry in a query or a lookup (agent Г, 13.10; ``search.authors``): skeletons across scripts,
Russian case forms, name-key matching (EXACT_KEY / INFLECTED / TRANSLIT / FUZZY), initials, homonyms, the author
mentions of a query (cues, eponyms, «горные работы») and the topic left. Synthetic registry rows; names chosen to look
like the corpus, nothing is read from it."""
from __future__ import annotations

import pytest

from vkm_corpus.search import authors as A


def _index() -> A.AuthorIndex:
    names = {"AUT-baryakh-cy": ("Барях А.А.", "барях|аа||CYRL", "CYRL"),
             "AUT-baryakh-la": ("Baryakh A.", "baryakh|a||LATN", "LATN"),
             "AUT-lebedeva-o": ("Лебедева О.О.", "лебедева|оо||CYRL", "CYRL"),
             "AUT-lebedeva-a": ("Лебедева А.М.", "лебедева|ам||CYRL", "CYRL"),
             "AUT-kudryashov": ("Кудряшов А.И.", "кудряшов|аи||CYRL", "CYRL"),
             "AUT-asanov": ("Асанов В.А.", "асанов|ва||CYRL", "CYRL"),
             "AUT-gorsky": ("Горский П.П.", "горский|пп||CYRL", "CYRL")}
    authors = [{"author_id": a, "name_display": n, "name_key": k, "script": s, "identity_status": "NAME_KEY_ONLY"}
               for a, (n, k, s) in names.items()]
    wa = [("VKM-WRK-044", "AUT-baryakh-cy", 1), ("VKM-WRK-044", "AUT-asanov", 3), ("VKM-WRK-025", "AUT-asanov", 1),
          ("VKM-WRK-025", "AUT-baryakh-cy", 2), ("VKM-WRK-036", "AUT-baryakh-la", 1),
          ("VKM-WRK-012", "AUT-lebedeva-o", 1), ("VKM-WRK-264", "AUT-lebedeva-a", 1),
          ("VKM-WRK-011", "AUT-kudryashov", 1), ("VKM-WRK-099", "AUT-gorsky", 1)]
    work_authors = [{"work_id": w, "author_id": a, "ordinal": o, "role": "AUTHOR",
                     "name_as_listed": names[a][0]} for w, a, o in wa]
    works = [{"work_id": w, "title": f"работа {w}", "publication_year": y, "status": "ACTIVE"}
             for w, y in (("VKM-WRK-044", 1996), ("VKM-WRK-025", 2008), ("VKM-WRK-036", 2010),
                          ("VKM-WRK-012", 2019), ("VKM-WRK-264", 2021), ("VKM-WRK-011", 2001),
                          ("VKM-WRK-099", 1990))]
    sources = [{"work_id": w, "source_id": w.replace("WRK", "SRC"), "link_type": "FULL_COPY", "is_primary": True}
               for w, _a, _o in wa]
    sources.append({"work_id": "VKM-WRK-044", "source_id": "VKM-SRC-202", "link_type": "PARTIAL_COPY",
                    "is_primary": False})
    return A.AuthorIndex.from_rows(authors, work_authors, works, sources, "SNAP")


def _match(text, **kw):
    return [(m.name_display, m.method) for m in _index().match(text, **kw)]


@pytest.mark.parametrize("a, b", [("Барях", "Baryakh"), ("Барях", "Barjakh"), ("Кудряшов", "Kudryashov"),
                                  ("Ермашов", "Yermashov"), ("Мусихин", "Musikhin"), ("Асанов", "Asanov")])
def test_skeleton_meets_across_scripts(a, b):
    assert A.skeleton(a) == A.skeleton(b)


def test_skeleton_keeps_different_names_apart():
    assert A.skeleton("Барях") != A.skeleton("Барыкин") and A.skeleton("Асанов") != A.skeleton("Иванов")


@pytest.mark.parametrize("word, nominative", [("Баряха", "барях"), ("Баряху", "барях"), ("Баряхом", "барях"),
                                              ("Кудряшову", "кудряшов"), ("Лебедевой", "лебедева"),
                                              ("Горского", "горский"), ("Асанова", "асанов")])
def test_nominative_candidates(word, nominative):
    assert nominative in A.nominatives(word)
    assert A.nominatives("Барях") == ["барях"] and A.nominatives("Baryakh") == ["baryakh"]


def test_match_methods_and_both_scripts():
    assert _match("Барях") == [("Барях А.А.", "EXACT_KEY"), ("Baryakh A.", "TRANSLIT")]
    assert _match("Баряха") == [("Барях А.А.", "INFLECTED"), ("Baryakh A.", "TRANSLIT")]
    assert _match("Baryakh") == [("Baryakh A.", "EXACT_KEY"), ("Барях А.А.", "TRANSLIT")]
    assert ("Baryakh A.", "TRANSLIT") in _match("Barjakh")                 # another Latin spelling
    assert _match("Кудряшову") == [("Кудряшов А.И.", "INFLECTED")]
    assert _match("Горского") == [("Горский П.П.", "INFLECTED")]


def test_initials_must_agree_and_word_order_does_not_matter():
    assert _match("Барях А.А.")[0] == ("Барях А.А.", "EXACT_KEY")
    assert _match("А.А. Барях")[0] == ("Барях А.А.", "EXACT_KEY")
    assert _match("Барях А.")[0] == ("Барях А.А.", "EXACT_KEY")             # a prefix of the initials
    assert _match("Барях В.В.") == []                                        # other initials: another person
    assert _match("Лебедевой О.О.") == [("Лебедева О.О.", "INFLECTED")]


def test_homonyms_are_all_returned_and_marked():
    found = _index().match("Лебедева")
    assert [m.name_display for m in found] == ["Лебедева А.М.", "Лебедева О.О."]
    assert all(m.method == "EXACT_KEY" for m in found)
    assert found[0].homonyms == ["AUT-lebedeva-o"] and found[1].homonyms == ["AUT-lebedeva-a"]
    rec = found[0].as_record()
    assert rec["homonym_clusters"] == ["AUT-lebedeva-o"] and "not an identified person" in rec["identity_note"]


def test_fuzzy_only_when_nothing_else_matched():
    assert _match("Кудряшев") == [("Кудряшов А.И.", "FUZZY")]               # one edit
    assert _match("Кудряшев", fuzzy=False) == []
    assert _match("Асанов") == [("Асанов В.А.", "EXACT_KEY")]                # no fuzzy neighbours once matched
    assert _match("Петров") == [] and _match("x") == []


def test_works_of_lead_author_first_then_newer():
    idx = _index()
    works = idx.works_of(["AUT-baryakh-cy"])
    assert [w["work"]["work_id"] for w in works] == ["VKM-WRK-044", "VKM-WRK-025"]   # ordinal 1, then ordinal 2
    assert [s["source_id"] for s in works[0]["sources"]] == ["VKM-SRC-044", "VKM-SRC-202"]
    assert works[0]["matched"] == [{"ordinal": 1, "role": "AUTHOR", "name_as_listed": "Барях А.А.",
                                    "author_id": "AUT-baryakh-cy"}]
    assert [a["name_as_listed"] for a in works[0]["authors"]] == ["Барях А.А.", "Асанов В.А."]


# ---------------------------------------------------------------------------------------------------- query
@pytest.mark.parametrize("query, cue, name", [
    ("работы Баряха по сдвижению", "works_of", "Баряха"),
    ("работы баряха", "works_of", "баряха"),
    ("труды А.А. Баряха", "works_of", "Баряха А.А."),
    ("Барях А.А. и др. оседание", "et_al", "Барях А.А."),
    ("Baryakh et al. subsidence", "et_al", "Baryakh"),
    ("papers by Baryakh on subsidence", "works_by", "Baryakh"),
    ("Асанов В.А. ползучесть", "initials_after", "Асанов В.А."),
    ("ползучесть А.А. Барях", "initials_before", "Барях А.А."),
    ("оседания по Кудряшову", "po", "Кудряшову"),
    ("реология соли Кудряшов", "surname", "Кудряшов"),
])
def test_mentions_cues(query, cue, name):
    found = A.mentions(query)
    assert found and (found[0].cue, found[0].name) == (cue, name)


@pytest.mark.parametrize("query", ["закон Бингама", "критерий Кулона", "модель Баряха", "горные работы на руднике",
                                   "оседание земной поверхности", "работы по закладке выработанного пространства"])
def test_no_author_route_for_eponyms_mining_works_and_plain_topics(query):
    assert A.resolve_route(query, _index()).status in ("NOT_DETECTED", "NO_REGISTRY_MATCH")


def test_resolve_route_topic_and_statuses():
    idx = _index()
    r = A.resolve_route("работы Баряха по сдвижению", idx)
    assert r.status == "MATCHED" and r.topic == "по сдвижению"
    assert [(a.name_display, a.method) for a in r.authors] == [("Барях А.А.", "INFLECTED"), ("Baryakh A.", "TRANSLIT")]
    assert r.mentions == [{"cue": "works_of", "text": "Баряха",
                           "author_ids": ["AUT-baryakh-cy", "AUT-baryakh-la"]}]
    assert A.resolve_route("работы Баряха", idx).topic == ""
    assert A.resolve_route("Барях и др.", idx).topic == ""
    assert A.resolve_route("Деформирование соляных пород", idx).status == "NOT_DETECTED"   # a capitalised word only
    miss = A.resolve_route("работы Петрова", idx)
    assert miss.status == "NO_REGISTRY_MATCH" and miss.mentions == [{"cue": "works_of", "text": "Петрова"}]
    fuzzy = A.resolve_route("работы Кудряшева", idx)                         # FUZZY never routes
    assert fuzzy.status == "NO_REGISTRY_MATCH" and fuzzy.ignored[0]["fuzzy"] == ["AUT-kudryashov"]
    assert A.resolve_route("работы Баряха", None).status == "UNAVAILABLE"
    assert A.resolve_route("реология соли Кудряшов", None).status == "NOT_DETECTED"
    assert A.resolve_route("оседание", idx).status == "NOT_DETECTED"


def test_canon_authors_is_cached_per_snapshot():
    class Canon:
        snap, calls = "S1", 0

        def snapshot_id(self):
            return self.snap

        def query(self, sql, params=()):
            Canon.calls += 1
            if "FROM authors" in sql:
                return [{"author_id": "AUT-1", "name_display": "Барях А.А.", "name_key": "барях|аа||CYRL",
                         "script": "CYRL"}]
            if "FROM work_authors" in sql:
                return [{"work_id": "VKM-WRK-044", "author_id": "AUT-1", "ordinal": 1, "role": "AUTHOR",
                         "name_as_listed": "Барях А.А."}]
            if "FROM works" in sql:
                return [{"work_id": "VKM-WRK-044", "publication_year": 1996, "status": "ACTIVE"}]
            return [{"work_id": "VKM-WRK-044", "source_id": "VKM-SRC-044", "link_type": "FULL_COPY",
                     "is_primary": True}]

    canon = Canon()
    ca = A.CanonAuthors(canon)
    first = ca.index()
    assert ca.index() is first and Canon.calls == 4
    canon.snap = "S2"
    assert ca.index() is not first and Canon.calls == 8
    assert [m.method for m in first.match("Баряха")] == ["INFLECTED"]


def test_own_works_come_before_supervised_ones():
    """«работы X» are the works X wrote (live check 13.10: theses Барях supervised came first): ``works_of`` puts the
    works where the cluster is AUTHOR before the supervised ones, and the author route drops the latter."""
    from vkm_corpus.search.registry_routes import OWN_ROLES

    base = _index()
    wa = [dict(r, work_id=w) for w, rows in base.work_authors.items() for r in rows]
    wa.append({"work_id": "VKM-WRK-065", "author_id": "AUT-baryakh-cy", "ordinal": 2, "role": "SUPERVISOR",
               "name_as_listed": "Барях А.А."})
    wa.append({"work_id": "VKM-WRK-065", "author_id": "AUT-gorsky", "ordinal": 1, "role": "AUTHOR",
               "name_as_listed": "Горский П.П."})
    works = list(base.works.values()) + [{"work_id": "VKM-WRK-065", "title": "диссертация", "publication_year": 2026,
                                          "status": "ACTIVE"}]
    sources = [dict(s, work_id=w) for w, ss in base.sources.items() for s in ss]
    sources.append({"work_id": "VKM-WRK-065", "source_id": "VKM-SRC-065", "link_type": "FULL_COPY",
                    "is_primary": True})
    authors = [{"author_id": a.author_id, "name_display": a.name_display,
                "name_key": f"{a.surname}|{a.initials}||{a.script}", "script": a.script,
                "identity_status": "NAME_KEY_ONLY"} for a in base.authors.values()]
    index = A.AuthorIndex.from_rows(authors, wa, works, sources, "SNAP")
    rows = index.works_of(["AUT-baryakh-cy"])
    assert rows[-1]["work"]["work_id"] == "VKM-WRK-065"          # newest, but supervised: last
    assert [a["role"] for a in rows[-1]["matched"]] == ["SUPERVISOR"] and "SUPERVISOR" not in OWN_ROLES
    assert all(a["role"] == "AUTHOR" for r in rows[:-1] for a in r["matched"])
