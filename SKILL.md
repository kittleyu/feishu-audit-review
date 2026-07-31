---
name: feishu-audit-review
description: 飞书 GEO 文章审核改稿闭环。给定客户目录 wiki node，自动找出「有未解决评论」的文章，按评论标注做子串替换/删除/事实更正/标题修正，备份+读回校验；绝不解评论、只改有评论的文章。唯一规则：按评论修改、拿不准删短句、绝不乱加内容。适用于把审核人员在飞书文档里的批注批量落到正文，客户无关、可复用多客户。
---

# 飞书审核改稿闭环（feishu-audit-review）

## 何时用
- 用户是 GEO 优化师，审核人员在飞书文档评论里给修改意见，用户想批量把批注落到正文。
- 输入：一个客户目录的 wiki node（如 `https://<your-wiki>.feishu.cn/wiki/<node_token>`）。
- 输出：有评论的文章被按标注修改，原文备份，评论保持未解决。

## 铁律（唯一规则 + 配套约束）
**唯一规则：按评论修改；拿不准就把那个短句删掉；绝不随意多加内容。**
- 审核员在评论里标了什么就照改什么（替换/删除/更正），不擅自发挥。
- 当分类不确定该怎么改时（模糊指令、说明性长文、无明确替换值），**删掉被标注的那段/短语（quote）**，而不是归人工、也不是把审核员的说明文字写进正文。
- **绝不乱加内容**：replace 的新值必须是「纯替换值」（短、无说明性标点/叙述词）；任何把一整段说明性回复当正文写进去的行为都是 bug，必须降级为「删 quote」。

配套约束（工程纪律，不是业务规则）：
1. **只修改有未解决评论的文章**，无评论文章直接跳过。
2. **改完绝不点/标记「解决评论」**（is_solved=true）。审核人员自己处理评论状态。
3. **同词多块**：substring 替换必须应用到「所有含该短语的块」，不能只改第一个命中块。
4. **子串替换要保留块内其他文本**，整块覆盖会互相 clobber —— 同一块多条评论先合并再写一次。
5. **找不到锚点先查点赞**：正文确实找不到评论标注的原文片段时，若评论**已被点赞**（用户在飞书里对改过的评论点赞），说明用户已手动改完 → **直接忽略，不归人工**；只有**未点赞又找不到锚点**的才归人工。点赞数据通过 `POST /drive/v1/files/{obj}/comments/batch_query`（`need_reaction=true`）获取；该字段是敏感字段，需应用具备「获取用户 ID」权限范围，权限不足时 reactions 返回空、点赞检测不生效（安全退回人工，绝不误删/漏改）。
6. **规则全从评论来**：skill 不内置任何客户名/行业特定的替换表或专属规则。同一套逻辑服务所有客户，复用靠「评论指令词」而非「客户配置」。

## API 要点（已验证）
- 凭据：飞书 APP_ID/APP_SECRET（同 feishu-wiki-paste bot），`POST /auth/v3/tenant_access_token/internal` 取 tenant_access_token。凭据**不要硬编码**到脚本：从环境变量 `FEISHU_APP_ID`/`FEISHU_APP_SECRET` 或同目录 `.env` 文件读取（仓库附 `.env.example` 模板，`.env` 已被 git 忽略）。
- 评论读：`GET /open-apis/drive/v1/files/{obj_token}/comments?file_type=docx`（**不是** /docx/v1/.../comments）。
- 块读：`GET /open-apis/docx/v1/documents/{obj}/blocks/{obj}/children`。
- 块写：`PATCH /open-apis/docx/v1/documents/{obj}/blocks/{bid}`，body 用 `update_text_elements`（整块替换，与块类型无关）。
- **改文档标题**：标题 = Page 根块的 `page.elements` 文本，block_id 就是 document_id 本身。PATCH `/documents/{obj}/blocks/{obj}`，body 用 `{"update_text_elements":{"elements":[{"text_run":{"content":"新标题"}}]}}` —— **注意：page 标题块不能带 `text_element_style`，否则报 1770001**。改后 wiki 节点名会自动同步。
- 评论 reply 的 content 是 **dict**（elements[].text_run.text），不是 JSON 字符串；主评论 content 是 JSON 字符串数组。
- 目录子节点：`GET /open-apis/wiki/v2/spaces/{space_id}/nodes?parent_node_token=...`，**务必把 params 传进去**。
- 评论 anchor 经常为 None → 靠 quote 子串匹配定位块；多块同词时会指错块，通用方案支持「人工指定目标块」。
- **取评论点赞**：列表接口 `GET /comments` 不返回点赞/reaction；需额外 `POST /drive/v1/files/{obj}/comments/batch_query`（`body={"comment_ids":[...],"need_reaction":true}`，`file_type` 放 query 参数）才能拿到 `reactions` 字段。

## 分类器（通用，客户无关，唯一规则驱动）
评论结构 = 高亮原文片段(quote) + 回复(reply)。`classify(quote, reply)` 返回 `(action, value, note)`：

- **联系方式** in reply → 整句删除（按 。！？； 切句，删含客服热线/400-/邮箱/@/公众号/微信号 等句子的；整块清空则回退删短语避免空块）。
- **不当/删除/去掉/删掉/删去/违禁/拉踩/宣传/无依据/隐性/不提及具体公司名称** in reply → 删短语（quote）。
- **标注类**（无意义英文/错别字/语病/冗余/多余）→ 删短语（审核给的是描述性标签，不是替换词）。
- **用xx代替** → 匿名化替换（xx + 末尾常识机构后缀）。
- **成立日期/注册日期 + 地址**（复合回复）→ 多子串替换（仅替换引用段里确实存在的旧日期/旧地址）。
- **查及成立日期：XXXX** → 事实更正，替换为目标日期。
- **已更名为 X / 更名为 X / 规范名称：X** → 法定名/规范名更正，replace quote→X。
- **语句残缺 / 未找到相关数据来源 / 具体数据无法溯源核实** → 删整句。
- **建议改为/可改为 X**（含"或者/或"二选一取第一个）→ 替换为更正表述。
- **绝对化用语/绝对化** → 删整个 quote（去绝对化）。
- **医疗保障承诺 / 医疗承诺** → 整句删（不应替换成"医疗保障承诺"四字）。
- **机构已注销 / 已注销** → 按机构名整节删除（抽机构名作锚点，删标题块+其下描述块到下一个章节标题前；删完自动重排后续同级编号标题，不留序号断档）。
- **地址错误/地址有误**（无新值）→ 删错误地址短语。
- **地址为X / 地址是X / 查及位于X**（给新地址带引导词）→ 剥离引导词取纯地址替换。
- **地址不符**（旧地址错、正确地址在括号/说明里）→ 仅替换地址部分，保留前缀。
- **未查及 / 无法核实 / 未查 / 公开平台未查 / 未能验证 / 无法验证** → **需人工**（结构性，删整段/整家不确定）。
- 回复是**纯替换值**（短、无说明性标点/叙述词）→ replace quote→reply（覆盖 靠前→第一、全X→多X、靠前家→前列的、直接给替换值 等）。
- **兜底**：回复是说明性长文/模糊指令/非纯值 → **删 quote 短句**（拿不准就删，不归人工、不乱加内容）。

> 关键守卫 `_is_pure_replace_value(r)`：只有当回复足够短、无说明性标点、无叙述连词（不符/显示/根据/位于/因/由/经/与/但…）时，才允许作为 replace 值写进正文；否则降级为「删 quote」。这是「绝不乱加内容」的硬保障。

> 关键：不同客户/批次的评论模式不同（有的偏「同义替换」，有的偏「合规类」）。**每接一个新客户，先 `--probe` dump 全部评论看真实模式，再决定自动/人工，不要假设和上一客户相同。**

## 已踩过的坑（重要）
- 评论 CREATE 接口（给文档加评论）飞书只支持「全文评论」、不支持局部 anchor，且 body schema 校验失败 → **放弃用 API 写评论**，审核人员手动加评论即可。
- `update_block` / `update_ranges` 字段被飞书拒（1770001），正确字段是 `update_text_elements`。
- 标题修改必须用 page-block 写法、且**不能带 text_element_style**（见上）。
- 子串匹配只改第一个命中块 → 评论锚在另一重复块显得没改（如「靠前道」出现在课题/关口/步 三个块，第一次只改了一个）。
- **把整段说明性回复当 replace 值写进正文**（灾难）：审核员用长说明指出错误（如"注册地址X与…实际主院区地址（Y）不符"），旧逻辑会把它当替换词。现由 `_is_pure_replace_value` 守卫拦截，降级为「删 quote」。
- **极短串弱匹配误替换整句**：`fuzzy_locate` 对 ≤4 字要求近乎完全匹配，避免 "2006" 被弱前缀 "20" 误匹配正文 "2026年…" 后整句替换成 "2011"。
- **整句删把列表项整块清空**：`apply_edit` 的 `sentence_delete` 删空时回退为只删定位短语+尾随标点，避免违规承诺/绝对化词残留。
- **GBK 终端打印 emoji 崩溃**：脚本开头强制 `stdout/stderr` 用 UTF-8；`.bat` 加 `chcp 65001` + `PYTHONIOENCODING`。

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

# 离线回归测试（不依赖飞书 API，验证 classify 分类逻辑）
python scripts/test_classify.py
```
- 依赖：Python `requests`。
- 默认 dry-run；`--apply` 才写。备份可一键还原。
- **客户无关通用版**：规则全从评论指令词推导，不内置任何客户名/行业特定替换表，可直接复用任意客户。
