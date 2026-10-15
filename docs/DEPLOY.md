# 用手机和电脑浏览器访问

本项目已提供部署文件，**尚未上线**。需要先在托管账号中部署，才能获得手机和电脑均可访问的 HTTPS 网址。工作区路径、截图链接和 `localhost` 都不是这个网址。

## Render 部署

此方案使用 Render 原生 Python Web Service、一个运行实例和 1 GB 持久盘。`starter` 服务和持久盘均为收费资源；创建前以 Render 控制台显示的当前价格、地区可用性与计费条件为准。本仓库中的配置不会自动创建服务或扣费。

1. 打开 [Render](https://render.com/)，使用自己的 GitHub 账号注册/登录，无需购买域名。允许 Render 读取 `YiyYun1116/skills-introduction-to-github` 仓库。
2. 选择 **New → Blueprint**，选择上述仓库和 **`signal-atlas-web` 分支**（不要选只有入门练习的 `main` 分支）。确认识别到 `signal-atlas` Web Service、付费套餐与 1 GB 持久盘，再确认费用并创建。地区按账号可用选项选择。
3. 等待部署与健康检查成功。在服务的 **Environment** 设置中查看自动生成的 `SIGNAL_ATLAS_ACCESS_PASSWORD`，妥善保存在密码管理器。默认用户名是 `research`；密码不得少于 16 个字符。不要将密码提交到 GitHub 或发到聊天中。
4. 在服务控制台点击 Render 实际生成的 **HTTPS 访问地址**。浏览器会提示用户名和密码。手机 Safari、Chrome 与电脑浏览器使用同一个地址及账号；可收藏该地址。实际网址以控制台为准，本项目未预先分配域名。
5. 首次启动数据库为空，页面默认真实数据模式并显示等待采集，后台自动读取公开订阅。“数据来源”页会显示下次计划、最近成功读取和采集结果；失败时保留原因。新部署不会自动包含开发环境先前抓取的数据，也不会用演示内容补齐空库。

`render.yaml` 配置了生产模式、自动生成访问密码、端口读取、单 worker 和持久化数据库。`/api/health` 用于托管健康检查，不需要登录；业务页面和接口要求登录。这是一个共用研究工作台，使用同一个账号的人会看到同一份关注列表和资料。

后台启动后自动采集，每轮结束约 30 分钟后再次运行，不需要另建定时任务服务。关闭手机或浏览器不影响采集；托管服务停止时也会停止。瞬时错误在单源每轮最多 3 次读取尝试内处理，持续失败可在来源页查看；没有新收录不等于失败。

“公司财报”页展示已收录的 SEC 年报、季报及可能的过渡报告和修订索引。每个 SEC 订阅每轮最多读取最近 100 条，不保证完整历史或全球公司覆盖，尚未自动提取全文财务指标。

### 上线后检查

- 手机和电脑均能打开控制台提供的 HTTPS 地址；未输入账号时应提示登录。
- 登录后打开“数据来源”，确认首次自动采集完成、下次计划已安排，并查看新收录、内容修订、无变化与失败来源。必要时手动同步。
- 展开“系统检查”查看服务、采集记录和最近资料窗口的结果；它不代替外部联网测试或内容真实性核验。
- 添加一条关注资产，重启服务后仍应存在。数据库位于持久盘 `/var/data/signal-atlas.sqlite3`，不要改回服务的临时文件目录。
- 持久盘仅服务于这一实例；保持一个实例和一个 worker。扩容前需要单独设计数据库与采集调度。

公开订阅会受来源服务或托管地区网络影响；部署成功本身不代表所有来源都可连接。更新代码通过该 GitHub 分支和 Render 的部署流程完成。上线前确认自己的访问方式、费用和持久盘备份安排。

Codex 开发环境的网络追加设置仍在配置草稿中，需要在该环境的设置界面审核、保存并发布。此流程只管理开发环境；不会替你创建 Render 服务，也不会给手机生成网站地址。

### 打开后看不到内容

- 没有 Render 生成的 HTTPS 地址：网站尚未部署，仓库链接和截图文件不能作为入口。
- 能看到页面和“等待采集”：网站可用但真实资料尚未入库，查看“数据来源”的首次任务结果。
- 显示连接或来源错误：按页面原因检查登录、服务日志或来源网络；`/api/health` 通过只能说明应用和数据库可读取。
- 有资料但筛选结果为空：清除搜索、资产或地区条件，再查看已收录材料。

## 其他支持 Docker 的托管服务

根目录 `Dockerfile` 使用 Python 3.12、UID/GID `10001:10001` 的非 root 用户。镜像不包含访问密码和本地数据库。托管服务需提供：

| 设置 | 值或要求 |
| --- | --- |
| HTTPS | 由托管入口或反向代理提供证书和 HTTPS |
| 启动命令 | 镜像默认命令 `python scripts/serve.py` |
| 端口 | `PORT`，其次 `SIGNAL_ATLAS_PORT`，均未设置时为 `8000` |
| `SIGNAL_ATLAS_ENV` | `production`，镜像已设置 |
| `SIGNAL_ATLAS_ACCESS_USER` | 默认 `research` |
| `SIGNAL_ATLAS_ACCESS_PASSWORD` | 在托管密钥设置中保存至少 16 字符的随机密码 |
| 持久存储 | 挂载 `/var/data`，确保运行 UID `10001` 可写；绑定挂载需由宿主预先设置权限 |
| 数据库路径 | `/var/data/signal-atlas.sqlite3`，镜像已设置 |
| 运行数量 | 一个实例、一个 worker |
| 健康检查 | `GET /api/health` |
| `FORWARDED_ALLOW_IPS` | 默认只信任 `127.0.0.1`；按实际反代 IP 设置，仅在托管隔离入口下使用 `*` |

本地构建镜像可使用 `docker build -t signal-atlas .`。将服务端口接入托管 HTTPS 入口；不要把本地 HTTP 端口当作手机公网访问地址。此配置不代替托管账号授权或实际部署。

## 配置核对依据与当前限制

相关官方说明：[Blueprint 字段](https://render.com/docs/blueprint-spec)、[持久盘](https://render.com/docs/disks)、[Web Service 和访问地址](https://render.com/docs/web-services)、[地区](https://render.com/docs/regions)、[Python 版本](https://render.com/docs/python-version)。

本次执行环境访问这些官方页面返回代理 `403 Forbidden`。已通过官方 GitHub 示例核对基础字段：[FastAPI 服务](https://github.com/render-examples/fastapi)、[自动生成密钥](https://github.com/render-examples/django)、[持久盘](https://github.com/render-examples/ghost)。当前完整控制台校验、价格及地区政策仍以创建页面为准。没有关闭 TLS 校验或绕过网络规则。

已完成自动化与浏览器验证，范围和结果见 [VALIDATION.md](VALIDATION.md)。未创建 Render 账号、购买资源或执行真实公网部署。可用 `SIGNAL_ATLAS_TEST_PYTHON=<目标Python路径> python scripts/hosted_check.py` 复现本地托管流程检查（测试工具需要 Playwright 和 Chromium）。Docker 备用方案尚未构建验证；推荐使用上面的原生 Python Blueprint。
