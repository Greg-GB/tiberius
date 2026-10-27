"""skill-sync CLI: upserting skill facts (category 'skill') from <skill>/SKILL.md dirs.

Fixtures follow the synthetic-content convention of the eval fixtures: fictional
skills on tmp_path, no real user data. Drives cli.main() directly, like test_tiberius_cli.
"""

import json

from hermes_plugin_tiberius import cli


def _run(capsys, *argv: str):
    rc = cli.main(list(argv))
    return rc, json.loads(capsys.readouterr().out.strip())


def _make_skill(base, name, description, triggers=None, fm_name=True):
    """Write base/<name>/SKILL.md with minimal frontmatter (fictional skill)."""
    d = base / name
    d.mkdir(parents=True)
    lines = ["---"]
    if fm_name:
        lines.append(f"name: {name}")
    if triggers:
        lines.append(f'triggers: "{triggers}"')
    if description is not None:
        lines.append(f'description: "{description}"')
    lines.append("---")
    (d / "SKILL.md").write_text(
        "\n".join(lines) + "\n\n# Body\n\nFictional instructions for tests.\n",
        encoding="utf-8")
    return d


def _skill_rows(capsys, db: str) -> list[dict]:
    return _run(capsys, "--db", db, "list", "--category", "skill", "--top", "50")[1]["results"]


def test_skill_sync_adds_skills(tmp_path, capsys):
    db = str(tmp_path / "m.db")
    skills = tmp_path / "skills"
    _make_skill(skills, "widget-fixer", "Use when a widget wobbles or jams. Covers spring tension and lubrication.")
    _make_skill(skills, "csv-wrangler", "Use when wrangling messy CSV exports. Covers ragged rows and quoted fields.")

    rc, out = _run(capsys, "--db", db, "skill-sync", "--dir", str(skills))
    assert rc == 0
    assert out["skills"] == 2 and len(out["added"]) == 2 and out["unchanged"] == 0
    assert out["skipped"] == [] and out["pruned"] == []

    rows = {r["content"].split(":")[0]: r for r in _skill_rows(capsys, db)}
    assert set(rows) == {"widget-fixer", "csv-wrangler"}
    assert rows["widget-fixer"]["content"] == \
        "widget-fixer: Use when a widget wobbles or jams. Covers spring tension and lubrication."
    for r in rows.values():
        assert "skill-id:" in r["tags"] and "skill-dir:" in r["tags"]
    assert str(skills / "widget-fixer") in rows["widget-fixer"]["tags"]

    # The retrieval surface works: description-vocabulary search finds the skill fact.
    out = _run(capsys, "--db", db, "search", "wobbles jams", "--category", "skill", "--top", "5")[1]
    assert out["results"][0]["content"].startswith("widget-fixer: ")


def test_skill_sync_idempotent(tmp_path, capsys):
    db = str(tmp_path / "m.db")
    skills = tmp_path / "skills"
    _make_skill(skills, "widget-fixer", "Use when a widget wobbles or jams.")
    _make_skill(skills, "csv-wrangler", "Use when wrangling messy CSV exports.")

    rc, first = _run(capsys, "--db", db, "skill-sync", "--dir", str(skills))
    assert rc == 0 and len(first["added"]) == 2
    ids = {a["skill"]: a["fact_id"] for a in first["added"]}

    rc, second = _run(capsys, "--db", db, "skill-sync", "--dir", str(skills))
    assert rc == 0
    assert second["added"] == [] and second["updated"] == [] and second["unchanged"] == 2

    rows = _skill_rows(capsys, db)
    assert {r["fact_id"] for r in rows} == set(ids.values())  # same rows, nothing churned
    assert all(r["trust_score"] == 0.5 for r in rows)  # no feedback in between, trust untouched


def test_skill_sync_updates_changed_description(tmp_path, capsys):
    db = str(tmp_path / "m.db")
    skills = tmp_path / "skills"
    _make_skill(skills, "widget-fixer", "Use when a widget wobbles. Covers spring tension.")
    rc, out = _run(capsys, "--db", db, "skill-sync", "--dir", str(skills))
    fid = out["added"][0]["fact_id"]

    # Change the description on disk and re-sync: the same fact is updated in place.
    (skills / "widget-fixer" / "SKILL.md").write_text(
        '---\nname: widget-fixer\ndescription: "Use when a widget jams. Covers gear alignment."\n---\n',
        encoding="utf-8")
    rc, out = _run(capsys, "--db", db, "skill-sync", "--dir", str(skills))
    assert rc == 0
    assert out["updated"] == [{"skill": "widget-fixer", "fact_id": fid}]
    assert out["added"] == [] and out["unchanged"] == 0

    # FTS was reindexed: new tokens match, the old ones no longer do (pattern from test_cli_update).
    out = _run(capsys, "--db", db, "search", "jam gear alignment", "--category", "skill", "--top", "5")[1]
    assert out["results"][0]["fact_id"] == fid
    out = _run(capsys, "--db", db, "search", "wobble spring", "--category", "skill", "--top", "5")[1]
    assert out["results"] == []


def test_skill_sync_triggers_land_in_tags_and_fts(tmp_path, capsys):
    db = str(tmp_path / "m.db")
    skills = tmp_path / "skills"
    _make_skill(skills, "widget-fixer",
                "Use when fixing mechanical fault patterns in assemblies.",
                triggers="wobble, jam, rattling")

    rc, out = _run(capsys, "--db", db, "skill-sync", "--dir", str(skills))
    assert rc == 0 and len(out["added"]) == 1

    tags = [t.strip() for t in _skill_rows(capsys, db)[0]["tags"].split(",")]
    assert "skill-id:widget-fixer" in tags
    assert "wobble" in tags and "jam" in tags and "rattling" in tags

    # The Phase 0 spike mechanism: symptom vocabulary the description lacks still
    # generates a candidate via the FTS-indexed tags column.
    out = _run(capsys, "--db", db, "search", "the assembly is rattling badly", "--category", "skill", "--top", "5")[1]
    assert out["results"][0]["content"].startswith("widget-fixer: ")


def test_skill_sync_prune(tmp_path, capsys):
    db = str(tmp_path / "m.db")
    skills = tmp_path / "skills"
    _make_skill(skills, "widget-fixer", "Use when a widget wobbles.")
    _make_skill(skills, "csv-wrangler", "Use when wrangling messy CSV exports.")
    rc, out = _run(capsys, "--db", db, "skill-sync", "--dir", str(skills))
    assert rc == 0 and len(out["added"]) == 2

    # Removing a skill from disk is NOT a deletion until --prune is passed (explicit).
    (skills / "csv-wrangler" / "SKILL.md").unlink()
    (skills / "csv-wrangler").rmdir()
    rc, out = _run(capsys, "--db", db, "skill-sync", "--dir", str(skills))
    assert rc == 0 and out["pruned"] == []
    assert len(_skill_rows(capsys, db)) == 2

    rc, out = _run(capsys, "--db", db, "skill-sync", "--dir", str(skills), "--prune")
    assert rc == 0 and out["unchanged"] == 1
    assert [p["skill"] for p in out["pruned"]] == ["csv-wrangler"]
    rows = _skill_rows(capsys, db)
    assert len(rows) == 1 and rows[0]["content"].startswith("widget-fixer: ")


def test_skill_sync_prune_keeps_unmanaged_facts(tmp_path, capsys):
    db = str(tmp_path / "m.db")
    skills = tmp_path / "skills"
    _make_skill(skills, "widget-fixer", "Use when a widget wobbles.")
    _run(capsys, "--db", db, "skill-sync", "--dir", str(skills))
    # A skill-category fact without a skill-id tag is not sync-managed and never pruned.
    _run(capsys, "--db", db, "add", "A manually recorded skill note.", "--category", "skill")

    rc, out = _run(capsys, "--db", db, "skill-sync", "--dir", str(skills), "--prune")
    assert rc == 0 and out["pruned"] == []
    assert len(_skill_rows(capsys, db)) == 2  # synced skill and the manual note both remain


def test_skill_sync_missing_dir_is_json_error(tmp_path, capsys):
    rc, out = _run(capsys, "--db", str(tmp_path / "m.db"), "skill-sync",
                   "--dir", str(tmp_path / "does-not-exist"))
    assert rc == 1  # JSON error on stdout, exit 1 — not a traceback (the CLI contract)
    assert "error" in out and "not found" in out["error"]


def test_skill_sync_missing_description_skipped(tmp_path, capsys):
    db = str(tmp_path / "m.db")
    skills = tmp_path / "skills"
    _make_skill(skills, "widget-fixer", description=None)  # frontmatter without a description key

    rc, out = _run(capsys, "--db", db, "skill-sync", "--dir", str(skills))
    assert rc == 0  # best-effort sync: report and skip, do not fail
    assert out["skills"] == 0 and out["added"] == []
    assert len(out["skipped"]) == 1
    assert out["skipped"][0]["skill"] == "widget-fixer"
    assert "description" in out["skipped"][0]["reason"]
    assert len(_skill_rows(capsys, db)) == 0

    # A SKILL.md with no frontmatter at all takes the same skip path.
    (skills / "widget-fixer" / "SKILL.md").write_text("# Just markdown\n\nNo frontmatter here.\n",
                                                      encoding="utf-8")
    rc, out = _run(capsys, "--db", db, "skill-sync", "--dir", str(skills))
    assert rc == 0 and out["skills"] == 0 and len(out["skipped"]) == 1


def test_skill_sync_name_falls_back_to_dir_name(tmp_path, capsys):
    db = str(tmp_path / "m.db")
    skills = tmp_path / "skills"
    _make_skill(skills, "cache-warmer", "Use when preheating caches before a traffic spike.", fm_name=False)

    rc, out = _run(capsys, "--db", db, "skill-sync", "--dir", str(skills))
    assert rc == 0 and len(out["added"]) == 1
    rows = _skill_rows(capsys, db)
    assert rows[0]["content"].startswith("cache-warmer: ")
    assert "skill-id:cache-warmer" in rows[0]["tags"]


def test_skill_sync_block_scalar_description(tmp_path, capsys):
    db = str(tmp_path / "m.db")
    skills = tmp_path / "skills"
    d = skills / "log-scavenger"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        "---\n"
        "name: log-scavenger\n"
        "description: >-\n"
        "  Use when mining old log archives.\n"
        "  Covers alignment of timestamps and timeline assembly.\n"
        "---\n",
        encoding="utf-8")

    rc, out = _run(capsys, "--db", db, "skill-sync", "--dir", str(skills))
    assert rc == 0 and len(out["added"]) == 1
    rows = _skill_rows(capsys, db)
    assert rows[0]["content"] == \
        "log-scavenger: Use when mining old log archives. Covers alignment of timestamps and timeline assembly."


def test_skill_sync_last_dir_wins(tmp_path, capsys):
    db = str(tmp_path / "m.db")
    a, b = tmp_path / "skills-a", tmp_path / "skills-b"
    _make_skill(a, "widget-fixer", "Use when a widget wobbles (alpha).")
    _make_skill(b, "widget-fixer", "Use when a widget wobbles (beta).")

    rc, out = _run(capsys, "--db", db, "skill-sync", "--dir", str(a), "--dir", str(b))
    assert rc == 0
    rows = _skill_rows(capsys, db)
    assert len(rows) == 1  # one fact per skill id, not one per dir
    assert "(beta)" in rows[0]["content"]  # the later dir wins
