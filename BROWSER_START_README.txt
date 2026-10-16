浏览器续采版：放进原项目，双击启动

1. 将本压缩包的全部内容解压到原来的 collector.py 所在目录。
   新增 browser_collect.py、start_browser.cmd、browser-wheels 等文件。
   不包含 collector.py、continue_collect.py，不覆盖原程序或原 data。

2. 双击 start_browser.cmd。
   它使用原项目 .venv；首次从附带文件离线安装组件到 data/browser-runtime，
   随后自动打开 Edge、读取已确认的公告、下载并提取报告。
   运行时保持新开的 Edge 和命令窗口打开。

3. 结束后，PDF 在原 data/reports；索引在 data/exports；日志在 data/logs。
   成功、失败和待复核附件均跳过；已有报告和异常证据保留。
   下次仍双击同一文件，继续尚待处理的项。

适用于当前已成功采集真实报告的项目。全新项目先在终端运行：
start_browser.cmd --limit 1

环境：Windows x64，已有 Edge、Python 3.11—3.13 和 pip。
附带安装包来源为官方 PyPI 的 Playwright 1.64.0 及依赖。
这些安装包保留原 wheel 内的许可证与发行元数据。
离线安装组件不等于离线采集；访问公告和下载附件仍需网络。

范围：仅内置已观察的 11 个广东公告，不是五年完整清单；广州排除，深圳另行处理。
只为此前 SSL 错误的公告增加一次浏览器尝试，不重试丢弃的 ZIP/PDF。
遇到网站访问限制、验证页、robots 拒绝或无法确认时停止，不能保证整夜无人值守。
未关闭证书校验，也不会使用个人浏览器账号、清空访问阻断或自动处理验证码。

验证：本机已验证 Edge 对本地网页的读取、ZIP 下载、PDF 提取和访问阻断。
目标网站在部署电脑上的连通性尚未验证；测试不代表全部公告可访问。
如结束后没有新增，发送 data/logs 中最新日志即可定位。
