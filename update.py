#!/usr/bin/env python3
"""
Държавен радар – автоматично обновяване на икономическите данни от Евростат.

Обновява:
  data/indicators.json  – инфлация (ХИПЦ), растеж на БВП, безработица, дефицит, дълг
  data/inflation.json   – годишна инфлация по ХИПЦ за последните 12 месеца
  data/meta.json        – дата на обновяване и дневник

Ако даден източник не отговори, старите стойности се запазват.
Данни, които изискват човешка преценка (обещания, закони, констатации, медии),
НЕ се пипат от този скрипт.
"""
import json
import datetime as dt
from pathlib import Path

import requests

DATA = Path(__file__).parent / "data"
API = "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/"
MONTHS = ["яну", "фев", "мар", "апр", "май", "юни", "юли", "авг", "сеп", "окт", "ное", "дек"]

# id в indicators.json -> заявка към Евростат
SERIES = {
    "hicp": {
        "dataset": "prc_hicp_manr",
        "params": {"geo": "BG", "coicop": "CP00", "unit": "RCH_A"},
        # От 2026 г. Евростат минава към класификацията COICOP 2018; старата серия спира през дек. 2025.
        # Пробват се всички варианти и се взема най-новият.
        "alternatives": [
            ("prc_hicp_manr", {"geo": "BG", "coicop18": "TOTAL", "unit": "RCH_A"}),
            ("prc_hicp_manr", {"geo": "BG", "coicop": "TOTAL", "unit": "RCH_A"}),
            ("prc_hicp_minr", {"geo": "BG", "coicop18": "TOTAL", "unit": "RCH_A"}),
        ],
        "freq": "M",
        "url": "https://ec.europa.eu/eurostat/databrowser/view/prc_hicp_manr/default/table",
    },
    "gdp": {
        "dataset": "namq_10_gdp",
        "params": {"geo": "BG", "unit": "CLV_PCH_SM", "s_adj": "SCA", "na_item": "B1GQ"},
        "freq": "Q",
        "url": "https://ec.europa.eu/eurostat/databrowser/view/namq_10_gdp/default/table",
    },
    "unemployment": {
        "dataset": "une_rt_m",
        "params": {"geo": "BG", "s_adj": "SA", "age": "TOTAL", "sex": "T", "unit": "PC_ACT"},
        "freq": "M",
        "url": "https://ec.europa.eu/eurostat/databrowser/view/une_rt_m/default/table",
    },
    "deficit_2025": {
        "dataset": "gov_10dd_edpt1",
        "params": {"geo": "BG", "na_item": "B9", "sector": "S13", "unit": "PC_GDP"},
        "freq": "A",
        "negate": True,  # Евростат дава салдо (минус = дефицит)
        "url": "https://ec.europa.eu/eurostat/databrowser/view/gov_10dd_edpt1/default/table",
    },
    "debt_2025": {
        "dataset": "gov_10dd_edpt1",
        "params": {"geo": "BG", "na_item": "GD", "sector": "S13", "unit": "PC_GDP"},
        "freq": "A",
        "url": "https://ec.europa.eu/eurostat/databrowser/view/gov_10dd_edpt1/default/table",
    },
}


def fetch_series(dataset, params, last=12):
    """Връща списък [(период, стойност)], подреден по време, от JSON-stat отговор."""
    q = dict(params, lang="EN", lastTimePeriod=str(last))
    r = requests.get(API + dataset, params=q, timeout=60)
    r.raise_for_status()
    js = r.json()
    return parse_jsonstat(js)


def parse_jsonstat(js):
    ids = js["id"]
    sizes = js["size"]
    time_dim = "time" if "time" in ids else ids[-1]
    t_pos = ids.index(time_dim)
    for i, s in enumerate(sizes):
        if i != t_pos and s != 1:
            raise ValueError(f"Неочаквано измерение {ids[i]} с {s} стойности")
    index = js["dimension"][time_dim]["category"]["index"]
    periods = sorted(index.items(), key=lambda kv: kv[1])
    # при всички други измерения с размер 1, позицията е индексът по време
    stride = 1
    for s in sizes[t_pos + 1:]:
        stride *= s
    values = js.get("value", {})
    out = []
    for label, pos in periods:
        v = values.get(str(pos * stride)) if isinstance(values, dict) else values[pos * stride]
        if v is not None:
            out.append((label, float(v)))
    return out


def period_label(p, freq):
    if freq == "M":  # 2026-10
        y, m = p.split("-")[:2]
        return f"{MONTHS[int(m) - 1]} {y}"
    if freq == "Q":  # 2026-Q3
        y, q = p.split("-Q")
        roman = ["I", "II", "III", "IV"][int(q) - 1]
        return f"{roman} тримесечие {y}"
    return f"{p} г."


def main():
    log = []
    ind_path = DATA / "indicators.json"
    indicators = json.loads(ind_path.read_text(encoding="utf-8"))
    by_id = {r["id"]: r for r in indicators}
    changed = False

    for iid, cfg in SERIES.items():
        try:
            last = 12 if cfg["freq"] != "A" else 3
            series, errors = [], []
            for ds, params in [(cfg["dataset"], cfg["params"])] + cfg.get("alternatives", []):
                try:
                    s_ = fetch_series(ds, params, last=last)
                    if s_ and (not series or s_[-1][0] > series[-1][0]):
                        series, used = s_, ds
                except Exception as e2:
                    errors.append(f"{ds}: {type(e2).__name__}")
            if not series:
                raise ValueError("празен отговор " + "; ".join(errors))
            period, value = series[-1]
            row0 = by_id.get(iid) or {}
            if row0.get("period_key") and period < row0["period_key"]:
                log.append(f"{iid}: Евростат дава по-стари данни ({period}) от текущите ({row0['period_key']}) – запазени са текущите")
                continue
            if cfg.get("negate"):
                value = -value
            value = round(value, 2)
            row = by_id.get(iid)
            if row is None:
                log.append(f"{iid}: липсва в indicators.json – пропуснато")
                continue
            new_period = period_label(period, cfg["freq"]) + ", Евростат"
            if row.get("value") != value or row.get("period") != new_period:
                log.append(f"{iid}: {row.get('value')} ({row.get('period')}) -> {value} ({new_period})")
                row["value"] = value
                row["period"] = new_period
                row["period_key"] = period
                row["source"] = "Евростат (автоматично)"
                row["url"] = cfg["url"]
                changed = True
            if iid == "hicp":
                infl = [{"month": p, "value": round(v, 1), "url": cfg["url"]} for p, v in series]
                (DATA / "inflation.json").write_text(json.dumps(infl, ensure_ascii=False, indent=1), encoding="utf-8")
                changed = True
        except Exception as e:  # пазим старите данни
            log.append(f"{iid}: грешка ({type(e).__name__}: {str(e)[:120]}); старата стойност е запазена")

    meta_path = DATA / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    meta["auto_checked"] = now
    if changed:
        ind_path.write_text(json.dumps(indicators, ensure_ascii=False, indent=1), encoding="utf-8")
        meta["updated"] = now
        meta["updated_by"] = "автоматично (Евростат)"
        meta["inflation_label"] = "Годишна инфлация, ХИПЦ (Евростат)"
    meta["auto_log"] = ([f"{now}: {line}" for line in log] + meta.get("auto_log", []))[:50]
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n".join(log) or "Няма промени.")


if __name__ == "__main__":
    main()
