# 专项债募投报告采集工具

用于采集 2022—2026 年珠三角除广州外八市的专项债募投报告，按广东省统一发行部分和深圳市分别组织。工具结合浏览器下载与程序处理，将附件直接保存至指定数据目录，筛选 ZIP 内的募投报告、募集说明书 PDF，完成哈希去重、来源追踪及 CSV/JSON 索引生成；Excel 导出为可选功能。

核心版本 **0.2.1**，提供 `continue_collect.py` 续采和 `start_browser.cmd` 浏览器启动入口。已通过一个真实浏览器下载样本和 **63 项离线/本地 HTTP/本机 Edge 检查**，动态列表完整性和五年全量采集尚未验证。

## 双击启动的浏览器续采

`start_browser.cmd` 自动选用项目 `.venv`（没有时尝试 `py -3`），启动 `browser_collect.py`：通过本机 Microsoft Edge 读取公告，随后复用原下载、提取与索引程序。已有数据原地保留，仍跳过已成功、已失败及待复核附件。无需逐页保存 HTML。当前仅支持广东已观察公告清单，未实现动态列表发现或深圳真实验收。

首次启动安装 Playwright 1.64.0 到 `data/browser-runtime`；发行压缩包附带 `browser-wheels` 时离线安装，仅源码版本则从配置的 Python 包源安装。不会安装第二个浏览器或修改 Edge 个人资料。离线包适用于 Windows x64、Python 3.11—3.13，需本机已有 Edge 和可用的 pip。

把发行包中的 `browser_collect.py`、`start_browser.cmd` 和 `browser-wheels` 放进已有 `collector.py`、`continue_collect.py` 的目录，双击 `start_browser.cmd` 即可。原目录中的 Python 源文件无需覆盖。直接使用源码仓库时也可运行：

```powershell
& ".\.venv\Scripts\python.exe" browser_collect.py --data data
```

默认处理已知清单内全部待处理附件，要求已有真实成功报告；首次采集先用 `start_browser.cmd --limit 1`。此前因 SSL/HTTPS 失败而无法读取的公告页最多增加一次浏览器尝试，历史错误证据保留；其他普通页面失败仍跳过。浏览器返回限制状态、验证页、robots 拒绝/无法确认或已有主机阻断状态时停止，不清空阻断、不忽略证书错误、不自动处理验证码。仅读取正文响应，脚本和其他子资源关闭；依赖动态脚本的附件不会被猜测出来。

运行时保持新开的 Edge 和命令窗口打开。每次的中文日志在 `data/logs`，PDF 仍在 `data/reports`，CSV/JSON 在 `data/exports`。这套入口通过了本机 Edge 对本地网页的读取、附件下载、提取和阻断验证；尚未在部署电脑上通过目标网站验收，不能保证消除其 SSL 故障或获得无人值守访问权限。

## 立即继续收集

在已有 `collector.py` 的项目目录放入 `continue_collect.py`，沿用原来的 `data`，无需重新导入样例或移动已下载文件。已跑通真实样本的 Windows PowerShell 环境可执行：

```powershell
& ".\.venv\Scripts\python.exe" continue_collect.py --data data --interval 4 --limit 0
```

`--limit 0` 处理内置已观察清单中所有待处理附件；首次使用、尚无真实报告时，先用 `--limit 1`。默认不传该参数时每次最多尝试 10 个附件。此清单包含跨 2022—2026 年的 **11 个已观察广东公告 URL**，不是五年完整清单；程序会逐页核对标题年份和新增专项债资格，仅处理目标七市附件。

续采跳过 `extracted`、`error`、`review` 附件，保留既有 PDF、部分下载和异常证据；普通连接失败只记录缺失后继续下一项，不自动重试。已记录的普通详情页失败也不重复请求。成功中含待复核成员的压缩包不会重新提取。默认每次请求至少间隔 4 秒；遇到访问限制、robots 拒绝或已保存的阻断状态仍会停止。

可随时按 Ctrl+C 中断，完成的结果会保留，并导出新的 CSV/JSON；重新执行同一命令继续待处理项。退出码 0 代表本次尽力采集结束，允许有缺失，不表示全量或覆盖率达标。

新增公告可放在 UTF-8 文本中，每行一个正常浏览确认的 `https://www.celma.org.cn/fxqgg/<编号>.jhtml` 链接，再加 `--urls-file 文件路径`。自定义清单替换内置清单；同一链接自动去重。深圳须单独使用 `--region 深圳 --urls-file 深圳清单.txt`，不能直接复用广东清单，真实深圳验收仍待完成。

## 当前状态

| 项目 | 已验证范围 |
|---|---|
| 真实公告 | 广东省 2026 年第十一批新增专项债券，公告编号 70584 |
| 已观察附件 | 目标七市 25 条 ZIP 链接，广州已排除 |
| 真实下载 | 佛山四十二期 ZIP 1 个，13,158,381 字节，CRC 通过 |
| 报告提取 | 1 份、18 页，封面和目录已核对；扫描版，未做 OCR |
| 数据保存 | ZIP 直接保存至数据目录，浏览器文件就地登记，不再复制第二份 |
| 测试 | 63 项离线/本地 HTTP/本机 Edge 检查通过，含离线组件安装、续采跳过、错误继续及访问阻断 |
| 当前限制 | 普通脚本请求曾被 CloudWAF 418 拦截；真实附件服务器的 Range 续传支持未验证 |

仓库只提供程序、测试和公开来源的样例元数据，不包含下载的 ZIP、PDF、SQLite 状态、个人浏览器会话或本地导出文件。样例清单只是一份公告的局部清单，不能作为 2022—2026 年完整性依据。

## 环境与启动

核心程序使用 **Python 3.11+ 标准库**，无需安装第三方 Python 包。Windows 可用 `py -3`，其他系统使用 `python3` 或对应解释器。

```bash
git clone https://github.com/jiuzhaopian/collector.git
cd collector
python collector.py --help
python -m unittest discover -s tests -v
```

Windows CMD 可用 `collect.cmd` 调用程序。以下命令的 `--data data` 指当前工作目录下的数据根目录，可改成部署机器上的绝对路径。

## 小批量流程

### 1. 导入已审核的公告与附件元数据

```bash
python collector.py --data data import-inventory samples/browser_inventory_70584.json
```

样例链接来自用户正常打开的 Edge 公告 DOM，证据类型为 `live_browser_dom`。该 JSON 是规范化观察记录，不是原始 HTTP HTML。

也支持离线导入正常保存的公告页面或公开页面的脱敏 HAR：

```bash
python collector.py --data data import-html saved_detail.html --url "https://www.celma.org.cn/fxqgg/70584.jhtml"
python collector.py --data data import-har public_pages.har
```

HAR 只读取成功的公开详情响应体，不重放 Cookie、Authorization 或管理接口；列表 JSON 留作字段核查，不猜测 API。

### 2. 将浏览器文件直接保存到数据目录

```bash
python collector.py --data data download-dir
```

该命令输出 `data/downloads` 的绝对路径。**在实际部署机器上，将浏览器下载位置或“另存为”位置设为这个目录。** 程序不会修改全局浏览器设置；当前没有可独立安装运行的浏览器自动化驱动，真实浏览器自动下载目前只在开发时通过 Codex 的浏览器连接验证。

先下载一个附件，例如样例中的佛山 ZIP，再就地登记：

```bash
python collector.py --data data import-file "data/downloads/9def1302-ccda-4133-99c6-24701340ce52.zip" --attachment-url "https://www.governbond.org.cn/uploadFiles/44/attachFiles/202609/9def1302-ccda-4133-99c6-24701340ce52.zip" --announcement-url "https://www.celma.org.cn/fxqgg/70584.jhtml"
```

文件名应与浏览器实际保存结果一致。`import-file` 不联网、不复制或移动 ZIP；它仅接受 `data/downloads/` 内已经完成的 `.zip/.pdf/.bin` 文件。外部下载目录和 `.part/.crdownload` 临时文件会被拒绝。重复登记同一文件复用来源记录，同一来源的不同内容或另一份文件不会静默覆盖已有记录。

### 3. 检查报告与索引

每个处理命令结束后生成新的导出目录，也可单独导出：

```bash
python collector.py --data data export
```

```text
data/
├── downloads/          原始附件与未完成的 .part
├── reports/            按 PDF SHA256 去重保存的报告
├── raw/                原始响应或标注类型的导入元数据
├── exports/<UTC时间>/  CSV、JSON、批次、覆盖及异常清单
├── state.sqlite        公告、附件、报告、来源和访问状态
└── audit.jsonl         请求与处理日志
```

CSV 使用 UTF-8 BOM，可在 Excel 中打开。保留原附件 URL、公告 URL、包内路径及哈希；`project_path` 只是包内目录，不代表已核实的项目名称。物理 PDF 可去重，但每个来源保留独立记录。

## 可选的 Excel 导出

`export_excel.mjs` 依赖 **Codex 自带的 Node.js / `@oai/artifact-tool`**；`excel.cmd` 仅适用于有该运行时的 Windows 电脑。此功能不属于纯标准库核心，不保证在没有 Codex 的电脑上可用。CSV/JSON 导出不受影响。

```cmd
excel.cmd "data\exports\实际导出目录\index.json" "新的索引.xlsx"
```

导出拒绝覆盖已有同名 Excel 文件。不要将本机依赖目录或 `node_modules` 提交到仓库。

## 普通 HTTP 下载与访问边界

以下功能已实现并通过本地 HTTP 测试，**不代表已通过目标网站联调**：

- 普通下载直接写入 `data/downloads/<来源哈希>.part`，成功后在同目录重命名为 `.zip/.pdf`，不复制完整附件。
- 校验长度、ZIP CRC、文件签名；中断保留部分文件，使用 ETag/Last-Modified 和 Range/If-Range 恢复。
- 服务器忽略 Range 返回 200 时重新写入；异常 Content-Range、验证器变化、416 等不计成功。
- 已登记的浏览器文件也会被后续下载命令校验、复用，不再生成第二份 `.bin`。

```bash
python collector.py --data data --interval 4 probe
python collector.py --data data --interval 4 discover --max-pages 50
python collector.py --data data --interval 4 download --region 广东 --limit 1
```

只有在来源正常允许访问时使用联网命令。程序单线程、默认 4 秒间隔、最低 3 秒，并遵守 robots 的更长间隔；robots 无法确认、403/418/429 或挑战页会停止并保留阻断状态，不自动重试。当前已经观察到目标网站的 CloudWAF 418，不要通过清空状态、伪装身份或代理轮换重试限制。

`discover` 只跟随实际 HTML 详情链接及分页；动态列表没有静态记录时明确报出 `DYNAMIC_LIST_UNVERIFIED`。动态列表真实接口、字段和总条数仍需核实，没有猜测 API 或枚举内容 ID。

## 筛选与验证范围

- 广东：珠海、佛山、惠州、东莞、中山、江门、肇庆；保留同城多个分包，广州在附件和包内路径阶段排除。
- 深圳：单独处理“一案两书”“实施方案”和早期“信息披露文件”等包名，尚未做真实深圳下载验收。
- 仅自动接受发行前公告类目、明确的新增专项债；再融资、置换和一般债排除，混合或跨年含混标题进入复核。
- 债券年份与网页发布日期分开保存，避免补发公告错分年份。
- PDF 文件名匹配 `募投`、`募集说明书`、`募集资金投向`、`募集资金投资`。仅叫“实施方案.pdf”等文件不自动归类；零命中包留在异常清单。
- 支持受限深度的嵌套 ZIP；加密、RAR/7z、危险路径和超限包需要复核。默认单附件 4 GiB，ZIP 单成员 1 GiB、总展开量 12 GiB、累计 50,000 成员、嵌套 3 层。
- 核心程序只检查 PDF 签名与尾部 EOF，不包含 OCR、逐页渲染或项目语义判断。本次真实样本的页数与封面核对是单独完成的。
- 退出码 0 仅说明当前命令完成，2 表示访问停止、失败或需复核；任何一个退出码都不能证明全量完整。

当前支持对已观察公告尽力续采，允许放弃失败项。仍需验证真实服务器续传能力、动态列表和深圳下载；现有清单不能据此宣称五年数据齐全或达到某个覆盖比例。
