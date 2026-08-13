"""Structural tests for the docker-compose files.

Written after shipping two compose bugs on 2026-08-05 that a single parse
would have caught:

  1. `volumes: [${ROFL_DATA:-./data}/...]` — inside a YAML FLOW sequence a
     plain scalar may not contain `{`, so every live compose failed to load
     with "did not find expected ',' or ']'". The collector compose survived
     only because it uses block style, where `{` is legal mid-scalar. Entries
     in flow sequences must be QUOTED.
  2. `tg-control` still mounted the 16 named `live_*_state` volumes after the
     bind-mount conversion deleted their declarations. That one does not
     crash — Compose would create 16 EMPTY volumes and the Telegram panel
     would report every leg as missing while the bots wrote elsewhere.

The lesson both share: the composes are load-bearing config that nothing else
in the suite touched, so a rewrite could break them silently. These tests are
cheap and they close that gap.

NOTE: this validates with PyYAML; Compose itself uses go-yaml. PyYAML is the
stricter of the two on case (1), so it is a sound guard, but it is NOT a
substitute for `docker compose config` on the box.

Run:  ./.venv/bin/python test_compose.py
"""
import glob

import yaml

COMPOSES = sorted(glob.glob("docker-compose*.yml"))
LEGS = [f"{s}-{t}" for s in ("btc", "eth", "sol", "xrp", "doge", "ada", "link", "avax")
        for t in ("t", "p")]


def _load(path):
    with open(path) as fh:
        return yaml.safe_load(fh)


def test_all_parse():
    assert COMPOSES, "no compose files found"
    for f in COMPOSES:
        d = _load(f)
        assert isinstance(d, dict) and "services" in d, f"{f}: no services"
        print(f"PASS parses            {f} ({len(d['services'])} services)")


def test_no_undeclared_named_volumes():
    """A mount source that is neither a path nor a declared volume makes
    Compose silently create an empty one — bug (2)."""
    for f in COMPOSES:
        d = _load(f)
        declared = set((d.get("volumes") or {}).keys())
        bad = []
        for name, svc in d["services"].items():
            for v in (svc.get("volumes") or []):
                src = str(v).split(":")[0]
                if src.startswith(("./", "/", "$", "~")):
                    continue
                if src not in declared:
                    bad.append(f"{name}: {v}")
        assert not bad, f"{f}: undeclared named volumes -> {bad[:5]}"
        print(f"PASS no stray volumes  {f}")


def test_live_legs_bind_to_data_root():
    """Every live leg must write state+logs under ${ROFL_DATA}, or pull-data.sh
    silently syncs an incomplete tree."""
    d = _load("docker-compose.bidir4h-live.yml")
    svc = d["services"]
    for leg in LEGS:
        assert leg in svc, f"missing service {leg}"
        vols = [str(v) for v in svc[leg]["volumes"]]
        for sub in ("state", "logs"):
            want = f"/live/{leg}/{sub}:/app/{sub}"
            assert any(want in v and "ROFL_DATA" in v for v in vols), \
                f"{leg}: no ROFL_DATA-rooted {sub} mount in {vols}"
    print(f"PASS bind mounts       all {len(LEGS)} legs -> ${{ROFL_DATA}}/live/<leg>/")


def test_tg_control_reads_same_paths_as_legs():
    """tg-control must read the SAME location the legs write, read-only."""
    d = _load("docker-compose.bidir4h-live.yml")
    tg = d["services"]["tg-control"]["volumes"]
    assert len(tg) == len(LEGS), f"tg-control mounts {len(tg)}, expected {len(LEGS)}"
    for leg in LEGS:
        m = [v for v in map(str, tg) if f"/live/{leg}/state:" in v]
        assert m, f"tg-control has no mount for {leg}"
        assert m[0].endswith(":ro"), f"tg-control mount for {leg} is not read-only"
        assert "ROFL_DATA" in m[0], f"tg-control mount for {leg} is not ROFL_DATA-rooted"
    print("PASS tg-control        16 read-only mounts, same paths as the legs")


def test_live_is_live_and_capped():
    d = _load("docker-compose.bidir4h-live.yml")
    svc = d["services"]
    for leg in LEGS:
        env = svc[leg]["environment"]
        assert env.get("MODE") == "live", f"{leg}: MODE is {env.get('MODE')!r}, not live"
        assert svc[leg].get("mem_limit"), f"{leg}: no mem_limit"
    assert d["name"] == "rofl4h-live", "live compose lost its explicit project name"
    print("PASS live stack        MODE=live + mem_limit on all 16, project pinned")


def test_collector_declares_symbols():
    """`--env-file` only feeds compose SUBSTITUTION, not the container. If the
    collector service does not DECLARE SYMBOLS, init-collector.sh's QUAL23 pin
    is silently ignored and it collects MAJORS8 instead — which is what
    happened on 2026-08-06, costing 22h of 15-symbol microstructure that
    cannot be backfilled."""
    d = _load("docker-compose.collector.yml")
    env = d["services"]["collector"]["environment"]
    assert "SYMBOLS" in env, \
        "collector service must declare SYMBOLS or .env.collector never reaches it"
    assert "DATA_DIR" in env
    print("PASS collector env     SYMBOLS declared (reaches the container)")


def test_paper_never_live():
    """Paper must never be able to trade — the one-way door."""
    d = _load("docker-compose.bidir4h-paper.yml")
    for name, svc in d["services"].items():
        env = (svc.get("environment") or {})
        if isinstance(env, dict) and "MODE" in env:
            assert env["MODE"] != "live", f"paper compose has MODE=live on {name}!"
    assert d["name"] != "rofl4h-live", "paper shares the live project name"
    print("PASS paper stack       no MODE=live anywhere, distinct project name")


def test_blend_weights_resolve():
    """Every live leg must resolve a STARTING_EQUITY, and the book must sum right.

    BLEND75 moved equity from 16 per-service lines onto the x-triple / x-pull
    anchors. If a merge key ever breaks, the leg does NOT error — it silently
    falls back to bot.py's own `STARTING_EQUITY` default of 100, which would
    mis-size real money with no log line. That is the exact failure shape this
    project keeps getting bitten by, so it gets a test rather than a comment.
    """
    d = _load("docker-compose.bidir4h-live.yml")
    legs = {n: s for n, s in d["services"].items()
            if n.endswith("-t") or n.endswith("-p")}
    assert len(legs) == 16, f"expected 16 legs, found {len(legs)}"
    seen = {"-t": set(), "-p": set()}
    for name, svc in legs.items():
        env = svc.get("environment") or {}
        eq = env.get("STARTING_EQUITY")
        assert eq, f"{name} resolves NO STARTING_EQUITY (anchor merge broken)"
        seen[name[-2:]].add(str(eq))
    assert len(seen["-t"]) == 1 and len(seen["-p"]) == 1, \
        f"legs disagree within a book: {seen}"
    t_raw, p_raw = seen["-t"].pop(), seen["-p"].pop()
    assert "TRIPLE_LEG_EQUITY" in t_raw, f"-t legs not on the triple var: {t_raw}"
    assert "PULL_LEG_EQUITY" in p_raw, f"-p legs not on the pull var: {p_raw}"
    t = float(t_raw.split(":-")[1].rstrip("}"))
    p = float(p_raw.split(":-")[1].rstrip("}"))
    book = 8 * t + 8 * p
    share = 8 * t / book
    assert abs(book - 1795.20) < 0.01, f"book defaults sum to {book:.2f}, not 1795.20"
    assert abs(share - 0.75) < 1e-6, f"triple share {share:.4f}, not the 0.75 of BLEND75"
    print(f"PASS blend weights     16/16 resolve; 8x{t} + 8x{p} = {book:.2f}, "
          f"triple share {share:.0%} (BLEND75)")


if __name__ == "__main__":
    test_all_parse()
    test_no_undeclared_named_volumes()
    test_live_legs_bind_to_data_root()
    test_tg_control_reads_same_paths_as_legs()
    test_live_is_live_and_capped()
    test_collector_declares_symbols()
    test_paper_never_live()
    test_blend_weights_resolve()
    print("\nAll compose tests passed.")
