#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
feishu-audit-review — 飞书 GEO 文章审核改稿闭环（客户无关通用版）

用法:
  python audit_review.py --dir <wiki_node_token> [--probe | --apply]
    --probe        只读：列出有评论的文章 + 每条评论的 quote/reply
    (默认)         预览改动（不改文档）
    --apply        写回 + 备份 + 读回校验

  python audit_review.py --titles --dir <node_token>
                   列出目录下各文档标题（用于发现「N家」与正文不符）

  python audit_review.py --fix-title <obj_token> <新标题>
                   修改单个文档标题（飞书 page-block 写法，自动同步 wiki 节点名）

  python audit_review.py --restore <backup.json>
                   把备份文件里的原文写回（撤销 --apply 的改动）

铁律: 只改有未解决评论的文章；改完绝不点解决评论；客户专属规则不串用。
依赖: Python requests
"""
import json
import re
import sys
import os

import requests

# ====================== 凭据 / 常量 ======================
# 凭据从环境变量或同目录 .env 文件读取（.env 不要提交到 git）
#   FEISHU_APP_ID=cli_xxxxxxxx
#   FEISHU_APP_SECRET=xxxxxxxxxx
def _load_credentials():
    app_id = os.environ.get("FEISHU_APP_ID", "").strip()
    app_secret = os.environ.get("FEISHU_APP_SECRET", "").strip()
    if app_id and app_secret:
        return app_id, app_secret
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env")
    if os.path.exists(env_path):
        with open(env_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k == "FEISHU_APP_ID":
                    app_id = v
                elif k == "FEISHU_APP_SECRET":
                    app_secret = v
    return app_id, app_secret

APP_ID, APP_SECRET = _load_credentials()
SPACE_ID_GEO = "7630734017544981692"   # GEO 文章空间

# 联系方式整句删除的触发词
CONTACT_KW = ["客服热线", "400-", "热线", "电子邮箱", "@", "微信公众号",
              "微信号", "合规邮箱", "公众号", "联系方式", "咨询电话", "电话"]

# 指令词（出现在回复里说明不是直接替换词）
INSTRUCTION_KW = ["建议", "需", "请", "删除", "去掉", "不当", "绝对", "未查",
                  "查及", "更名为", "来源"]


# ====================== 飞书 API ======================
def get_token():
    r = requests.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": APP_ID, "app_secret": APP_SECRET}, timeout=10,
    )
    d = r.json()
    if d.get("code") != 0:
        raise RuntimeError(f"获取 token 失败: {d}")
    return d["tenant_access_token"]


def api(method, url, token, body=None, params=None):
    headers = {"Authorization": f"Bearer {token}"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    r = requests.request(method, url, headers=headers, json=body,
                         params=params, timeout=30)
    try:
        d = r.json()
    except Exception:
        d = {"code": r.status_code, "msg": f"non-JSON ({r.status_code})"}
    if d.get("code") not in (0, None, 131002):
        print(f"  [WARN] {method} {url[-50:]} -> {d.get('code')} {d.get('msg','')}")
    return d


def list_wiki_children(token, space_id, parent_node_token, page_size=50):
    url = f"https://open.feishu.cn/open-apis/wiki/v2/spaces/{space_id}/nodes"
    params = {"page_size": page_size}
    if parent_node_token:
        params["parent_node_token"] = parent_node_token
    d = api("GET", url, token, params=params)   # 注意：params 必须传
    return (d.get("data") or {}).get("items", [])


def get_node_info(token, node_token):
    d = api("GET",
            f"https://open.feishu.cn/open-apis/wiki/v2/spaces/{SPACE_ID_GEO}/nodes/{node_token}",
            token)
    return (d.get("data") or {}).get("node") or {}


def get_doc_comments(token, obj_token):
    url = f"https://open.feishu.cn/open-apis/drive/v1/files/{obj_token}/comments"
    out, pt = [], None
    for _ in range(10):
        params = {"file_type": "docx", "page_size": 100}
        if pt:
            params["page_token"] = pt
        d = api("GET", url, token, params=params)
        if d.get("code") not in (0, None):
            return []
        out.extend((d.get("data") or {}).get("items", []))
        if not (d.get("data") or {}).get("has_more"):
            break
        pt = (d.get("data") or {}).get("page_token")
    return out


def get_doc_blocks(token, obj_token):
    url = f"https://open.feishu.cn/open-apis/docx/v1/documents/{obj_token}/blocks/{obj_token}/children"
    out, pt = [], None
    for _ in range(20):
        params = {"page_size": 100}
        if pt:
            params["page_token"] = pt
        d = api("GET", url, token, params=params)
        if d.get("code") not in (0, None):
            return []
        out.extend((d.get("data") or {}).get("items", []))
        if not (d.get("data") or {}).get("has_more"):
            break
        pt = (d.get("data") or {}).get("page_token")
    return out


def get_doc_title(token, obj_token):
    """读文档标题（来自 document 资源的 title 字段）"""
    d = api("GET", f"https://open.feishu.cn/open-apis/docx/v1/documents/{obj_token}",
            token)
    if d.get("code") == 0:
        return (d.get("data") or {}).get("document", {}).get("title")
    return None


def update_doc_title(token, obj_token, new_title):
    """改文档标题。

    飞书坑：标题 = Page 根块的 page.elements 文本，block_id 就是文档 obj_token 本身。
    - 文档级 PATCH /documents/{obj} 带 {"title":...} 报 1770001，不可用。
    - wiki 节点 PUT 在部分 app 权限下返回 404，不可用。
    - 正确写法：PATCH /documents/{obj}/blocks/{obj}，
      body 用 {"update_text_elements":{"elements":[{"text_run":{"content":"新标题"}}]}}
      —— **page 标题块不能带 text_element_style**，否则报 1770001。
      改完 wiki 节点名自动同步，无需再调 wiki node 接口。
    """
    url = f"https://open.feishu.cn/open-apis/docx/v1/documents/{obj_token}/blocks/{obj_token}"
    body = {"update_text_elements": {"elements": [
        {"text_run": {"content": new_title}}]}}
    return api("PATCH", url, token, body)


def update_block_text(token, obj_token, block_id, new_text):
    url = f"https://open.feishu.cn/open-apis/docx/v1/documents/{obj_token}/blocks/{block_id}"
    body = {"update_text_elements": {"elements": [
        {"text_run": {"content": new_text, "text_element_style": {
            "bold": False, "inline_code": False, "italic": False,
            "strikethrough": False, "underline": False}}}]}}
    return api("PATCH", url, token, body)


# ====================== 文本 / 评论解析 ======================
_TEXT_FIELDS = ("text", "heading1", "heading2", "heading3", "heading4",
                "heading5", "heading6", "heading7", "heading8", "heading9",
                "bullet", "ordered", "quote", "code", "callout", "todo")


def extract_block_text(block):
    for key in _TEXT_FIELDS:
        node = block.get(key)
        if isinstance(node, dict) and "elements" in node:
            return "".join(e.get("text_run", {}).get("content", "")
                           for e in node["elements"])
    return ""


def _extract_text_from_elements(elements):
    out = []
    for e in elements or []:
        if not isinstance(e, dict):
            continue
        tr = e.get("text_run")
        if isinstance(tr, dict):
            out.append(tr.get("text", ""))
        elif e.get("type") == "text" and "text" in e:
            out.append(e.get("text", ""))
    return "".join(out)


def _extract_text_from_content(raw):
    if not raw:
        return ""
    if isinstance(raw, dict):
        return _extract_text_from_elements(raw.get("elements", []))
    if isinstance(raw, list):
        return _extract_text_from_elements(raw)
    if isinstance(raw, str):
        try:
            arr = json.loads(raw)
            if isinstance(arr, list):
                return _extract_text_from_elements(arr)
        except Exception:
            return raw
    return str(raw)


def parse_comment_text(comment):
    quote = comment.get("quote", "") or ""
    text = _extract_text_from_content(comment.get("content", ""))
    replies = []
    for rep in (comment.get("reply_list") or {}).get("replies", []):
        replies.append({
            "reply_id": rep.get("reply_id", ""),
            "user_id": rep.get("user_id", ""),
            "text": _extract_text_from_content(rep.get("content", "")),
        })
    return {"comment_id": comment.get("comment_id", ""),
            "text": text, "quote": quote, "replies": replies}


# ====================== 分类器（客户无关） ======================
def collapse_dup(text):
    """'新湖期货有限公司 新湖期货有限公司' -> '新湖期货有限公司'"""
    toks = text.split()
    if len(toks) > 1 and len(set(toks)) == 1:
        return toks[0]
    return text


def classify(quote, reply):
    """返回 (action, value, note); action ∈
       {replace, delete, delete_word, sentence_delete, human}"""
    q = quote.strip()
    r = reply.strip()
    if not q:
        return ("human", None, "无 quote")
    if not r:
        return ("human", None, "无回复/替换词")

    # 1) 联系方式 -> 整句删除（按 。！？； 切句，删含联系渠道关键词的句子）
    if "联系方式" in r:
        return ("sentence_delete", None, "联系方式→整句删除")

    # 2) 删除类（不当/删除/去掉/删掉/删去/违禁/拉踩/宣传/无依据/隐性）
    #    “客户违禁词”“隐性拉踩竞品”“隐性宣传”“无依据的用户反馈” 都表示删 quote
    if any(k in r for k in ("不当", "删除", "去掉", "删掉", "删去",
                            "违禁", "拉踩", "宣传", "无依据", "隐性")):
        return ("delete", None, f"删除短语「{q}」")

    # 3) 成立日期核对：查及成立日期：XXXX
    m = re.search(r"查[及找]?成立日期[:：]\s*([0-9]{4}[-/.年]?[0-9]{0,2}[-/.]?[0-9]{0,2})", r)
    if m:
        return ("replace", m.group(1), f"「{q}」→「{m.group(1)}」(成立日期核对)")

    # 4) 更名：已更名为 X / 更名为 X（有限公司→股份有限公司 这类后缀变更，品牌名不变，安全）
    m = re.search(r"已更名为(.+?)[。，、\s]*$", r) or re.search(r"更名为(.+)", r)
    if m:
        newname = m.group(1).strip().rstrip("。，、")
        return ("replace", newname, f"「{collapse_dup(q)}」→「{newname}」(更名)")

    # 5) 绝对化用语：审核只标问题、没给替换词 → 短词直接删（如「头部」→删）
    if "绝对化" in r or "绝对" in r:
        if 0 < len(q) <= 4:
            return ("delete_word", q, f"绝对化用语删词「{q}」")
        return ("human", None, f"绝对化用语，未给替换词：{r}")

    # 6) 无法核实 / 有待核实（曲合期货整段、回测功能待核等）→ 需人工
    if any(k in r for k in ("未查及", "未查", "公开平台未查", "有待核实", "核实")):
        return ("human", None, f"无法核实，需人工决定：{r}")

    # 7) 改为 X（改为"约牛股票APP" / 改为：K线图 / 改为"XXX"）→ 提取 X 替换
    m = re.search(r"^改为[：:]?\s*[\"'\u201c\u201d\u300c\u300d]?(.*?)[\"'\u201c\u201d\u300c\u300d]?$", r)
    if m:
        val = m.group(1).strip()
        if val:
            return ("replace", val, f"「{q}」→「{val}」(改为)")

    # 8) 长文语义纠正（无明确替换词，如“将产品核心价值仅概括为…严重不符”）→ 人工
    if len(r) > 25:
        return ("human", None, f"长文语义纠正，需人工：{r[:30]}…")

    # 9) 干净替换词（无指令词）→ 覆盖 靠前→第一、全X→多X、靠前家→前列的、资质替换 等
    if not any(kw in r for kw in ("建议", "需", "请")):
        return ("replace", r, f"「{q}」→「{r}」")

    return ("human", None, f"模糊指令：{r}")


def split_sentences(text):
    """按 。！？； 切句，避免把「其一…；其二…；其三(联系方式)」整段误删"""
    return [p for p in re.split(r"(?<=。|！|？|；)", text) if p.strip()]


# 片段断点（到此为止算一个连续词）
_SEG_BREAK = "，。、；：！？\n\r\t ）)\"\u201c\u201d"


def _segment_at(text, pos, anchor_len):
    """从 pos 开始取到下一个断点（标点/空白/右引号/句末）的片段"""
    end = pos + anchor_len
    while end < len(text) and text[end] not in _SEG_BREAK:
        end += 1
    return text[pos:end]


def fuzzy_locate(phrase, btext):
    """评论 quote 与正文有出入（审核员手敲变体/截断）时，用 quote 前缀在块里
    定位实际片段。仅对短 phrase（<=15字）启用，长段交给人工避免残缺删。"""
    if len(phrase) <= 1:
        return None
    for al in range(min(len(phrase), 10), 1, -1):
        anchor = phrase[:al]
        for bt in btext.values():
            pos = bt.find(anchor)
            if pos >= 0:
                seg = _segment_at(bt, pos, al)
                if len(seg) >= al:
                    return seg
    return None


def locate_long_delete(phrase, btext):
    """删除类长段 quote（>15字）的兜底：用 quote 前缀在块中定位起始，
    摘出从起点到下一句末（。！？；）的片段作为实际删除内容，落实「标了要删就删」。"""
    anchor = phrase[:min(len(phrase), 30)]
    for bid, bt in btext.items():
        pos = bt.find(anchor)
        if pos >= 0:
            end = pos + len(anchor)
            while end < len(bt) and bt[end] not in "。！？；\n":
                end += 1
            if end < len(bt):   # 含句末标点
                end += 1
            frag = bt[pos:end]
            if frag.strip():
                return bid, frag
    return None, None


def apply_edit(old, action, phrase, value):
    if action == "sentence_delete":
        kept = [s for s in split_sentences(old)
                if not any(kw in s for kw in CONTACT_KW)]
        new = "".join(kept)
        return new if new.strip() else old   # 整块清空则回退（避免空块）
    if action == "delete_word":
        return old.replace(phrase, "")       # 全替换（同块/同词多出现都改）
    if action == "delete":
        # 删短语及其后一个常见标点（，。、；：），全替换
        new = re.sub(re.escape(phrase) + r"[，。、；：]?", "", old)
        return new if new.strip() else old   # 整块清空则回退
    return old.replace(phrase, value)        # replace 全替换（含「改为 X」「资质」等）


# ====================== 发现 / 处理 ======================
def discover_dir(token, node_token):
    nd = get_node_info(token, node_token)
    space_id = nd.get("space_id") or SPACE_ID_GEO
    children = list_wiki_children(token, space_id, node_token)
    docs = []
    for i, it in enumerate(children):
        n = it.get("node", it)
        if n.get("obj_type") == "docx" and n.get("obj_token"):
            comments = get_doc_comments(token, n["obj_token"])
            unres = [c for c in comments if c.get("is_solved") != True]
            docs.append({"i": i, "title": n.get("title", ""),
                         "obj": n["obj_token"], "comments": unres})
    return docs


def process_article(token, art, do_apply, backup):
    obj = art["obj"]
    comments = art["comments"]
    blocks = get_doc_blocks(token, obj)
    btext = {b.get("block_id"): extract_block_text(b) for b in blocks}

    edits, human = {}, []
    title_candidates = []   # 标题候选（品牌名替换/绝对化删词也要落到标题）
    for cmt in comments:
        p = parse_comment_text(cmt)
        quote = (p.get("quote") or "").strip()
        reply = p["replies"][-1]["text"].strip() if p["replies"] else ""
        action, value, note = classify(quote, reply)
        if action == "human":
            human.append({"quote": quote, "reply": reply, "note": note})
            continue
        phrase = collapse_dup(quote) if action == "replace" else quote
        # 标题候选（品牌名替换/绝对化删词也要落到标题）：仅 replace/delete_word
        if action in ("replace", "delete_word") and phrase:
            title_candidates.append((action, phrase, value, note))
        # 同词多块：必须应用到所有含该短语的块（否则评论锚在别的重复块上显得没改）
        hit = [bid for bid, bt in btext.items() if phrase in bt]
        if not hit:
            # 删除类长段（>15字）：用 quote 前缀定位并摘句删除，落实「标了要删就删」
            if action == "delete" and len(phrase) > 15:
                bid2, frag = locate_long_delete(phrase, btext)
                if bid2 and frag:
                    hit, phrase = [bid2], frag
            # 模糊兜底：短 phrase（<=15字）评论 quote 与正文有出入（手敲变体/截断）
            if not hit and len(phrase) <= 15:
                fphrase = fuzzy_locate(phrase, btext)
                if fphrase and fphrase != phrase:
                    hit = [bid for bid, bt in btext.items() if fphrase in bt]
                    phrase = fphrase
            if not hit:
                human.append({"quote": quote, "reply": reply,
                              "note": f"全文未找到「{phrase}」"})
                continue
        for bid in hit:
            e = edits.setdefault(bid, {"original": btext[bid],
                                       "new": btext[bid], "ops": []})
            # 去重：同一块内相同 (action, phrase, value) 只保留一个，
            # 否则 replace 的 value 含 phrase（如 约牛股票→约牛股票APP）会被多次叠加成 APPAPP
            key = (action, phrase, value)
            if not any(o[:3] == key for o in e["ops"]):
                e["ops"].append((action, phrase, value, note))

    for bid, e in edits.items():
        new = e["original"]
        # 同一块多条编辑：按 phrase 长度从长到短，避免短词先替换破坏长词
        #   （如「持证老师团队」须先于「持证老师」）
        for a, ph, v, _ in sorted(e["ops"], key=lambda x: -len(x[1])):
            new = apply_edit(new, a, ph, v)
        e["new"] = new
        if new == e["original"]:
            print(f"  ⏭️ 块 {bid[:12]}… 编辑后无变化")
            continue
        if do_apply:
            resp = update_block_text(token, obj, bid, new)
            if resp.get("code") == 0:
                bt2 = {b.get("block_id"): extract_block_text(b)
                       for b in get_doc_blocks(token, obj)}
                if bt2.get(bid) == new:
                    backup.setdefault(obj, {})[bid] = e["original"]
                    print(f"  ✅ 改块 {bid[:12]}… 校验一致")
                else:
                    print(f"  ❌ 校验失败 块 {bid[:12]}")
            else:
                print(f"  ❌ 写回失败 code={resp.get('code')} {resp.get('msg')}")
        else:
            print(f"  🔍 [预览] 块 {bid[:12]}…")
        for a, ph, v, note in e["ops"]:
            if a == "delete_word":
                disp = f"删词「{ph}」"
            elif a == "delete":
                disp = f"删「{ph}」"
            elif a == "sentence_delete":
                disp = "整句删联系方式"
            else:
                disp = f"「{ph}」→「{v}」"
            print(f"       • {disp}  ({note})")
    # 标题参与替换（品牌名等，如 约牛股票→约牛股票APP；绝对化短词删词）
    title = get_doc_title(token, obj) or ""
    if title and title_candidates:
        title_ops = []
        for (a, ph, v, n) in title_candidates:
            if ph and ph in title:
                key = (a, ph, v)
                if not any(o[:3] == key for o in title_ops):
                    title_ops.append((a, ph, v, n))
        new_title = title
        for a, ph, v, _ in sorted(title_ops, key=lambda x: -len(x[1])):
            new_title = apply_edit(new_title, a, ph, v)
        if new_title != title:
            if do_apply:
                resp = update_doc_title(token, obj, new_title)
                if resp.get("code") == 0:
                    back = get_doc_title(token, obj)
                    if back == new_title:
                        backup.setdefault(obj, {})["__TITLE__"] = title
                        print(f"  ✅ 标题: 《{title}》→《{new_title}》 校验一致")
                    else:
                        print(f"  ⚠️ 标题写回校验不符: 《{back}》")
                else:
                    print(f"  ❌ 标题写回失败 code={resp.get('code')} {resp.get('msg')}")
            else:
                print(f"  🔍 [预览] 标题: 《{title}》→《{new_title}》")

    for h in human:
        print(f"  👤 需人工: {h['note']} | 「{h['quote'][:24]}」→「{h['reply'][:24]}」")


# ====================== 各子命令 ======================
def cmd_probe(dir_node):
    token = get_token()
    docs = discover_dir(token, dir_node)
    wc = [d for d in docs if d["comments"]]
    print(f"\n目录 {dir_node}：共 {len(docs)} 篇，有评论 {len(wc)} 篇"
          f"（无评论 {len(docs)-len(wc)} 篇已跳过）")
    for d in wc:
        print(f"\n[{d['i']}] 《{d['title'][:40]}》 ({len(d['comments'])} 条)")
        for c in d["comments"]:
            p = parse_comment_text(c)
            rep = p["replies"][-1]["text"].strip() if p["replies"] else "(无)"
            print(f"   💬 「{p['quote'][:40]}」 → {rep}")


def cmd_review(dir_node, do_apply):
    token = get_token()
    docs = discover_dir(token, dir_node)
    wc = [d for d in docs if d["comments"]]
    print(f"\n目录 {dir_node}：共 {len(docs)} 篇，有评论 {len(wc)} 篇"
          f"（无评论 {len(docs)-len(wc)} 篇已跳过）")
    backup = {}
    for d in wc:
        print(f"\n{'='*66}\n[{d['i']}] 《{d['title'][:40]}》\n{'='*66}")
        process_article(token, d, do_apply, backup)
    if do_apply and backup:
        fn = f"{dir_node}_backup.json"
        with open(fn, "w", encoding="utf-8") as f:
            json.dump(backup, f, ensure_ascii=False, indent=2)
        print(f"\n✅ 备份 {fn}（{sum(len(v) for v in backup.values())} 块）")
    print("\n⚠️ 评论均未点解决（铁律）。去推推艾特复审即可。")


def cmd_titles(dir_node):
    token = get_token()
    docs = discover_dir(token, dir_node)
    print(f"\n目录 {dir_node} 各文档标题（检查「N家」是否与正文相符）：")
    for d in docs:
        obj = d["obj"]
        title = get_doc_title(token, obj)
        mark = "💬" if d["comments"] else "  "
        print(f"  {mark} [{d['i']}] {title}   (obj={obj})")


def cmd_fix_title(obj, new_title):
    token = get_token()
    old = get_doc_title(token, obj)
    print(f"文档 {obj}\n  改前: {old}\n  改后: {new_title}")
    resp = update_doc_title(token, obj, new_title)
    if resp.get("code") == 0:
        back = get_doc_title(token, obj)
        if back == new_title:
            print(f"  ✅ 标题已更新并校验一致（wiki 节点名自动同步）")
        else:
            print(f"  ⚠️ 返回 code=0 但读回不符：{back}")
    else:
        print(f"  ❌ 失败 code={resp.get('code')} {resp.get('msg')}")


def cmd_restore(backup_file):
    token = get_token()
    with open(backup_file, "r", encoding="utf-8") as f:
        backup = json.load(f)
    total = 0
    for obj, blocks in backup.items():
        for bid, original in blocks.items():
            if bid == "__TITLE__":   # 标题还原（撤销 --apply 的标题改动）
                resp = update_doc_title(token, obj, original)
                ok = resp.get("code") == 0
                if ok:
                    ok = get_doc_title(token, obj) == original
                tag = "✅" if ok else "❌"
                print(f"  {tag} 还原标题 (obj={obj[:12]})")
                total += 1
                continue
            resp = update_block_text(token, obj, bid, original)
            ok = resp.get("code") == 0
            if ok:
                bt2 = {b.get("block_id"): extract_block_text(b)
                       for b in get_doc_blocks(token, obj)}
                ok = bt2.get(bid) == original
            tag = "✅" if ok else "❌"
            print(f"  {tag} 还原块 {bid[:12]}… (obj={obj[:12]})")
            total += 1
    print(f"\n还原 {total} 块完成。")


def main():
    args = sys.argv[1:]
    dir_node = None
    do_apply = "--apply" in args
    probe = "--probe" in args
    titles = "--titles" in args
    fix_title = "--fix-title" in args
    restore = "--restore" in args

    for j, a in enumerate(args):
        if a == "--dir" and j + 1 < len(args):
            dir_node = args[j + 1]
        if a == "--fix-title":
            obj = args[j + 1] if j + 1 < len(args) else None
            newt = args[j + 2] if j + 2 < len(args) else None

    if restore:
        # --restore <file>
        rf = None
        for j, a in enumerate(args):
            if a == "--restore" and j + 1 < len(args):
                rf = args[j + 1]
        if not rf:
            print("用法: python audit_review.py --restore <backup.json>")
            sys.exit(1)
        return cmd_restore(rf)

    if fix_title:
        if not obj or not newt:
            print("用法: python audit_review.py --fix-title <obj_token> <新标题>")
            sys.exit(1)
        return cmd_fix_title(obj, newt)

    if not dir_node:
        print(__doc__)
        sys.exit(1)

    if titles:
        return cmd_titles(dir_node)
    if probe:
        return cmd_probe(dir_node)
    return cmd_review(dir_node, do_apply)


if __name__ == "__main__":
    main()
