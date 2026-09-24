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

RSS 使用配置中的来源分类；GitHub collector 当前仍传入通用 `github` 分类，生成时回退到标题分类，其配置分类尚未贯穿采集链路。

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

CLI 从进程环境读取配置，不会自动加载 `.env`。`--dry-run` 仅跳过 SQLite 去重与写入，仍会调用 Claude API、写出 MDX 并产生费用，不能当作免费预览。

## 手动采集与发布边界

`collect.yml` 的 schedule 已注释，仅允许 `workflow_dispatch`。在 GitHub Actions 的 **Collect & Generate** 工作流中手动运行会使用仓库的 `ANTHROPIC_API_KEY` secret 调用付费 API；工作流固定 `PUBLISH_CANONICAL=false`，只上传 `output/inbox/` 为 `inbox-drafts` artifact。

CI 尚未持久化 SQLite 数据库，每次手动运行可能重复采集和生成；不要恢复 schedule 或连续触发来代替去重。标题像 GitHub Issue、slug 非法或正文缺少 wikilink 的输出会被质量门跳过。

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
| `GITHUB_TOKEN` | ❌ | GitHub PAT（提高 API 限流） |
| `PUBLISH_CANONICAL` | ❌ | 默认 false；只有精确的 true 才跳过 inbox 层，但生成页仍标记 canonical: false，不代表审核通过；采集工作流固定为 false |

## 相关仓库

- [sre-wiki](https://github.com/lin327/sre-wiki) — Astro + React 静态知识库，云服务器 Docker + nginx 部署
