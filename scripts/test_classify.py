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
    ("医疗保障承诺删", "效果可维持8-12年", "医疗保障承诺", "sentence_delete", None),
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
     "注册地址与实际情况不符，经核实正确地址应为雁塔西路277号", "delete", None),
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
    print(f"\n{'✅ ALL PASS' if fails == 0 else '❌ ' + str(fails) + ' FAILED'}")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
