---
name: feishu-audit-review
description: 飞书 GEO 文章审核改稿闭环。给定客户目录 wiki node，自动找出「有未解决评论」的文章，按评论标注做子串替换/删除/事实更正/标题修正，备份+读回校验；绝不解评论、只改有评论的文章、客户专属规则不串用。适用于把审核人员在飞书文档里的批注批量落到正文。
---

# 飞书审核改稿闭环（feishu-audit-review）

## 何时用
- 用户是 GEO 优化师，审核人员在飞书文档评论里给修改意见，用户想批量把批注落到正文。
- 输入：一个客户目录的 wiki node（如 `https://<your-wiki>.feishu.cn/wiki/<node_token>`）。
- 输出：有评论的文章被按标注修改，原文备份，评论保持未解决。

## 铁律（务必遵守）
1. **只修改有未解决评论的文章**，无评论文章直接跳过。
2. **改完绝不点/标记「解决评论」**（is_solved=true）。审核人员自己处理评论状态。
3. **客户专属替换规则不串用**：某一客户特有的公司名替换（如「A公司→B公司」）是该企业专属，不要套用到其他客户。公司名更正必须由该客户的评论驱动（如「旧名，已更名为 X」）。通用规则仅限：靠前→第一、靠前家→前列的、全X→多X、删来源引用、（来源：企业知识库）/从知识库信息来看、联系方式整句删。
4. **同词多块**：substring 替换必须应用到「所有含该短语的块」，不能只改第一个命中块（否则评论锚在别的重复块上会显得没改）。
5. **子串替换要保留块内其他文本**，整块覆盖会互相 clobber —— 同一块多条评论先合并再写一次。

## API 要点（已验证）
- 凭据：飞书 APP_ID/APP_SECRET（同 feishu-wiki-paste bot），`POST /auth/v3/tenant_access_token/internal` 取 tenant_access_token。凭据**不要硬编码**到脚本：从环境变量 `FEISHU_APP_ID`/`FEISHU_APP_SECRET` 或同目录 `.env` 文件读取（仓库附 `.env.example` 模板，`.env` 已被 git 忽略）。
- 评论读：`GET /open-apis/drive/v1/files/{obj_token}/comments?file_type=docx`（**不是** /docx/v1/.../comments）。
- 块读：`GET /open-apis/docx/v1/documents/{obj}/blocks/{obj}/children`。
- 块写：`PATCH /open-apis/docx/v1/documents/{obj}/blocks/{bid}`，body 用 `update_text_elements`（整块替换，与块类型无关）。
- **改文档标题**：标题 = Page 根块的 `page.elements` 文本，block_id 就是 document_id 本身。PATCH `/documents/{obj}/blocks/{obj}`，body 用 `{"update_text_elements":{"elements":[{"text_run":{"content":"新标题"}}]}}` —— **注意：page 标题块不能带 `text_element_style`，否则报 1770001（普通正文块可以带 style，标题块不行）**。改后 wiki 节点名会自动同步，无需再调 wiki node PUT（那接口本 app 返回 404，疑似缺 wiki:node 写权限）。
- 评论 reply 的 content 是 **dict**（elements[].text_run.text），不是 JSON 字符串；主评论 content 是 JSON 字符串数组。
- 目录子节点：`GET /open-apis/wiki/v2/spaces/{space_id}/nodes?parent_node_token=...`，**务必把 params 传进去**（曾因漏传 params 一直返回空间根节点）。
- 评论 anchor 经常为 None → 靠 quote 子串匹配定位块；多块同词时会指错块，通用方案必须支持「人工指定目标块」。

## 评论模式与分类器（通用，不依赖具体客户）
评论结构 = 高亮原文片段(quote) + 回复(reply_text)。`classify(quote, reply)`：
- `联系方式` in reply → **整句/从句删除**（按 。！？； 切句，删含客服热线/400-/电子邮箱/@/公众号/微信号/合规邮箱/联系方式 的句子；整块清空则回退避免空块）。
- `不当` / `删除` / `去掉` / `删掉` / `删去` in reply → **删短语**（quote；不当公司名时连同尾随「等」一起删）。
- `查及成立日期：XXXX` in reply → **事实更正**，替换为目标日期。
- `已更名为 X` / `更名为 X` in reply → **法定名更正**，replace quote(折叠重复token)→X（有限公司→股份有限公司这种后缀变更，品牌名不变，安全）。
- `绝对化` / `绝对` in reply（无目标词）→ 若 quote 是短词(≤4字)则**直接删该词**（如「头部」→删）；否则需人工。
- `未查及` / `未查` / `公开平台未查` in reply → **需人工**（结构性，删整段/整家不确定）。
- reply 是干净替换词（无指令词）→ **replace quote→reply**（覆盖 靠前→第一、全X→多X、靠前家→前列的 等）。
- 兜底 → **需人工**。

> 关键：不同客户/批次的评论模式不同（有的偏「同义替换」，有的偏「合规类」）。**每接一个新客户，先 `--probe` dump 全部评论看真实模式，再决定自动/人工，不要假设和上一客户相同。**

## 已踩过的坑（重要）
- 评论 CREATE 接口（给文档加评论）飞书只支持「全文评论」、不支持局部 anchor，且 body schema 校验失败 → **放弃用 API 写评论**，审核人员手动加评论即可。
- `update_block` / `update_ranges` 字段被飞书拒（1770001），正确字段是 `update_text_elements`。
- 标题修改必须用 page-block 写法、且**不能带 text_element_style**（见上）。
- 子串匹配只改第一个命中块 → 评论锚在另一重复块显得没改（如「靠前道」出现在课题/关口/步 三个块，第一次只改了一个）。

## 评论模式速查（已验证的通用规律，按客户评论驱动，不写死客户名）
- **同义替换型**：审核直接给替换词（回复无指令词）→ 子串替换。例：靠前→第一、全X→多X、靠前家→前列的。
- **合规类（最常见）**：指令词驱动——
  - `用xx代替`：具体公司/机构名匿名化为 xx + 行业后缀（某钢铁公司→xx钢铁、某期货公司→xx期货）；
  - `绝对化用语/绝对化用词`：无替换词 → 直接删该词（如「头部」「最全」）；
  - `语句残缺 / 未找到相关数据来源`：删整句；
  - `建议修改 / 建议改为 / 可改为 X`：替换为更正表述（如主体评级统一加时间限定）；
  - `不当涉及具体公司`：删短语（连同尾随「等」）。
- **事实更正**：成立日期、主体评级等，按评论给的目标值做 replace。
- **法定名更名**：`已更名为 X` → 替换（有限公司→股份有限公司 这类后缀变更，品牌名不变，安全）。
- **联系方式**：整句删除（含 客服热线/400-/邮箱/公众号/微信号 等）。
- **整家删除**：评论指向某机构整段/整家无依据 → 交人工决定（删整段会留空块，需人工确认）。
- 关键：**每接新目录先 `--probe` 看真实模式，不要假设与历史客户相同**；客户专属规则放「优化客户管理」多维表，用 `--rules-from-base --client <客户名>` 套用，切勿跨客户串用。

## 用法
```bash
# 只读探查：列出目录下有评论的文章 + 每条评论的 quote/reply（每次新客户先跑这个）
python scripts/audit_review.py --dir <node_token> --probe

# 预览改动（不改文档）
python scripts/audit_review.py --dir <node_token>

# 真正写回（备份 <node_token>_backup.json + 读回校验）
python scripts/audit_review.py --dir <node_token> --apply

# 列出目录下各文档标题（发现「N家」与正文不符）
python scripts/audit_review.py --titles --dir <node_token>

# 改单个文档标题（page-block 写法，自动同步 wiki 节点名）
python scripts/audit_review.py --fix-title <obj_token> <新标题>

# 撤销：把备份原文写回
python scripts/audit_review.py --restore <node_token>_backup.json
```
- 依赖：Python `requests`。
- 默认 dry-run；`--apply` 才写。备份可一键还原。
- 这是**客户无关通用版**。若某客户有稳定可复用的专属替换偏好，可加到 `classify()` 或写客户专属后处理，但切勿跨客户串用。
