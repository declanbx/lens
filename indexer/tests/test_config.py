"""Config loading tests (CONTRACTS.md §3/§6/§9).

Focus on behaviours that previously diverged or were silently dropped:

  * the stdlib YAML fallback must un-escape double-quoted backslash escapes so a
    pattern like ``"leiden_2\\.0"`` compiles to the SAME regex as under pyyaml
    (escaped dot), keeping stdlib-only-core tag classification equivalent;
  * ``--include`` / ``--exclude`` overrides land on the Config dataclass (they
    used to vanish into ``raw`` only) so the walker can honour them.
"""

from __future__ import annotations

import re

import pytest

from repo_index import config as config_mod


def _stdlib_tags(monkeypatch):
    """Load the config with pyyaml forced OFF (stdlib fallback parser path)."""
    monkeypatch.setattr(config_mod, "yaml", None)
    return config_mod.load_config()


def _tag_pattern(cfg, name):
    for tag, pat in cfg.ontology_tags:
        if tag == name:
            return pat
    return None


def test_stdlib_yaml_fallback_matches_pyyaml_escapes(monkeypatch):
    """The stdlib fallback decodes ``\\.`` the same way pyyaml does so the legacy
    / canonical ontology regexes match identically under both parsers."""
    pyyaml_cfg = config_mod.load_config()  # pyyaml if installed
    legacy_py = _tag_pattern(pyyaml_cfg, "legacy")
    canon_py = _tag_pattern(pyyaml_cfg, "canonical")

    stdlib_cfg = _stdlib_tags(monkeypatch)
    legacy_std = _tag_pattern(stdlib_cfg, "legacy")
    canon_std = _tag_pattern(stdlib_cfg, "canonical")

    assert legacy_std is not None and canon_std is not None
    # The compiled pattern strings must agree between the two parsers.
    if legacy_py is not None:
        assert legacy_std.pattern == legacy_py.pattern
        assert canon_std.pattern == canon_py.pattern
    # And the escaped dot must match a literal "2.0" (NOT a doubled backslash).
    assert legacy_std.search("outputs/leiden_2.0/foo.csv")
    assert canon_std.search("x/leiden_cosine_2.0/y")
    # A backslash-then-char regex (the OLD bug) would NOT match "2.0":
    assert re.compile(r"leiden_2\\.0").search("leiden_2.0") is None


def test_include_exclude_overrides_land_on_config():
    """--include / --exclude (folded into overrides) reach the Config fields the
    walker reads — they are no longer silently dropped into raw only."""
    cfg = config_mod.load_config(
        overrides={"include_globs": ["a/*.py"], "exclude_globs": ["b/**"]}
    )
    assert cfg.include_globs == ["a/*.py"]
    assert cfg.exclude_globs == ["b/**"]
    # Defaults are empty (no filtering) when not overridden.
    base = config_mod.load_config()
    assert base.include_globs == []
    assert base.exclude_globs == []


def test_inline_map_split_respects_quoted_commas(monkeypatch):
    """The stdlib inline-map parser splits on commas OUTSIDE quotes only, so a
    pattern containing a comma is not shredded."""
    monkeypatch.setattr(config_mod, "yaml", None)
    text = (
        'ontology_tags:\n'
        '  - {tag: comma_tag, pattern: "a,b|c"}\n'
    )
    parsed = config_mod._stdlib_yaml_fallback(text)
    tags = parsed.get("ontology_tags")
    assert isinstance(tags, list) and len(tags) == 1
    assert tags[0]["tag"] == "comma_tag"
    assert tags[0]["pattern"] == "a,b|c"
