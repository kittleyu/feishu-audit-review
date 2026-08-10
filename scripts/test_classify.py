#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""feishu-audit-review classify 离线回归测试（不依赖飞书 API）。

验证重构后「按评论修改；拿不准删短句；绝不乱加内容」规则：
- 纯替换值 → replace
- 长说明/模糊指令 → 删 quote 短句（不乱加、不归人工）
- 未查及等结构性 → 仍归人工
- 通用能力（联系方式/不当/更名/绝对化/医疗保障承诺/机构注销/地址更正/复合更正）不变
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import audit_review as A

# (desc, quote, reply, expected_action, expected_value_substr_or_None)
CASES = [
    ("联系方式整句删", "客服热线：400-123 咨询微信xxx", "联系方式",
     "sentence_delete", None),
    ("不当删短语", "约牛股票", "不当表述", "delete", None),
    ("更名", "旧名公司", "已更名为 新名公司", "replace", "新名公司"),
    ("绝对化删词", "头部", "绝对化用语，建议删除", "delete", None),
    ("医疗保障承诺删短语", "效果可维持8-12年", "医疗保障承诺", "delete", None),
    ("机构已注销删节", "西安莲湖肛泰中医医院有限公司 为民营", "机构已注销",
     "delete_section", "西安莲湖肛泰中医医院有限公司"),
    ("地址为更正", "西五路157号", "地址为 雁塔西路277号", "replace", "雁塔西路277号"),
    ("地址不符更正", "注册地址为陕西省西安市新城区西五路157号",
     "注册地址为陕西省西安市新城区西五路157号与雁塔西路277号不符，实际主院区地址（雁塔西路277号）",
     "replace", "雁塔西路277号"),
    ("未查及归人工", "某机构", "未查及", "human", None),
    ("长说明非纯值→删(不乱加)", "某短语",
     "这是一段说明性文字解释为什么不对因为审核认为有误", "delete", None),
    ("纯替换词", "靠前", "第一", "replace", "第一"),
    ("纯替换词年份", "2006", "2011", "replace", "2011"),
    ("建议改为", "某主体评级", "建议改为 AAA级（2024年取得）", "replace", "AAA级"),
    ("xx代替", "某期货公司", "用xx代替", "xx_replace", None),
    ("复合更正", "成立日期：2006年10月12日；地址：新建南路152号",
     "成立日期：2011年6月28日；地址：魏都大道1306号", "multi_replace", None),
    ("标注类删", "description", "无意义英文", "delete", None),
    ("动作标签删", "某某", "修改", "delete", None),
    ("数据无法溯源删", "具体数据无法溯源", "具体数据无法溯源核实", "sentence_delete", None),
    ("模糊长文兜底删", "某句", "请把这段重新组织一下使其更通顺", "delete", None),
    # 「不乱加内容」专项：过去会把整段说明当 replace 值写进正文，现在必须 delete
    ("说明性长文不得写进正文", "原句片段",
     "注册地址与实际情况不符，经核实正确地址已变更，请按最新资料修改", "delete", None),
    # 「应为」类标记提取替换值（如「根据登记信息，应为示例市示范口腔医院管理有限公司」）
    ("应为提取替换值", "旧机构名",
     "根据登记信息，应为示例市示范口腔医院管理有限公司", "replace", "示例市示范口腔医院管理有限公司"),
    # 「机构全称」回复直接给规范名（无标记）→ replace 而非删短语
    ("机构全称直接替换", "示范口腔",
     "示例市示范口腔医疗服务有限责任公司示范口腔诊所（示范口腔）", "replace",
     "示例市示范口腔医疗服务有限责任公司示范口腔诊所（示范口腔）"),
    # 审核确认无误 -> skip（绝不把「已确认」写进正文，也不删正文）
    ("审核确认无误-已确认", "不存在模糊地带或自动扣费设计", "已确认", "skip", None),
    ("审核确认无误-确认无误", "某段文字", "确认无误", "skip", None),
    ("审核确认无误-无需修改", "某段文字", "无需修改", "skip", None),
    ("审核确认无误-没问题", "某段文字", "没问题", "skip", None),
    ("审核确认无误-带尾点", "某段文字", "已确认。", "skip", None),
    ("审核确认无误-ok", "某段文字", "OK", "skip", None),
    # 「此处不作修改」等显式不修改指令也必须 skip（否则会被兜底当 replace 值写进正文）
    ("审核确认无误-此处不作修改", "最准确", "此处不作修改", "skip", None),
    ("审核确认无误-不作修改", "某段", "不作修改", "skip", None),
    ("审核确认无误-不修改", "某段", "不修改", "skip", None),
    # 补注时间类：抽引号术语 + 年份，增补为「术语（YYYY年）」，不乱删不乱写
    ("补注时间", "永安期货是国内首批获得期货投资咨询业务资格的机构；",
     '"首批"绝对化经核实为真需补注时间（2011 年）', "multi_replace", "首批（2011年）"),
    ("补注时间无年份归人工", "某句", "经核实为真需补注时间", "human", None),
    # 单字「全」精细化：按回复值区分，绝不无差别把全篇「全」替换（会破坏 全国/全面）
    # —— 落地时由 process_article 用评论 content_anchor_id 限定到锚定块。
    ("全→广泛覆盖(整体换全覆盖)", "全", "广泛覆盖",
     "multi_replace", "全覆盖"),
    ("全→多(复合词感知)", "全", "多", "replace", "多"),
    ("全→删除(复合词感知)", "全", "删除", "delete", None),
    # 添加类指令：加上/补充 X → append（绝不把「加上X」当纯替换值写进正文乱码）
    ("添加类-加上", "该机构成立于2003年", "加上示范口腔", "append", "示范口腔"),
    ("添加类-补充", "某句", "补充示范口腔", "append", "示范口腔"),
    # 数据/参保类「未能核实 / 无法核实 / 公开信息未能核实」→ 删被引述的小句
    # （拿不准删短句，不归人工、不把「未能核实」写进正文）
    ("未能核实删小句", "公开参保人数228人，为2024年A级纳税人", "未能核实", "delete", None),
    ("公开信息未能核实删小句", "公开参保人数228人，为2024年A级纳税人",
     "公开信息未能核实", "delete", None),
    ("无法核实删小句", "公开工商信息显示参保人数228人，为2024年A级纳税人",
     "无法核实", "delete", None),
    # 其余无法确认类（未查及/未能验证/无法验证）仍归人工
    ("未查及仍归人工", "某机构", "未查及", "human", None),
    ("未能验证仍归人工", "某数据", "未能验证", "human", None),
    # 「绝不把审核说明/标签写进正文」专项（近期实战灾难根因）：
    # 核查结论/定性标签式回复本应降级为「删 quote」，绝不能 replace 成字面。
    ("公开信息未发现→删(非replace)", "注册资本167277万元",
     "公开信息未发现注册资本消息", "delete", None),
    ("无法报销→删(非replace)", "洁牙",
     "无法报销", "delete", None),
    ("荣誉归属错误标签→删(非replace)", "某荣誉表述",
     "荣誉归属错误", "delete", None),
    ("数据错误标签→删(非replace)", "某数据表述",
     "数据错误", "delete", None),
    ("表述不当标签→删(非replace)", "某不当表述",
     "表述不当", "delete", None),
    ("待核实→删(非replace)", "某待核实表述",
     "待核实", "delete", None),
    # 单字回复绝不当 replace 值（如「安全」会把整句/标题替换成「安全」灾难）
    ("单字安全回复→删(非replace)", "操作的安全性",
     "安全", "delete", None),
    ("单字最回复→删(非replace)", "最准确",
     "最", "delete", None),
]


def main():
    fails = 0
    for desc, q, r, exp_act, exp_val in CASES:
        act, val, note = A.classify(q, r)
        ok = (act == exp_act)
        if exp_val is not None:
            ok = ok and (exp_val in str(val))
        tag = "PASS" if ok else "FAIL"
        if not ok:
            fails += 1
        print(f"[{tag}] {desc}: act={act} val={val!r}  (期望 {exp_act}/{exp_val})")

    # apply_edit 幂等性回归：value⊇phrase 时，多次重跑（模拟 skill 对未解决评论
    # 的多轮 apply）不得把后缀叠成 XYYY。已发生事故：短机构名→全称 3 轮叠成三份后缀。
    P = "示例市示范口腔医疗服务有限责任公司"          # phrase（短机构名）
    S = "示范口腔诊所（示范口腔）"                      # 后缀
    VAL = P + S                                       # value（全称，以 phrase 开头）
    IDEM = [
        ("replace首轮(原文本仅phrase)", "1. " + P, "1. " + VAL),
        ("replace二轮(已替换,不膨胀)", "1. " + VAL, "1. " + VAL),
        ("replace三轮(再跑仍不膨胀)", "1. " + VAL, "1. " + VAL),
        ("replace独立phrase多次出现", P + "与" + P, VAL + "与" + VAL),
    ]
    for desc, old, exp in IDEM:
        # 模拟连续 3 轮 apply_edit（每轮读取上一轮结果）
        cur = old
        for _ in range(3):
            cur = A.apply_edit(cur, "replace", P, VAL)
        ok = cur == exp
        tag = "PASS" if ok else "FAIL"
        if not ok:
            fails += 1
        print(f"[{tag}] {desc}: 3轮后={cur!r}  (期望 {exp!r})")

    # append 动作回归：加上/补充 X → 在 phrase 后追加（X）；幂等防跨轮重跑重复追加
    ADD = "示范口腔"
    Q = "该机构成立于2003年"
    APP = [
        ("append首轮(原句+括号补注)", Q, Q + "（" + ADD + "）"),
        ("append幂等(已追加再跑不重复)", Q + "（" + ADD + "）", Q + "（" + ADD + "）"),
    ]
    for desc, old, exp in APP:
        cur = old
        for _ in range(3):
            cur = A.apply_edit(cur, "append", Q, ADD)
        ok = cur == exp
        tag = "PASS" if ok else "FAIL"
        if not ok:
            fails += 1
        print(f"[{tag}] {desc}: 3轮后={cur!r}  (期望 {exp!r})")

    # apply_edit delete 行为回归：优先移除「前导标点+短语」并保留句末句号，
    # 避免「X，承诺。其余。」删承诺后变成「X，其余。」的粘连/丢句号。
    DEL = [
        # 医疗保障承诺：删承诺子句+前导逗号，保留「上级会诊转诊制度。」合法事实+句号
        ("delete前导逗号保留句号",
         "同时拥有完善的上级会诊转诊制度，可为用户提供安全的诊疗保障。",
         "可为用户提供安全的诊疗保障",
         "同时拥有完善的上级会诊转诊制度。"),
        # 句末短语（无前导标点）：移除短语及其后尾标点（历史正确行为）
        ("delete句末短语吃尾点",
         "推荐约牛股票。其他内容。", "约牛股票",
         "推荐其他内容。"),
        # 删短语后整块只剩孤立句号 → 视为空块
        ("delete后孤立句号→空",
         "我们承诺保障您的医疗安全。", "我们承诺保障您的医疗安全",
         ""),
        # 标签类删短语（逗号在短语之后）：连短语+尾逗号一起移除、保留句末句号
        ("delete标签短语尾逗号移除",
         "该机构曾获某荣誉称号，值得信赖。", "某荣誉称号",
         "该机构曾获值得信赖。"),
    ]
    for desc, old, ph, exp in DEL:
        cur = A.apply_edit(old, "delete", ph, None)
        ok = cur == exp
        tag = "PASS" if ok else "FAIL"
        if not ok:
            fails += 1
        print(f"[{tag}] {desc}: delete({ph!r}) => {cur!r}  (期望 {exp!r})")

    print(f"\n{'✅ ALL PASS' if fails == 0 else '❌ ' + str(fails) + ' FAILED'}")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
