#!/usr/bin/env python3
"""
Държавен радар – автоматичен списък на новите законопроекти от parliament.bg.

Тегли от публичния API на Народното събрание (https://www.parliament.bg/api/v1/)
законопроектите, разпределени към всички постоянни комисии, и записва най-новите
в data/parliament_bills.json: дата, сигнатура, заглавие, вносители, комисии, връзки.

Това е фактически списък без оценка. Оценката („кой плаща / кой печели“,
сигнали, гласувания) се добавя в bills.json след преглед от редактор.
Ако API-то не отговори, старият файл се запазва.
"""
import json
import re
import time
import datetime as dt
from pathlib import Path

import requests

DATA = Path(__file__).parent / "data"
API = "https://www.parliament.bg/api/v1/"
SITE = "https://www.parliament.bg"
KEEP_DAYS = 90      # колко назад да се пазят законопроекти
MAX_BILLS = 60      # най-много в списъка
DETAIL_LIMIT = 60   # най-много заявки за подробности
HEADERS = {"User-Agent": "darzhaven-radar/1.0 (+github.com)", "Accept": "application/json"}


def get(path, tries=4):
    """Заявка с повторни опити: parliament.bg понякога не отговаря от първия път."""
    last = None
    for i in range(tries):
        try:
            r = requests.get(API + path, headers=HEADERS, timeout=(30, 90))
            r.raise_for_status()
            return r.json()
        except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as e:
            last = e
            if isinstance(e, requests.HTTPError) and e.response is not None and e.response.status_code < 500:
                break
            time.sleep(10 * (i + 1))
    raise last


def as_list(js):
    """Отговорът може да е списък или обект със списък вътре."""
    if isinstance(js, list):
        return js
    if isinstance(js, dict):
        for v in js.values():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return v
    return []


def parse_date(s):
    if not s:
        return None
    s = str(s).strip()
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%d.%m.%Y", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return dt.datetime.strptime(s[:len(dt.datetime.now().strftime(fmt))], fmt).date()
        except ValueError:
            continue
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return dt.date(int(m[1]), int(m[2]), int(m[3]))
    return None


def person_name(p):
    """Име на вносител от запис в imp_list, без да предполагаме точните полета."""
    names = [str(p[k]).strip() for k in sorted(p) if re.search(r"Name\d?$", k) and p[k]]
    name = " ".join(n for n in names if n)
    if not name:
        for k in ("A_ns_CL_value", "A_ns_C_Name", "name", "Name"):
            if p.get(k):
                name = str(p[k]).strip()
                break
    if not name:
        vals = [str(v).strip() for k, v in sorted(p.items()) if isinstance(v, str) and v.strip() and not k.lower().endswith(("id", "date"))]
        name = vals[0] if vals else ""
    party = p.get("A_ns_CL_value_short")
    return f"{name} ({party})" if name and party else name


def main():
    log = []
    out_path = DATA / "parliament_bills.json"
    meta_path = DATA / "meta.json"
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    try:
        committees = as_list(get("coll-list/bg/3"))
        if not committees:
            raise ValueError("празен списък с комисии")
        bills = {}
        for c in committees:
            cid = c.get("A_ns_C_id")
            if not cid:
                continue
            try:
                for b in as_list(get(f"com-acts/bg/{cid}/1")):
                    bid = b.get("L_Act_id")
                    if not bid:
                        continue
                    e = bills.setdefault(bid, {"id": bid, "title": b.get("L_ActL_title"), "sign": b.get("L_Act_sign"),
                                               "date": None, "committees": [], "session": b.get("L_SesL_value")})
                    d = parse_date(b.get("L_Act_date"))
                    if d:
                        e["date"] = d.isoformat()
                    cname = c.get("A_ns_CL_value")
                    if cname and cname not in e["committees"]:
                        e["committees"].append(cname)
            except Exception as ex:
                log.append(f"комисия {cid}: {type(ex).__name__}")
            time.sleep(0.3)

        cutoff = (dt.date.today() - dt.timedelta(days=KEEP_DAYS)).isoformat()
        recent = sorted((b for b in bills.values() if b["date"] and b["date"] >= cutoff),
                        key=lambda b: b["date"], reverse=True)[:MAX_BILLS]

        old = {}
        if out_path.exists():
            try:
                old = {b["id"]: b for b in json.loads(out_path.read_text(encoding="utf-8")).get("bills", [])}
            except Exception:
                old = {}

        details = 0
        debug_sample = None
        for b in recent:
            prev = old.get(b["id"]) or {}
            if details >= DETAIL_LIMIT:
                for k in ("submitters", "withdrawn", "pdf", "stage", "adopted", "dv"):
                    if k in prev:
                        b[k] = prev[k]
            else:
                details += 1
                try:
                    d = get(f"bill/{b['id']}")
                    d = d if isinstance(d, dict) else {}
                    subs = [n for n in (person_name(x) for x in (d.get("imp_list") or []) + (d.get("imp_list_min") or [])) if n]
                    b["submitters"] = subs
                    b["withdrawn"] = bool(d.get("withdrawn"))
                    folder = d.get("A_ns_folder")
                    files = d.get("file_list") or []
                    fn = str(files[0].get("FILENAME") or "") if files else ""
                    if fn and folder:
                        b["pdf"] = f"{SITE}/bills/{folder}/{fn}"
                    elif b.get("sign") and folder:
                        b["pdf"] = f"{SITE}/bills/{folder}/{b['sign']}.pdf"
                    # Етап
                    dv_iss, dv_year = d.get("L_Act_dv_iss"), d.get("L_Act_dv_year")
                    b["adopted"] = bool(dv_iss)
                    b["dv"] = f"ДВ бр. {dv_iss}/{dv_year}" if dv_iss else None
                    hall = d.get("steno_hall") or []
                    if b["withdrawn"]:
                        b["stage"] = "Оттеглен"
                    elif dv_iss:
                        b["stage"] = f"Приет и обнародван ({b['dv']})"
                    elif d.get("L_Act_veto"):
                        b["stage"] = "Вето на президента"
                    elif hall:
                        b["stage"] = "Обсъждан в пленарна зала"
                    elif d.get("stan_list") or d.get("stan_list2"):
                        b["stage"] = "Доклад на комисия"
                    else:
                        b["stage"] = "Внесен, в комисии"
                    for dl in d.get("dist_list") or []:
                        cn = dl.get("A_ns_CL_value")
                        if cn and cn not in b["committees"]:
                            b["committees"].append(cn)
                    if debug_sample is None and (hall or d.get("activity")):
                        debug_sample = {"bill_id": b["id"], "activity": (d.get("activity") or [])[:3] if isinstance(d.get("activity"), list) else d.get("activity"),
                                        "steno_hall": hall[:2] if isinstance(hall, list) else hall,
                                        "imp_list_min": (d.get("imp_list_min") or [])[:2], "stan_list": (d.get("stan_list") or [])[:1]}
                except Exception as ex:
                    for k in ("submitters", "withdrawn", "pdf", "stage", "adopted", "dv"):
                        if k in prev:
                            b[k] = prev[k]
                    log.append(f"законопроект {b['id']}: {type(ex).__name__}")
                time.sleep(0.3)
            # Вносителят по номера (сигнатурата) на законопроекта, когато API не го дава:
            # 52-602-… = Министерски съвет, 52-653-… = общ законопроект на комисия, 52-654-… = народни представители
            sign = str(b.get("sign") or "")
            if not b.get("submitters"):
                if "-602-" in sign:
                    b["submitters"] = ["Министерски съвет"]
                elif "-653-" in sign:
                    b["submitters"] = ["Общ законопроект на комисия (обединява няколко проекта)"]
            b["url"] = f"{SITE}/bg/bills/ID/{b['id']}"

        if not recent:
            raise ValueError("няма законопроекти за последните %d дни – вероятно форматът на API е променен" % KEEP_DAYS)
        new_ids = [b["id"] for b in recent if b["id"] not in old]
        out = {"checked": now, "source": API, "bills": recent}
        if debug_sample is not None:
            out["debug_sample"] = debug_sample  # за диагностика на липсващи вносители
        out_path.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        log.insert(0, f"parliament.bg: {len(recent)} законопроекта за последните {KEEP_DAYS} дни, нови: {len(new_ids)}")
    except Exception as ex:
        log.insert(0, f"parliament.bg: грешка ({type(ex).__name__}: {str(ex)[:120]}); старият списък е запазен")

    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["parliament_checked"] = now
    meta["auto_log"] = ([f"{now}: {line}" for line in log[:10]] + meta.get("auto_log", []))[:50]
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n".join(log))


if __name__ == "__main__":
    main()
