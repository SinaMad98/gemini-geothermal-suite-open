import hashlib
import http.server
import json
import math
import os
import random
import shutil
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
SUB = ROOT / "hackathon_submission"
OUT = ROOT / "verify_out"
OUT.mkdir(exist_ok=True)
RESULTS = []
RUNTIMES = {}
OUTPUTS = ["results_daily.csv", "results_summary.csv", "validation_report.txt"]


def rec(section, name, status, detail=""):
    RESULTS.append(dict(section=section, name=name, status=status, detail=str(detail)))
    print(f"[{status}] {section} | {name} | {detail}", flush=True)


def close_check(section, name, actual, claim, tol):
    ok = actual is not None and abs(actual - claim) <= tol
    rec(section, name, "PASS" if ok else "FAIL", f"actual {actual}, claimed {claim}, tol {tol}")


def run_py(args, label, timeout=1800):
    t0 = time.time()
    p = subprocess.run(
        [sys.executable, *args],
        cwd=SUB,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    RUNTIMES[label] = round(time.time() - t0, 1)
    (OUT / f"{label}.log").write_text(p.stdout + "\n--- stderr ---\n" + p.stderr, encoding="utf-8")
    return p


def restore():
    subprocess.run(["git", "checkout", "--", "hackathon_submission"], cwd=ROOT, check=True)
    subprocess.run(["git", "clean", "-fdq", "hackathon_submission"], cwd=ROOT, check=True)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def numeric_diff(a, b):
    worst, where = 0.0, None
    for col in a.columns:
        if col in b.columns and pd.api.types.is_numeric_dtype(a[col]) and a[col].dtype != bool:
            d = (a[col] - b[col]).abs().max()
            if pd.notna(d) and d > worst:
                worst, where = float(d), col
    return worst, where


def section_repro():
    s = "1 Reproducibility run_full_year"
    committed = pd.read_csv(SUB / "results_daily.csv")
    hashes = []
    for i in range(1, 4):
        p = run_py(["run_full_year.py"], f"run_full_year_{i}")
        rec(s, f"run {i} exit code", "PASS" if p.returncode == 0 else "FAIL", p.returncode)
        if p.returncode != 0:
            rec(s, f"run {i} stderr", "FAIL", p.stderr[-800:])
            return
        hashes.append(tuple(digest(SUB / f) for f in OUTPUTS))
    rec(s, "3 runs byte-identical", "PASS" if len(set(hashes)) == 1 else "FAIL", hashes[0])
    new = pd.read_csv(SUB / "results_daily.csv")
    rows_ok = len(new) == len(committed) == 365
    rec(s, "365 daily rows", "PASS" if rows_ok else "FAIL", len(new))
    worst, where = numeric_diff(new, committed)
    rec(
        s,
        "matches committed results_daily.csv",
        "PASS" if worst <= 1e-3 else "FAIL",
        f"max abs diff {worst} in {where}",
    )
    report = (SUB / "validation_report.txt").read_text(encoding="utf-8", errors="replace")
    passes = sum(ln.startswith("  PASS") for ln in report.splitlines())
    fails = sum(ln.startswith("  FAIL") for ln in report.splitlines())
    rec(s, "validator verdict", "PASS" if passes == 18 and fails == 0 else "FAIL", f"{passes} PASS, {fails} FAIL")


def section_claims():
    s = "2 README claims vs fresh run"
    m = pd.read_csv(SUB / "results_summary.csv").iloc[0]
    d = pd.read_csv(SUB / "results_daily.csv")
    close_check(s, "CO2 t/yr", float(m.co2_t_per_year), 6626, 1)
    close_check(s, "no-geothermal CO2", float(m.co2_reference_no_geothermal_t), 26386, 1)
    close_check(s, "CO2 saving %", float(m.co2_saving_vs_no_geothermal_pct), 74.9, 0.05)
    close_check(s, "geothermal share %", float(m.geothermal_chain_share_pct), 87.1, 0.05)
    close_check(s, "produced gas share %", float(m.produced_gas_share_pct), 10.5, 0.05)
    close_check(s, "backup share %", float(m.backup_boiler_share_pct), 2.4, 0.05)
    close_check(s, "peak boiler MW", float(m.peak_backup_boiler_MW), 3.5, 0.05)
    close_check(s, "peak electric chiller MW", float(m.peak_electric_chiller_MW), 10, 0.05)
    close_check(s, "peak absorption MW", float(m.peak_absorption_chiller_MW), 3.7, 0.05)
    act = d[d.geothermal_active]
    close_check(s, "min flow m3/h", float(act.flow_per_doublet_m3h.min()), 55, 0.5)
    close_check(s, "max flow m3/h", float(act.flow_per_doublet_m3h.max()), 202, 0.6)
    close_check(s, "T reservoir", float(act.T_reservoir_C.max()), 111, 0.01)
    close_check(s, "T wellhead", float(act.T_producer_C.max()), 104, 0.01)
    h = pd.read_csv(SUB / "GEMINI_Heating_Demand_Daily_2027.csv")
    c = pd.read_csv(SUB / "GEMINI_Cooling_Demand_Daily_2027.csv")
    close_check(s, "demand heat GWh", float(h.iloc[:, 1].sum() * 24 / 1000), 111.67, 0.005)
    close_check(s, "demand cooling GWh", float(c.iloc[:, 1].sum() * 24 / 1000), 23.74, 0.005)
    close_check(s, "peak heat MW", float(h.iloc[:, 1].max()), 30, 2.0)
    close_check(s, "peak cooling MW", float(c.iloc[:, 1].max()), 10, 0.5)
    maint = d[d.maintenance]
    span = (pd.to_datetime(maint.date.max()) - pd.to_datetime(maint.date.min())).days + 1
    rec(s, "maintenance one 35-day block", "PASS" if len(maint) == 35 == span else "FAIL", f"{maint.date.min()}..{maint.date.max()}")
    months = set(pd.to_datetime(maint.date).dt.month)
    rec(s, "maintenance in Jul-Aug", "PASS" if months <= {7, 8} else "FAIL", sorted(months))


def section_offline():
    s = "3 Offline agent"
    restore()
    seen = []
    for i in range(1, 4):
        p = run_py(["agent.py", "--offline", "--iterations", "30"], f"agent_offline_{i}")
        rec(s, f"run {i} exit code", "PASS" if p.returncode == 0 else "FAIL", p.returncode)
        if p.returncode != 0:
            rec(s, f"run {i} stderr", "FAIL", p.stderr[-800:])
            restore()
            return
        j = json.loads((SUB / "agent_best_design.json").read_text(encoding="utf-8"))
        hist = pd.read_csv(SUB / "agent_history.csv")
        sims = int(hist.co2_t_per_year.notna().sum())
        seen.append((j["co2_t_per_year"], json.dumps(j["design"], sort_keys=True), sims))
    rec(s, "3 runs identical", "PASS" if len(set(seen)) == 1 else "FAIL", seen[0][0])
    close_check(s, "offline CO2", float(seen[0][0]), 6721, 1)
    close_check(s, "offline simulations", float(seen[0][2]), 22, 0)
    p = run_py(["run_full_year.py"], "run_full_year_after_offline")
    co2 = float(pd.read_csv(SUB / "results_summary.csv").iloc[0].co2_t_per_year)
    rec(
        s,
        "README step 1 offline then step 2 keeps submitted design",
        "PASS" if abs(co2 - 6626) <= 1 else "FAIL",
        f"run_full_year now reports {co2} t, submitted design is 6626 t",
    )
    restore()


def section_optimize():
    s = "4 Grid search and sensitivity"
    restore()
    committed = pd.read_csv(SUB / "design_comparison.csv")
    p = run_py(["optimize_design.py"], "optimize_design", timeout=2400)
    rec(s, "exit code", "PASS" if p.returncode == 0 else "FAIL", p.returncode)
    if p.returncode != 0:
        rec(s, "stderr", "FAIL", p.stderr[-800:])
        restore()
        return
    rec(s, "runtime seconds, README says about 120", "INFO", RUNTIMES["optimize_design"])
    comp = pd.read_csv(SUB / "design_comparison.csv")
    rec(s, "72 designs", "PASS" if len(comp) == 72 else "FAIL", len(comp))
    feas = comp[comp.feasible]
    rec(s, "feasible designs", "INFO", f"{len(feas)} of {len(comp)}")
    for n, claim in ((1, 14924), (2, 8760), (3, 6725)):
        close_check(s, f"best {n}-doublet", float(feas[feas.n_doublets == n].co2_t_per_year.min()), claim, 1)
    close_check(s, "best at 2500 m", float(feas[feas.well_depth_m == 2500].co2_t_per_year.min()), 8324, 1)
    a = comp.sort_values("design").reset_index(drop=True)
    b = committed.sort_values("design").reset_index(drop=True)
    worst, where = numeric_diff(a, b)
    rec(s, "matches committed design_comparison.csv", "PASS" if worst <= 0.11 else "FAIL", f"max abs diff {worst} in {where}")
    sens = pd.read_csv(SUB / "sensitivity.csv")
    rec(s, "all sensitivity cases feasible", "PASS" if sens.feasible.all() else "FAIL", f"{int(sens.feasible.sum())}/{len(sens)}")
    for param, val, claim in (
        ("cp", 3900.0, 7222),
        ("skin", 10.0, 7015),
        ("grid_ef_kg_per_kwh", 0.1, 5021),
        ("grid_ef_kg_per_kwh", 0.5, 8199),
        ("chiller_eta_carnot", 0.3, 6991),
        ("chiller_condensing_c", 40.0, 6956),
    ):
        row = sens[(sens.parameter == param) & (sens.value == val)]
        close_check(s, f"sensitivity {param}={val}", float(row.co2_t_per_year.iloc[0]), claim, 1)
    restore()


def load_model():
    sys.path.insert(0, str(SUB))
    import dispatch
    import plant_model

    return plant_model, dispatch


def submitted(plant_model):
    j = json.loads((SUB / "agent_best_design.json").read_text(encoding="utf-8"))
    return plant_model.Design(**j["design"]), j


def section_validator(pm, dp):
    s = "5 Validator catches violations"
    restore()
    dates, H, C = dp.load_demand(SUB)
    a = pm.Assumptions()
    small = pm.Design(n_doublets=1, backup_boiler_mw=5, electric_chiller_mw=4, absorption_chiller_mw=0)
    df = dp.Plant(small, a).simulate_year(dates, H, C)
    checks = dp.validate(df, small)
    failed = [n for n, ok, _ in checks if not ok]
    rec(s, "undersized plant rejected", "PASS" if failed else "FAIL", failed)
    rec(
        s,
        "undersized plant numbers, README 72.6 C, 23.6 C, 13.7 MW",
        "INFO",
        f"{df.T_heating_supply_C.min():.1f} C, {df.T_cooling_supply_C.max():.1f} C, "
        f"{(df.heating_demand_MW - df.heating_delivered_MW).max():.1f} MW",
    )
    design, _ = submitted(pm)
    base = dp.Plant(design, a).simulate_year(dates, H, C)
    baseline = [n for n, ok, _ in dp.validate(base, design) if not ok]
    rec(s, "submitted design passes before tampering", "PASS" if not baseline else "FAIL", baseline)
    day = int(base.index[base.geothermal_active][0])
    off = int(base.index[~base.geothermal_active][0])

    def tamper(col, value, at=day):
        def f(x):
            x.loc[at, col] = value

        return f

    cases = [
        ("Flow per active doublet", tamper("flow_per_doublet_m3h", 40.0)),
        ("Flow per active doublet", tamper("flow_per_doublet_m3h", 310.0)),
        ("No flow on inactive", tamper("flow_per_doublet_m3h", 100.0, off)),
        ("Volume balance", tamper("injection_per_doublet_m3h", 1.0)),
        ("Brine cooling", tamper("brine_cooling_C", 41.0)),
        ("Heating supply", tamper("T_heating_supply_C", 99.0)),
        ("Cooling supply", tamper("T_cooling_supply_C", 21.0)),
        ("Heating demand met", tamper("heating_delivered_MW", 0.0)),
        ("Cooling demand met", tamper("cooling_delivered_MW", -1.0)),
        ("Geothermal operating days", tamper("geothermal_active", False)),
        ("Pump efficiency", tamper("esp_elec_MW", 99.0)),
        ("Energy balance", tamper("energy_closure_kW", 50.0)),
        ("Whole-plant", tamper("plant_heat_residual_MW", 1.0)),
        ("Electricity balance", tamper("elec_residual_MW", 1.0)),
        ("Gas mass balance", tamper("gas_burned_kg_s", 0.0)),
        ("Produced-gas CO2", tamper("gas_co2_gemini_t", 0.0)),
    ]
    for key, fn in cases:
        x = base.copy()
        fn(x)
        result = dp.validate(x, design)
        caught = [n for n, ok, _ in result if not ok and n.startswith(key)]
        rec(s, f"tamper detected: {key}", "PASS" if caught else "FAIL", [n for n, ok, _ in result if not ok])


def swamee_jain(rho, mu, q_m3s, d, eps):
    v = q_m3s / (math.pi * d * d / 4)
    re = rho * v * d / mu
    f = 0.25 / math.log10(eps / (3.7 * d) + 5.74 / re**0.9) ** 2
    return f, v, re


def section_physics(pm, dp):
    s = "6 Independent physics audit"
    restore()
    design, _ = submitted(pm)
    a = pm.Assumptions()
    captured = []
    original = pm.build_project

    def capture(p):
        solver = original(p)
        captured.append((p, solver))
        return solver

    pm.build_project = capture
    rows = pm.build_well_table(design, a)
    pm.build_project = original
    z = design.well_depth_m
    rho, mu, g = pm.BRINE_DENSITY, pm.BRINE_VISCOSITY, 9.81
    d = a.tubing_id_m
    pi = pm.productivity_index_m3h_bar(a.skin, a.r_drainage_m, a.r_well_m)
    p_res = pm.reservoir_pressure(z)
    close_check(s, "Darcy PI recomputed m3/h/bar", round(pi, 2), 11.22, 0.01)
    hydro = rho * g * z / 1e5
    rec(s, "reservoir pressure vs brine column at depth", "INFO", f"p_res {p_res:.1f} bar, column {hydro:.1f} bar")
    table = []
    for p, solver in captured:
        get = solver.get_output
        try:
            q = get("producer_well", "flow_rate")
        except Exception:
            continue
        if not pm.MIN_FLOW_M3H <= q <= pm.MAX_FLOW_M3H:
            continue
        q_s = q / 3600
        rough = p["components"]["injector_well"]["parameters"].get("roughness_m", 0.01)
        f, v, re = swamee_jain(rho, mu, q_s, d, rough)
        fric = f * z / d * rho * v * v / 2 / 1e5
        f_s, _, _ = swamee_jain(rho, mu, q_s, d, 4.6e-5)
        fric_s = f_s * z / d * rho * v * v / 2 / 1e5
        bh_req = p_res + q / pi
        whp_req = bh_req - hydro + fric
        whp_req_smooth = bh_req - hydro + fric_s
        dp_esp_hand = a.wellhead_pressure_bar + hydro + fric - (p_res - q / pi)
        intake = get("producer_well", "esp_intake_pressure")
        dp_esp = get("producer_well", "esp_discharge_pressure") - intake
        table.append(
            dict(
                q_m3h=round(q, 1),
                roughness_m=rough,
                friction_factor=round(f, 4),
                friction_bar=round(fric, 1),
                friction_bar_eps_46um=round(fric_s, 1),
                bh_required_bar=round(bh_req, 1),
                inj_whp_model_bar=round(get("injector_well", "wellhead_pressure"), 3),
                inj_whp_hand_bar=round(whp_req, 1),
                inj_whp_hand_eps_46um_bar=round(whp_req_smooth, 1),
                filter_b_out_bar=round(get("filter_b", "pressure_out_pa") / 1e5, 2),
                inj_pump_model_kW=round(get("injector_well", "power_el") / 1e3, 1),
                esp_dp_model_bar=round(dp_esp, 1),
                esp_dp_hand_bar=round(dp_esp_hand, 1),
                esp_power_kW=round(dp_esp * 1e5 * q_s / pm.PUMP_EFFICIENCY / 1e3, 1),
                esp_curve_power_kW=round(get("producer_well", "esp_power_el") / 1e3, 1),
            )
        )
    t = pd.DataFrame(table)
    t.to_csv(OUT / "well_audit.csv", index=False)
    print(t.to_string(), flush=True)
    if len(t) == 0:
        rec(s, "well audit table", "FAIL", "no operating points captured")
        return
    top = t.iloc[-1]
    rec(s, "well audit at highest flow", "INFO", top.to_dict())
    esp_gap = (t.esp_dp_model_bar - t.esp_dp_hand_bar).abs().max()
    rec(s, "ESP dp model vs hand", "PASS" if esp_gap <= 0.1 * t.esp_dp_hand_bar.max() else "FAIL", f"max gap {esp_gap:.1f} bar")
    injector_needed = (t.inj_whp_hand_bar > t.filter_b_out_bar + 0.5) & (t.inj_pump_model_kW <= 0.01)
    rec(
        s,
        "injector pump zero only where hand calc agrees",
        "FAIL" if injector_needed.any() else "PASS",
        f"{int(injector_needed.sum())} of {len(t)} operating points need pressure by hand calc but model pump is 0",
    )
    try:
        from gemini_simulator.nodal_solver import _well_bottomhole_pressure_injection
        from gemini_simulator.well_flow import _build_dpdt, _resolve_trajectory_segments

        p, solver = captured[-1]
        cfg = p["components"]["injector_well"]["parameters"]
        probe = []
        for q in (50.0, 100.0, 200.0, 300.0):
            well = _build_dpdt(_resolve_trajectory_segments(cfg), roughness=cfg.get("roughness_m", 0.01), pvt=cfg.get("pvt"))
            bh = _well_bottomhole_pressure_injection(well, q / 3600, 1e5, 65 + 273.15) / 1e5
            probe.append(dict(q_m3h=q, bh_at_1bar_whp=round(bh, 1), bh_required=round(p_res + q / pi, 1)))
        rec(s, "GEMINI injector column probe at 1 bar WHP, friction should reduce BHP as q rises", "INFO", probe)
        drops = probe[-1]["bh_at_1bar_whp"] < probe[0]["bh_at_1bar_whp"]
        rec(s, "injector friction sign", "PASS" if drops else "FAIL", "BHP must fall with rate when flowing down at fixed WHP")
    except Exception as e:
        rec(s, "injector probe", "FAIL", repr(e))

    d_all = pd.read_csv(SUB / "results_daily.csv")
    act = d_all[d_all.geothermal_active]
    co2_hand = (
        d_all.net_elec_MW.clip(lower=0) * 24 * 1000 * a.grid_ef_kg_per_kwh / 1000
        + d_all.gas_co2_gemini_t
        + d_all.heat_backup_boiler_MW * 24 * 3.6 * a.gas_ef_kg_per_gj / a.backup_boiler_eff / 1000
    ).sum()
    close_check(s, "annual CO2 rebuilt from daily rows", round(float(co2_hand), 1), round(float(d_all.co2_t.sum()), 1), 1)
    gas_hand = (act.doublets_active * act.flow_per_doublet_m3h * pm.GLR * pm.RHO_CO2_N * 24 / 1000).sum()
    close_check(s, "produced-gas CO2 hand", round(float(gas_hand), 1), round(float(act.gas_co2_gemini_t.sum()), 1), 5)
    brine_hand = (
        act.doublets_active * act.flow_per_doublet_m3h / 3600 * rho * a.cp * (act.T_producer_C - 2 - (act.T_injector_C + 1)) / 1e6
    )
    gap = (brine_hand - act.heat_geo_brine_MW).abs().max()
    rec(s, "brine heat rho q cp dT vs reported", "PASS" if gap < 0.01 else "FAIL", f"max gap {gap:.4f} MW")
    gas_heat_hand = act.doublets_active * act.flow_per_doublet_m3h / 3600 * pm.GLR * pm.X_CH4 * pm.LHV_CH4_N * (
        a.produced_gas_to_boiler * a.backup_boiler_eff + (1 - a.produced_gas_to_boiler) * a.chp_eff * 2 / 3
    ) / 1e6
    over = (act.heat_produced_gas_MW - gas_heat_hand).max()
    rec(s, "produced-gas heat within fuel energy", "PASS" if over < 0.01 else "FAIL", f"max excess {over:.4f} MW")
    n_hp = int((act.heat_hp_MW > 0).sum())
    rec(s, "heat pump days in submitted design", "INFO", n_hp)
    t_hex_ok = (act.T_producer_C - 2 >= 100).all() or n_hp > 0
    rec(s, "HEX hot end above 100 C when no heat pump", "PASS" if t_hex_ok else "FAIL", f"{act.T_producer_C.max() - 2:.1f} C")
    q_per = (act.heat_hex_MW / act.doublets_active.clip(lower=1)) * 1e6
    t_brine_in = act.T_producer_C - 2
    t_brine_out = t_brine_in - q_per / (act.flow_per_doublet_m3h / 3600 * rho * a.cp)
    pinch = pd.concat([t_brine_in - 100, t_brine_out - a.t_return], axis=1).min(axis=1)
    rec(s, "HEX second law pinch", "PASS" if (pinch >= 0).all() else "FAIL", f"min pinch {pinch.min():.2f} C")
    pump_hint = t[["q_m3h", "inj_whp_hand_bar", "filter_b_out_bar"]].copy()
    pump_hint["need"] = (pump_hint.inj_whp_hand_bar - pump_hint.filter_b_out_bar).clip(lower=0)
    qs = act.flow_per_doublet_m3h.values
    need = pd.Series(qs).apply(lambda q: float(pd.Series(pump_hint.need.values, index=pump_hint.q_m3h.values).sort_index().reindex([q], method="nearest").iloc[0]))
    mwh = (act.doublets_active.values * qs / 3600 * need.values * 1e5 / pm.PUMP_EFFICIENCY).sum() * 24 / 1e6
    tco2 = mwh * 1000 * a.grid_ef_kg_per_kwh / 1000
    rec(s, "estimated missing injector pump energy by hand calc", "INFO", f"{mwh:.0f} MWh/yr, {tco2:.0f} t CO2/yr, {100 * tco2 / d_all.co2_t.sum():.1f} % of total")


def section_fuzz(pm, dp):
    s = "7 Random design robustness"
    restore()
    import agent

    dates, H, C = dp.load_demand(SUB)
    a = pm.Assumptions()
    rng = random.Random(42)
    designs = []
    for _ in range(30):
        v = {}
        for k, (lo, hi, is_int, _) in agent.VARS.items():
            v[k] = rng.randint(lo, hi) if is_int else round(rng.uniform(lo, hi), 3)
        designs.append(v)
    keys = list(agent.VARS)
    designs.append({k: agent.VARS[k][0] for k in keys})
    designs.append({k: agent.VARS[k][1] for k in keys})
    crashes, passed, failed, no_point, bad_pass = [], 0, 0, 0, []
    for v in designs:
        d = agent.to_design(v)
        try:
            df = dp.Plant(d, a).simulate_year(dates, H, C)
            checks = dp.validate(df, d)
            if all(ok for _, ok, _ in checks):
                passed += 1
                act = df[df.geothermal_active]
                sound = (
                    act.brine_cooling_C.max() <= 40.001
                    and act.flow_per_doublet_m3h.between(50, 300).all()
                    and (df.heating_demand_MW - df.heating_delivered_MW).max() <= 1e-3
                    and (df.cooling_demand_MW - df.cooling_delivered_MW).max() <= 1e-3
                )
                if not sound:
                    bad_pass.append(v)
            else:
                failed += 1
        except RuntimeError:
            no_point += 1
        except Exception as e:
            crashes.append((v, repr(e), traceback.format_exc()[-300:]))
    rec(s, "designs simulated", "INFO", f"{len(designs)}: {passed} pass, {failed} fail, {no_point} no well operating point")
    rec(s, "no unhandled crashes", "PASS" if not crashes else "FAIL", crashes[:3])
    rec(s, "every PASS is physically within limits", "PASS" if not bad_pass else "FAIL", bad_pass[:3])


GOOD = dict(n_doublets=3, well_depth_m=3000, hex_ua_kw_per_k=1600, esp_stages=60, backup_boiler_mw=35, electric_chiller_mw=12, absorption_chiller_mw=12)
SCRIPT = [
    "this is not json at all",
    '{"reasoning": "truncated", "design": {"n_doublets": 3',
    '{"reasoning": "no design"}',
    '{"reasoning": "finish too soon", "action": "finish"}',
    '{"reasoning": "a list", "design": [1, 2, 3]}',
    json.dumps(dict(reasoning="out of range", design=dict(GOOD, n_doublets=9))),
    json.dumps(dict(reasoning="text value", design=dict(GOOD, esp_stages="many"))),
    '{"reasoning": null, "design": {"n_doublets": NaN}}',
    json.dumps(dict(reasoning="good", design=GOOD)),
    json.dumps(dict(reasoning="repeat", design=GOOD)),
    json.dumps(dict(reasoning="deeper", design=dict(GOOD, well_depth_m=3300, esp_stages=45))),
    '{"reasoning": "done", "action": "finish"}',
]


def section_mock_llm():
    s = "8 LLM loop with scripted replies, no real model"
    restore()
    state = {"i": 0}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            reply = SCRIPT[min(state["i"], len(SCRIPT) - 1)]
            state["i"] += 1
            body = json.dumps({"message": {"content": reply}}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    p = run_py(["agent.py", "--host", f"http://127.0.0.1:{port}", "--model", "mock", "--iterations", "14"], "agent_mock")
    server.shutdown()
    rec(s, "bad replies do not crash the loop", "PASS" if p.returncode == 0 else "FAIL", p.stderr[-800:] if p.returncode else "exit 0")
    log = (SUB / "agent_log.md").read_text(encoding="utf-8") if (SUB / "agent_log.md").exists() else ""
    rec(s, "repeat rejected without simulation", "PASS" if "repeat rejected" in log else "FAIL")
    rec(s, "schema errors reported", "PASS" if "rejected by schema check" in log else "FAIL")
    if p.returncode == 0:
        j = json.loads((SUB / "agent_best_design.json").read_text(encoding="utf-8"))
        rec(s, "mock agent best design", "INFO", j["co2_t_per_year"])
    restore()
    before = digest(SUB / "agent_best_design.json")
    p = run_py(["agent.py", "--host", "http://127.0.0.1:9", "--iterations", "2"], "agent_unreachable")
    kept = digest(SUB / "agent_best_design.json") == before
    rec(s, "unreachable LLM exits cleanly and keeps results", "PASS" if p.returncode == 0 and kept else "FAIL", p.stdout[-200:])
    restore()


def section_repo():
    s = "9 Repo and documentation"
    restore()

    def git(*args):
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace").stdout

    remote = git("branch", "-r")
    rec(s, "backup-v1-working branch cited by README exists", "PASS" if "backup-v1-working" in remote else "FAIL")
    if "origin/development" in remote:
        changed = sorted(git("diff", "--name-only", "origin/development", "--", "src", "unit_test").split())
        rec(s, "src and unit_test files changed against development", "INFO", changed)
        listed = {
            "src/gemini_simulator/registry.py",
            "src/gemini_simulator/well_flow.py",
            "src/gemini_model/heatpump/heatpump_basic.py",
            "unit_test/gemini_model/heatpump/test_heatpump_basic.py",
        }
        extra = sorted(set(changed) - listed)
        rec(s, "only the changes the README documents", "PASS" if not extra else "FAIL", extra)
    src = (SUB / "agent.py").read_text(encoding="utf-8")
    rec(s, "agent.py docstring matches 18 checks", "FAIL" if "16 checks" in src else "PASS", "docstring says 16 checks" if "16 checks" in src else "")
    pm_src = (SUB / "plant_model.py").read_text(encoding="utf-8")
    readme = (SUB / "README.md").read_text(encoding="utf-8")
    dangling = "Known GEMINI issues" in pm_src and "Known GEMINI issues" not in readme
    rec(s, "plant_model.py README section reference exists", "FAIL" if dangling else "PASS", "cites README section Known GEMINI issues" if dangling else "")
    for name in ("run_doublet_plant_example.py", "run_parallel_hex_plant_example.py"):
        p = subprocess.run([sys.executable, str(ROOT / "simulator_examples" / name)], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300, env=dict(os.environ, PYTHONPATH=str(ROOT / "src"), MPLBACKEND="Agg"))
        rec(s, f"example {name}", "PASS" if p.returncode == 0 else "FAIL", p.stderr[-300:] if p.returncode else "exit 0")
    restore()


def corrected_table(pm, roughness, fix_injector):
    original = pm.build_project

    def table(design, a):
        captured = []

        def capture(p):
            for w in ("producer_well", "injector_well"):
                p["components"][w]["parameters"]["roughness_m"] = roughness
            solver = original(p)
            captured.append(solver)
            return solver

        pm.build_project = capture
        try:
            rows = pm.build_well_table(design, a)
        finally:
            pm.build_project = original
        if not fix_injector:
            return rows
        by_q = {}
        for solver in captured:
            try:
                by_q[solver.get_output("producer_well", "flow_rate")] = solver.get_output("filter_b", "pressure_out_pa")
            except Exception:
                continue
        z = design.well_depth_m
        rho = pm.BRINE_DENSITY
        pi = rows[0]["pi_m3h_bar"] if rows else 1.0
        for r in rows:
            q = r["q_m3h"]
            f, v, _ = swamee_jain(rho, pm.BRINE_VISCOSITY, q / 3600, a.tubing_id_m, roughness)
            fric = f * z / a.tubing_id_m * rho * v * v / 2
            need = (pm.reservoir_pressure(z) + q / pi) * 1e5 - rho * 9.81 * z + fric
            hyd = q / 3600 * max(0.0, need - by_q[q])
            r["inj_hydraulic_w"] = hyd
            r["inj_pump_w"] = hyd / pm.PUMP_EFFICIENCY
        return rows

    return table


def section_impact(pm, dp):
    s = "10 Impact of corrected injector and roughness"
    restore()
    dates, H, C = dp.load_demand(SUB)
    a = pm.Assumptions()
    design, _ = submitted(pm)
    original = dp.build_well_table
    rows = []

    def evaluate(d, roughness, fix):
        dp.build_well_table = corrected_table(pm, roughness, fix)
        try:
            df = dp.Plant(d, a).simulate_year(dates, H, C)
        except RuntimeError as e:
            return dict(design=d.label(), roughness_m=roughness, injector_fixed=fix, feasible=False, note=str(e))
        finally:
            dp.build_well_table = original
        checks = dp.validate(df, d)
        m = df.net_elec_MW.sum() * 24
        return dict(
            design=d.label(),
            depth=d.well_depth_m,
            stages=d.esp_stages,
            roughness_m=roughness,
            injector_fixed=fix,
            feasible=all(ok for _, ok, _ in checks),
            failed="; ".join(n for n, ok, _ in checks if not ok),
            co2_t=round(df.co2_t.sum(), 1),
            net_elec_MWh=round(m, 0),
            esp_MWh=round(df.esp_elec_MW.sum() * 24, 0),
            inj_MWh=round(df.inj_pump_elec_MW.sum() * 24, 0),
            geo_share=round(100 * (df.heat_hex_MW + df.heat_hp_MW).sum() / (df.heat_hex_MW + df.heat_hp_MW + df.heat_produced_gas_MW + df.heat_backup_boiler_MW).sum(), 1),
            max_flow=round(df.flow_per_doublet_m3h.max(), 1),
            min_flow=round(df[df.geothermal_active].flow_per_doublet_m3h.min(), 1),
        )

    base = evaluate(design, 0.01, False)
    close_check(s, "baseline wrapper reproduces 6626", base.get("co2_t"), 6626.4, 0.2)
    for rough, fix in ((0.01, True), (4.6e-5, False), (4.6e-5, True)):
        r = evaluate(design, rough, fix)
        rows.append(r)
        rec(s, f"submitted design, roughness {rough} m, injector fixed {fix}", "INFO", r)
    for rough in (0.01, 4.6e-5):
        for depth in (2500.0, 2800.0, 3000.0, 3200.0, 3300.0, 3500.0):
            for stages in (15, 30, 45, 60):
                d = pm.Design(n_doublets=3, well_depth_m=depth, hex_ua_w_per_k=2000e3, esp_stages=stages, backup_boiler_mw=35, electric_chiller_mw=15, absorption_chiller_mw=8)
                rows.append(evaluate(d, rough, True))
    t = pd.DataFrame(rows)
    t.to_csv(OUT / "impact.csv", index=False)
    for rough in (0.01, 4.6e-5):
        g = t[(t.roughness_m == rough) & (t.feasible == True) & (t.injector_fixed == True) & t.depth.notna()]  # noqa: E712
        for depth, sub in g.groupby("depth"):
            b = sub.sort_values("co2_t").iloc[0]
            rec(s, f"best with fix, roughness {rough}, {depth:.0f} m", "INFO", f"{b.co2_t} t, stages {b.stages}, ESP {b.esp_MWh} MWh, injector {b.inj_MWh} MWh")


def write_report():
    df = pd.DataFrame(RESULTS)
    df.to_csv(OUT / "results.csv", index=False)
    counts = df.status.value_counts().to_dict()
    lines = [f"# Hackathon verification on {sys.platform}, Python {sys.version.split()[0]}", "", f"Totals: {counts}", "", f"Runtimes (s): {RUNTIMES}", ""]
    for section, group in df.groupby("section", sort=False):
        lines += [f"## {section}", "", "| Status | Check | Detail |", "|---|---|---|"]
        for _, r in group.iterrows():
            detail = r.detail.replace("|", "/").replace("\n", " ")[:500]
            lines.append(f"| {r.status} | {r['name']} | {detail} |")
        lines.append("")
    text = "\n".join(lines)
    (OUT / "report.md").write_text(text, encoding="utf-8")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(text)
    print(text, flush=True)
    return counts.get("FAIL", 0)


def guarded(fn, *args):
    try:
        fn(*args)
    except Exception:
        rec(fn.__name__, "section crashed", "FAIL", traceback.format_exc()[-1200:])


def main():
    guarded(section_repro)
    guarded(section_claims)
    guarded(section_offline)
    guarded(section_optimize)
    restore()
    pm, dp = load_model()
    guarded(section_validator, pm, dp)
    guarded(section_physics, pm, dp)
    guarded(section_fuzz, pm, dp)
    guarded(section_mock_llm)
    guarded(section_repo)
    guarded(section_impact, pm, dp)
    plots = SUB / "plots"
    if plots.exists():
        shutil.copytree(plots, OUT / "plots", dirs_exist_ok=True)
    sys.exit(1 if write_report() else 0)


if __name__ == "__main__":
    main()
