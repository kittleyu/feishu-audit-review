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
# 以下为使用者自己的飞书基础设施标识，建议通过环境变量覆盖（避免在公开仓库写死）
SPACE_ID_GEO = os.environ.get("FEISHU_SPACE_ID_GEO", "7630734017544981692")   # GEO 文章空间（默认仅适配本机使用者，可改）
RULES_BASE_APP = os.environ.get("FEISHU_RULES_BASE_APP", "Ys1AbSmgHaukF2sKUdQc8OlYnDd")  # 「优化客户管理」多维表 app_token
RULES_TABLE = os.environ.get("FEISHU_RULES_TABLE", "客户维护规则")  # 管理表内存放客户维护规则的表名

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
    """'示例公司有限公司 示例公司有限公司' -> '示例公司有限公司'"""
    toks = text.split()
    if len(toks) > 1 and len(set(toks)) == 1:
        return toks[0]
    return text


def parse_suggest(r, q):
    """解析 改为/可改为/建议修改/建议改为 的替换值；
    多值（如「全流程」「全方位」→「多环节服务」「多领域合作」）按 quote 在
    列举中的出现顺序对应取值，避免乱配。"""
    # 必须锚定「建议改为/建议修改为/改为/可改为」标记，只从标记后取值；
    # 否则 (.+) 会从位置0贪婪捕获整句，把列举词(全流程/全方位)也卷进 vals，
    # 导致 vals[0] 取到「全流程」自己而非建议值。
    m = re.search(r'(?:建议修改(?:为)?|建议改为|可\s*改为|改为)[：:]?\s*-*\s*', r)
    if not m:
        return None
    after = r[m.end():]
    vals = re.findall(r'[「"](.*?)[」"]', after)
    if not vals:
        v = after.strip().lstrip("—-").strip('。，、').strip('「"」"')
        return v or None
    # 多值：按 quote 在标记前列举里的顺序对应取值
    pre = r[:m.start()]
    pre_list = re.findall(r'[「"](.*?)[」"]', pre)
    if q in pre_list:
        idx = pre_list.index(q)
        if idx < len(vals):
            return vals[idx]
    return vals[0]


def classify(quote, reply):
    """返回 (action, value, note); action ∈
       {replace, delete, delete_word, sentence_delete, xx_replace, human}"""
    q = quote.strip()
    r = reply.strip()
    if not q:
        return ("human", None, "无 quote")
    if not r:
        return ("human", None, "无回复/替换词")

    # 1) 联系方式 -> 整句删除（按 。！？； 切句，删含联系渠道关键词的句子）
    if "联系方式" in r:
        return ("sentence_delete", None, "联系方式→整句删除")

    # 2) 用xx代替（具体公司/机构名匿名化，替换成 xx + 行业后缀）
    #    必须早于 #3 删除类，否则"具体公司"会被误判为删短语（整串删掉而非替换成xx）
    if "xx代替" in r or "用xx" in r or "具体名称用xx" in r:
        return ("xx_replace", None, f"用xx代替：{q}")

    # 3) 删除类（不当/删除/去掉/删掉/删去/违禁/拉踩/宣传/无依据/隐性/不提及具体公司名称）
    #    “不提及具体公司名称” 表示删 quote（不是把短语换成指令文字）
    if any(k in r for k in ("不当", "删除", "去掉", "删掉", "删去",
                            "违禁", "拉踩", "宣传", "无依据", "隐性",
                            "不提及", "具体公司")):
        return ("delete", None, f"删除短语「{q}」")

    # 4) 成立日期核对：查及成立日期：XXXX
    m = re.search(r"查[及找]?成立日期[:：]\s*([0-9]{4}[-/.年]?[0-9]{0,2}[-/.]?[0-9]{0,2})", r)
    if m:
        return ("replace", m.group(1), f"「{q}」→「{m.group(1)}」(成立日期核对)")

    # 5) 更名：已更名为 X / 更名为 X（有限公司→股份有限公司 这类后缀变更，品牌名不变，安全）
    m = re.search(r"已更名为(.+?)[。，、\s]*$", r) or re.search(r"更名为(.+)", r)
    if m:
        newname = m.group(1).strip().rstrip("。，、")
        return ("replace", newname, f"「{collapse_dup(q)}」→「{newname}」(更名)")

    # 6) 语句残缺 / 未找到相关数据来源 -> 删整句（无来源的例证/残缺句应去除，全部接受）
    if "语句残缺" in r or "残缺" in r:
        return ("sentence_delete", q, f"语句残缺→删整句「{q}」")
    if "未找到相关数据" in r or "未找到相关" in r:
        return ("sentence_delete", q, f"无数据来源→删整句「{q}」")

    # 7) 绝对化用语/用词/承诺（无替换词）→ 删整个 quote（去绝对化，全部接受）
    if "绝对化" in r or "绝对" in r:
        return ("delete", None, f"绝对化删词「{q}」")

    # 8) 无法核实 / 有待核实（如某家待核实、整家删除等）→ 人工
    if any(k in r for k in ("有待核实", "核实", "未查及", "未查", "公开平台未查")):
        return ("human", None, f"无法核实，需人工决定：{r}")

    # 9) 改为 X（改为/可改为/建议修改/建议改为，多值按 quote 顺序对应）
    sv = parse_suggest(r, q)
    if sv:
        return ("replace", sv, f"「{q}」→「{sv}」(改为)")

    # 10) 干净替换词（无指令词）→ 覆盖 靠前→第一、全X→多X、靠前家→前列的、
    #     资质替换、直接给替换值(如 AAA 统一更正「2019年期货行业首家取得…」) 等
    if not any(kw in r for kw in ("建议", "需", "请")):
        return ("replace", r, f"「{q}」→「{r}」")

    # 11) 长文语义纠正（>25字，无明确替换词）→ 人工（兜底，防把长指令当替换）
    if len(r) > 25:
        return ("human", None, f"长文语义纠正，需人工：{r[:30]}…")

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


# 长替换短语的合法结束词（用于补尾：评论 quote 被截断时，按正文实际词收尾）
REPLACE_END_WORDS = ["期货公司", "期货机构", "期货经营机构",
                     "有限公司", "股份有限公司", "证券公司", "证券机构"]


def locate_long_replace(phrase, btext, value):
    """替换类长短语（>15字，如「业内首家获得"AAA"级…期货机构」）兜底：
    用 quote 前缀在正文定位起点，优先用 REPLACE_END_WORDS 补尾（解决评论截断，
    如「期货机」→正文「期货机构」），定位不到则退化到句末截取。"""
    anchor = phrase[:min(len(phrase), 28)]
    while anchor and anchor[-1] in "，。、；：）) ":
        anchor = anchor[:-1]
    if not anchor:
        return None, None
    for bid, bt in btext.items():
        pos = bt.find(anchor)
        if pos >= 0:
            best = None
            for w in REPLACE_END_WORDS:
                ep = bt.find(w, pos)
                if ep >= 0:
                    cand = ep + len(w)
                    if best is None or cand < best:
                        best = cand
            if best:
                return bid, bt[pos:best]
            # 退化：延伸到句末
            end = pos + len(phrase)
            while end < len(bt) and bt[end] not in "。！？；\n":
                end += 1
            if end < len(bt):
                end += 1
            frag = bt[pos:end]
            if frag.strip():
                return bid, frag
    return None, None


def xxify(token):
    """具体公司/机构名匿名化：xx + 行业后缀。
    例：「某钢铁公司」→xx钢铁、「某股份公司」→xx、「某期货公司」→xx期货。"""
    t = token.strip()
    if not t:
        return "xx"
    if "钢铁" in t or t.endswith("钢"):
        return "xx钢铁"
    if "股份" in t:
        return "xx"
    for suf in ("期货", "证券", "银行", "保险", "基金"):
        if suf in t:
            return "xx" + suf
    return "xx"


def apply_edit(old, action, phrase, value):
    if action == "sentence_delete":
        if value is None:
            # 联系方式类：按联系渠道关键词删整句
            kept = [s for s in split_sentences(old)
                    if not any(kw in s for kw in CONTACT_KW)]
        else:
            # 残缺/无来源类：删除含定位片段(phrase)的整句
            kept = [s for s in split_sentences(old) if phrase not in s]
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


def load_client_rules_from_base(token, client):
    """从「优化客户管理」多维表读某客户的维护规则，返回 [(action, phrase, value, note)]。
    规则类型(单选): 替换/删短语/删词/整句删 → replace/delete/delete_word/sentence_delete。
    无「客户维护规则」表或无该客户规则时返回 []（不报错）。"""
    t = api("GET", f"https://open.feishu.cn/open-apis/bitable/v1/apps/{RULES_BASE_APP}/tables", token)
    tid = None
    for tb in (t.get("data") or {}).get("tables", []):
        if tb.get("name") == RULES_TABLE:
            tid = tb.get("table_id")
            break
    if not tid:
        print(f"  [WARN] 管理表中未找到「{RULES_TABLE}」表，跳过客户规则套用")
        return []
    out = []
    pt = None
    for _ in range(20):
        params = {"page_size": 100}
        if pt:
            params["page_token"] = pt
        r = api("GET",
                f"https://open.feishu.cn/open-apis/bitable/v1/apps/{RULES_BASE_APP}/tables/{tid}/records",
                token, params=params)
        for rec in (r.get("data") or {}).get("items", []):
            f = rec.get("fields", {})
            if (f.get("客户名") or "").strip() != client:
                continue
            rt = f.get("规则类型") or {}
            rtype = rt.get("text") if isinstance(rt, dict) else rt
            phrase = (f.get("查找内容") or "").strip()
            value = (f.get("替换为") or "").strip()
            note = (f.get("备注") or "").strip()
            action = {"替换": "replace", "删短语": "delete",
                      "删词": "delete_word", "整句删": "sentence_delete"}.get(rtype)
            if action and phrase:
                out.append((action, phrase, value, note or f"规则[{rtype}]"))
        d = r.get("data") or {}
        if not d.get("has_more"):
            break
        pt = d.get("page_token")
    return out


def process_article(token, art, do_apply, backup, extra_rules=None):
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
        if action == "xx_replace":
            # 具体公司名匿名化：拆 quote 成多个名，逐个替换成 xx+行业后缀
            tokens = [t.strip() for t in re.split(r'[、，,\s]+', quote) if t.strip()]
            for tok in tokens:
                val = xxify(tok)
                thit = [bid for bid, bt in btext.items() if tok in bt]
                if not thit and len(tok) <= 15:
                    ftok = fuzzy_locate(tok, btext)
                    if ftok and ftok != tok:
                        thit = [bid for bid, bt in btext.items() if ftok in bt]
                        tok = ftok
                if not thit:
                    human.append({"quote": tok, "reply": reply,
                                  "note": f"xx代替未找到「{tok}」"})
                    continue
                for bid in thit:
                    e = edits.setdefault(bid, {"original": btext[bid],
                                               "new": btext[bid], "ops": []})
                    key = ("replace", tok, val)
                    if not any(o[:3] == key for o in e["ops"]):
                        e["ops"].append(("replace", tok, val, note))
            continue
        phrase = collapse_dup(quote) if action == "replace" else quote
        # 标题候选（品牌名替换/绝对化删词也要落到标题）：仅 replace/delete_word
        if action in ("replace", "delete_word") and phrase:
            title_candidates.append((action, phrase, value, note))
        # 同词多块：必须应用到所有含该短语的块（否则评论锚在别的重复块上显得没改）
        hit = [bid for bid, bt in btext.items() if phrase in bt]
        if not hit:
            # 替换类长短语（>15字）：前缀定位 + 结束词补尾（解决评论截断，如「期货机」）
            if action == "replace" and len(phrase) > 15:
                bid2, frag = locate_long_replace(phrase, btext, value)
                if bid2 and frag:
                    hit, phrase = [bid2], frag
            # 删除/整句删类长段（>15字）：前缀定位并摘句/摘片段
            if not hit and action in ("delete", "sentence_delete") and len(phrase) > 15:
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
            # 否则 replace 的 value 含 phrase（如 某品牌→某品牌APP）会被多次叠加成 APPAPP
            key = (action, phrase, value)
            if not any(o[:3] == key for o in e["ops"]):
                e["ops"].append((action, phrase, value, note))

    # 机会4：叠加客户维护规则（来自管理表多维表，无论有无评论都套用）
    if extra_rules:
        for (action, phrase, value, note) in extra_rules:
            if not phrase:
                continue
            if action in ("replace", "delete_word"):
                title_candidates.append((action, phrase, value, note))
            hit = [bid for bid, bt in btext.items() if phrase in bt]
            if not hit:
                if action == "delete" and len(phrase) > 15:
                    bid2, frag = locate_long_delete(phrase, btext)
                    if bid2 and frag:
                        hit, phrase = [bid2], frag
                if not hit and len(phrase) <= 15:
                    fphrase = fuzzy_locate(phrase, btext)
                    if fphrase and fphrase != phrase:
                        hit = [bid for bid, bt in btext.items() if fphrase in bt]
                        phrase = fphrase
                if not hit:
                    print(f"  👤 规则未命中: {note} | 「{phrase[:24]}」")
                    continue
            for bid in hit:
                e = edits.setdefault(bid, {"original": btext[bid],
                                           "new": btext[bid], "ops": []})
                key = (action, phrase, value)
                if not any(o[:3] == key for o in e["ops"]):
                    e["ops"].append((action, phrase, value, note))

    for bid, e in edits.items():
        new = e["original"]
        # 同一块多条编辑：按 phrase 长度从长到短，避免短词先替换破坏长词
        #   （如「某资质团队」须先于「某资质」，避免短词先替换破坏长词）
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
                disp = "整句删联系方式" if v is None else f"整句删「{ph[:18]}」"
            else:
                disp = f"「{ph}」→「{v}」"
            print(f"       • {disp}  ({note})")
    # 标题参与替换（品牌名等，如 某品牌→某品牌APP；绝对化短词删词）
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
def cmd_probe(dir_node, from_index=None):
    token = get_token()
    docs = discover_dir(token, dir_node)
    if from_index is not None:
        docs = docs[from_index:]
    wc = [d for d in docs if d["comments"]]
    print(f"\n目录 {dir_node}：共 {len(docs)} 篇，有评论 {len(wc)} 篇"
          f"（无评论 {len(docs)-len(wc)} 篇已跳过）")
    for d in wc:
        print(f"\n[{d['i']}] 《{d['title'][:40]}》 ({len(d['comments'])} 条)")
        for c in d["comments"]:
            p = parse_comment_text(c)
            rep = p["replies"][-1]["text"].strip() if p["replies"] else "(无)"
            print(f"   💬 「{p['quote'][:40]}」 → {rep}")


def cmd_review(dir_node, do_apply, from_index=None, client_rules=None):
    token = get_token()
    docs = discover_dir(token, dir_node)
    if from_index is not None:
        docs = docs[from_index:]
    wc = [d for d in docs if d["comments"]]
    print(f"\n目录 {dir_node}：共 {len(docs)} 篇，有评论 {len(wc)} 篇"
          f"（无评论 {len(docs)-len(wc)} 篇已跳过）")
    backup = {}
    for d in wc:
        print(f"\n{'='*66}\n[{d['i']}] 《{d['title'][:40]}》\n{'='*66}")
        process_article(token, d, do_apply, backup, extra_rules=client_rules)
    if do_apply and backup:
        fn = f"{dir_node}_backup.json"
        with open(fn, "w", encoding="utf-8") as f:
            json.dump(backup, f, ensure_ascii=False, indent=2)
        print(f"\n✅ 备份 {fn}（{sum(len(v) for v in backup.values())} 块）")
    print("\n⚠️ 评论均未点解决（铁律）。交由审核人员处理评论状态即可。")


def cmd_titles(dir_node, from_index=None):
    token = get_token()
    docs = discover_dir(token, dir_node)
    if from_index is not None:
        docs = docs[from_index:]
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
    from_index = None
    rules_from_base = "--rules-from-base" in args
    client = None
    obj = newt = None   # --fix-title 的目标 obj / 新标题（循环内赋值）

    for j, a in enumerate(args):
        if a == "--dir" and j + 1 < len(args):
            dir_node = args[j + 1]
        if a == "--from-index" and j + 1 < len(args):
            try:
                from_index = int(args[j + 1])
            except ValueError:
                from_index = None
        if a == "--fix-title":
            obj = args[j + 1] if j + 1 < len(args) else None
            newt = args[j + 2] if j + 2 < len(args) else None
        if a == "--client" and j + 1 < len(args):
            client = args[j + 1]

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
        return cmd_titles(dir_node, from_index)
    if probe:
        return cmd_probe(dir_node, from_index)
    client_rules = None
    if rules_from_base:
        if not client:
            print("用法: --rules-from-base 需配合 --client <客户名>，例如 --rules-from-base --client 某客户")
            sys.exit(1)
        client_rules = load_client_rules_from_base(get_token(), client)
        print(f"📋 从管理表载入「{client}」维护规则 {len(client_rules)} 条")
    return cmd_review(dir_node, do_apply, from_index, client_rules=client_rules)


if __name__ == "__main__":
    main()
