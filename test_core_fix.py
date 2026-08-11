# -*- coding: utf-8 -*-
"""离线验证 audit_review.py 两个核心 bug 的修复：
   Bug1 #9.4 硬编码截断（尊重真实回复值、不再强加「的」）
   Bug2 带尾标点 quote 做 replace 吞句号
   Bug3 同块多值「靠前家」全局替换并值 → 逐 occurrence 消费
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "scripts"))
import audit_review as ar


def strip_guard(action, phrase, value):
    # 复刻 process_article 里的去尾标点守卫
    if action == "replace" and value and phrase and phrase[-1] in "。！？；" \
            and not value.endswith(phrase[-1]):
        return phrase[:-1]
    return phrase


def test_classify_94():
    cases = [
        ("靠前家上市公司", "较早", ("multi_replace", [("靠前家", "较早")])),
        ("靠前家", "位于行业前列的", ("multi_replace", [("靠前家", "位于行业前列的")])),
        ("靠前家上市公司", "较早上市的期货公司", ("multi_replace", [("靠前家", "较早上市的期货公司")])),
        ("靠前家", "第一", ("multi_replace", [("靠前家", "第一")])),
        ("靠前家", "前列的", ("multi_replace", [("靠前家", "前列的")])),
        ("靠前家", "较早的", ("multi_replace", [("靠前家", "较早的")])),
    ]
    ok = True
    for q, r, expect in cases:
        act, val, note = ar.classify(q, r)
        got = (act, val)
        status = "OK " if got == expect else "FAIL"
        if got != expect:
            ok = False
        print(f"  [{status}] classify({q!r},{r!r}) -> {got}  (期望 {expect})")
    return ok


def test_period_guard():
    cases = [
        ("全覆盖。", "广泛覆盖", "全覆盖"),        # 吞句号修复：剥掉尾标点
        ("全覆盖。", "广泛覆盖。", "全覆盖。"),     # value 自带句号则不剥（防双标点）
        ("头部，", "头部", "头部，"),              # 逗号不在剥离集，保守保留
        ("中泰期货", "中泰证券", "中泰期货"),       # 无尾标点不变
    ]
    ok = True
    for ph, v, expect in cases:
        got = strip_guard("replace", ph, v)
        status = "OK " if got == expect else "FAIL"
        if got != expect:
            ok = False
        print(f"  [{status}] strip_guard({ph!r},{v!r}) -> {got!r}  (期望 {expect!r})")
    return ok


def test_per_occurrence():
    # 复刻 process_article 的 apply 循环（含逐 occurrence 逻辑）
    from collections import defaultdict

    def apply_loop(ops, original):
        new = original
        ops = sorted(ops, key=lambda x: -len(x[1]))
        phrase_groups = defaultdict(list)
        for i, (a, ph, v, n) in enumerate(ops):
            if a == "replace":
                phrase_groups[ph].append(i)
        applied_idx = set()
        for ph, idxs in phrase_groups.items():
            if len(idxs) <= 1:
                continue
            occ, start = 0, 0
            while True:
                pos = new.find(ph, start)
                if pos < 0:
                    break
                if occ < len(idxs):
                    _, _, v, _ = ops[idxs[occ]]
                    new = new[:pos] + v + new[pos + len(ph):]
                    applied_idx.add(idxs[occ])
                    start = pos + len(v)
                else:
                    start = pos + len(ph)
                occ += 1
        for i, (a, ph, v, _) in enumerate(ops):
            if i in applied_idx:
                continue
            new = ar.apply_edit(new, a, ph, v)
        return new

    text = "它是行业靠前家上市公司，也是省内靠前家期货公司。"
    ops = [
        ("replace", "靠前家", "较早的", "n1"),
        ("replace", "靠前家", "位于行业前列的", "n2"),
    ]
    expect = "它是行业较早的上市公司，也是省内位于行业前列的期货公司。"
    got = apply_loop(ops, text)
    ok1 = got == expect
    print(f"  [{'OK ' if ok1 else 'FAIL'}] 同块两值逐次替换 -> {got}")
    if not ok1:
        print(f"       期望 -> {expect}")

    # 单 op 全局替换（无重复 phrase）→ 行为不变：全部替换
    ops2 = [("replace", "靠前家", "前列的", "n")]
    expect2 = text.replace("靠前家", "前列的")
    got2 = apply_loop(ops2, text)
    ok2 = got2 == expect2
    print(f"  [{'OK ' if ok2 else 'FAIL'}] 单 op 全局替换 -> {got2}")

    # 幂等：重新跑一轮（原文已无 靠前家）→ 无变化
    got3 = apply_loop(ops, got)
    ok3 = got3 == got
    print(f"  [{'OK ' if ok3 else 'FAIL'}] 二次重跑幂等 -> {got3}")

    return ok1 and ok2 and ok3


if __name__ == "__main__":
    print("== Bug1 #9.4 classify ==")
    a = test_classify_94()
    print("== Bug2 去尾标点守卫 ==")
    b = test_period_guard()
    print("== Bug3 同块多值逐 occurrence ==")
    c = test_per_occurrence()
    print("\n== 结果 ==")
    print("Bug1:", "PASS" if a else "FAIL")
    print("Bug2:", "PASS" if b else "FAIL")
    print("Bug3:", "PASS" if c else "FAIL")
    sys.exit(0 if (a and b and c) else 1)
