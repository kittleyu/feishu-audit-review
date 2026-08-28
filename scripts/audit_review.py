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
from collections import defaultdict

import requests

# 强制 stdout/stderr 使用 UTF-8，避免 Windows GBK 终端下打印 emoji/中文时崩溃
try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

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
# 飞书 wiki 空间 ID（必填）：通过环境变量 FEISHU_SPACE_ID 提供（兼容旧名
# FEISHU_SPACE_ID_GEO），或命令行 --space-id 覆盖。绝不写死私有默认值。
SPACE_ID_GEO = os.environ.get("FEISHU_SPACE_ID") or os.environ.get("FEISHU_SPACE_ID_GEO")
_SPACE_ID_CLI = None   # 命令行 --space-id 覆盖（由 main() 设置）


def set_space_id(sid):
    """命令行 --space-id 注入空间 ID（优先级最高）。"""
    global _SPACE_ID_CLI
    _SPACE_ID_CLI = sid


def resolve_space_id(node_space_id=None):
    """解析有效空间 ID：命令行 > 节点自带 > 环境变量。均无则报错退出。"""
    sid = _SPACE_ID_CLI or node_space_id or SPACE_ID_GEO
    if not sid:
        print("❌ 缺少飞书空间 ID。请通过环境变量 FEISHU_SPACE_ID 或命令行 --space-id 提供。")
        sys.exit(1)
    return sid

# 联系方式整句删除的触发词
CONTACT_KW = ["客服热线", "400-", "热线", "电子邮箱", "@", "微信公众号",
              "微信号", "合规邮箱", "公众号", "联系方式", "咨询电话", "电话"]


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
            f"https://open.feishu.cn/open-apis/wiki/v2/spaces/{resolve_space_id()}/nodes/{node_token}",
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


def enrich_reactions(token, obj_token, comments):
    """用批量评论接口取回「点赞/reaction」数据（need_reaction=true）。

    列表接口 GET /comments 不返回该字段，需额外一次 batch_query。
    注意：reactions 是敏感字段，需应用具备「获取用户 ID」权限范围；
    若权限不足，reactions 会返回为空（None）——此时点赞检测不生效，
    退回「找不到锚点→归人工」的现行为，绝不误删、绝不漏改。
    """
    if not comments:
        return
    ids = [c.get("comment_id") for c in comments if c.get("comment_id")]
    if not ids:
        return
    url = f"https://open.feishu.cn/open-apis/drive/v1/files/{obj_token}/comments/batch_query"
    try:
        d = api("POST", url, token,
                body={"comment_ids": ids, "need_reaction": True},
                params={"file_type": "docx"})
    except Exception:
        return
    if d.get("code") not in (0, None):
        return
    items = ((d.get("data") or {}).get("comments")
             or (d.get("data") or {}).get("items") or [])
    by_id = {it.get("comment_id"): it for it in items}
    for c in comments:
        it = by_id.get(c.get("comment_id"))
        if it is not None:
            c["reactions"] = it.get("reactions")


def is_comment_liked(cmt):
    """评论是否被点赞（即用户已手动改过，按铁律直接忽略，不再归人工）。

    兼容 reactions 的多种返回形态：list（非空）/ dict（含非空值）。
    """
    r = cmt.get("reactions") if isinstance(cmt, dict) else None
    if not r:
        return False
    if isinstance(r, list):
        return len(r) > 0
    if isinstance(r, dict):
        for v in r.values():
            if isinstance(v, list) and v:
                return True
            if isinstance(v, dict) and v:
                return True
        return bool(r)
    return False


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


# 章节/机构标题块类型（heading1..heading9）
HEAD_TYPES = {3, 4, 5, 6, 7, 8, 9}


def is_section_header(block):
    """判断块是否为「章节/机构标题」（整节删除时用它定删除边界：
    删到下一个标题块之前停止，避免误删后续不相关内容）。
    判定：heading 类型块，或正文以「1. 」「2、」等编号开头。"""
    if block.get("block_type") in HEAD_TYPES:
        return True
    t = (extract_block_text(block) or "").strip()
    return bool(re.match(r"^\d+[\.、]\s*\S", t))


def extract_inst_name(text):
    """从评论 quote/reply 中抽取机构名作删除锚点。
    优先取前导整段（到空格/句号前）若含机构后缀；否则全文搜第一个机构名。
    覆盖：有限公司/有限责任公司/医院/诊所/门诊部/卫生所/医疗中心。
    这样即使审核员手敲的描述口径与正文不同（正文用「该机构」等代称），
    也能用机构名在正文定位到对应句子/整节。"""
    if not text:
        return None
    # 1) 前导：取第一段（到首个空白/标点）若含机构后缀
    m = re.match(r"\s*([\u4e00-\u9fa5]{2,}(?:有限责任公司|有限公司|医院|诊所|"
                 r"门诊部|卫生所|医疗中心|中心))", text)
    if m:
        return m.group(1)
    # 2) 全文搜第一个机构名（有限公司/有限责任公司 优先于 医院 等短后缀）
    m = re.search(r"([\u4e00-\u9fa5]{2,}(?:有限责任公司|有限公司)|"
                  r"[\u4e00-\u9fa5]{2,}(?:医院|诊所|门诊部|卫生所|医疗中心))", text)
    if m:
        return m.group(1)
    return None


def parse_suggest(r, q):
    """解析 改为/可改为/建议修改/建议改为 的替换值；
    多值（如「全流程」「全方位」→「多环节服务」「多领域合作」）按 quote 在
    列举中的出现顺序对应取值，避免乱配。回复含「或者/或」二选一时取第一个。"""
    # 必须锚定「建议改为/建议修改为/改为/可改为」标记，只从标记后取值；
    # 否则 (.+) 会从位置0贪婪捕获整句，把列举词(全流程/全方位)也卷进 vals，
    # 导致 vals[0] 取到「全流程」自己而非建议值。
    m = re.search(r'(?:建议修改(?:为)?|建议改为|可\s*改为|更正为|需更正为|改为|应为|应更改为|调整为|规范为|规格名称|规格为)[：:]?\s*-*\s*', r)
    if not m:
        return None
    after = r[m.end():]
    vals = re.findall(r'[「"](.*?)[」"]', after)
    if vals:
        # 多值：按 quote 在标记前列举里的顺序对应取值
        pre = r[:m.start()]
        pre_list = re.findall(r'[「"](.*?)[」"]', pre)
        if q in pre_list and pre_list.index(q) < len(vals):
            v = vals[pre_list.index(q)]
        else:
            v = vals[0]
    else:
        v = after.strip().lstrip("—-").strip('。，、').strip('「"」“”')
    if not v:
        return None
    # 二选一（"或者"/"或"）：取第一个，按评论直接改（审核给了任一可接受）
    for sep in ("或者", "或"):
        if sep in v:
            v = v.split(sep)[0].strip()
            break
    return v


def normalize_date_cn(s):
    """把「2011-06-28 / 2011.06.28 / 2011年06月28日」统一成「2011年6月28日」"""
    s = s.strip().strip("。，、")
    m = re.match(r"(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})日?", s)
    if m:
        return f"{m.group(1)}年{int(m.group(2))}月{int(m.group(3))}日"
    m2 = re.match(r"(\d{4})(?:年)?", s)
    if m2:
        return f"{m2.group(1)}年"
    return s


def _looks_like_inst_fullname(r):
    """回复是否像『纯机构/单位全称』（审核直接给的规范名，应 replace 而非删短语）。

    判定：剥去末尾中文括号说明（如「（示范口腔）」）后以机构后缀结尾，且不含说明性
    标点（，。、；：！？换行）与叙述连词（根据/显示/为/系/即/因/由/经/称/说明/实际/
    与/和/但/故/建议/需/请/目前/运营/下设/注册/地址/位于），长度 ≤40。

    例：『示例市示范口腔医疗服务有限责任公司示范口腔诊所（示范口腔）』→ True；
        『该院目前运营的院区包括院本部、鹿泉院区…』→ False（无机构后缀+含叙述词）。"""
    s = re.sub(r'（[^（）]*）$', '', r)  # 去掉末尾（...）说明
    if not re.search(r'(公司|医院|诊所|门诊部|中心|学校|大学|银行|集团|'
                     r'事务所|研究院|学院)$', s):
        return False
    if any(p in r for p in "，。、；：！？\n\r\t"):
        return False
    if any(w in r for w in ("根据", "显示", "为", "系", "即", "因", "由", "经",
                            "称", "说明", "实际", "与", "和", "但", "故", "建议",
                            "需", "请", "目前", "运营", "下设", "注册", "地址", "位于")):
        return False
    if len(r) > 40:
        return False
    return True


# 审核「说明/标签」回复：核查结论或定性标签，绝不能作为替换值写进正文
# （如「公开信息未发现注册资本消息」「无法报销」「荣誉归属错误」「数据错误」等）。
# 这类回复一旦出现，classify 必须降级为「删 quote 短句」，绝不能 replace（否则字面写进正文）。
# 注意：带显式替换值的回复（建议改为X/应为X/更名/地址为X）已被更早的分支拦截，
# 不会落到这里；本正则只拦截「纯说明/标签」式回复。
# 2026-08-28 增补：表述错误（RVuW 实战）、露出（JLno 实战「底层知识库露出」＝
# 该句泄露内部知识库来源，是定性标签非替换值；中文改稿语境「露出」几乎不会是替换值）。
_AUDIT_NOTE_RE = re.compile(
    r"(公开信息未发现|未发现|无法证实|未能证实|无法报销|待核实|需核实|"
    r"未查询到|未查到|查无|荣誉归属错误|数据无法核实|数据错误|事实性错误|"
    r"表述不当|表述错误|内容错误|信息错误|归属错误|露出)"
)


def _is_audit_note(r):
    """回复是否只是审核的『核查结论/定性标签』而非替换值。"""
    return bool(_AUDIT_NOTE_RE.search(r))


# 地址提取守卫：从审核回复里抽出的「新地址」若含叙述词或超长，则不是纯地址，
# 绝不能写进正文（会把「与…不符」「实际主院区地址」等说明文当地址写进去）。
# 触发即降级为「删 quote 短语」。长度上限 30（中文地址极少超此）。
_ADDR_NARRATIVE_RE = re.compile(
    r"(与|不符|实际|和|但|根据|显示|为|系|即|因|由|经|称|说明|建议|需|请|"
    r"目前|运营|下设|注册|位于|主院区|院区|核实|查及|公开|变更|更正|错误|有误)"
)
_ADDR_FEATURE_RE = re.compile(r"(路|街|号|大厦|广场|镇|乡|道|幢|室)")

# 地址主体提取（#9.52/#9.53 共用）：省?市+区+路号 / 纯路号。
# ⚠️ ① 省/市/区名限 2-3 字（{2,3}）——旧的 {2,} 贪婪会把「注册地址为」这类前缀
#       也当省名/市名吞掉（city 提取出「注册地址为陕西省西安市」的灾难根因）。
#    ② 分支2（纯路号兜底）路名限 {1,4} 字——旧的 {2,} 从位置0贪婪吞整句
#       （句尾有「号」即可命中），绕过分支1 的省市校验，同样吞掉前缀。
#       例：「注册地址为陕西省西安市新城区西五路157号」必须只抓「西五路157号」
#       或「陕西省西安市新城区西五路157号」，绝不能把「注册地址为」包进去。
_ADDR_OLD_RE = re.compile(
    r"((?:[\u4e00-\u9fa5]{2,3}省)?[\u4e00-\u9fa5]{2,3}市[\u4e00-\u9fa5]{1,3}[区县]"
    r"[\u4e00-\u9fa5\d]*(?:路|街|号|大厦|广场|镇|乡)[\u4e00-\u9fa5\d]*"
    r"|[\u4e00-\u9fa5\d]{1,4}(?:路|街道|街|号|大厦|广场|镇|乡)[\u4e00-\u9fa5\d]*)"
)

# #9.53 从旧地址里补回省/市名（括号内新地址常只有路号）：优先 省+市，其次 市。
_ADDR_CITY_RE = re.compile(
    r"([\u4e00-\u9fa5]{2,3}省[\u4e00-\u9fa5]{2,3}市|[\u4e00-\u9fa5]{2,3}市)"
)

# m_old 匹配可能把「为/地址/注册地址」等前缀词当省/市名开头吞进去
# （如「注册地址为陕西省…」→「为陕西省…」），匹配后统一剥离，保证 olda 是纯地址主体。
_ADDR_PREFIX_STRIP = re.compile(r"^(?:注册地址为|地址为|注册地址|地址|为|是)+")


def _is_clean_address(addr):
    """抽出的新地址是否「干净」（纯地址，非说明文）。"""
    if not addr or len(addr) > 30:
        return False
    if _ADDR_NARRATIVE_RE.search(addr):
        return False
    return bool(_ADDR_FEATURE_RE.search(addr))


def _is_pure_replace_value(r):
    """回复是否可作为『纯替换值』直接替换 quote。

    这是「绝不乱加内容」的核心守卫：只有当回复足够短、无说明性标点、
    无叙述连词/动词（即不像一段说明文字）时，才允许把它写进正文当替换值；
    否则视为审核员的说明性文字，调用方应降级为「删 quote 短句」而不是写进正文。

    例：'第一' / '雁塔西路277号' / '控股' 通过；
        '注册地址X与…实际主院区地址（Y）不符' / '建议改为知名专家' 不通过。
        '公开信息未发现注册资本消息' / '无法报销' / '荣誉归属错误' / '安全'(单字) 不通过。"""
    if not r:
        return False
    # 单字符回复绝不当替换值：太模糊/危险（如「安全」→会把整句或标题替换成「安全」、
    # 或覆盖合法文本；「最」「全」由 #9.95 / delete 专用分支感知处理）。一律降级为「删 quote」。
    if len(r) <= 1:
        return False
    # 审核说明/标签回复：核查结论或定性标签（非替换值），必须降级为「删 quote」
    # （否则会把「公开信息未发现…」「无法报销」「荣誉归属错误」等字面写进正文——灾难）。
    if _is_audit_note(r):
        return False
    # 机构全称豁免：回复是审核直接给的规范机构名（如
    # 「示例市示范口腔医疗服务有限责任公司示范口腔诊所（示范口腔）」），
    # 应 replace 而非删短语。允许中文括号、允许超 15 字（机构名常较长）。
    if _looks_like_inst_fullname(r):
        return True
    if len(r) > 15:
        return False
    # 纯数字/编号类回复（如 "1"）不是合法替换词：写进正文会污染内容
    #（生成「覆盖1的期货公司」等），必须降级为「删 quote」（拿不准就删短语），
    # 绝不能 replace 成 "1"。飞书评论里 "1" 多为编号/点赞等误入回复的噪声。
    # 仅拒绝 ≤2 位的纯数字（编号噪声）；放行 4 位年份等合法数字替换值（如 2011）。
    if re.fullmatch(r"\d+", r) and len(r) <= 2:
        return False
    if any(p in r for p in "，。、；：！？\n\r\t（）()「」“”\""):
        return False
    # 叙述性连词/动词，暗示这是一段说明而非替换词
    if any(w in r for w in ("不符", "显示", "根据", "位于", "为", "系", "即",
                            "因", "由", "经", "称", "说明", "实际", "应为",
                            "与", "和", "但", "故", "建议", "需", "请")):
        return False
    return True


# 审核确认类回复（无修改指令，应跳过而非替换/删除）：回复去掉尾标点后完全匹配其一
_CONFIRM_TOKENS = {
    "已确认", "确认", "确认无误", "无需修改", "无需更改", "不需要修改",
    "没问题", "无误", "内容无误", "正确无误", "已核实", "经核实无误",
    "同意", "无异议", "已阅", "阅", "无问题",
    # 显式"不修改"类（语义=保持原样，等价于确认无误；早于兜底 replace 避免写成灾难）
    "此处不作修改", "不作修改", "不修改", "不做修改", "无须修改",
}


def classify(quote, reply):
    """返回 (action, value, note); action ∈
       {skip, replace, delete, delete_word, sentence_delete, xx_replace, delete_section, multi_replace, append, human}
       唯一规则：按评论修改；拿不准就把 quote 那段短句删掉；绝不把回复里的说明性
       文字当正文写进去（不乱加内容）。skip = 审核确认无误，保持原样不改动。"""
    q = quote.strip()
    r = reply.strip()
    if not q:
        return ("human", None, "无 quote")
    if not r:
        # 空回复：仅高亮了 quote（多为错误地址标注）。若 quote 像地址 → 删该错误地址短语；
        # 否则无法安全处理 → 人工。
        if re.search(r"(?:位于|路|号|区|省|市|地址)", q):
            return ("delete", None, f"空回复·删错误地址「{q}」")
        return ("human", None, "无回复/替换词")

    # 0) 审核确认无误（回复是确认类、无修改指令）→ 跳过：不替换、不删除、不归人工。
    #    把「已确认」当替换值写进正文是灾难（既破坏原意又违反「不乱加内容」）；
    #    删除已确认正确的内容同样有害。安全做法是保持原样，交由审核人员后续处理。
    rr = r.rstrip("。.！!？?；;，,、")
    if (len(rr) <= 10 and rr in _CONFIRM_TOKENS) or rr.lower() in ("ok", "ok.", "yes", "no change", "confirmed"):
        return ("skip", None, f"审核已确认无误，跳过：{r}")

    # 0.7) 审核「问题类型标注」识别：回复是审核给的批注标签（绝对化用语 / 金融保障承诺 /
    #      无限定承诺 / 表达不通顺 / 文字笔误 / 不得涉及电话 / 拆分矛盾表述 等），
    #      **不是替换值**。绝不能把标签文字写进正文（如把「安全」改成「金融保障承诺」、
    #      把号码改成「不得涉及电话」）。按标签语义处理：删短语 / 抽笔误替换 / 归人工。
    #      注意：刻意不含「事实性错误 / 荣誉归属错误 / 数据错误」——那类走 #6.5 建议改为
    #      已能正确 replace（如「建议改为旗下全资子公司永安资本…」），此处若拦截会破坏之。
    _LABEL_KW = ("绝对化用语", "绝对化承诺", "无限定承诺", "金融保障承诺",
                 "表达不通顺", "不通顺", "语句不通",
                 "文字笔误", "笔误", "不得涉及电话", "涉电话", "拆分矛盾表述")
    if any(k in r for k in _LABEL_KW):
        # 笔误：抽 「X」应为「Y」/ "X"应为"Y" → 替换错别字（保留原句其余内容）
        m = re.search(r'[""\'「]?([^""\'「」]{1,8})[""\'「]?\s*应为\s*[""\'「]?([^""\'「」]{1,8})[""\'「]?', r)
        if m and "笔误" in r:
            # 返回「修正后的整句」作为 value（process_article 用 quote 作 phrase 整体替换），
            # 绝不能把 group1(旧词) 当 value——否则整句会被替换成旧词本身（灾难）。
            new_q = q.replace(m.group(1), m.group(2))
            if new_q != q:
                return ("replace", new_q, f"笔误「{m.group(1)}」→「{m.group(2)}」")
        if "拆分" in r:
            return ("human", None, f"拆分矛盾表述需重写：{r}")
        # 其余纯标签（绝对化/承诺/不通顺/涉电话等）→ 删 quote 短语（拿不准删短句）
        return ("delete", None, f"删短语（批注「{r}」）「{q}」")

    # 0.8) 补注时间类（如「首批」经核实为真需补注时间（2011年））→ 真正补注：
    #      抽回复里被引号包裹的术语（如「首批」）+ 年份（YYYY年），把该术语增补为
    #      「术语（YYYY年）」。审核本意是补注而非删除/改写，故用 multi_replace 只动该词。
    #      若无法解析出口径（缺术语或年份）→ 仍归人工，绝不乱加内容。
    if "补注时间" in r or "经核实为真" in r:
        my = re.search(r"(\d{4})\s*年", r)
        mt = re.search(r'[""「]([^""「」]{1,6})[""」]', r)
        if my and mt:
            term, year = mt.group(1), my.group(1)
            return ("multi_replace", [(term, f"{term}（{year}年）")],
                    f"补注时间：{term}→{term}（{year}年）")
        return ("human", None, f"需补注时间但口径无法解析：{r}")

    # 1) 联系方式 -> 整句删除（按 。！？； 切句，删含联系渠道关键词的句子）
    if "联系方式" in r:
        return ("sentence_delete", None, "联系方式→整句删除")

    # 1.5) 地址有误 → 删除该错误地址短语（审核未给新值，按决策直接删表述）
    if "地址错误" in r or "地址有误" in r:
        return ("delete", None, f"删除错误地址「{q}」")

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

    # 3.5) 标注类（无意义英文/错别字/语病/冗余/多余）→ 删短语
    #      审核给的是「描述性标签」，不是替换词；若当成替换值会把正文
    #      替换成「无意义英文」四字（错误）。标了就删该短语本身。
    #      "英文" 单独出现也删；但「改为英文X」含"改为"则走 #6.5 替换。
    if any(k in r for k in ("无意义", "错别字", "语病", "冗余", "多余")) \
            or ("英文" in r and "改为" not in r):
        return ("delete", None, f"标注「{r}」→删短语「{q}」")

    # 3.7) 复合更正：成立日期 + 地址（"成立日期：X；地址：Y" / "注册日期：X；地址：Y"）
    #      审核把两类更正写在同一回复里；拆成多子串替换（仅替换引用段里确实存在的
    #      旧日期/旧地址，绝不把整段 quote 替换成回复原文）。
    m_cdate = re.search(r"(?:成立日期|注册日期)[:：]\s*([0-9]{4}[-/.年]?[0-9]{0,2}[-/.月]?[0-9]{0,2})", r)
    m_caddr = re.search(r"地址[:：]\s*([^；;]+)", r)
    if m_cdate or m_caddr:
        subs = []
        if m_cdate:
            newd = normalize_date_cn(m_cdate.group(1))
            qd = re.search(r"\d{4}年\d{1,2}月\d{1,2}日|\d{4}年\d{1,2}月|\d{4}年", q)
            if qd:
                subs.append((qd.group(0), newd))
        if m_caddr:
            newa = m_caddr.group(1).strip().rstrip("；;。、")
            qa = re.search(r"(?:[\u4e00-\u9fa5]{2,}(?:省|市|区|县))?[\u4e00-\u9fa5]*(?:路|街道|街|号|大厦|广场|镇|乡)[^，。；]*", q)
            if qa:
                subs.append((qa.group(0), newa))
        if subs:
            return ("multi_replace", subs, f"复合更正：{subs}")

    # 4) 成立日期核对：查及成立日期：XXXX
    m = re.search(r"查[及找]?成立日期[:：]\s*([0-9]{4}[-/.年]?[0-9]{0,2}[-/.月]?[0-9]{0,2})", r)
    if m:
        return ("replace", m.group(1), f"「{q}」→「{m.group(1)}」(成立日期核对)")

    # 5) 更名：已更名为 X / 更名为 X（有限公司→股份有限公司 这类后缀变更，品牌名不变，安全）
    m = re.search(r"已更名为(.+?)[。，、\s]*$", r) or re.search(r"更名为(.+)", r)
    if m:
        newname = m.group(1).strip().rstrip("。，、")
        return ("replace", newname, f"「{collapse_dup(q)}」→「{newname}」(更名)")

    # 5.5) 规范名称：X → 更名（剥离引导词「规范名称：」取纯名称）
    #      审核用「规范名称：新名」给更名，否则整串(含「规范名称：」)会写进正文
    m = re.search(r"规范名称[:：]\s*(.+?)\s*$", r)
    if m:
        newname = m.group(1).strip().rstrip("。，、")
        return ("replace", newname, f"「{collapse_dup(q)}」→「{newname}」(规范名称更名)")

    # 6) 语句残缺 / 未找到相关数据来源 -> 删整句（无来源的例证/残缺句应去除，全部接受）
    if "语句残缺" in r or "残缺" in r:
        return ("sentence_delete", q, f"语句残缺→删整句「{q}」")
    if "未找到相关数据" in r or "未找到相关" in r:
        return ("sentence_delete", q, f"无数据来源→删整句「{q}」")

    # 6.8) 具体数据无法溯源核实 → 删整句（无来源数据表述应去除，全部接受；
    #      须早于 #8「核实」关键词，否则会被误判归人工）
    if "无法溯源核实" in r or "数据无法溯源" in r:
        return ("sentence_delete", q, f"数据无法溯源→删整句「{q}」")

    # 6.5) 建议改为/可改为 X（置于绝对化删词前，避免「建议改为知名专家」被当成纯删词）
    sv = parse_suggest(r, q)
    if sv:
        return ("replace", sv, f"「{q}」→「{sv}」(改为)")

    # 6.55) 双重否定/多字修正：审核指出 quote 含多余的「不」导致语义反转
    #       （如「不不对」→「不对」、「不会向投资者不对」→「不会向投资者对」、
    #        「"不对…"前多了"不"字」），需删掉多余的一个「不」使语义正确。
    #        触发：回复含「双重否定」或「多了…不…字」；且 quote 实际含 ≥2 个「不」。
    #        修正：删掉 quote 中**第二个**「不」（这些双重否定都是「不…不…」，
    #        删第二个「不」即得正确语义；删第一个会反转，故取第二个）。
    if (("双重否定" in r) or ("多了" in r and "不" in r)) and q.count("不") >= 2:
        idxs = [i for i, ch in enumerate(q) if ch == "不"]
        second = idxs[1]
        new_q = q[:second] + q[second + 1:]
        if new_q != q and new_q.strip():
            return ("replace", new_q, f"双重否定/多字修正「{q}」→「{new_q}」")

    # 7) 绝对化用语/用词/承诺（无替换词）→ 删整个 quote（去绝对化，全部接受）
    if "绝对化" in r or "绝对" in r:
        return ("delete", None, f"绝对化删词「{q}」")

    # 8) 数据/参保类「未能核实 / 无法核实 / 公开信息未能核实」→ 删被引述的小句
    #    审核指出该数据无法核实，按「拿不准删短句」删除 quote 那句表述（不删整段、
    #    也不把「未能核实」四字写进正文）。须早于 #8.1 归人工；须在 #6.5 parse_suggest
    #    之后（若含「应为X」已由 #6.5 抽值走 replace，不会落到这里）。
    if "未能核实" in r or "无法核实" in r:
        return ("delete", None, f"数据未能核实→删小句「{q}」")

    # 8.1) 其余无法确认类（有待核实/未查及/未能验证/无法验证等）→ 人工
    #      仅匹配「有待核实/未查及」等"无法确认"语义；不含单独的"核实"
    #      （如"经核实正确地址应为X"是已给新值的更正，不该归人工，应走更正/删短句）。
    if any(k in r for k in ("有待核实", "未查及", "未查", "公开平台未查",
                            "未能验证", "无法验证")):
        return ("human", None, f"无法核实，需人工决定：{r}")

    # 9.5) 查及位于 X（给出新地址但带引导词）→ 剥离引导词取纯地址
    m = re.search(r"^(?:查及位于|查及)[:：]?\s*(.+?)\s*$", r)
    if m:
        newaddr = m.group(1).strip()
        return ("replace", newaddr, f"「{q}」→「{newaddr}」(地址更正)")

    # 9.55) 地址为/地址是/地址：X（给出新地址但带引导词）→ 剥离引导词取纯地址
    #       审核用「地址为X」给新地址，否则整串(含「地址为」)会写进正文
    m = re.search(r"^(?:地址为|地址是|地址[:：])\s*(.+?)\s*$", r)
    if m:
        newaddr = m.group(1).strip()
        return ("replace", newaddr, f"「{q}」→「{newaddr}」(地址更正)")

    # 9.52) 地址更正（实际位于 X / X 为 YY 地址）：抽正确地址替换 quote 中的错误地址
    #   审核回复指出旧地址错、给出实际地址，如"唐都医院实际位于西安市灞桥区新寺路1号"。
    #   须早于 #10 兜底 replace（否则整段说明会被当替换值写进正文）。仅替换地址部分。
    #   ⚠️ 遇「不符」类 reply 跳过（交 #9.53 括号提取更可靠；本分支 fallback 正则
    #      尾部 [\u4e00-\u9fa5\d]* 无界会吞"与…不符"说明文）；但 reply 含「位于」时
    #      仍走本分支（第一个正则 位于([^，。；]+) 靠逗号截断，安全）。提取的新地址
    #      须过 _is_clean_address 守卫（含叙述词/超长则降级删短语，绝不写说明文进正文）。
    if "位于" in r or "不符" not in r:
        m_real = re.search(r"(?:实际)?位于([^，。；]+)", r)
        if not m_real:
            m_real = re.search(r"([\u4e00-\u9fa5]{2,}市[\u4e00-\u9fa5]{1,3}(?:区|县)"
                               r"[\u4e00-\u9fa5\d]*(?:路|街|号)+[\u4e00-\u9fa5\d]*)", r)
        if m_real:
            newa = m_real.group(1).strip().rstrip("；;。、")
            if _is_clean_address(newa):
                m_old = _ADDR_OLD_RE.search(q)   # 只抓地址主体，不吞「注册地址为」前缀
                if m_old:
                    olda = _ADDR_PREFIX_STRIP.sub("", m_old.group(0).strip())
                    if olda and olda in q:
                        new_full = q.replace(olda, newa)
                        if new_full != q:
                            return ("replace", new_full, f"地址更正「{olda}」→「{newa}」")

    # 9.53) 地址不符更正：注册地址X与…实际主院区地址（Y）不符 → 地址 X→Y
    #       审核用长说明指出旧地址错、正确地址在括号里；须早于 #10 兜底 replace
    #       （否则整段说明会被当替换词写进正文）。仅替换地址部分，保留「注册地址为」等前缀。
    #       ⚠️ m_old 只抓地址主体（省?市+区+路号 / 纯路号），不吞「注册地址为」前缀
    #          （旧正则贪婪会吞前缀，city 误带"注册地址为"）；m_city 用 search 在 olda
    #          内找"X省Y市"（re.match 从开头会误吞前缀）；newa 须过 _is_clean_address。
    if "不符" in r:
        m_new = re.search(r"[（(]([^）)]{2,})[）)]", r)        # 括号里的正确地址
        m_old = _ADDR_OLD_RE.search(q)                        # 只抓地址主体，不吞前缀
        if m_new and m_old:
            olda = _ADDR_PREFIX_STRIP.sub("", m_old.group(1).strip())
            newa = m_new.group(1).strip()
            if olda and _is_clean_address(newa):
                m_city = _ADDR_CITY_RE.search(olda)   # 优先省+市，其次市
                city = m_city.group(1) if m_city else ""
                new_full = q.replace(olda, city + newa)
                if new_full != q:
                    return ("replace", new_full, f"地址更正「{olda}」→「{city}{newa}」")

    # 9.6) 医疗保障承诺类违规表述 → 删短语（不用整句删，避免连带删掉同句合法事实
    #      如「上级会诊转诊制度」，也保留句末句号防粘连）。quote 即审核高亮的承诺子句，
    #      用 delete 精准移除即可；不应替换成「医疗保障承诺」四字，也不整句删。
    if "医疗保障承诺" in r or "医疗承诺" in r or "保障承诺" in r:
        return ("delete", None, f"违规承诺表述→删短语「{q}」")

    # 9.7) 机构已注销/注销 → 删该机构整节（按机构名定位，描述口径不同也能命中）
    #    注意「已于2025年9月10日注销」这类把「已」与「注销」用日期隔开的变体，
    #    故用「注销」而非「已注销」做关键词（前者完全覆盖后者）。
    if "注销" in r:
        name = extract_inst_name(q)
        if name:
            return ("delete_section", name, f"机构已注销→删整节「{name}」")
        # q 抽不到机构全名（如只高亮「公司」两字）→ 无法可靠定位正文机构整节，
        # 归人工，绝不拿 reply 里的分公司名当正文机构名去误删。
        return ("human", None, f"注销但 quote 未含机构全名，无法定位整节：{r}")

    # 9.71) 卫健委无结果（查无此机构）→ 删该机构整节（与机构已注销同等处理）
    if "卫健委无结果" in r or "卫健委无" in r:
        name = extract_inst_name(q) or extract_inst_name(r)
        if name:
            return ("delete_section", name, f"卫健委无结果→删整节「{name}」")
        return ("delete", None, f"卫健委无结果→删表述「{q}」")

    # 9.8) 动作标签（修改/格式规范/规范/调整/完善/整改/优化）无具体替换值 → 删短语
    #      审核给的是「动作指令」而非替换词；若当成替换值会把正文替换成
    #      「修改/格式规范」四字（错误）。标了就删 quote 本身。
    if any(k in r for k in ("格式规范", "整改", "调整", "完善", "优化")) \
            or r.strip() in ("修改", "规范", "规范表述"):
        return ("delete", None, f"动作标签「{r}」→删短语「{q}」")

    # 9.4) 「靠前家」→ 审核回复给出的替换词（前列的/较早/较早的/位于行业前列的/第一…）。
    #       审核回复 r 本身就是「靠前家」这个 token 的替换值，必须尊重 r 的真实值，
    #       不能无差别套硬编码预设（会吞掉更长更精确的回复，如「位于行业前列的」被截成
    #       「前列的」），也不能给「较早」硬加「的」变成「较早的」。
    #       描述性/长文回复（非纯替换值）→ 降级删短语，绝不把说明文字写进正文。
    #       同块若出现两个「靠前家」且评论给了不同替换值，由 process_article 按 occurrence
    #       顺序逐次消费（避免全局替换把多处并成同一值）。
    if "靠前家" in q:
        rv = r.strip().rstrip("。.，、；;：")
        # 局部校验：回复是「靠前家」的替换值（短、无指令词）。
        # 注意：刻意不用全局 _is_pure_replace_value（其把「位于」列为连词会误拒合法值
        # 如「位于行业前列的」）；这里只拦真正的指令/说明词，放行「位于/为」等合法值片段。
        _INST_WORDS = ("建议", "需", "请", "改为", "说明", "不符", "实际",
                       "与", "和", "但", "故", "经", "称", "系", "即", "因",
                       "由", "根据", "显示", "应为")
        if rv and 0 < len(rv) <= 12 and not any(w in rv for w in _INST_WORDS):
            return ("multi_replace", [("靠前家", rv)], f"靠前家→{rv}")
        # 描述性回复（含指令词/较长说明）兜底：从回复抽预设关键词，避免把整段说明写进正文
        if "前列" in r:
            return ("multi_replace", [("靠前家", "前列的")], "靠前家→前列的")
        if "较早" in r:
            return ("multi_replace", [("靠前家", "较早的")], "靠前家→较早的")
        if "第一" in r:
            return ("multi_replace", [("靠前家", "第一")], "靠前家→第一")
        return ("delete", None, f"靠前家相关→删短语「{q}」")

    # 9.9) 「全X、全Y」→「多X、多Y」类并列更正：审核把一列"全XX"改成"多XX"
    #       （如 全品种、全业务链 → 多品种、多业务）。按位置映射逐子串替换，
    #       不整段删除（铁律：审核已给明确替换口径，必须按评论执行，不能过度保护）。
    if ("、" in q) and ("、" in r):
        qp = [x.strip() for x in q.split("、")]
        rp = [x.strip() for x in r.split("、")]
        if len(qp) == len(rp) and len(qp) >= 2 \
                and all(p.startswith("全") for p in qp) \
                and all(p.startswith("多") for p in rp) \
                and all(a in q for a, _ in zip(qp, rp)):
            subs = [(a, b) for a, b in zip(qp, rp)]
            return ("multi_replace", subs, f"全→多并列替换{subs}")

    # 9.95) 单字「全」精细化：评论仅标了单字「全」，回复是扩展/删除口径。
    #       绝不能无差别把全篇「全」替换（会破坏 全国/全面 等固定词）；落地时由
    #       process_article 用评论 content_anchor_id 限定到锚定块。
    #       - 回复含「广泛覆盖」→ 整体「全覆盖」→「广泛覆盖」（只换单字会成「广泛覆盖覆盖」）
    #       - 回复含「多」→ 「全」→「多」，apply_edit 复合词感知保护 全面/全部/全国/全覆盖…
    #       - 回复含「删除」→ 删单字「全」，apply_edit 去掉 覆 保护使「全覆盖」→「覆盖」
    #       - 其他「全」回复（拿不准）→ 删该短语（锚定块内），绝不无差别替换
    if q == "全":
        if "广泛覆盖" in r:
            # 整体「全覆盖」→「广泛覆盖」（old_sub/new_sub 明确；multi_replace 走锚点限定）
            return ("multi_replace", [("全覆盖", "广泛覆盖")], "全覆盖→广泛覆盖")
        if "多" in r:
            return ("replace", "多", "全→多（复合词感知）")
        if "删除" in r:
            return ("delete", None, "删单字全（复合词感知）")
        # 其他回复：若回复是纯替换值（短、无说明性标点/连词），按值替换
        # （如「全」→「完整交易周期」）；否则拿不准→删该短语（锚定块内），绝不无差别替换。
        if _is_pure_replace_value(r):
            return ("replace", r, f"单字全→「{r}」（锚定块内替换）")
        return ("delete", None, f"单字全→删短语「{q}」（{r}）")

    # 9.97) 添加类指令：加上/补充/补加 X → 在 quote 后追加「（X）」，不替换原句、不删事实。
    #       审核本意是「补上机构名/限定词」而非改写或删除（如「该机构成立于2003年」→「加上示范口腔」
    #       意为补注机构归属）。绝不能把「加上X」当纯替换值写进正文（会变成乱码「加上示范口腔」），
    #       也绝不能删原句（丢失事实）。落地由 apply_edit 的 append 分支处理（幂等防跨轮重跑重复追加）。
    m = re.search(r'^(?:加上|补加|补充[上入]?)\s*(.+)$', r)
    if m:
        add = m.group(1).strip().rstrip("。.，、；;")
        if add:
            return ("append", add, f"添加类指令→追加「（{add}）」到「{q}」")

    # 9.99) 回复是 quote 内的一个词（标注/注解，非替换值）：审核高亮了一整段，
    #       回复只是其中某个关键词（如 quote="操作的安全性"、reply="安全"），
    #       意为「这个词有问题，删掉它」→ 删 quote 短语（拿不准删短句），
    #       绝不能把「安全」当 replace 值写进正文（会把整句/标题替换成「安全」灾难）。
    #       仅短回复(2~4字)且为 quote 子串时触发，避免误伤正常替换值（如「第一」「控股」）。
    if 2 <= len(r) <= 4 and r and r in q:
        return ("delete", None, f"回复为 quote 内关键词（注解）→删短语「{q}」")

    # 10) 回复无指令词：判断是否可作为『纯替换值』
    #     纯值（短、无说明性标点/叙述词）→ replace quote→reply
    #     非纯值（说明性长文/句子）→ 降级为删 quote 短句（绝不乱加内容）
    if not any(kw in r for kw in ("建议", "需", "请")):
        if _is_pure_replace_value(r):
            return ("replace", r, f"「{q}」→「{r}」")
        return ("delete", None, f"回复非纯替换值→删短语「{q}」")

    # 11) 兜底：长文/模糊指令一律删 quote 短句（拿不准就删，不归人工、不乱加）
    if len(r) > 25:
        return ("delete", None, f"拿不准→删短语「{q}」")
    return ("delete", None, f"拿不准→删短语「{q}」")


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
    定位实际片段。仅对短 phrase（<=15字）启用，长段交给人工避免残缺删。
    守卫：极短串（<=4字）要求近乎完全匹配（前 len-1 字须命中），否则返回 None。
    避免 "2006" 被弱前缀 "20" 误匹配到正文 "2026年…" 后将整句替换成 "2011" 的
    灾难（极短串模糊匹配几乎必为误判，应归人工）。"""
    if len(phrase) <= 1:
        return None
    # 匹配质量要求：极短串必须高比例前缀命中；长串才容忍尾部变体
    min_al = 3 if len(phrase) > 4 else len(phrase)
    for al in range(min(len(phrase), 10), min_al - 1, -1):
        anchor = phrase[:al]
        for bt in btext.values():
            pos = bt.find(anchor)
            if pos >= 0:
                seg = _segment_at(bt, pos, al)
                if len(seg) >= al:
                    return seg
    return None


def resolve_hits(btext, phrase, cmt_anchor, limit=1):
    """定位含 phrase 的块。若评论带 content_anchor_id 且 phrase 为单字(<=limit)，
    则**仅**在锚定块内查找——避免单字（全/最）无差别扩散到全篇、把 全国/全面 等
    固定词也误改（曾见「全」→「广泛覆盖」把全篇「全」都替换成灾难）。多字短语(>limit)
    仍全文定位（特异性足够，且 靠前/靠前家/中泰期货 等需文档级一致生效）。
    锚定块内找不到 phrase → 返回 []（交由上层 unfound，绝不跨块扩散）。"""
    if cmt_anchor and len(phrase) <= limit:
        if phrase in (btext.get(cmt_anchor) or ""):
            return [cmt_anchor]
        return []
    return [bid for bid, bt in btext.items() if phrase in bt]


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


# 长短语替换的合法结束词（用于补尾：评论 quote 被截断时，按正文实际词收尾）。
# 常识机构/公司后缀，按长度降序匹配优先长后缀（避免「有限公司」抢匹配「有限责任公司」）。
REPLACE_END_WORDS = ["有限责任公司", "股份有限公司", "有限公司", "股份公司",
                     "医院", "诊所", "门诊部", "卫生院", "医疗中心", "中心医院",
                     "证券公司", "证券机构", "期货公司", "期货机构", "基金公司",
                     "保险公司", "信托公司", "财务公司", "科技公司", "研究中心",
                     "研究院", "事务所", "集团", "银行", "学校", "大学"]


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
            best = None  # (w_len, cand) 优先最长后缀，避免「有限公司」抢匹配「有限责任公司」
            for w in REPLACE_END_WORDS:
                ep = bt.find(w, pos)
                if ep >= 0:
                    cand = ep + len(w)
                    if best is None or len(w) > best[0]:
                        best = (len(w), cand)
            if best:
                return bid, bt[pos:best[1]]
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


# 常识机构/公司后缀（用于匿名化保留行业区分，非客户专属）
_INST_SUFFIXES = ("有限责任公司", "股份有限公司", "有限公司", "股份公司",
                  "医院", "诊所", "门诊部", "卫生院", "医疗中心", "中心医院",
                  "集团", "银行", "证券公司", "证券机构", "期货公司", "期货机构",
                  "基金公司", "保险公司", "信托公司", "财务公司", "科技公司",
                  "研究中心", "研究院", "事务所", "学校", "大学")


def xxify(token):
    """具体公司/机构名匿名化：xx + 末尾常识机构后缀（用于保留行业区分）。
    纯通用、不写死任何具体行业/客户：后缀从 token 自身推断。
    例：「某钢铁公司」→xx公司、「某期货公司」→xx期货、「西安某医院」→xx医院。"""
    t = token.strip()
    if not t:
        return "xx"
    for suf in _INST_SUFFIXES:
        if t.endswith(suf):
            return "xx" + suf
    return "xx"


def _safe_replace(old, phrase, value):
    """子串替换（replace 动作用），对 value 含 phrase 的情况做幂等保护。

    背景：skill 每次运行都会重处理「未解决」评论（铁律：绝不点解决评论），
    故同一篇文章可能被 apply 多轮。普通替换（value 不含 phrase）一轮后 phrase
    即从正文消失，重跑自然幂等；但当 value ⊇ phrase（如短机构名→以该短名开头
    的全称「示例市示范口腔医疗服务有限责任公司」→「…示范口腔诊所（示范口腔）」），
    替换后 value 内仍含 phrase，重跑会再次命中并把后缀再叠一份，3 轮即「XYYY」式
    膨胀（已发生过的真实事故）。

    幂等策略：value 含 phrase 时，跳过「已落在一段完整 value 内」的 phrase 命中
    （即该位置已是上一轮替换留下的 value），只替换尚未替换的独立 phrase。这样
    重跑对已替换文本为 no-op，不再膨胀。"""
    if not phrase or value == phrase:
        return old
    if phrase not in old:
        return old
    if phrase not in value:
        # value 不含 phrase：替换后 phrase 消失，重跑自然幂等
        return old.replace(phrase, value)
    # value 含 phrase：标记 old 中「已是完整 value」的区间，跳过其内的 phrase 命中
    covered = []
    start = 0
    while True:
        k = old.find(value, start)
        if k < 0:
            break
        covered.append((k, k + len(value)))
        start = k + len(value)

    def _in_covered(i):
        for cs, ce in covered:
            if cs <= i < ce:
                return True
        return False

    out = []
    i, n = 0, len(old)
    while i < n:
        if old.startswith(phrase, i) and not _in_covered(i):
            out.append(value)
            i += len(phrase)
        else:
            out.append(old[i])
            i += 1
    return "".join(out)


def _is_punct_only(s):
    """字符串是否仅由标点/空白构成（删短语后残留孤立句号等，应视为空块）。"""
    return all(c in "，。、；：！？…—" for c in s)


def _delete_phrase(old, phrase):
    """删短语：优先连同前导标点(，、；：)一起移除并保留句末句号，避免
    「X，承诺表述。其余。」删承诺后变成「X，其余。」的粘连/丢句号。

    - 短语前有前导标点（如「，可为用户提供安全的诊疗保障」）→ 连前导标点一起删，
      保留句末句号：「...上级会诊转诊制度。」（合法事实+句号都保留）。
    - 找不到前导标点（句末短语场景，如「推荐约牛股票。」）→ 移除短语及其后一个尾标点
      （原行为），避免残留「推荐。其他内容。」里的孤立句号。"""
    if not phrase or phrase not in old:
        return old
    pat = re.compile(r"[，、；：]\s*" + re.escape(phrase))
    new = pat.sub("", old)
    if new != old:
        return new
    # 句末短语场景：移除短语及其后一个尾标点
    return re.sub(re.escape(phrase) + r"[，。、；：]?", "", old)


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
        if new.strip():
            return new
        # 整块清空则回退：改为只删定位短语(及尾随标点)，避免违规承诺/绝对化词残留。
        # 若回退后仍为空（整块都是要删的内容，如整块都是联系方式句）→ 返回空串触发整块清空，
        # 绝不 return old 残留违规内容；phrase 为 None 时 old.replace(None) 会崩，直接清空。
        if not phrase:
            return ""
        fallback = re.sub(r"[，。、；：:]\s*$", "", old.replace(phrase, "")).strip()
        return fallback if fallback else ""
    if action == "delete_word":
        return old.replace(phrase, "")       # 全替换（同块/同词多出现都改）
    if action == "append":
        # 添加类指令（加上/补充 X）：在 phrase 后追加「（X）」，保留原句事实。
        # 幂等：add 已出现在正文则 no-op，防跨轮重跑重复追加（如「（示范口腔）（示范口腔）」）。
        add = value or ""
        if not add:
            return old
        if add in old:
            return old
        if phrase and phrase in old:
            return old.replace(phrase, phrase + "（" + add + "）", 1)
        return old + "（" + add + "）"
    if action == "delete":
        if phrase == "最":
            # 绝对化用词「最」删除时，保护时间/顺序/方位复合词
            # （最后/最终/最初/最近/最新/最早/最前/最上…），这些不是最高级，
            # 删「最」会破坏句子（最后一环→后一环）。只删「最+自由词」的绝对化用法。
            PROTECT = "后终初近新早先前上中下内外底高"
            tmp = re.sub(r"最([" + PROTECT + r"])",
                         lambda m: "\x00" + m.group(1), old)   # 占位保护
            tmp = re.sub(re.escape(phrase) + r"[，。、；：]?", "", tmp)  # 删其余「最」
            tmp = tmp.replace("\x00", "最")                     # 还原保护
            return tmp if tmp.strip() else old
        if phrase == "全":
            # 单字「全」删除：仅删“全+自由名词”，保护固定词 全面/全部/完全/全权/全国/全资
            # （避免「全面结算会员」→「面结算会员」、「全部变量」→「部变量」式灾难）。
            # 注意：刻意不保护「覆」（全覆盖→覆盖 正是删除意图，如「省级行政区的全覆盖网络」→「覆盖网络」）。
            PROT = "面部完权国资"
            tmp = re.sub(r"全([" + PROT + r"])",
                         lambda m: "\uE000" + m.group(1), old)   # 占位保护固定词
            tmp = re.sub(r"全[，。、；：]?", "", tmp)              # 删其余独立「全」(含 全覆盖→覆盖)
            tmp = tmp.replace("\uE000", "全")                      # 还原保护词
            return tmp if tmp.strip() else old
        # 通用删短语：优先「前导标点(，、；：) + 短语」整体移除，保留句末句号防粘连
        # （医疗保障承诺/标签类删短语常是「X，承诺表述。」→「X。」而非「X，」）；
        # 找不到前导标点（句末短语场景）则移除短语及其后一个尾标点（原行为）。
        new = _delete_phrase(old, phrase)
        if not new.strip() or _is_punct_only(new.strip()):
            # 评论要删的短语几乎就是整块全部内容 → 标记删除整块（不再保留空块/孤立标点）
            residual = old.replace(phrase, "").strip()
            if len(residual) <= 1:
                return ""                      # 空标记：process_article 将删除该 block
            # 否则回退只删定位短语（保留其他内容，避免误删整块）
            fallback = re.sub(r"[，。、；：:]\s*$", "", residual).strip()
            return fallback if fallback else old
        return new
    if action == "replace" and len(phrase) == 1:
        # 单字替换为多字 value：整词感知，避免无差别替换破坏正文复合词
        if phrase == "全" and "多品种" in value:
            # 仅「全品种」占位整词替换为「多品种」；保护「全面/全部/完全/全覆盖」等
            # （曾见正文 13 处「全面结算会员」被误改成「多品种面结算会员」的灾难性误伤）
            tmp = old.replace("全品种", "多品种")
            return tmp if tmp.strip() else old
        if phrase == "全" and value == "多方位":
            # 审核把「全方位」里的「全」标成「多方位」，本意是「全方位」→「多方位」
            tmp = old.replace("全方位", "多方位")
            return tmp if tmp.strip() else old
        if phrase == "全":
            # 其余「全」→多/…：仅替换“全+自由名词”，保护固定词 全面/全部/完全/全权/全国/全覆盖
            # （避免「全面结算会员」→「多面结算会员」、「全部变量」→「多部变量」）
            PROT = "面部完权国覆资"
            tmp = re.sub(r"全([" + PROT + r"])",
                         lambda m: "\uE000" + m.group(1), old)   # 占位保护固定词
            tmp = tmp.replace("全", value)                        # 替换其余独立「全」
            tmp = tmp.replace("\uE000", "全")                      # 还原保护词
            return tmp if tmp.strip() else old
        if phrase == "最" and value == "较早":
            # 审核把「最早」里的「最」标成「较早」，本意是「最早」→「较早」
            tmp = old.replace("最早", "较早")
            return tmp if tmp.strip() else old
    return _safe_replace(old, phrase, value)  # replace 全替换（含「改为 X」「资质」等）；value⊇phrase 时幂等防膨胀


# ====================== 发现 / 处理 ======================
def discover_dir(token, node_token):
    nd = get_node_info(token, node_token)
    space_id = resolve_space_id(nd.get("space_id"))
    children = list_wiki_children(token, space_id, node_token)
    docs = []
    for i, it in enumerate(children):
        n = it.get("node", it)
        if n.get("obj_type") == "docx" and n.get("obj_token"):
            comments = get_doc_comments(token, n["obj_token"])
            unres = [c for c in comments if c.get("is_solved") != True]
            if unres:
                enrich_reactions(token, n["obj_token"], unres)
            docs.append({"i": i, "title": n.get("title", ""),
                         "obj": n["obj_token"], "comments": unres})
    return docs


def process_article(token, art, do_apply, backup, extra_rules=None):
    obj = art["obj"]
    comments = art["comments"]
    blocks = get_doc_blocks(token, obj)
    btext = {b.get("block_id"): extract_block_text(b) for b in blocks}

    edits, human, ignored, section_deletes, skipped = {}, [], [], [], []
    block_deletes = {}   # delete 动作导致整块清空 → 真正删除该 block
    def unfound(q, r, n, c):
        """找不到锚点时的处置：已点赞(用户已手动改)→忽略；否则→归人工。"""
        if is_comment_liked(c):
            ignored.append({"quote": q, "reply": r,
                            "note": n + "（已点赞，用户已手动改→忽略）"})
        else:
            human.append({"quote": q, "reply": r, "note": n})
    title_candidates = []   # 标题候选（品牌名替换/绝对化删词也要落到标题）
    for cmt in comments:
        p = parse_comment_text(cmt)
        quote = (p.get("quote") or "").strip()
        reply = p["replies"][-1]["text"].strip() if p["replies"] else ""
        cmt_anchor = (cmt.get("extra") or {}).get("content_anchor_id")
        action, value, note = classify(quote, reply)
        if action == "skip":
            # 审核确认不改动（或客户专属闸门/规则拦截）：内容保持原样，评论保留。
            # ⚠️ 必须显式记录——静默 continue 会让审核读报告误以为评论被处理/删掉，
            #    进而手动在飞书解决评论，造成「评论删了、内容没改」的假象。
            skipped.append({"quote": quote, "reply": reply, "note": note})
            continue
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
                    unfound(tok, reply, f"xx代替未找到「{tok}」", cmt)
                    continue
                for bid in thit:
                    e = edits.setdefault(bid, {"original": btext[bid],
                                               "new": btext[bid], "ops": []})
                    key = ("replace", tok, val)
                if not any(o[:3] == key for o in e["ops"]):
                    e["ops"].append(("replace", tok, val, note))
            continue
        if action == "delete_section":
            # 整节删除：用机构名在根级块定位，删该块到下一个章节标题前
            name = value
            h = None
            for idx, b in enumerate(blocks):
                if name in (extract_block_text(b) or ""):
                    h = idx
                    break
            if h is None:
                unfound(quote, reply, f"删整节未找到「{name}」", cmt)
                continue
            end = len(blocks)
            for j in range(h + 1, len(blocks)):
                if is_section_header(blocks[j]):
                    end = j
                    break
            del_num = None
            hm = re.match(r"^(\d+)[.、．]", (extract_block_text(blocks[h]) or "").strip())
            if hm:
                del_num = int(hm.group(1))
            section_deletes.append((name, h, end, del_num))
            continue
        if action == "multi_replace":
            # 复合更正：value 为 [(old_sub, new_sub), ...]，对每个子串在其出现的
            # 所有正文块内做替换（同词多块也要落全，避免只改第一处显得没改）
            subs = value
            for (old_sub, new_sub) in subs:
                hits = resolve_hits(btext, old_sub, cmt_anchor)
                if not hits:
                    unfound(quote, reply, f"复合更正：未找到「{old_sub}」", cmt)
                    continue
                for bid in hits:
                    e = edits.setdefault(bid, {"original": btext[bid],
                                               "new": btext[bid], "ops": []})
                    key = ("replace", old_sub, new_sub)
                    if not any(o[:3] == key for o in e["ops"]):
                        e["ops"].append(("replace", old_sub, new_sub, note))
            continue

        q_clean = quote.replace("**", "").strip()
        phrase = collapse_dup(q_clean) if action == "replace" else q_clean
        # 去尾标点守卫：评论 quote 常把句末标点（。！？；）一起高亮，但替换值 value 不含
        # 该标点。若不剥离，会把句号一并替换掉造成「广泛覆盖这意味着」式粘连。
        # 仅当 value 不以该标点结尾时才剥离（value 自带标点则不剥，避免双标点）。
        if action == "replace" and value and phrase and phrase[-1] in "。！？；" \
                and not value.endswith(phrase[-1]):
            phrase = phrase[:-1]
        # 通用守卫：评论 quote 被截断、reply 已含应被吃掉的尾字时，在 anchor 块内把定位
        # 短语向右扩展，连同尾字一并替换为 value（避免「value+尾字」冗余；并借更长短语
        # 独占锚定块，规避同短语多块不同值的全文扩散冲突）。仅当评论带 anchor 且在 anchor
        # 块内能匹配 phrase 时生效；无 anchor 不扩展（保守，不影响无锚点评论）。
        if action == "replace" and cmt_anchor and cmt_anchor in btext and value:
            _ab = btext[cmt_anchor]
            _ap = _ab.find(phrase)
            if _ap >= 0:
                _suf = ""
                _j = _ap + len(phrase)
                while _j < len(_ab) and _ab[_j] not in "，。、；：！？\n ）) ":
                    _suf += _ab[_j]; _j += 1
                _best = 0
                for _k in range(1, len(_suf) + 1):
                    if value.endswith(_suf[:_k]) and _suf[:_k] and value[:-_k] not in ("", phrase):
                        _best = _k
                if _best and (phrase + _suf[:_best]) in _ab:
                    phrase = phrase + _suf[:_best]
        # 标题候选（品牌名替换/绝对化删词也要落到标题）：仅 replace/delete_word，
        # 且短语长度>1（单字 全/最 等不落标题，避免误伤标题里的 全国/全面 等）。
        if action in ("replace", "delete_word") and phrase and len(phrase) > 1:
            title_candidates.append((action, phrase, value, note))
        # 同词多块：默认应用到所有含该短语的块；但若评论带锚点且 phrase 较短，
        # 则 resolve_hits 已将其限定到锚定块（避免 全/最 等短词扩散到全篇）。
        hit = resolve_hits(btext, phrase, cmt_anchor)
        if not hit:
            # 锚定短词：锚定块内都找不到 phrase → 直接 unfound，绝不跨块扩散到全篇
            if cmt_anchor and len(phrase) <= 3:
                unfound(quote, reply, f"锚定块未找到「{phrase}」", cmt)
                continue
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
                unfound(quote, reply, f"全文未找到「{phrase}」", cmt)
                continue
        for bid in hit:
            e = edits.setdefault(bid, {"original": btext[bid],
                                       "new": btext[bid], "ops": []})
            # 去重：同一块内相同 (action, phrase, value) 只保留一个，
            # 否则 replace 的 value 含 phrase（如 某品牌→某品牌APP）会被多次叠加成 APPAPP
            key = (action, phrase, value)
            if not any(o[:3] == key for o in e["ops"]):
                e["ops"].append((action, phrase, value, note))

    for bid, e in edits.items():
        new = e["original"]
        ops = sorted(e["ops"], key=lambda x: -len(x[1]))
        # 同 phrase 的 replace 多 op（如一块内两个「靠前家」给了不同替换值）→ 逐 occurrence
        # 消费，避免全局替换把多处并成同一值（#9.4 同块多值场景）。仅当同 phrase 的 op ≥2
        # 才启用逐次替换；其余（含单 op 的 中泰期货→中泰证券 等）走普通 apply_edit，行为不变。
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
        # 同一块多条编辑：按 phrase 长度从长到短，避免短词先替换破坏长词
        #   （如「某资质团队」须先于「某资质」，避免短词先替换破坏长词）
        #   已按 occurrence 处理的重复 phrase op 跳过（applied_idx），避免二次替换/膨胀。
        for i, (a, ph, v, _) in enumerate(ops):
            if i in applied_idx:
                continue
            new = apply_edit(new, a, ph, v)
        e["new"] = new
        if new == "":
            block_deletes[bid] = e["original"]
            print(f"  🗑️ 整块删除(评论要求删整段) 块 {bid[:12]}…")
            continue
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

    # 整节删除（机构已注销等）：块级删除 [h, end)，按 h 降序保证下标有效
    for name, h, end, _d in sorted(set(section_deletes), key=lambda x: -x[1]):
        if end <= h:
            continue
        if do_apply:
            deleted = [blocks[k] for k in range(h, end)]
            url = (f"https://open.feishu.cn/open-apis/docx/v1/documents/{obj}"
                   f"/blocks/{obj}/children/batch_delete?document_revision_id=-1")
            resp = api("DELETE", url, token, body={"start_index": h, "end_index": end})
            if resp.get("code") == 0:
                blocks2 = get_doc_blocks(token, obj)
                full2 = "\n".join(extract_block_text(b) or "" for b in blocks2)
                if name not in full2:
                    rec = backup.setdefault(obj, {})
                    rec.setdefault("__SECTION__", []).append({
                        "name": name, "range": [h, end],
                        "deleted": [{"block_id": b.get("block_id"),
                                     "block_type": b.get("block_type"),
                                     "text": extract_block_text(b) or ""}
                                    for b in deleted]})
                    print(f"  ✅ 删整节「{name}」 区间[{h},{end}) 共{end-h}块 校验一致")
                else:
                    print(f"  ❌ 删整节校验失败：删除后正文仍含「{name}」")
            else:
                print(f"  ❌ 删整节失败 code={resp.get('code')} {resp.get('msg')}")
        else:
            print(f"  🔍 [预览] 删整节「{name}」 区间[{h},{end}) 共{end-h}块:")
            for k in range(h, end):
                print(f"       [{k}] {(extract_block_text(blocks[k]) or '')[:50]}")

    # 删整节后重排后续同级编号标题（如删第2节后 3.→2.、4.→3.），避免留序号断档
    deleted_nums = sorted({num for (_, _, _, num) in section_deletes if num})
    if deleted_nums:
        if do_apply:
            blocks = get_doc_blocks(token, obj)   # 删除后刷新块列表
        skip = set()
        if not do_apply:   # 预览时未删，跳过将被删的标题块本身
            for (_, hh, ee, _) in section_deletes:
                skip.update(range(hh, ee))
        for idx, b in enumerate(blocks):
            if idx in skip:
                continue
            t = (extract_block_text(b) or "").strip()
            m = re.match(r"^(\d+)([.、．])\s*\S", t)
            if not m:
                continue
            old_num = int(m.group(1))
            delta = sum(1 for dn in deleted_nums if dn < old_num)
            if delta == 0:
                continue
            new_num = old_num - delta
            new_text = str(new_num) + m.group(2) + t[m.end(2):]
            if do_apply:
                resp = update_block_text(token, obj, b.get("block_id"), new_text)
                ok = resp.get("code") == 0
                if ok:
                    cur = {x.get("block_id"): extract_block_text(x) or ""
                           for x in get_doc_blocks(token, obj)}
                    ok = cur.get(b.get("block_id")) == new_text
                if ok:
                    rec = backup.setdefault(obj, {})
                    rec.setdefault("__RENUMBER__", []).append(
                        {"block_id": b.get("block_id"), "old_text": t, "new_text": new_text})
                    print(f"  🔢 序号重排 #{old_num}→#{new_num}：{new_text[:40]}")
                else:
                    print(f"  ❌ 序号重排失败 #{old_num}→#{new_num} "
                          f"code={resp.get('code')} {resp.get('msg')}")
            else:
                print(f"  🔍 [预览] 序号重排 #{old_num}→#{new_num}：{new_text[:40]}")

    # 整块删除（delete 动作导致整块清空，评论要求删整段）：真正删除该 block
    if block_deletes:
        blocks_now = get_doc_blocks(token, obj) if do_apply else blocks
        idx_now = {b.get("block_id"): i for i, b in enumerate(blocks_now)}
        for bid in sorted(set(block_deletes), key=lambda b: -idx_now.get(b, 0)):
            orig = block_deletes[bid]
            if do_apply:
                i = idx_now.get(bid)
                if i is None:
                    print(f"  ⚠️ 删整块找不到下标 块 {bid[:12]}")
                    continue
                url = (f"https://open.feishu.cn/open-apis/docx/v1/documents/{obj}"
                       f"/blocks/{obj}/children/batch_delete?document_revision_id=-1")
                resp = api("DELETE", url, token, body={"start_index": i, "end_index": i + 1})
                if resp.get("code") == 0:
                    blocks2 = get_doc_blocks(token, obj)
                    if not any(x.get("block_id") == bid for x in blocks2):
                        backup.setdefault(obj, {})[bid] = orig
                        print(f"  ✅ 删整块 块 {bid[:12]}… 校验一致")
                        blocks_now = blocks2
                        idx_now = {b.get("block_id"): i2
                                   for i2, b in enumerate(blocks2)}
                    else:
                        print(f"  ❌ 删整块校验失败 块 {bid[:12]}")
                else:
                    print(f"  ❌ 删整块失败 code={resp.get('code')} {resp.get('msg')}")
            else:
                print(f"  🔍 [预览] 删整块 块 {bid[:12]}…")

    for h in human:
        print(f"  👤 需人工: {h['note']} | 「{h['quote'][:24]}」→「{h['reply'][:24]}」")
    for ig in ignored:
        print(f"  🙈 已忽略(点赞): {ig['note']} | 「{ig['quote'][:24]}」")
    for sk in skipped:
        print(f"  ⏭️ 跳过(评论保留·内容未改): 「{sk['quote'][:28]}」→「{sk['reply'][:28]}」 ({sk['note']})")
    if skipped:
        print(f"  ⚠️ 上述 {len(skipped)} 条评论按规则保留未改，请勿在飞书解决/删除。")
    return len(human), len(ignored), len(skipped)


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


def cmd_review(dir_node, do_apply, from_index=None):
    token = get_token()
    docs = discover_dir(token, dir_node)
    if from_index is not None:
        docs = docs[from_index:]
    wc = [d for d in docs if d["comments"]]
    print(f"\n目录 {dir_node}：共 {len(docs)} 篇，有评论 {len(wc)} 篇"
          f"（无评论 {len(docs)-len(wc)} 篇已跳过）")
    backup = {}
    tot_h = tot_ig = tot_sk = 0
    for d in wc:
        print(f"\n{'='*66}\n[{d['i']}] 《{d['title'][:40]}》\n{'='*66}")
        try:
            h, ig, sk = process_article(token, d, do_apply, backup)
        except Exception as _e:
            import traceback as _tb
            _tb.print_exc()
            print(f"  ❌ 处理 [{d['i']}] 异常，跳过该篇继续：{_e!r}")
            h, ig, sk = 0, 0, 0
        tot_h += h
        tot_ig += ig
        tot_sk += sk
    if do_apply and backup:
        fn = f"{dir_node}_backup.json"
        with open(fn, "w", encoding="utf-8") as f:
            json.dump(backup, f, ensure_ascii=False, indent=2)
        print(f"\n✅ 备份 {fn}（{sum(len(v) for v in backup.values())} 块）")
    print(f"\n⚠️ 评论均未点解决（铁律）。自动忽略(已点赞) {tot_ig} 条，"
          f"跳过未改 {tot_sk} 条，仍需人工 {tot_h} 条。")
    if tot_sk:
        print(f"   ⚠️ {tot_sk} 条「跳过未改」的评论仍保留在飞书，请勿解决/删除，"
              f"交由审核人员按规则确认。")
    else:
        print("   交由审核人员处理评论状态即可。")


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
            if bid == "__SECTION__":   # 整节删除的备份（块已不存在，无法自动重建）
                print(f"  ⏭️ 跳过整节删除备份 (obj={obj[:12]})：需按备份中的 "
                      f"deleted 文本手动重建被删块")
                continue
            if bid == "__RENUMBER__":   # 序号重排还原（撤销 --apply 的序号重排）
                for rec in original:
                    resp = update_block_text(token, obj, rec["block_id"], rec["old_text"])
                    ok = resp.get("code") == 0
                    if ok:
                        cur = {x.get("block_id"): extract_block_text(x) or ""
                               for x in get_doc_blocks(token, obj)}
                        ok = cur.get(rec["block_id"]) == rec["old_text"]
                    tag = "✅" if ok else "❌"
                    print(f"  {tag} 还原序号重排 {rec['block_id'][:12]}…")
                    total += 1
                continue
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
    import argparse

    p = argparse.ArgumentParser(
        prog="audit_review.py",
        description="飞书 GEO 文章审核改稿：按评论批量修改有未解决评论的文章（备份+读回校验，绝不解评论）。",
        epilog="示例:\n"
               "  python audit_review.py --dir <wiki_node>                 # 只读模式，输出修改计划\n"
               "  python audit_review.py --dir <wiki_node> --apply          # 应用修改（自动备份+读回校验）\n"
               "  python audit_review.py --dir <wiki_node> --probe          # 只看该目录下所有文章标题\n"
               "  python audit_review.py --dir <wiki_node> --titles         # 列出所有文章标题\n"
               "  python audit_review.py --dir <wiki_node> --from-index 3   # 从第 3 篇开始\n"
               "  python audit_review.py --fix-title <obj_token> <新标题>    # 单篇改标题\n"
               "  python audit_review.py --restore backup.json              # 回滚备份",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--dir", dest="dir_node", metavar="NODE",
                   help="客户目录 wiki node（或文档 obj_token，单独处理单篇）")
    p.add_argument("--probe", action="store_true", help="只列出目录下文章及评论数，不产出计划")
    p.add_argument("--apply", action="store_true", help="应用修改（默认只读输出计划）")
    p.add_argument("--titles", action="store_true", help="只列出目录下所有文章标题")
    p.add_argument("--from-index", type=int, default=None, metavar="N",
                   help="从第 N 篇文章开始处理（0 起）")
    p.add_argument("--space-id", dest="space_id", default=None, metavar="SID",
                   help="飞书 wiki 空间 ID（覆盖环境变量 FEISHU_SPACE_ID）")
    p.add_argument("--fix-title", nargs=2, metavar=("OBJ_TOKEN", "新标题"),
                   help="单篇改标题，不处理正文")
    p.add_argument("--restore", metavar="BACKUP.json",
                   help="回滚某次备份文件，不联网")
    args = p.parse_args()

    if args.space_id:
        set_space_id(args.space_id)

    if args.restore:
        return cmd_restore(args.restore)

    if args.fix_title:
        return cmd_fix_title(args.fix_title[0], args.fix_title[1])

    if not args.dir_node:
        p.print_help()
        sys.exit(1)

    if args.titles:
        return cmd_titles(args.dir_node, args.from_index)
    if args.probe:
        return cmd_probe(args.dir_node, args.from_index)
    return cmd_review(args.dir_node, args.apply, args.from_index)


if __name__ == "__main__":
    main()
