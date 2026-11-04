# Agendex MVP：开发接口

支持本地和受登录保护的HTTPS部署。前端 React/TypeScript + shadcn/ui；后端 FastAPI + SQLite。所有 API 位于 `/api`，通常返回 JSON，字段使用 snake_case；下载接口返回 CSV。没有预置的生效策略或分类结果；用户在网页保存策略并点击开始后，后端才调用 Jev。

## 页面

- Agent目录：搜索、语言/Star/策略标签筛选、排序、分页、多选、查看详情、手动调整已有策略标签、发起分类。目录、详情和筛选使用当前有效标签。内部初筛分数、来源标记与筛选阶段不进入用户界面；后端保留这些字段以支持研究追溯。
- 分类策略：先创建并保存，可手动填写或由GLM助手生成草稿；没有内置模板。策略列表只显示名称，后台版本与结果页的历史查看、复用继续保留。策略表单不设置数据来源；API保留历史evidence_kinds字段以兼容旧客户端与冻结版本。
- 分类任务：只从已保存策略中选择，再选择模型、数据来源和范围；选择项显示策略名称，不带版本后缀，也不从任务表单打开策略编辑器。没有策略时先前往策略页创建。可查看真实状态、进度、费用和耗时，暂停/继续/取消。耗时使用created_at到结束时updated_at的总历时（包含排队与暂停）；未结束时按当前时间更新。网页来源默认metadata＋readme，完整仓库含代码选项禁用并提示等待实现。

## 登录

公网模式通过环境配置启用单账号会话；密码保存为scrypt哈希，签名Cookie有效12小时并启用Secure、HttpOnly、SameSite=Lax。缺少账号、哈希、会话密钥或HTTPS来源时拒绝启动。除会话/登录/退出外，全部业务API、导出和接口文档均要求登录；静态页面只加载登录入口。登录失败按来源限频，退出后清除Cookie。

- `GET /api/auth/session` → `{enabled,authenticated,username:null|string}`。本地未启用时返回enabled=false、authenticated=true。
- `POST /api/auth/login` 请求 `{username,password}` → 会话状态与HttpOnly Cookie。密码不返回或写入日志。
- `POST /api/auth/logout` → 未登录的会话状态并清除Cookie。

前端确认会话后才请求目录数据；业务请求返回401时回到登录页。部署配置见[部署说明](deploy/README.md)。

## API

- `GET /api/health` → `{status, jev_configured, glm_configured, model, models:[{id,label,configured}]}`。保留旧的Jev字段，模型列表分别报告Jev 1.14和GLM 5.3的配置状态。
- `GET /api/stats` → `{total_repositories, tagged_repositories, policy_count, active_jobs, languages:[{name,count}], dataset:{dataset_id,label,source_count,selected_count,threshold,screening_stage,fine_screened,protected_keep_count,awesome_supplement_count,imported_at}}`。
- `GET /api/repositories?q=&language=&min_stars=0&sort=stars|updated|name&page=1&page_size=25&policy_id=&tag_id=&tag_ids=&retention_source=` → `{items:Repository[],total,page,page_size}`。来源可选`metadata_prescreen`、`awesome_readme_reference`，空值不限制。`tag_ids`为逗号分隔的多个标签ID，非空时优先于兼容的`tag_id`；任一所选标签匹配即可，其他筛选条件仍同时满足。任务filters中的tag_ids使用数组。标签条件基于含人工标注的当前有效标签，不产生重复仓库。
- `GET /api/repositories/export.csv` → 完整可见目录的 CSV 附件，不受网页搜索、筛选或分页限制；包含仓库元数据和当前标签，不含内部初筛分数、来源字段或 README 正文。
- `GET /api/repositories/{id}` → Repository 加`evidence:Evidence[]`、`classifications:Classification[]`、`supplement:{reason,sources,metadata_readme_conflict?}|null`。清单来源保留`source_repo/source_url/source_line`，详情可链接冻结行号。
- `GET /api/repositories/{id}/annotations` → `{items:AnnotationPolicy[]}`，按当前活动策略返回该仓库各标签的模型判定、显式人工选择及有效判定。
- `PUT /api/repositories/{id}/annotations` 请求 `{policy_id,base_revision,overrides:[{tag_id,applied:boolean}]}` → 更新后的Repository详情，替换该仓库在当前策略下的全部人工选择；`overrides:[]`恢复自动判定。base_revision须匹配当前策略版本，过期返回409。保存不调用模型，不改写原评分。
- `GET /api/policies` → `{items:Policy[]}`；`GET /api/policies/{id}` → Policy。
- `POST /api/policies/generate` 请求 `{messages:[{role:"user"|"assistant",content}],draft:{...当前表单}}` → `{generation_id,message,policy,model,usage,cost}`。仅调用GLM 5.3生成可编辑策略，返回策略经字段及标签校验。生成协议v2只将name/description/mode/threshold/tags作为策略内容交给模型，示例与草稿均不包含evidence_kinds；提示要求编写标签含义与判断标准，不主动加入Metadata、README等来源限制。服务器在返回结构中恢复原草稿的兼容evidence_kinds，缺失时使用metadata＋readme；模型额外返回的该字段被忽略，实际分类来源由任务选择。不保存策略或创建任务，也不改写既有策略或历史回复。生成示例与固定输出结构在`backend/agendex/policy_generation.py`维护；请求、原始回复和失败状态记录到`policy_generations`。
- `POST /api/policies`、`PUT /api/policies/{id}` → Policy；请求 `{name,description,mode,threshold?,tags:[{id?,label,description,threshold?}],evidence_kinds:["metadata"]}`。旧的策略级 threshold 仅作为未指定标签阈值时的兼容默认值，输出标签总是含明确 threshold。POST遇到同名且可编辑内容相同的活动策略，直接返回已有策略；比较忽略新生成的标签ID和隐藏的历史evidence_kinds，保留标签顺序、规则与阈值。PUT无实际变更时不新增版本；显式阈值应用和历史恢复保留新版本语义。
- `GET /api/policies/{id}/versions` → `{items:Policy[]}`，保留全部策略版本。
- `GET /api/policies/{id}/results?revision=&q=&page=1&page_size=25` → `{policy,items:PolicyResult[],total,page,page_size,tag_stats:TagStats[]}`。
- `GET /api/policies/{id}/results/export.csv?revision=&q=&thresholds=` → 所选策略版本的全部匹配结果，每个仓库与标签一行，包含策略版本、标签、分数、阈值、判定及评分时间等，并附source/model_score/manual_applied。`q`沿用结果页的仓库名称搜索；无分页限制。可选`thresholds`为完整`{tag_id:number}`映射的JSON，值须为有限的[0,1]数字；按保存分数预览导出并保留人工选择，不写入策略或重新调用模型。`threshold_source`区分`saved`与`preview`；自动单选保留原模型赢家或none。
- `POST /api/policies/{id}/thresholds` 请求 `{base_revision,thresholds:{tag_id:number}}` → 新版本 Policy。只按已保存分数重算，不调用 Jev；base_revision 过期返回 409。
- `POST /api/policies/{id}/restore/{revision}` → 新版本 Policy；从选定历史版本恢复规则和阈值，保留原版本。
- `DELETE /api/policies/{id}` → `{ok:true}`；保留历史策略版本和任务。
- `POST /api/jobs` 请求 `{policy_id,model?:"typesafe/jev-1.14"|"glm-5.3",evidence_kinds?:string[],scope:{repository_ids?:number[],filters?:{q,language,min_stars,sort,policy_id,tag_id,tag_ids?:string[],retention_source},limit?:number},skip_completed:true}` → Job。网页明确提交本次数据来源；显式evidence_kinds为非空、不重复的metadata/readme列表，source_code尚不开放。省略evidence_kinds的旧客户端沿用策略保存的范围。来源和实际证据快照随任务冻结，不改写策略或旧结果。省略model保持原Jev默认行为；实际请求模型保存到任务并用于缓存及在途判重，不因其他模型已评分而直接跳过。仓库来源筛选与目录使用同一参数，空值不限制。网页始终启用已有评分复用；相同版本、模型、数据来源、仓库范围、冻结证据及实际请求内容的任务重复提交时返回原任务，覆盖queued/running/paused/completed/completed_with_errors，不重复新增任务行；暂停状态不自动恢复，失败项不自动重试。已取消任务可重新提交。扩展仓库范围会创建新任务，并复用其中已有的兼容评分。重叠的兼容请求在派发前再次检查缓存和在途任务，等待已有请求后复用。兼容旧API的`skip_completed:false`是显式重跑，仍禁止相同请求同时在途。
- `GET /api/jobs` → `{items:Job[]}`；`GET /api/jobs/{id}` → Job 加 `items`（最近记录）。
- `POST /api/jobs/{id}/pause|resume|cancel` → Job。
- `POST /api/repositories/{id}/evidence` 请求 `{kind,version,source_key?,source_url,content,provenance}` → Evidence；源码须提供文件路径 source_key，供后续 README/代码采集器写入，不启动采集。

错误返回 HTTP 4xx/5xx 及 `{detail:"可读错误"}`。分页页码从 1 开始；page_size 上限 100。

CSV下载使用带BOM的UTF-8编码及附件文件名；外部文本若以电子表格公式起始字符开头，会添加文本前缀。导出在同一只读事务中读取目录或指定策略版本，不触发任务、网络采集或数据库导入。

### Repository

`{id,full_name,url,description,stars,language,topics:string[],license,default_branch,created_at,updated_at,pushed_at,archived,agent_probability:number|null,prescreen_probability:number|null,retention_source,protected_keep,original_prescreen_decision,tags:[{policy_id,tag_id,label,score,source}]}`。标签source区分model与manual。

`agent_probability`为兼容字段；关键词初筛分数由`prescreen_probability`明确提供，不作为已确认实现概率。`retention_source`区分元数据初筛与Awesome文档补入；无Description保护项属于元数据初筛来源，分数为null。Awesome补入项保留原有低分或null，不因纳入候选而重写。

### Policy

`{id,name,description,mode:"multi_label"|"single_label",threshold,tags:[{id,label,description,threshold}],evidence_kinds:string[],revision,created_at,updated_at,result_count,applied_count}`。名称/规则必填；标签有稳定 ID。evidence_kinds保留历史兼容默认值，网页不在策略表单编辑它；新任务以明确提交的来源为准。标签阈值在 [0,1] 内。实际变更保存为不可变版本，无变化的重复保存返回原版本；阈值应用和历史恢复仍保留新版本记录。没有默认生效的标签定义。

### 策略结果

PolicyResult：`{repository_id,full_name,url,stars,language,model,scored_at,source_job_id,source_policy_revision,evidence_ids,winner_tag_id,results:[{tag_id,label,score,threshold,applied,source?,model_score?,model_applied?,manual_applied?}]}`。当前结果按人工标注叠加有效判定；只有人工标注、尚无模型评分的行，其model/scored_at/source_job_id为null、evidence_ids为空。multi_label的winner_tag_id为null；single_label默认保留模型原赢家或none，当前版本存在人工选中时使用人工标签。自动单选阈值重算只能保留原模型赢家。

TagStats：`{tag_id,label,threshold,scored_count,applied_count,manual_applied_count?,distribution:[{score,count}]}`。distribution包含仍由模型判定的有效分数，单选只包含其模型赢家；人工选择单列manual_applied_count，不随阈值预览改变。历史结果保留原统计语义。用于网页本地预览阈值变化，不是自动校准或准确率保证。统计覆盖该版本的全部保存结果，文本搜索仅影响列表。

网页每个标签可独立调整阈值，先在本地预览数量与当前页结果，点击「应用阈值」后保存新版本、更新仓库标签。历史版本只读，可「复用此版本」恢复为新的当前版本。

### 人工标注

AnnotationPolicy：`{policy_id,policy_name,policy_revision,mode,tags:[{tag_id,label,description,model_score:number|null,model_applied:boolean,manual_applied:boolean|null,score:number|null,applied:boolean,source:"manual"|"model"|null}]}`。manual_applied为null表示没有显式人工选择；恢复自动判定后继续使用已有模型结果。

人工添加标签的有效score为1、明确排除为0，source为manual；保留原model_score、model_applied与模型请求/响应。单标签策略中，人工选择一个标签会压制其他标签，其有效判定为false；标注读取接口仍只把用户明确提交的选择作为manual_applied。当前策略结果、目录、详情、筛选和导出应用人工覆盖；当前结果也可包含尚无模型评分的人工标注仓库，历史版本的自动结果保留。

人工选择按策略分类方式、公共说明及标签名称/规则匹配。仅修改阈值时继续生效，定义变化后旧标注不套用；手动结果不作为模型评分缓存，也不参与模型费用或准确率统计。

### Job

`{id,policy_id,policy_name,policy_revision,model,evidence_kinds:string[],status,total,completed,failed,skipped,cost,unknown_cost_count,created_at,updated_at,error}`。evidence_kinds为该任务实际冻结的数据来源，历史任务按原策略范围恢复显示。

状态：`queued/running/paused/completed/completed_with_errors/failed/cancelled`。失败不是阴性；重启后未完成任务暂停，已发出但无持久化响应的请求标记失败，不自动重复付费。暂停/取消不强行中断正在进行的 HTTP 请求，但不能再派发新请求。

### Evidence / Classification

Evidence：`{id,repository_id,kind,version,source_key,source_url,content_hash,captured_at,content,provenance}`。kind 支持 metadata/readme/source_code，版本可以是采集快照或真实 commit/blob 标识，不能将快照冒称 commit。source_key 代表同一版本内的文件路径；相同版本、相同路径的内容不可变，变化需写入新版本。

Classification：`{policy_id,policy_name,policy_revision,job_id,model,created_at,evidence_ids,results:[{tag_id,label,score,applied}]}`。标签来自用户策略，分类对应确定的策略版本和证据快照。

## 约束

- 默认目录使用关键词路线的`fine-screen/kept.jsonl`，按已保存的精筛结果切换可见成员；默认数据集身份为`keyword-v2-fine-screen`，阶段为`metadata_readme_fine_screen`，`fine_screened=true`。原候选和证据保留在同一工作库，初筛分数、保护状态和补入来源不改。旧Topic工作库和冻结结果分开保留，原始文件只读。
- 目录搜索、统计、语言计数及新分类任务范围只包含当前可见成员；按ID读取历史仓库、策略历史结果和任务记录仍可访问原仓库，README同步也不因目录切换而删除证据。
- 不预置策略，不发起全量分类，不恢复 README 采集。
- OpenRouter Key 仅在服务端，从环境变量或现有 macOS 钥匙串读取；不写入前端、数据库或日志。
- Jev 一次请求读取一个仓库的证据、回答所有标签问题。保存原始请求/响应（无凭证）、实际模型、usage 和错误。
- 网站任务队列默认8并发；一个请求对应一个仓库的全部标签，不将8个仓库拼成一个请求。README正文完整输入，请求体保护上限为32 MiB；模型上下文拒绝或其他失败保留错误，不静默裁剪或重试。
- 模型评分与标签判定分开保存。阈值、策略名称变化不重新调用 Jev；规则、公共说明、分类模式、证据或请求模型变化必须重新检查兼容性。只复用完整、有效且证据匹配的原始分数，保留源任务与实际模型。
- 相同语义策略可复用已有推理结果，标签 ID 改变时按明确的标签定义映射；单选必须保留原 none/赢家。兼容缓存命中计为复用/跳过，新增费用为 0。失败或不完整响应不能作为缓存。
- 每个策略版本都保留自己的结果引用和阈值；新版本重算不修改旧结果。后台旧任务完成后，只能为兼容的当前版本补充分数，规则已变化时不得套用。
- 人工标注独立于原始模型分数保存；有效标签覆盖不会修改源任务或评分缓存。阈值预览保持人工选择，模型新结果不自动清除人工覆盖。
- 阈值重判和历史恢复优先继承指定版本的源评分及其冻结证据；新增证据不抹掉已保存结果。新分类任务使用最新证据，并重新检查缓存兼容性。
- 结构化元数据与版本证据分开保存。扩展证据来源无需改变策略/任务/结果的身份关系。
- 前端使用同源 `/api`，开发时代理至 `127.0.0.1:8000`；Vite 使用 `127.0.0.1:5173`。本地模式拒绝非本地来源的写请求；公网模式只接受已配置HTTPS主机，所有写请求必须带匹配的Origin。

