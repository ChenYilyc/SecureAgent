# 建筑知识库的文控流程（本地原型）

初次在项目根目录运行：

```cmd
.\.venv\Scripts\python.exe -m app.document_control migrate
.\.venv\Scripts\python.exe -m app.document_control list
```

迁移保留旧台账中的草稿和生效状态。相同原文重复提交不会多一份版本或多一批条款。原文的归档副本保留在 data/construction_archive；每条引用包含项目、文号、修订号、章节和该副本的行号。登记库为 data/construction_control.sqlite3，检索沿用 data/construction_chunks.jsonl。

## 日常收件

在 data/inbox/QL01（或另一个项目号）放一批 Markdown 文件。最小约定是“文号_R01.md”、一个一级标题和编号二级条款标题。每个项目一个收件目录。在项目根目录执行，下面的文号和版本换成实际已收件的值。

```cmd
.\.venv\Scripts\python.exe -m app.document_control receive --project QL01 --actor clerk
.\.venv\Scripts\python.exe -m app.document_control list
.\.venv\Scripts\python.exe -m app.document_control approve --project QL01 --document QL-DOC-013 --revision R01 --reviewer reviewer
.\.venv\Scripts\python.exe -m app.document_control history
```

- 同一批收件要么全部登记，要么全部回滚。同一版内容相同则跳过，有变化则报告文件名并要求另发修订版。源文件内容按哈希归档；未引用的归档副本不参与检索。
- 收件默认草稿。审批的数据库事务会把同项目、同文号之前的现行版变为历史，将所选草稿变为现行，同时写审批事件。之后自动发布索引。
- 发布保留各版本的条款，普通查询仅在指定项目的 effective 版本中检索。发布失败时保留上一份完整索引；台账已经提交的审批可以用以下命令重试发布。

```cmd
.\.venv\Scripts\python.exe -m app.document_control publish
```

## 验收

```cmd
.\.venv\Scripts\python.exe -m unittest -v tests.test_construction_versions tests.test_document_control
.\.venv\Scripts\python.exe -m app.search_construction --project QL01 "材料报审记录必须在几天内归档？"
.\.venv\Scripts\python.exe -m app.search_construction --project HT02 "材料报审记录必须在几天内归档？"
```

搜索的当前样本分别给出 2 和 5 个工作日，均为虚构项目流程。同一项目内检索到的其他相关条款可能只涉及相近主题。

本版是真实文件和持久数据的本地工作流原型。actor/reviewer 是经办记录；生产审批必须由后端取得已登录者身份并校验角色和项目授权。实际企业工作流还需接入权威文控系统、备份和恢复，并按需要增加 DOCX、PDF、扫描件的解析与预览。
