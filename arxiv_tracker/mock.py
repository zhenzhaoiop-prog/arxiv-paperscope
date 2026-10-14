"""Deterministic offline fixtures. NEVER a fallback for real AI failures."""
from __future__ import annotations

from .storage import encode
from .scoring import DIMENSIONS
from .summary import SUMMARY_SECTIONS, DEEP_SECTIONS


def mock_papers(day):
    from datetime import timedelta
    published = (day - timedelta(days=1)).isoformat()
    timestamp = day.isoformat()
    examples = [
        ("9999.00001", "A new heavy-fermion compound under pressure",
         "We report growth of single crystals of a new heavy-fermion compound. Resistivity and specific heat reveal a pressure-tuned phase transition. The pairing symmetry remains unresolved."),
        ("9999.00002", "Electronic order in a kagome material",
         "A kagome single crystal exhibits charge order and magnetotransport anomalies. The observations suggest competing electronic orders; time-reversal symmetry breaking is not established."),
        ("9999.00003", "A theoretical model for optical waveguides",
         "We calculate optical propagation in a classical waveguide with no evidence for a quantum material application."),
    ]
    papers = []
    for aid, title, abstract in examples:
        version = 2 if aid == "9999.00001" and day.day % 2 else 1
        key = f"{aid}v{version}"
        papers.append({"key": key, "id": aid, "version": version, "title": title,
                       "abstract": abstract, "authors": ["Mock Author (虚构测试数据)"],
                       "published": published, "updated": timestamp,
                       "categories": ["cond-mat.str-el"], "primary_category": "cond-mat.str-el",
                       "doi": "", "url": f"https://arxiv.org/abs/{key}", "pdf_url": f"https://arxiv.org/pdf/{key}"})
    return papers


class MockClient:
    def __init__(self, cfg, budget):
        self.cfg, self.budget, self.calls = cfg, budget, []

    def generate(self, key, stage, messages, validator):
        import json
        self.calls.append((key, stage))
        spec = self.cfg["deepseek"]["stages"][stage]
        request_id, started = self.budget.reserve(key, stage, spec["model"], messages, spec["max_tokens"])
        if stage in ("screening", "review"):
            score = 90 if "00001" in key else 76 if "00002" in key else 20
            category = self.cfg["research"]["interests"][3 if "00001" in key else 1]["name"]
            value = {"paper_key": key, "dimensions": {d: {"score": score, "reason": "Mock：用于流程验证，非真实科研判断。"} for d in DIMENSIONS},
                     "categories": [category] if score >= 40 else [], "new_material": "00001" in key,
                     "recommendation": "Mock：新材料、电子有序或低温实验相关，需真实API复核。"}
        else:
            user = json.loads(messages[-1]["content"])
            sections = DEEP_SECTIONS if stage == "deep" else SUMMARY_SECTIONS
            heavy = "00001" in key
            quote_material = ("We report growth of single crystals of a new heavy-fermion compound."
                              if heavy else "A kagome single crystal exhibits charge order and magnetotransport anomalies.")
            quote_result = ("Resistivity and specific heat reveal a pressure-tuned phase transition."
                            if heavy else "The observations suggest competing electronic orders; time-reversal symmetry breaking is not established.")
            quote_open = "The pairing symmetry remains unresolved." if heavy else quote_result
            def entry(kind, text, quote=None):
                return {"kind": kind, "text": "Mock演示：" + text,
                        "evidence": [{"quote": quote, "page": None}] if quote else []}
            background = entry("AI推测", "可围绕压力调控与竞争有序态探讨强关联电子的相变机制。" if heavy
                               else "可围绕笼目结构中的电子有序与输运异常开展研究。")
            material = entry("实验事实", "摘要报告新型重费米子化合物的单晶生长，但未给出化学式和空间群。" if heavy
                             else "研究对象为笼目单晶；摘要未给出具体化学式和空间群。", quote_material)
            methods = entry("实验事实", "采用电阻率和比热测量研究压力调控相变。" if heavy
                            else "报告磁输运异常；测量温区、磁场范围和样品制备条件未报告。", quote_result if heavy else quote_material)
            results = entry("实验事实" if heavy else "作者解释", "电阻率与比热支持压力调控相变；摘要没有可引用的转变温度和压力数值。" if heavy
                            else "作者认为电子有序之间可能存在竞争，但未确立时间反演对称性破缺。", quote_result)
            novelty = entry("作者解释", "潜在创新在于新化合物单晶及其压力调控响应，需全文确认。" if heavy
                            else "潜在价值在于电子有序与磁输运现象的联系，机制尚需证据。", quote_material)
            comparison = entry("未报告", "所给摘要未提供已有材料体系的定量对照，无法判定是否优于既有研究。")
            unresolved = entry("作者解释", "配对对称性仍未解决。" if heavy else "时间反演对称性破缺尚未确立。", quote_open)
            next_step = entry("AI推测", "建议优先核查晶体生长与压力条件，补充低温比热、磁化率和输运对照。" if heavy
                              else "建议结合结构表征、磁输运和独立对称性探测区分竞争有序态。")
            entries = ([background, material, methods, results, novelty, comparison, unresolved, next_step]
                       if stage == "summary" else [background, material, methods, results, unresolved, novelty, comparison, next_step])
            value = {"paper_key": key, "chinese_title": "Mock演示：" + ("新型重费米子化合物的压力调控研究" if heavy else "笼目材料中的电子有序"),
                     "sections": {s: [e] for s, e in zip(sections, entries)}}
        usage = {"prompt_tokens": 800 if stage in ("screening", "review") else 1500,
                 "prompt_cache_hit_tokens": 100, "completion_tokens": 350 if stage in ("screening", "review") else 1000,
                 "completion_tokens_details": {"reasoning_tokens": 100 if spec["thinking"] else 0}}
        self.budget.settle(request_id, spec["model"], started, usage)
        return validator(json.loads(encode(value)))
