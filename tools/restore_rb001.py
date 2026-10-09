# 一次性恢复脚本：把 RB-001（sangat 词序规则）写回生产 APPDATA rules.json
import json
import re

P = r"C:\Users\junxu.chen\AppData\Roaming\tepat\rules.json"

rule = {
    "id": "RB-001",
    "entry_id": "RB-001",
    "plugin": "regex_match",
    "conf": "medium",
    "conf_trial": "high",
    "status": "trial",
    "desc": "Flags Malay degree-word order: an adjective followed by 'sangat' "
            "(e.g. 'murah sangat') instead of the standard 'sangat' before the "
            "adjective (e.g. 'sangat murah').",
    "note": "Dalam bahasa Melayu standard, 'sangat' diletakkan sebelum kata sifat: "
            "'sangat murah', bukan 'murah sangat'.",
    "source": "LLM-translated from human description (2026-10-09)",
    "examples": {
        "trigger": ["Barang ini murah sangat.", "Harga dia mahal sangat.",
                    "Filem itu menarik sangat."],
        "clear": ["Barang ini sangat murah.", "Harga dia sangat mahal.",
                  "Dia sangat-sangat ingin pergi."],
    },
    "params": {
        "pattern": r"\b(\w+)\s+sangat\b(?!\s+\w)",
        "exceptions": [r"\bsangat[\s-]+sangat\b", r"\bpaling\s+\w+\s+sangat\b"],
        "suggestion": r"sangat \1",
    },
}

d = json.load(open(P, encoding="utf-8"))
assert d.get("rules") == [], f"unexpected existing rules: {[r.get('id') for r in d['rules']]}"
d["rules"].append(rule)
json.dump(d, open(P, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

# 自测
pat = re.compile(rule["params"]["pattern"])
exc = [re.compile(e) for e in rule["params"]["exceptions"]]
for s in rule["examples"]["trigger"]:
    hit = bool(pat.search(s)) and not any(e.search(s) for e in exc)
    print("trigger:", "HIT " if hit else "MISS", s)
for s in rule["examples"]["clear"]:
    hit = bool(pat.search(s)) and not any(e.search(s) for e in exc)
    print("clear:  ", "PASS" if not hit else "FLAG", s)
print("rules in file:", len(json.load(open(P, encoding="utf-8"))["rules"]))
