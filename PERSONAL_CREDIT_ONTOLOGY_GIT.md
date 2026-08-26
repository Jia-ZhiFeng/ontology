# 个人信审本体 Git

## 定位

| 层级 | 职责 | 是否影响线上本体 |
|---|---|---|
| 本体 Git | 保存不可变快照、分支、标签、日志和结构差异 | 否 |
| 版本工作流 | 草稿审核、批准、发布、回滚 | 仅发布或回滚时影响 |

本体 Git 是编辑协作层，不替代现有 `draft → reviewed → approved → published` 发布门禁。加载任意 Git 引用只把快照放入前端编辑器。

## 已实现能力

| 能力 | 行为 |
|---|---|
| 初始化 | 首次访问时把当前活动本体导入 `main` 根提交 |
| 提交 | 将完整 `OntologyProposal` 保存为不可变、内容寻址快照 |
| 分支 | 从提交、标签或其他分支创建分支 |
| 并发保护 | 提交携带 `expected_head`，分支已推进时拒绝覆盖 |
| 标签 | 为提交创建不可变命名引用 |
| 日志 | 沿第一父提交读取历史，单次最多 200 条 |
| Diff | 比较 ObjectType、Property、LinkType、Interface、Function、ActionType 和 DynamicSecurity 的新增、删除、修改 |
| 加载 | 按分支、标签、完整或唯一短提交 ID 读取快照 |

## API

| 方法 | 路径 | 用途 |
|---|---|---|
| `GET` | `/api/ontology/git/status` | 分支、标签和默认 HEAD |
| `GET` | `/api/ontology/git/log?ref=main&limit=50` | 提交历史 |
| `GET` | `/api/ontology/git/diff?from_ref=main&to_ref=feature/x` | 结构差异 |
| `GET` | `/api/ontology/git/show/{ref}` | 读取提交快照 |
| `POST` | `/api/ontology/git/commits` | 提交快照 |
| `POST` | `/api/ontology/git/branches` | 创建分支 |
| `POST` | `/api/ontology/git/tags` | 创建标签 |

## 数据表

| 表 | 用途 |
|---|---|
| `mvp_control.ontology_git_commits` | 不可变提交和完整本体快照 |
| `mvp_control.ontology_git_branches` | 可移动分支 HEAD |
| `mvp_control.ontology_git_tags` | 不可变标签引用 |

## 安全边界

| 约束 | 说明 |
|---|---|
| API 认证 | 与其他 `/api/*` 接口一致，要求 `X-API-Key` |
| 不自动发布 | commit、branch、tag、show、diff 均不写 NebulaGraph |
| 不自动合并 | 当前 MVP 不提供服务端三方自动合并，避免静默覆盖本体规则 |
| 不删除历史 | 当前 API 不提供删除提交、分支或标签 |
