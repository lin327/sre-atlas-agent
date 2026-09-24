# SRE Atlas Agent

采集 SRE / 运维知识的 Agent，从 RSS、GitHub Issues/PR 聚合内容，经 Claude API 生成中文 MDX 草稿，默认写入 `output/inbox/<category>/`。草稿经人工审核、PR 合并后才进入 [sre-wiki](https://github.com/lin327/sre-wiki)，站点为 [SRE Atlas](https://pineapple-user.site/)。

当前只实现 RSS 和 GitHub 采集；官方文档 `docs` 采集器尚未实现，已从 `config/sources.yaml` 移除对应配置。

## 架构

```
人工启动 CLI / workflow_dispatch
  → RSS / GitHub → SQLite 去重 → Claude 生成与质量门
  → output/inbox/<category>/<slug>.mdx（canonical: false）
  → 人工整理并通过 PR 进入 sre-wiki
  → Wiki 构建镜像 → 云服务器 Docker + nginx（另行部署）
```

## 项目结构

```
sre-atlas-agent/
├── agent/
│   ├── __init__.py
│   ├── collectors/
│   │   ├── __init__.py
│   │   ├── rss_collector.py          # RSS 采集（feedparser + 重试）
│   │   └── github_collector.py       # GitHub Issues/PR（REST API + 分页）
│   ├── category_map.py               # 关键词 → 分类映射（7 类）
│   ├── generator.py                  # Claude API 内容生成 + 质量门控
│   ├── dedup.py                      # SQLite 去重（WAL 模式）
│   ├── scheduler.py                  # 定时调度（默认 6 小时）
│   └── main.py                       # CLI 入口
├── config/
│   ├── sources.yaml                  # 数据源配置（RSS + GitHub）
│   └── settings.py                   # 应用设置
├── data/
│   └── sre_atlas.db                  # SQLite 数据库（自动生成，gitignore）
├── output/                           # 本地产物，不提交
│   └── inbox/                        # 未审核草稿，运行时按需创建
│       ├── kubernetes/
│       ├── linux/
│       ├── docker/
│       ├── architectures/
│       ├── incidents/
│       ├── runbooks/
│       └── comparisons/
├── tests/
│   ├── __init__.py
│   ├── conftest.py                   # 共用 fixtures
│   ├── test_dedup.py
│   ├── test_github_collector.py
│   ├── test_generator.py
│   └── test_category_map.py
├── .github/
│   └── workflows/
│       ├── ci.yml                    # PR/push 触发 lint + pytest
│       └── collect.yml               # 手动采集并上传 inbox 草稿
├── .tasks/                           # Claude Code 任务文件（临时）
├── .gitignore
├── .env.example
├── Dockerfile                        # 默认单次运行（--once）
├── requirements.txt
└── README.md
```

## 分类体系

| 分类 | 目录 | 匹配关键词 |
|------|------|-----------|
| Linux 基础 | `linux/` | linux, kernel, systemd, shell, iptables |
| Kubernetes | `kubernetes/` | k8s, pod, deployment, service, ingress, helm |
| Docker | `docker/` | docker, image, docker-compose, registry |
| 架构设计 | `architectures/` | distributed, scalability, service mesh, api gateway |
| 事件处理 | `incidents/` | postmortem, root cause, incident, on-call |
| 运维手册 | `runbooks/` | runbook, playbook, troubleshooting, diagnostic |
| 技术选型 | `comparisons/` | vs, comparison, alternative, benchmark |

目录相对于 `output/inbox/`。优先采用有效来源分类，未知分类才按标题和标签分类；旧名 `runbook` / `architecture` 会映射为复数。源配置统一使用上述 7 类。

RSS 和 GitHub collector 均传递配置中的来源分类；GitHub 未配置分类时留空，由生成器分类。

## 快速开始

```bash
# 1. 克隆仓库
git clone https://github.com/lin327/sre-atlas-agent.git
cd sre-atlas-agent

# 2. 创建虚拟环境
python3 -m venv .venv
source .venv/bin/activate

# 3. 安装依赖
pip install -r requirements.txt

# 4. 配置环境变量
cp .env.example .env
# 编辑 .env 填入 ANTHROPIC_API_KEY
set -a
source .env
set +a

# 5. 手动采集并生成一次（会调用 Claude API 并计费）
PUBLISH_CANONICAL=false python -m agent.main --once --output output/
```

CLI 从进程环境读取配置，不会自动加载 `.env`。

> [!CAUTION]
> 🔴 **`--dry-run` 仍会调 API**：仍调用 Claude API、写出 MDX 并产生费用，只跳过 SQLite 去重与写入。即使已恢复 DB，第二次 `--once --dry-run` 也不会跳过已处理 URL，不能当作免费预览。需要去重时使用不带 `--dry-run` 的 `--once`。

## 手动采集与发布边界

`collect.yml` 的 schedule 保持注释，仅允许 `workflow_dispatch`。在 GitHub Actions 的 **Collect & Generate** 工作流中**手动触发会计费**，使用仓库的 `ANTHROPIC_API_KEY` secret；缺少 key 会在采集前失败。工作流注入内置 `GITHUB_TOKEN`；本地未配置 token 时，collector 会记录匿名访问、每小时 60 次限流的降级日志。工作流固定 `PUBLISH_CANONICAL=false`，草稿上传为 `inbox-drafts-<attempt>` artifact。

**重复触发只在最新 DB 成功恢复后才具备去重保护**，新 URL 仍可能计费。工作流按分支串行运行，使用独立运行/attempt 的 cache key 保存 `data/sre_atlas.db`，下次恢复该分支最新缓存并检查完整性及 URL 记录表。采集结束后先 checkpoint WAL，将 DB 与本次草稿一起备份为 `dedup-db-<attempt>` artifact，备份成功后才保存缓存。即使 inbox 为空导致任务失败，也会备份可用 DB；两个 artifact 均保留 30 天，备份中 `data/sre_atlas.db` 与 `output/inbox/` 保留各自路径。

- 首次运行没有历史库时，才勾选 `initialize_db`；默认不勾选，未恢复 DB 就停止，不会静默使用空库生成。
- 后续在同一分支运行，先确认日志显示 DB 已就绪。缓存丢失或上次保存失败时，填写最近有效备份的 `restore_run_id` 和 `restore_attempt`（默认 `1`），从 artifact 恢复；不要用初始化选项绕过丢失的历史。无法找回最新 DB 时，停止重复触发。
- `output/inbox/` 没有非空 MDX 时任务失败，上传也使用 `if-no-files-found: error`。没有新 URL、采集失败或质量门全部跳过都可能造成空产物；先查日志，不要靠反复运行重试。

当前去重只记录成功写出草稿的 URL，不记录 API 调用或失败尝试；生成失败、质量门跳过的内容可能在下次再次调用 API。标题像 GitHub Issue、slug 非法或正文缺少 wikilink 的输出会被质量门跳过。

下载草稿后，人工核对来源、适用版本和技术步骤，补齐 Wiki 所需 frontmatter，再通过 PR 纳入 Wiki。未审核稿如需交给 Wiki 仓库管理，应放 `src/inbox/`，不要放进会生成公开路由的 `src/pages/inbox/`；审核通过后才进入 `src/pages/<category>/` 并标记 `canonical: true`。遵循 Wiki 的 [CONTENT_CONTRACT.md](https://github.com/lin327/sre-wiki/blob/main/CONTENT_CONTRACT.md)。

本仓库没有自动同步或推送 Wiki 的脚本；生成成功、上传 artifact、合并内容 PR 和更新服务器容器是不同步骤。

## CLI 参数

```
python -m agent.main [OPTIONS]

  --once              单次运行后退出
  --dry-run           仍采集、生成和写 MDX，仅跳过数据库去重与写入
  --config PATH       数据源配置文件（默认 config/sources.yaml）
  --output DIR        输出根目录（默认 output/，草稿再追加 inbox/）
  --interval HOURS    持续模式间隔（默认 6 小时）
  --log-level LEVEL   日志级别 DEBUG|INFO|WARNING|ERROR
```

## 环境变量

| 变量 | 必填 | 说明 |
|------|------|------|
| `ANTHROPIC_API_KEY` | ✅ | Claude API key |
| `DATABASE_PATH` | ❌ | SQLite 路径（默认 data/sre_atlas.db） |
| `GITHUB_TOKEN` | ❌ | 工作流注入内置 token；本地可设置 PAT。缺失时记录降级日志，以匿名限流采集 |
| `PUBLISH_CANONICAL` | ❌ | 默认 false；只有精确的 true 才跳过 inbox 层，但生成页仍标记 canonical: false，不代表审核通过；采集工作流固定为 false |

## 相关仓库

- [sre-wiki](https://github.com/lin327/sre-wiki) — Astro + React 静态知识库，云服务器 Docker + nginx 部署
