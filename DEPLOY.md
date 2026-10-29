# 部署与服务器运行

代码跑在 `.servers.conf` 的 `[cluster_agent_server]` 上。

| 项 | 值 |
|---|---|
| 服务器 | `117.50.195.94:3929`（root，key 认证） |
| 代码目录 | `/mnt/fin_bench/sync_datasets/code/2_cluster_algorithm` |
| 数据目录 | `/mnt/fin_bench/sync_datasets/datasets` |
| 仓库 | `ssh://git@interactive-n01laf763gwn:22/chenmingyang2/cluster_algorithm.git` |

本地只开发；服务器只拉取、只运行，不在服务器上改代码。

## 链路

1. 本地提交并 `git push origin main`；
2. 服务器执行 `./deploy.sh`：`git fetch` + `git merge --ff-only` + 冒烟测试；
3. 跑业务：`source .deploy/env.sh`，然后 `./run.sh` 或 `python3 bin/cluster.py ...`。

只接受快进合并。远端和服务器一旦分叉，部署会直接停下，不会自动 merge。

## 服务器布局

| 路径 | 用途 | 入库 |
|---|---|---|
| `.` | main 分支 checkout | 是 |
| `.deploy/git-readonly(.pub)` | 仓库级只读部署密钥 | 否 |
| `.deploy/git-known-hosts` | 固定主机指纹 | 否 |
| `.deploy/fonts/wqy-zenhei.ttc` | 中文字体（服务器无 CJK 字体） | 否 |
| `.deploy/env.sh` | PATH 与字体环境变量 | 否 |
| `.venv/` | Python 3.12 + Pillow | 否 |

`chart` 子命令需要 CJK 字体；`CLUSTER_CHART_FONT` 未设置时回退到 macOS 字体路径。

## 日常发布

```bash
git commit -am "..." && git push origin main          # 本地
ssh <server> 'cd /mnt/fin_bench/sync_datasets/code/2_cluster_algorithm && ./deploy.sh'
```

## 首次初始化

实例重建后，`.deploy/` 与 `.venv/` 若丢失，按下面顺序恢复：

```bash
cd /mnt/fin_bench/sync_datasets/code/2_cluster_algorithm
mkdir -p .deploy/fonts
ssh-keygen -q -t ed25519 -N "" -C "ths_cluster_agent readonly" -f .deploy/git-readonly
cat .deploy/git-readonly.pub        # 加到仓库 Settings → Deploy keys，只读
ssh-keyscan -p 22 interactive-n01laf763gwn > .deploy/git-known-hosts
python3 -m venv .venv && .venv/bin/pip install pillow
apt-get download fonts-wqy-zenhei && dpkg-deb -x fonts-wqy-zenhei_*.deb /tmp/wqy
cp /tmp/wqy/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc .deploy/fonts/
printf '%s\n' 'D=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)' \
  'export PATH="$D/.venv/bin:$PATH"' \
  'export CLUSTER_CHART_FONT="$D/.deploy/fonts/wqy-zenhei.ttc"' > .deploy/env.sh
git init -q
git remote add origin ssh://git@interactive-n01laf763gwn:22/chenmingyang2/cluster_algorithm.git
git config core.sshCommand "ssh -F /dev/null -i $PWD/.deploy/git-readonly -o IdentitiesOnly=yes -o BatchMode=yes -o UserKnownHostsFile=$PWD/.deploy/git-known-hosts -o StrictHostKeyChecking=yes"
git fetch origin main && git checkout -f -B main origin/main
```

## 排错

| 现象 | 处理 |
|---|---|
| `Permission denied (publickey)` | 部署公钥没加，或加到了别的仓库 |
| `Host key verification failed` | `.deploy/git-known-hosts` 缺失 / 指纹变化 |
| `No module named 'PIL'` | 没 `source .deploy/env.sh` |
| `OSError: cannot open resource` | 字体路径不存在，检查 `CLUSTER_CHART_FONT` |
| `merge --ff-only` 失败 | 服务器和远端分叉，先确认服务器上没改代码，再 `git reset --hard origin/main` |
