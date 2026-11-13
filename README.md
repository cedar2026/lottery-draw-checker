# 彩票开奖小助手

双色球 + 大乐透，盯着自己买的号，开奖后用 Bark 推送到手机。只报奖级，不报金额。
推送语带上开奖日期、周几、体/福彩，一眼就知道这是哪一天开的奖。

三个版本，同一套核心，配置通用（`settings.json` / `tickets.json` 放同一个文件夹里就能混用）：

| 版本 | 源码文件 | exe 文件 | 适合谁 |
| --- | --- | --- | --- |
| 服务器版 | `server/lottery_checker.pyw` | `lottery_checker.exe` | 挂服务器上常驻，开奖日晚上自动抓、自动推，不用打开 |
| 个人版 | `personal/lottery_checker_personal.pyw` | `lottery_checker_personal.exe` | 装自己电脑上，早上上班/下班打开就自动抓一次，抓到新一期就推，看完就关 |
| agent 版 | `agent/lottery_cli.py` + `agent/SKILL.md` | `lottery_cli.exe` | 给智能体用的命令行版，智能体按 SKILL.md 配好、设好定时任务，到点自己跑 |

## 小白上手（不用装 Python，推荐）

1. **下载 exe**：打开 [Releases 页](https://github.com/cedar2026/lottery-draw-checker/releases)，下载最新版的 exe。自己电脑上用，下载 `lottery_checker_personal.exe` 就行。
2. **第一次打开**：Windows 可能会弹「Windows 已保护你的电脑」——点「更多信息」→「仍要运行」。没买代码签名证书都会这样，不是病毒。
3. **装 Bark**：iPhone 上打开 App Store 搜 `Bark` 安装，打开 App，里面那串 key 就是你的推送密钥，复制下来。（Bark 是 iOS 应用，收推送需要一台 iPhone。）
4. **填 key、加号**：打开 exe，在界面里填上 Bark key，再加自己买的号码。
5. **验货**：点「测试」发一条测试推送，手机收到才算链路真的通了。收不到就检查 key 有没有复制全。
6. **购彩提醒（可选）**：在「购彩提醒」页加提醒，选推送到哪个 key、勾选周几、设准确时间点，到点推一次、当天不重复推。

## 源码运行（要装 Python）

1. **装 Python**：打开 [python.org](https://www.python.org/downloads/) 下载 Windows 安装包，安装时勾选 `Add Python to PATH`，然后一路下一步。装完打开命令行输 `python --version`，能显示版本号就是装好了。
2. **下载代码**：在本仓库页面点绿色的 `Code` 按钮 → `Download ZIP`，下载后解压到一个文件夹。
3. **运行**：Windows 上双击 `.pyw` 文件就行。agent 版在命令行跑 `python3 lottery_cli.py check`，具体看 `agent/SKILL.md`。
4. **填 key、加号、验货**：同上面第 3–5 步。时间一律按北京时间（固定 UTC+8）。

只用 Python 标准库，不用再装别的依赖（`certifi` 可选装，证书出问题时多一条兜底）。

## 能干什么

- 自动抓开奖号：福彩官网、体彩官网为主，500.com 为备用源
- 开奖日期校验：日期不是今天就不推、继续等，不会把上一期当成今天推
- 购彩提醒：每条提醒自选推送到哪个 Bark 密钥、勾选周几、设准确时间点，带启用/停用开关，到点推一次、当天不重复推
- Bark 推送：没中就一句「没中。开奖号：…」；中了就喊三遍「中奖了中奖了中奖了。」
- 密钥半暗文显示：列表里只显示前后各 3 位，中间打码
- 只核对自己填的号，不预测、不推荐号码

## 干净，就是它的卖点

- 不预测：不给号码、不分析走势，只核对你自己填的号。
- 不收集：号码和 Bark key 只存在你自己电脑本地，不上传到任何地方。
- 不带广告：没有预测、没有购彩引导，开源自托管，挂服务器上不用管。

## 隐私提醒

号码和 Bark key 存在本地的 `settings.json` / `tickets.json` 里，**不要提交到仓库**。本仓库的 `.gitignore` 已经把它们和日志文件都排除了。

## 声明

这就是个核对开奖的小工具。彩票是概率游戏，不预测、不保证中奖。
