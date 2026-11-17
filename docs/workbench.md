# 建筑项目文控工作台

员工可以用中文提问、查看现行条款，再点开引用核对原件。资料员提交草稿，审批人发布新修订，公司管理员为人员分配项目和岗位。公网工作台需要项目账号。没有账号也可以在 GitHub 核对示例问题和原文。[公网工作台](https://secureagent.vercel.app/workbench) · [滨河路项目《月度计量资料组卷指引》](../examples/project_library/BH04/BH-BL-219_R01.md)

要用住宅、市政和机电项目演示同题异答、PDF 原件和修订审批，可导入随项目提供的文档样本。[多项目演示手册](project_library.md)

## 本机启动

在 SecureAgent 项目根目录的 Windows CMD 里运行下列命令。已有 SQLite 台账可直接使用。

```bat
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m app.workbench_accounts demo --project QL01
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

浏览器打开 <http://127.0.0.1:8000/workbench>。第一次创建账号时，终端会为三个账号分别显示随机口令。再次运行会保留原口令。如需一个新的个人账号，管理员可以在本机使用 `python -m app.workbench_accounts add zhang --role reviewer --project QL01`，按提示输入口令。

## 用一份 PDF 验收

1. 用 `demo-clerk` 登录。在 **提交草稿** 中选择项目自带的 `examples/workbench/QL-WB-101_R02.pdf`。文件名将填入文号和修订号。上传后，在预览区核对第 1 页 §2.1 的归档时限。此时用员工账号查看，台账中应看不到这个新版本。
2. 切换到 `demo-reviewer`。在台账里选择同一草稿，核对原件，点击 **批准并发布**。本机台账记录审批账号，按项目和文号替换现行版本，然后刷新条款索引。若页面提示现行版在预览后有变化，刷新预览再决定是否发布。
3. 用 `demo-employee` 登录，查询“移交清单应在几天内归档？”。§2.1 写明项目资料员在接收方签收后 **1 个工作日内** 归档。点击结果下方的引用会选中该条款，再点击 **打开归档原件** 可在浏览器查看第 1 页。

样本是虚构的项目文件。修订号是登记编号，R02 可以作为这个文号的第一份现行版。每个项目、文号同一时刻最多一个版本现行。员工打开已有引用时，服务器会再次检查项目授权和版本状态。资料员和审批人可以在各自获授权的项目里查看历史版。

## 原件与检索

- Word 接收普通 `.docx`，提取正文中的段落和表格。Word 的“正文块”按顶层段落或表格计数。可在条款预览中核对文字并下载归档原件。
- 文本 PDF 按页记录来源，条款跨页时显示页码范围。工作台单件上传上限为本机 8 MiB、Vercel 4 MiB，PDF 最多 100 页。遇到没有文字层的扫描页，收件会标出页码，需先 OCR 再提交。复杂表格的阅读顺序应以原件为准。
- Markdown 支持现有的编号条款格式。文号、修订号和项目由收件页面登记。原件以 SHA-256 校验，审批身份、时间和旧版替代事件可在预览中的操作记录查到。
- 员工登录后在“问项目资料”直接提问。服务根据当前项目选择答复方式。仅启用检索的项目会列出相关现行原文和出处。全新本机配置默认按字面检索，云端 PostgreSQL 路径默认尝试混合检索。允许使用外部问答的项目会在提问框下提示 DeepSeek 的使用方式，有候选时用一次模型请求核对各个子问。

## 为指定项目启用员工问答

在已经设置 `DEEPSEEK_API_KEY` 的服务端 CMD 中，比如只给演示项目 QL01 启用问答，使用下列命令启动工作台。

```bat
set "SECUREAGENT_ALLOW_REMOTE_QA=1"
set "SECUREAGENT_QA_PROJECTS=QL01"
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

项目编号与台账一致，多个编号用英文逗号分隔。未列入的项目使用本地检索。全局开关关闭、项目列表为空或缺少密钥时，员工仍可查本地现行条款。改完配置需重启服务并刷新页面。工作台的 `/ask` 接口只接收问题，项目取自经会话授权的请求路径。旧 `/search` 的显式模型请求也受这份名单约束。此配置作用于工作台 HTTP 接口，开发与评测用的命令行入口独立运行。

## 让员工用口语查条款

在项目根目录的 Windows CMD 中准备中文模型，然后用同一套演示题比较两种检索方式。

```bat
.\.venv\Scripts\python.exe -m pip install -r requirements-semantic.txt
.\.venv\Scripts\python.exe -m app.semantic_retrieval prepare
.\.venv\Scripts\python.exe -m evals.retrieval_baseline --compare
```

首次准备会下载固定版本的分词器和量化模型，并校验 SHA-256。嵌入模型在本机 CPU 上运行。运行脚本时默认使用字面检索。要让网页查询使用两路检索，在运行 Uvicorn 的 CMD 窗口按 Ctrl+C，然后执行以下命令。

```bat
set "SECUREAGENT_RETRIEVAL=hybrid"
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

用项目 QL01 的员工账号查询“交接单签字收下以后，最晚何时存好？”。示例 PDF 已发布时，可点开 `QL-WB-101 · R02 · §2.1`，核对接收方签收后 1 个工作日的时限。网页的混合检索由应用服务在 CPU 上执行。已配置外部问答的项目会进一步核对候选条款能否支持所问事实。

字面和向量检索共用同一批本项目现行条款。BGE 将问题和条款编码成单位向量，比较向量内积，输入最长 512 个 token。两路各取最多 20 条，按 RRF 合并名次，最终向工作台返回前三条。两路融合分相同优先采用向量排名，然后沿用发布索引顺序。[BGE 模型说明](https://huggingface.co/BAAI/bge-small-zh-v1.5) · [ONNX 权重来源](https://huggingface.co/Xenova/bge-small-zh-v1.5) · [RRF 方法](https://cormack.uwaterloo.ca/cormack/cormacksigir09-rrf.pdf)

本机把已计算的段落向量存入 `data/construction_vectors.sqlite3`。Vercel 将这份可重建的缓存放在实例临时目录。向量按项目、模型版本及输入文本摘要复用。新版发布后，查询只取当前可见正文对应的向量。此版本在项目范围内做精确向量比较，查询耗时会随条款数量增加。

## 一次查询引用多份文件

在项目根目录运行以下命令，分别核对 QL01 已发布的会审归档流程和原件保管流程。

```bat
set "SECUREAGENT_RETRIEVAL=hybrid"
.\.venv\Scripts\python.exe -m app.answer_construction --project QL01 --multi "图纸会审纪要由谁办理归档？归档后，纸质原件由哪个岗位长期保管？"
```

`--multi` 对完整问题检索一次，按中文或英文问号给子问编号，有候选时在一次 DeepSeek 请求中核对这些子问。每项可引用一条或多条候选原文。此例应分别引用 QL-DOC-007 R01 §3.2 与 QL-ARC-014 R01 §4.2。

工作台给 QL01 启用外部问答后，员工可以直接问“图纸会审纪要的纸质原件必须保管多少年？归档后，纸质原件由哪个岗位长期保管？”。在上述示例资料中，第一项应显示依据不足，第二项应引用 §4.2。点击引用可以定位条款，再打开归档原件。服务在返回前复核项目的现行版本。若一项答复依赖的某条引用已被换版，该项会显示依据不足，其他项仍独立核对。

## 公网部署与数据存放

公网应用由 Vercel 运行 FastAPI。Supabase PostgreSQL 保存账号、项目授权、归档原件、SHA-256 摘要、文档版本、条款和审计记录。服务重新部署后连接同一数据库即可读取已发布内容。管理员可以建立或归档工程、维护账号和项目岗位。员工打开原件时，服务重新核对该账号在项目中的权限。

1. 在自己的 Supabase 项目应用仓库里的数据库迁移，然后运行 `python -m app.bootstrap_admin <管理员登录名>` 创建首位公司管理员。脚本会隐藏输入数据库连接串与新密码。[迁移目录](../supabase/migrations)
2. 在 Vercel 的项目环境变量中设置服务端 `SECUREAGENT_DATABASE_URL`，值为该 Supabase 实例的 PostgreSQL 连接串。Vercel 默认使用 `postgres` 文档后端和 `hybrid` 检索。需要模型核对的项目，再配置 `DEEPSEEK_API_KEY`、`SECUREAGENT_ALLOW_REMOTE_QA=1` 和 `SECUREAGENT_QA_PROJECTS`。保存配置后部署含 `app/main.py` 的仓库。[配置示例](../.env.example) · [Vercel 配置](../vercel.json)
3. 全新的数据库需要业务资料。可以由资料员收件、审批人发布，或先在本机运行 `python -m app.demo_projects`，然后运行 `python -m app.import_cloud_documents --projects XR03 BH04 SG05` 导入虚构样本。导入工具会单独提示输入目标数据库连接串。最后在管理员页面把项目岗位授予演示账号。
4. 登录员工账号，在 BH04 查询“月度计量的要求有哪些？”，点开引用核对 §5.1–§5.3。已启用模型核对的项目会逐项给出依据，其他项目可以查看检索到的现行条款。[原文](../examples/project_library/BH04/BH-BL-219_R01.md)

## 工程入口

`app/workbench.py` 负责网页接口与项目权限。`app/office_sources.py` 记录条款和原件位置。`app/document_control.py` 管理版本生效、操作记录和索引发布。本机 SQLite 台账若审批成功但索引导出失败，排除磁盘占用等原因后可从项目根目录运行 `.\.venv\Scripts\python.exe -m app.document_control publish`，从台账重新发布。

在根目录运行 `.\.venv\Scripts\python.exe -m unittest discover -s tests -v` 可以离线验证授权、版本切换和引用。数据库集成测试另需专用本机 PostgreSQL 测试环境。企业统一登录与扫描件 OCR 属于后续集成范围。
