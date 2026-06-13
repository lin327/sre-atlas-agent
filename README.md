# SRE Atlas Agent

自动采集 SRE / 运维知识的 Agent，从 RSS、GitHub Issues、官方文档中聚合内容，经 Claude API 生成结构化 MDX 页面，自动推送到 sre-wiki 仓库更新前端。

## 架构

```
Debian PC (crontab 每6h)              GitHub (sre-wiki)            k3s
┌─────────────────────┐              ┌──────────────┐          ┌──────────┐
│ agent --once        │  git push    │  main 分支    │  CI/CD   │ Astro    │
│ 采集→去重→生成 MDX  │ ──────────→ │  *.mdx 文件   │ ───────→ │ 自动构建  │
│                     │              │              │          │ nginx    │
│ SQLite: data/*.db   │              └──────────────┘          └──────────┘
└─────────────────────┘
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
├── output/                           # 生成的 MDX 文件（按分类目录）
│   ├── kubernetes/
│   ├── linux/
│   ├── docker/
│   ├── architecture/
│   ├── incidents/
│   ├── runbook/
│   └── comparisons/
├── tests/
│   ├── __init__.py
│   ├── conftest.py                   # 共用 fixtures
│   ├── test_dedup.py
│   ├── test_generator.py
│   └── test_category_map.py
├── scripts/
│   └── sync-to-wiki.sh              # 一键同步：采集 + 推送到 sre-wiki
├── .github/
│   └── workflows/
│       └── ci.yml                    # PR/push 触发 lint + pytest
├── .tasks/                           # Claude Code 任务文件（临时）
├── .gitignore
├── .env.example
├── Dockerfile                        # 可选：容器化运行
├── requirements.txt
└── README.md
```

## 分类体系

| 分类 | 目录 | 匹配关键词 |
|------|------|-----------|
| Linux 基础 | `linux/` | filesystem, process, network, kernel, systemd |
| Kubernetes | `kubernetes/` | k8s, pod, deployment, service, ingress, helm |
| Docker | `docker/` | container, image, compose, registry |
| 架构设计 | `architecture/` | system-design, capacity, high-availability, scalability |
| 事件处理 | `incidents/` | postmortem, root-cause, incident, response, on-call |
| 运维手册 | `runbook/` | backup, logging, monitoring, performance, runbook |
| 技术选型 | `comparisons/` | vs, comparison, alternatives, decision |

## 快速开始

```bash
# 1. 克隆仓库
git clone git@github-lin327:lin327/sre-atlas-agent.git
cd sre-atlas-agent

# 2. 创建虚拟环境
python3 -m venv .venv
source .venv/bin/activate

# 3. 安装依赖
pip install -r requirements.txt

# 4. 配置环境变量
cp .env.example .env
# 编辑 .env 填入 ANTHROPIC_API_KEY

# 5. 试运行（不写数据库）
python -m agent.main --once --dry-run

# 6. 正式运行一次
python -m agent.main --once

# 7. 持续运行（每 6 小时采集一次）
python -m agent.main
```

## 自动同步到 Wiki

```bash
# 一键：采集 → 推送到 sre-wiki
bash scripts/sync-to-wiki.sh

# 或用 crontab（每 6 小时自动执行）
0 */6 * * * /path/to/sre-atlas-agent/scripts/sync-to-wiki.sh >> /var/log/sre-atlas.log 2>&1
```

## CLI 参数

```
python -m agent.main [OPTIONS]

  --once              单次运行后退出
  --dry-run           采集+生成但不写数据库
  --config PATH       数据源配置文件（默认 config/sources.yaml）
  --output DIR        输出目录（默认 output/）
  --interval HOURS    持续模式间隔（默认 6 小时）
  --log-level LEVEL   日志级别 DEBUG|INFO|WARNING|ERROR
```

## 环境变量

| 变量 | 必填 | 说明 |
|------|------|------|
| `ANTHROPIC_API_KEY` | ✅ | Claude API key |
| `DATABASE_PATH` | ❌ | SQLite 路径（默认 data/sre_atlas.db） |
| `GITHUB_TOKEN` | ❌ | GitHub PAT（提高 API 限流） |

## 相关仓库

- [sre-wiki](https://github.com/lin327/sre-wiki) — SRE Atlas 前端（Astro + React + Neon）
