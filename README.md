# 专项债募投报告采集工具

用于采集 2022—2026 年珠三角除广州外八市的专项债募投报告，按广东省统一发行部分和深圳市分别组织。工具结合浏览器下载与程序处理，将附件直接保存至指定数据目录，筛选 ZIP 内的募投报告、募集说明书 PDF，完成哈希去重、来源追踪及 CSV/JSON 索引生成；Excel 导出为可选功能。

当前版本 **0.2.1**，定位为小批量验证原型。已通过一个真实浏览器下载样本和 **45 项离线/本地 HTTP 测试**，尚未完成异机浏览器运行、动态列表接口和全自动批量采集验证。

## 当前状态

| 项目 | 已验证范围 |
|---|---|
| 真实公告 | 广东省 2026 年第十一批新增专项债券，公告编号 70584 |
| 已观察附件 | 目标七市 25 条 ZIP 链接，广州已排除 |
| 真实下载 | 佛山四十二期 ZIP 1 个，13,158,381 字节，CRC 通过 |
| 报告提取 | 1 份、18 页，封面和目录已核对；扫描版，未做 OCR |
| 数据保存 | ZIP 直接保存至数据目录，浏览器文件就地登记，不再复制第二份 |
| 测试 | 45 项离线/本地 HTTP 测试通过 |
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

当前只推进小样本和目标机器运行验证，不开展全量采集。后续需要验证部署机器的浏览器保存配置、少量不同城市/年份样本及真实服务器续传能力；全量前还需建立每年的固定批次分母。
