# -*- coding: utf-8 -*-
"""feishu-audit-review · 通用 per-directory 调试模板（BLOCK_FIX 模式）
================================================================

什么时候用本模板（而不是改 skill 核心）：
  当 skill 默认的 classify + apply_edit 在某篇 / 某块上仍产生「灾难性」结果时——
  审核说明写进正文 / 整句删过度删除合法事实 / 单字删除毁文 / 弯引号 no-op /
  粘连丢句号 / 叠字 / classify #9.4 硬编码截断 等——不要去改核心脚本，
  改用本模板对该目录做「定点覆盖 + 其余自动」的精准修正。

本模板是最近 5 个真实 fix_*.py（G5GF / QuYQ / Ohd6 / XSiew / FcvF）的并集提炼，
四人机制覆盖全部历史案例：

  机制 1 · BLOCK_FIX  按 block_id 显式覆盖该块所有自动 op（ohd6/xsiew/fcvf 用）
  机制 2 · OVERRIDE    按 (block_id, 评论quote) 跳过 classify、直接施加精准替换
                         （QuYQ 用；也适合绕过 classify #9.4 硬编码截断，如 G5GF）
  机制 3 · SKIP        按 (block_id, 评论quote) 整条评论跳过（防全文匹配误伤，如单字"安全"）
  机制 4 · TITLE_OVERRIDE  按文章标题整体替换（G5GF 用，绕过标题类评论）

用法：
  python templates/debug_blkfix.py --dir <NODE> --dry       # 只读预览每行改动
  python templates/debug_blkfix.py --dir <NODE>             # 真实写回 + 备份 + 读回校验
  python templates/debug_blkfix.py                          # 用下方默认 NODE
  python scripts/audit_review.py --restore <备份json>        # 回滚

工作原理（与 5 个真实 fix_*.py 完全一致）：
  1. discover_dir 列出目录下所有文章
  2. 仅处理「有未解决评论」的文章（铁律：只改有评论的；绝不点解决评论）
  3. 对每条评论跑 classify(quote, reply)：skip/human 跳过；其余累积到 block_ops[bid]
  4. BLOCK_FIX 覆盖 → OVERRIDE 叠加 → SKIP 排除；空覆盖 [] = 该块归人工不动
  5. apply_edit 应用（短语从长到短），生成 plan
  6. --dry 只打印；否则写回 + 备份 JSON + 逐块读回校验（校验失败会告警）

================================================================
★★★ 你只需编辑下面 ① ~ ④ 四块 ★★★
================================================================

① 目录节点：从飞书 wiki URL 末尾取  https://.../wiki/<NODE>
"""
import sys, io, os, json, datetime, argparse
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))
import audit_review as ar

NODE = 'REPLACE_WITH_YOUR_NODE'   # 改成你的目录 node；或用 --dir 覆盖

# =================================================================
# ② BLOCK_FIX —— 按 block_id 覆盖该块所有自动 op（最常用）
#    条目格式：[ (action, find, repl), ... ]
#      ('delete',  '短语', '')          删除「短语」（默认吞一个尾标点）
#      ('replace', 'find', '')          纯子串移除、**不吃尾标点**（保留句号，防粘连）——最常用
#      ('replace', 'find', 'repl')      替换（防粘连 / 防叠字的整词改写）
#      ('replace', '，前导标点短语', '')  连前导逗号一起移除 + 保留句末句号（医疗保障承诺标配）
#      []                               空列表 = 该块归人工不动（压掉技能自动删，常用于防单字毁文 / 标题割裂）
#
#    灾难 → 写法对照（均来自真实案例，照抄即可）：
#      A. 医疗保障承诺整句删过度 → ('replace', '，为医师诊疗提供安全保障', '')   只删承诺、保合法事实+句号
#      B. 审核说明写进正文(公开信息未发现/无法报销/荣誉归属错误) → ('replace', '，注册资本167277万元', '')  精准删被引述小句
#      C. 联系方式过度删 → ('delete', '、官方客服热线4000013344', '')  只删号码、保渠道列表
#      D. 联系方式整条 item 删 → ('delete', '二是拨打官方400服务热线4000013344咨询；', '')  删整条列表项防断裂
#      E. 防粘连：'行业头部'→'行业前列' → ('replace', '行业头部', '行业前列')
#      F. 防叠字：'全业务流程'→'完整业务流程' → ('replace', '全业务流程', '完整业务流程')
#      G. 弯引号：荣誉删短语须用文档实际弯引号 ""(U+201C/D)；用直引号 " 会 phrase not in old 静默 no-op
#      H. 归人工不动(防单字毁文/标题割裂) → bid: []
#
#    注意「前导标点」：find 串务必带上原文真实的前导逗号/顿号/句号，否则删后
#    会出现「X，其余。」双标点或粘连。find 串里的引号/标点必须与文档逐字符一致
#    （输入法 unicode 归一、弯直引号差异都会导致 no-op，调试时用 codepoint 核对）。
# =================================================================
BLOCK_FIX = {
    # 例：医疗保障承诺只删短语、保事实+句号
    # 'doxcnXXXX': [('replace', '，为医师诊疗提供安全保障', '')],
    # 例：归人工不动（防单字删毁文）
    # 'doxcnYYYY': [],
}

# =================================================================
# ③ OVERRIDE —— 按 (block_id, 评论原始quote) 跳过 classify、直接施加精准替换
#    适用：某条评论 classify 会出错（如 #9.4 硬编码把长回复截坏、把说明当替换值），
#    你想完全接管它。命中后该评论的自动 op 被跳过，改走这里的 (find, repl) 精确 .replace()。
#    例：G5GF 绕过 #9.4："靠前家上市公司"→"较早上市的期货公司"
#        ('doxcnZZZZ', '靠前家上市公司'): ('靠前家上市公司', '较早上市的期货公司'),
#    例：QuYQ 整句扩删（技能删"首选"会留残缺）
#        ('doxcnAAAA', '首选'): ('，是很多怕疼的拔牙患者的首选。', '。'),
# =================================================================
OVERRIDE = {
    # ('doxcnXXXX', '评论quote原文'): ('find', 'repl'),
}

# =================================================================
# ④ SKIP —— 按 (block_id, 评论原始quote) 整条评论跳过（不施加任何改动）
#    适用：该评论若走自动分类会全文匹配误伤同文档其它句子（如单字"安全"命中"误区"段多句）。
#    例：SKIP = {('doxcnBBBB', '安全')}
# =================================================================
SKIP = set()

# =================================================================
# ⑤ TITLE_OVERRIDE —— 按文章标题整体替换（可选，G5GF 用）
#    适用：评论要求改的是页面标题而非正文块。
#    例：TITLE_OVERRIDE = {'旧标题含中泰期货': '新标题含中泰证券'}
# =================================================================
TITLE_OVERRIDE = {}


# ====================== 以下为通用骨架，通常无需改动 ======================
def _expand_phrase(btext, cmt_anchor, phrase, value):
    """可选增强：当 reply 尾部恰等于 phrase 尾部时，扩展短语为 phrase+尾字，
    规避「靠前家」+ reply 只给「第一」时丢尾字（靠前次→第一 变「第一临」）。"""
    if not (cmt_anchor and cmt_anchor in btext and value):
        return phrase
    _ab = btext[cmt_anchor]
    _ap = _ab.find(phrase)
    if _ap < 0:
        return phrase
    _suf = ''
    _j = _ap + len(phrase)
    while _j < len(_ab) and _ab[_j] not in '，。、；：！？\n ）) ':
        _suf += _ab[_j]
        _j += 1
    _best = 0
    for _k in range(1, len(_suf) + 1):
        if value.endswith(_suf[:_k]) and _suf[:_k] and value[:-_k] not in ('', phrase):
            _best = _k
    if _best and (phrase + _suf[:_best]) in _ab:
        return phrase + _suf[:_best]
    return phrase


def main(node, dry=False):
    token = ar.get_token()
    docs = ar.discover_dir(token, node)
    backup = {}
    plan = []
    title_changes = []
    override_apply = {}   # bid -> list of (find, repl)

    for d in docs:
        obj = d['obj']
        title = d.get('title') or ''
        comments = ar.get_doc_comments(token, obj) or []
        if not comments:
            continue  # 铁律：只改有未解决评论的文章
        blocks = ar.get_doc_blocks(token, obj)
        btext = {b.get('block_id'): ar.extract_block_text(b) for b in blocks}
        block_ops = {}

        for cmt in comments:
            p = ar.parse_comment_text(cmt)
            quote = (p.get('quote') or '').strip()
            reply = p['replies'][-1]['text'].strip() if p['replies'] else ''
            if not quote or not reply:
                continue
            cmt_anchor = (cmt.get('extra') or {}).get('content_anchor_id')
            # SKIP：整条评论跳过（防全文匹配误伤）
            if (cmt_anchor, quote) in SKIP:
                continue
            action, value, note = ar.classify(quote, reply)
            if action in ('skip', 'human'):
                continue
            q_clean = quote.replace('**', '').strip()
            phrase = ar.collapse_dup(q_clean) if action == 'replace' else q_clean
            if not phrase:
                continue
            phrase = _expand_phrase(btext, cmt_anchor, phrase, value)
            hits = ar.resolve_hits(btext, phrase, cmt_anchor)
            if not hits:
                continue
            for bid in hits:
                # OVERRIDE：跳过该评论的 classify 自动动作，改走精准替换
                if (bid, quote) in OVERRIDE:
                    override_apply.setdefault(bid, []).append(OVERRIDE[(bid, quote)])
                    continue
                e = block_ops.setdefault(bid, [])
                key = (action, phrase, value)
                if not any(o[:3] == key for o in e):
                    e.append((action, phrase, value, note))

        # BLOCK_FIX 覆盖：按 block_id 显式覆盖该块所有自动 op（空列表=归人工不动）
        for bid, fixes in BLOCK_FIX.items():
            if bid in btext:
                block_ops[bid] = [(a, f, r, 'blockfix') for (a, f, r) in fixes]

        # 应用：先自动 op（短语从长到短），再叠加 OVERRIDE 精准替换
        for bid in block_ops:
            txt = btext[bid]
            new = txt
            for a, ph, v, _ in sorted(block_ops.get(bid, []), key=lambda x: -len(x[1])):
                new = ar.apply_edit(new, a, ph, v)
            for (find, repl) in override_apply.get(bid, []):
                if find in new:
                    new = new.replace(find, repl)
            if new != txt:
                plan.append((obj, title, bid, txt, new))
                backup.setdefault(obj, {})[bid] = txt

        # 标题覆盖（可选）
        for old_t, new_t in TITLE_OVERRIDE.items():
            if old_t in title:
                nt = title.replace(old_t, new_t)
                if nt != title:
                    title_changes.append((obj, title, nt))
                    backup.setdefault(obj, {})['__TITLE__'] = title

    if dry:
        print('=== DRY RUN ===')
        for obj, title, bid, old, new in plan:
            print(f'[{title[:22]}] {bid}')
            print(f'  OLD: {old}')
            print(f'  NEW: {new}')
            print()
        for obj, old_t, new_t in title_changes:
            print(f'[TITLE] {old_t}  ->  {new_t}')
        print(f'总改写块数: {len(plan)}，标题变更: {len(title_changes)}')
        return

    ok = 0
    for obj, title, bid, old, new in plan:
        if bid == '__TITLE__':
            continue
        ar.update_block_text(token, obj, bid, new)
        ok += 1
    for obj, old_t, new_t in title_changes:
        ar.update_doc_title(token, obj, new_t)
        ok += 1
    bad = 0
    for obj, title, bid, old, new in plan:
        if bid == '__TITLE__':
            continue
        cur = None
        for b in ar.get_doc_blocks(token, obj):
            if b.get('block_id') == bid:
                cur = ar.extract_block_text(b)
                break
        if cur != new:
            bad += 1
            print(f'⚠️ 校验失败 {bid}: 实际={cur[:60]!r} 期望={new[:60]!r}')
    for obj, old_t, new_t in title_changes:
        cur = None
        for dd in ar.discover_dir(token, node):
            if dd['obj'] == obj:
                cur = dd.get('title')
                break
        if cur != new_t:
            bad += 1
            print(f'⚠️ 标题校验失败: 实际={cur} 期望={new_t}')
    ts = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    bkname = f'{node}_before_dbg_audit_{ts}_backup.json'
    with open(bkname, 'w', encoding='utf-8') as f:
        json.dump(backup, f, ensure_ascii=False, indent=2)
    print(f'写回 {ok} 处，校验失败 {bad} 处。备份: {bkname}')


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--dir', default=NODE, help='飞书 wiki 目录 node')
    ap.add_argument('--dry', action='store_true', help='只读预览，不写回')
    args = ap.parse_args()
    main(args.dir, dry=args.dry)
